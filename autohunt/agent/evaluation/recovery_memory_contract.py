"""Offline recovery/reconciliation/semantic-memory contracts; synthetic inputs only."""
from __future__ import annotations

import asyncio
import copy
import gc
import io
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

import httpx
from cryptography.fernet import Fernet

from app.core.agent_tools import AgentToolSpec
from app.delivery.reconciliation import judge_receipt, read_receipt, resume_fingerprint
from app.jobs.store import JobStore
from app.runtime.context import current_run
from app.runtime.context import RunContext
from app.runtime.memory_context import compile_context
from app.runtime.history_reset import RUN_TABLES
from app.runtime.semantic_memory import SemanticMemory
from app.runtime.store import RunStore
from app.workflow.execution import advance
from app.workflow.failure_recovery import recovery_decision, recover_failed_step
from app.workflow.mock_coordinator import MockWorkflowCoordinator
from evaluation.approval_contract import DurableApprovalContracts
from evaluation.search_delivery_contract import job

TRACES = []


class FailureRecovery(DurableApprovalContracts):
    def read_plan(self, args):
        value = self.plan()
        value["steps"][0].update(tool="job_stats_read", effect="read", arguments=args)
        self.store.save_plan(self.run["id"], value)
        return value

    async def test_read_parameter_replan_runs_gateway_and_does_not_replay_write(self):
        self.enter()
        calls = []
        async def generate(request):
            calls.append(json.loads(request.user_message))
            return NS(text='{"tool":"job_stats_read","arguments":{}}')
        self.registry.response_llm = NS(generate=generate)
        value = self.read_plan({"unsupported": True})
        await advance(value, self.registry)
        self.assertEqual(value["steps"][0]["status"], "completed")
        self.assertEqual(self.calls, [("job_stats_read", {})])
        self.assertEqual(value["automatic_recovery_count"], 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(value["revision"], 1)

    async def test_transient_read_retry_is_bounded_and_durable(self):
        self.enter()
        value = self.read_plan({})
        value["steps"][0].update(status="failed", failure={"code": "timeout", "retryable": True, "effect_started": False})
        self.assertTrue(await recover_failed_step(value, value["steps"][0], self.registry, lambda p: self.store.save_plan(self.run["id"], p)))
        saved = self.store.get(self.run["id"])["checkpoint"]["plan"]
        saved["steps"][0].update(status="failed", failure={"code": "timeout", "retryable": True})
        self.assertEqual(recovery_decision(saved["steps"][0], self.registry)["action"], "stop")

    async def test_uncertain_external_only_reconciles_even_if_retryable(self):
        value = self.plan(True)
        step = value["steps"][0]
        step.update(status="outcome_indeterminate", failure={"retryable": True, "uncertain": True})
        self.assertEqual(recovery_decision(step, self.registry)["action"], "reconcile")
        self.assertFalse(await recover_failed_step(value, step, self.registry, lambda p: None))
        self.assertEqual(self.calls, [])

    async def test_repair_cannot_switch_tool_or_add_target(self):
        self.enter()
        for answer in ('{"tool":"mock_delivery_start","arguments":{}}',
                       '{"tool":"job_stats_read","arguments":{"task_id":"other"}}'):
            async def generate(_): return NS(text=answer)
            self.registry.response_llm = NS(generate=generate)
            value = self.read_plan({"bad": 1})
            await advance(value, self.registry)
            self.assertEqual(value["steps"][0]["status"], "waiting_input")
        self.assertEqual(self.calls, [])

    async def test_valid_parameters_cannot_be_changed_by_repair(self):
        self.enter()
        async def execute(args): self.calls.append(args); return {}
        self.registry._tools["job_stats_read"] = AgentToolSpec("job_stats_read", "synthetic",
            {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1}, "stage": {"type": "string"}}, "additionalProperties": False},
            "read", execute)
        async def generate(_): return NS(text='{"tool":"job_stats_read","arguments":{"limit":1,"stage":"all"}}')
        self.registry.response_llm = NS(generate=generate)
        value = self.read_plan({"limit": 0, "stage": "review"})
        await advance(value, self.registry)
        self.assertEqual(value["steps"][0]["status"], "waiting_input")
        self.assertEqual(self.calls, [])

    async def test_exhausted_transport_retry_is_not_multiplied_by_step_recovery(self):
        step = self.read_plan({})["steps"][0]
        step.update(status="failed", failure={"code": "timeout", "retryable": True, "retry_exhausted": True})
        self.assertNotEqual(recovery_decision(step, self.registry)["action"], "retry")

    async def test_revoked_and_unclassified_tools_never_get_automatic_replan(self):
        step = self.plan(True)["steps"][0]
        for code in ("authority_revoked", "invalid_arguments", "permission_denied", "unknown_tool"):
            step.update(status="blocked", failure={"code": code, "effect_started": False})
            self.assertNotIn(recovery_decision(step, self.registry)["action"], {"retry", "replan"})

    async def test_replan_failure_cannot_loop_on_next_advance(self):
        self.enter()
        seen = []
        async def generate(_): seen.append(1); raise TimeoutError()
        self.registry.response_llm = NS(generate=generate)
        value = self.read_plan({"bad": 1})
        await advance(value, self.registry)
        await advance(value, self.registry)
        self.assertEqual(len(seen), 1)
        self.assertEqual(value["steps"][0]["status"], "waiting_input")

    async def test_cancel_during_inference_never_reschedules_read(self):
        self.enter()
        async def generate(_):
            self.store.update(self.run["id"], cancel_requested=1)
            return NS(text='{"tool":"job_stats_read","arguments":{}}')
        self.registry.response_llm = NS(generate=generate)
        value = self.read_plan({"bad": 1})
        await advance(value, self.registry)
        self.assertEqual(self.calls, [])
        self.assertNotEqual(value["steps"][0]["status"], "pending")

    async def test_memory_tool_uses_scoped_source_reader_not_public_memory(self):
        self.enter()
        self.context.memory_source_read = lambda ident: {"text": "synthetic source", "source_id": ident}
        self.registry.memory = NS(read=lambda: "public memory")
        self.assertEqual((await self.registry._semantic_memory_read({"source_id": "synthetic"}))["text"], "synthetic source")
        self.assertEqual((await self.registry._memory_read({}))["content"], "public memory")

    async def test_invalid_business_quantity_cannot_be_silently_dropped(self):
        self.enter()
        async def generate(_): return NS(text='{"tool":"job_stats_read","arguments":{}}')
        self.registry.response_llm = NS(generate=generate)
        value = self.read_plan({"result_limit": 150})
        await advance(value, self.registry)
        self.assertEqual(value["steps"][0]["status"], "waiting_input")
        self.assertEqual(value["steps"][0]["arguments"], {"result_limit": 150})
        self.assertEqual(self.calls, [])


def receipt_fixture():
    payload = {"name": "synthetic", "summary": "synthetic approved document", "skills": ["SQL"], "education": "",
        "experience_years": 0, "work_history": [{"autohunt_binding": {"document_id": "doc", "version": 1, "digest": "hash"}}]}
    run = {"id": "delivery", "job_id": 1, "target_url": "http://127.0.0.1:8080/job.html?id=17", "action": "submit_resume",
        "mock_applicant_id": 7, "mock_resume_id": 9, "mock_resume_digest": resume_fingerprint(payload),
        "resume_document_id": "doc", "resume_version": 1, "resume_digest": "hash"}
    remote = {"account": {"id": 7, "role": "applicant"}, "applications": [{"id": 11, "applicant_id": 7, "job_id": 17, "resume_id": 9}],
        "resumes": [{"id": 9, "applicant_id": 7, **payload}]}
    return run, remote


class Reconciliation(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.jobs = JobStore(Path(self.temp.name) / "jobs.db")
        self.jobs.upsert_job(job(17))
        self.jobs.create_delivery_run("delivery", 1)
        self.run, self.remote = receipt_fixture()
        with self.jobs._connect() as db:
            for key, value in self.run.items():
                if key not in {"id", "job_id"}:
                    db.execute(f"UPDATE delivery_runs SET {key}=? WHERE id='delivery'", (value,))
        self.jobs.update_delivery_run("delivery", status="outcome_indeterminate", checkpoint="submit_started")
        self.run = self.jobs.deliveries_to_reconcile()[0]

    def tearDown(self): gc.collect(); self.temp.cleanup()

    def judge(self, run=None, remote=None):
        return judge_receipt(run or self.run, applicant_id=7, **(remote or self.remote))

    async def test_confirmed_receipt_atomically_updates_run_and_board_once(self):
        self.remote["resumes"][0]["experience_years"] = 0.0
        observation = self.judge()
        self.assertEqual(observation["status"], "confirmed")
        self.assertTrue(self.jobs.record_delivery_reconciliation(self.run, observation))
        self.assertFalse(self.jobs.record_delivery_reconciliation(self.run, observation))
        self.assertEqual(self.jobs.get_delivery_run("delivery").status, "completed")
        self.assertEqual(self.jobs.get_job(1)["stage"], "waiting")

    async def test_absence_unreachable_and_wrong_resume_never_schedule_send(self):
        for mutate, status in ((lambda r: r.update(applications=[]), "not_observed"),
                (lambda r: r["applications"][0].update(resume_id=100), "conflict"),
                (lambda r: r["resumes"][0].update(summary="edited"), "conflict"),
                (lambda r: r["account"].update(id=8), "unknown")):
            remote = copy.deepcopy(self.remote); mutate(remote)
            observation = self.judge(remote=remote)
            self.assertEqual(observation["status"], status)
            self.assertFalse(self.jobs.record_delivery_reconciliation(self.run, observation))
            self.run = self.jobs.deliveries_to_reconcile()[0]
            self.assertEqual(self.jobs.get_delivery_run("delivery").status, "outcome_indeterminate")
            self.assertEqual(self.jobs.mock_delivery_queue(), [])

    async def test_http_adapter_only_gets_constant_paths_and_handles_errors(self):
        seen = []
        values = {"/api/auth/me": self.remote["account"], "/api/applications/mine": self.remote["applications"], "/api/resumes/mine": self.remote["resumes"]}
        def respond(request):
            seen.append((request.method, request.url.host, request.url.path))
            return httpx.Response(200, json=values[request.url.path])
        result = await read_receipt(self.run, 7, transport=httpx.MockTransport(respond))
        self.assertEqual(result["status"], "confirmed")
        self.assertTrue(all(method == 'GET' and host == '127.0.0.1' for method, host, path in seen))
        result = await read_receipt(self.run, 7, transport=httpx.MockTransport(lambda _: httpx.Response(401)))
        self.assertEqual(result["status"], "unknown")

    async def test_foreign_origin_legacy_binding_and_duplicate_receipts_stay_unknown(self):
        for args in ({"target_url": "https://example.com/job.html?id=17"}, {"mock_resume_digest": None}, {"mock_applicant_id": 8}):
            self.assertEqual(self.judge(run={**self.run, **args})["status"], "unknown")
        remote = copy.deepcopy(self.remote); remote["applications"] *= 2
        self.assertEqual(self.judge(remote=remote)["status"], "conflict")

    async def test_racing_cancel_or_new_attempt_cannot_be_overwritten(self):
        self.jobs.update_delivery_run("delivery", status="cancelled")
        self.assertFalse(self.jobs.record_delivery_reconciliation(self.run, self.judge()))
        self.jobs.update_delivery_run("delivery", status="outcome_indeterminate")
        self.run = self.jobs.deliveries_to_reconcile()[0]
        self.jobs.create_delivery_run("new-delivery", 1)
        self.assertFalse(self.jobs.record_delivery_reconciliation(self.run, self.judge()))

    async def test_restart_preserves_fingerprint_and_unknown_state(self):
        reopened = JobStore(self.jobs.path)
        reopened.reconcile_incomplete_runs()
        saved = reopened.deliveries_to_reconcile()[0]
        self.assertEqual(saved["mock_resume_digest"], self.run["mock_resume_digest"])
        self.assertTrue(reopened.record_delivery_reconciliation(saved, self.judge(run=saved)))

    async def test_coordinator_reconciles_without_browser_or_sender(self):
        coordinator = MockWorkflowCoordinator(self.jobs, NS(), NS(current=lambda: NS(id=None)), NS(), NS())
        self.jobs.bind_mock_applicant(7)
        with patch("app.delivery.reconciliation.read_receipt", return_value=self.judge()) as reader:
            await coordinator.reconcile_uncertain_deliveries()
            await coordinator.reconcile_uncertain_deliveries()
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(self.jobs.get_delivery_run("delivery").status, "completed")

    async def test_missing_binding_does_not_make_http_calls(self):
        def reject(_): raise AssertionError("no HTTP for missing binding")
        result = await read_receipt({**self.run, "mock_resume_digest": None}, 7, transport=httpx.MockTransport(reject))
        self.assertEqual(result["status"], "unknown")

    async def test_removed_job_does_not_reappear_after_reconciliation(self):
        with self.jobs._connect() as db:
            db.execute("UPDATE jobs SET removed_at='2026-10-20',board_visible=0 WHERE id=1")
        self.assertTrue(self.jobs.record_delivery_reconciliation(self.run, self.judge()))
        self.assertEqual(self.jobs.get_job(1)["board_visible"], 0)

    async def test_other_account_backlog_does_not_hide_current_accounts_receipt(self):
        for index in range(12):
            self.jobs.create_delivery_run("foreign" + str(index), 1)
            self.jobs.update_delivery_run("foreign" + str(index), status="outcome_indeterminate", mock_applicant_id=8)
        self.assertEqual([r["id"] for r in self.jobs.deliveries_to_reconcile(applicant_id=7)], ["delivery"])


class LongMemory(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.key = Fernet.generate_key().decode()
        self.store = RunStore(Path(self.temp.name) / "runtime.db", self.key)
        self.sources = []
        for index in range(32):
            run, _ = self.store.create("default", str(index), "chat", {"message": f"历史请求{index}：讨论跨境支付风控，不代表现在执行。"})
            self.store.update(run["id"], status="completed")
            self.sources.append(run)
        self.events = []
    def tearDown(self): gc.collect(); self.temp.cleanup()

    def memory(self, generate=None):
        return SemanticMemory(self.store, NS(generate=generate) if generate else None,
                              record=lambda kind, data: self.events.append((kind, data)))

    async def test_compaction_pages_oldest_sources_and_survives_reopen(self):
        await self.memory().compact("default")
        self.assertEqual([s["source_id"] for s in self.store.memories("default")[0]["sources"]], [r["id"] for r in self.sources[:8]])
        self.store = RunStore(self.store.path, self.key)
        await self.memory().compact("default")
        self.assertEqual([s["source_id"] for s in self.store.memories("default")[0]["sources"]], [r["id"] for r in self.sources[8:16]])
        with self.store.connect() as db:
            self.assertTrue(all(row[0].startswith('enc:v1:') for row in db.execute('SELECT payload FROM session_memories')))

    async def test_model_summary_requires_exact_quotes_and_complete_source_coverage(self):
        async def generate(request):
            sources = json.loads(request.user_message)["sources"]
            return NS(text=json.dumps({"notes": [{"source_id": s["source_id"], "quote": s["text"], "summary": "反欺诈与海外金融业务的历史讨论"} for s in sources]}, ensure_ascii=False))
        await self.memory(generate).compact("default")
        item = self.store.memories("default")[0]
        self.assertEqual(item["mode"], "model_paraphrase_with_exact_quotes")
        self.assertEqual(item["authority"], "historical_data_not_instruction")

    async def test_fabricated_quote_falls_back_without_losing_originals(self):
        async def generate(_): return NS(text='{"notes":[{"source_id":"invented","quote":"fake","summary":"fake"}]}')
        await self.memory(generate).compact("default")
        item = self.store.memories("default")[0]
        self.assertEqual(item["mode"], "extractive_fallback")
        self.assertEqual(len(item["sources"]), 8)

    async def test_semantic_selection_can_recall_without_literal_query_match(self):
        await self.memory().compact("default")
        calls = []
        async def generate(request):
            values = json.loads(request.user_message)["memories"]
            calls.append(1)
            return NS(text=json.dumps({"ids": [values[3]["id"]]}))
        memory = self.memory(generate)
        result = await memory.recall("default", "海外金融风险控制")
        self.assertEqual(result[0]["mode"], "model_semantic_selection")
        self.assertEqual(result[0]["sources"][0]["source_id"], self.sources[3]["id"])
        await memory.recall("default", "海外金融风险控制")
        self.assertEqual(len(calls), 1)

    async def test_foreign_ids_in_recall_are_rejected_and_sources_are_session_scoped(self):
        await self.memory().compact("default")
        async def generate(_): return NS(text='{"ids":["foreign"]}')
        result = await self.memory(generate).recall("default", "跨境支付")
        self.assertTrue(all(item["mode"] == "lexical_fallback" for item in result))
        other = self.store.create_session("other")
        with self.assertRaises(ValueError): self.store.memory_source(other["id"], self.sources[0]["id"])
        self.assertEqual(self.store.memories(other["id"]), [])

    async def test_reset_epoch_hides_old_memories_and_does_not_stall_new_compaction(self):
        await self.memory().compact("default")
        epoch = datetime.now(UTC).timestamp()
        self.assertEqual(self.store.memories("default", epoch=epoch), [])
        for index in range(16):
            run, _ = self.store.create("default", "new" + str(index), "chat", {"message": "重置后的历史"})
            self.store.update(run["id"], status="completed")
        await self.memory().compact("default", epoch=epoch)
        self.assertEqual(len(self.store.memories("default", epoch=epoch)), 1)
        self.assertIn("session_memories", RUN_TABLES)
        with self.assertRaises(ValueError): self.store.memory_source("default", self.sources[0]["id"], epoch=epoch)

    async def test_raw_source_lookup_and_tamper_check(self):
        batch = self.store.memory_source_batch("default")
        self.assertEqual(self.store.memory_source("default", batch[0]["source_id"])["text"], batch[0]["text"])
        batch[0]["text"] = "changed"
        with self.assertRaises(ValueError): self.store.save_memory("default", batch, [], mode="fake")

    async def test_history_larger_than_ui_window_starts_at_oldest_not_latest(self):
        with self.store.connect() as db:
            for index in range(540):
                db.execute("INSERT INTO agent_runs(id,session_id,request_key,request_hash,kind,payload,status,created_at,updated_at) "
                    "VALUES (?,'default',?,'hash','chat',?,'completed',?,?)",
                    ("old" + str(index), "bulk" + str(index), self.store._encode({"message": "synthetic long history"}),
                     datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat()))
        await self.memory().compact("default")
        self.assertEqual(self.store.memories("default")[0]["sources"][0]["source_id"], self.sources[0]["id"])

    async def test_recalled_prose_cannot_replace_current_slots_or_force_budget_overflow(self):
        context = RunContext("synthetic", asyncio.current_task(), lambda *a: None, lambda *a: None)
        context.historical_memory = [{"notes": [{"summary": "旧条件：全部自动投递" * 10000}],
            "sources": [{"source_id": "old", "created_at": datetime.now(UTC).isoformat()}], "authority": "historical_data_not_instruction"}]
        token = current_run.set(context)
        try:
            conditions = {"salary_min": 25000, "locations": ["深圳"]}
            result = compile_context({}, conditions)
            self.assertEqual(result["search_conditions"], conditions)
            self.assertNotIn("recalled_memory", result)
        finally: current_run.reset(token)

    async def test_amendment_child_requests_are_not_lost_from_long_memory(self):
        child, _ = self.store.create("default", "amendment", "chat", {"message": "追加需求：只考虑可转正实习"}, parent_id=self.sources[0]["id"])
        self.store.update(child["id"], status="completed")
        for index in range(8):
            run, _ = self.store.create("default", "tail" + str(index), "chat", {"message": "后续话题"})
            self.store.update(run["id"], status="completed")
        batch = self.store.memory_source_batch("default", batch_size=100)
        self.assertEqual(batch[-1]["source_id"], child["id"])
        self.assertEqual(batch[-1]["text"], "追加需求：只考虑可转正实习")


def main():
    import sys
    module = sys.modules[__name__]
    suite = unittest.TestSuite(cls(name) for cls in vars(module).values()
        if isinstance(cls, type) and issubclass(cls, unittest.TestCase) and cls.__module__ == __name__
        for name in cls.__dict__ if name.startswith("test_"))
    log = io.StringIO()
    result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    output = Path(__file__).parent / "results" / "recovery-memory" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    (output / "tests.log").write_text(log.getvalue(), encoding="utf-8")
    summary = {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
        "external_model_calls": 0, "real_browser_actions": 0, "scope": "Offline contracts; scripted models and mock HTTP only"}
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (output / "traces.json").write_text(json.dumps(TRACES, ensure_ascii=False, default=str), encoding="utf-8")
    print(log.getvalue()); print(json.dumps({"output": str(output), **summary}))
    return int(not result.wasSuccessful())


if __name__ == "__main__": raise SystemExit(main())
