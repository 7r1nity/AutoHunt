"""Recorded failure shapes across planner, durable revisions and delivery queue.

Offline only: scripted model responses, temporary SQLite, no recruitment calls.
"""
from __future__ import annotations

import asyncio
import copy
import gc
import importlib
import json
import tempfile
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from app.core.job_filters import evaluate_filter, filter_facts_digest
from app.core.models import ResumeDocumentView, DeliveryTaskView, BrowserTaskStatus
from app.runtime.resilience import PreflightRefused
from app.jobs.store import JobStore
from app.llm.base import LLMResult
from app.runtime.store import RunStore
from app.workflow.execution import advance
from app.workflow.mock_coordinator import MockWorkflowCoordinator
from app.workflow.planning import RequestDraft, validate_draft
from app.workflow.recovery import recover_plan
from app.workflow.revision import RevisionDraft, edited_plan
from app.workflow.validation import preflight, preflight_warnings
from evaluation.salary_revision_contract import RevisionContracts, STAMP

TRACES = []


def leaf(text):
    return {"op": "condition", "text": text}


def delivery_step(**changes):
    return {"id": "deliver_extra", "tool": "mock_delivery_start", "arguments": {"filters": leaf("正式岗位")},
            "depends_on": ["search"], "user_requested": True, **changes}


class DurableCommandContracts(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = RevisionContracts.asyncSetUp
    asyncTearDown = RevisionContracts.asyncTearDown
    enter = RevisionContracts.enter

    def search_only(self):
        self.value["steps"] = self.value["steps"][:2]
        self.store.update(self.parent["id"], checkpoint={"plan": self.value, "phase": "action_plan", "browser_id": "browser-1"})

    def normalize(self, payload):
        return validate_draft(RequestDraft.model_validate(payload), self.registry, "并且把正式岗自动投递",
            dialogue=self.context.dialogue, conditions={"queries": ["合成分析师"]}, stamp=STAMP)

    async def test_recorded_revise_plus_top_level_steps_is_merged_not_rejected(self):
        self.enter(); self.search_only()
        original = copy.deepcopy(self.value)
        payload = {"operation": "revise", "steps": [delivery_step()],
                   "goals": [{"quote": "把正式岗自动投递", "disposition": "steps", "step_ids": ["deliver_extra"]}]}
        normalized = self.normalize(payload)
        self.assertEqual(len(normalized["revision"]["add_steps"]), 1)
        self.assertEqual(normalized["revision"]["plan_id"], self.value["id"])
        answer = await self.runtime.revise_plan(self.edit_run["id"], normalized["revision"], "并且把正式岗自动投递")
        saved = self.store.get(self.parent["id"])["checkpoint"]["plan"]
        self.assertEqual(saved["steps"][:2], original["steps"])
        self.assertEqual(saved["steps"][-1]["evidence_step"], "search")
        self.assertFalse(saved["paused"])
        self.assertEqual(self.calls, [])
        self.assertIn("已追加", answer.message)
        TRACES.append({"case": "recorded-invalid_revision", "normalized": normalized,
                       "persisted_plan": saved, "executors": self.calls})

    async def test_reopen_database_and_background_completion_executes_addition_once(self):
        self.enter(); self.search_only()
        edit = self.normalize({"operation": "revise", "steps": [delivery_step()]})["revision"]
        await self.runtime.revise_plan(self.edit_run["id"], edit, "追加投递")
        self.runtime.store = RunStore(self.store.path, self.key)
        self.runtime.jobs = NS(search_conversation_history=lambda *args, **kw: [], add_conversation_message=lambda *args, **kw: None)
        self.runtime.observe_browser = lambda _: self.view.model_copy(update={"status": BrowserTaskStatus.COMPLETED})
        self.runtime.browser_summary = lambda *args: None
        self.runtime.search_state = lambda _: {"queries": ["合成分析师"]}
        await self.runtime.watch_plan(self.parent["id"])
        await self.runtime.watch_plan(self.parent["id"])
        self.assertEqual([name for name, args in self.calls], ["job_results_read", "mock_delivery_start"])
        self.assertEqual(self.calls[-1][1]["filters"]["text"], "正式岗位")
        self.assertEqual(self.runtime.store.get(self.parent["id"])["status"], "completed")
        TRACES.append({"case": "restart-and-async-completion", "executors": self.calls,
                       "events": self.runtime.store.public_events(self.parent["id"])})

    async def test_model_finishing_after_search_completion_creates_child_without_replay(self):
        self.enter(); self.search_only()
        for step in self.value["steps"]:
            step["status"] = "completed"
        self.value["status"] = "finished"
        self.store.update(self.parent["id"], checkpoint={"plan": self.value, "phase": "action_plan"}, status="completed")
        self.context.plan_lookup = lambda plan_id: self.runtime.plan_for_revision("default", plan_id)
        payload = {"operation": "revise", "revision": {"plan_id": "plan-1", "expected_revision": 0}, "steps": [delivery_step()]}
        normalized = self.normalize(payload)
        await self.runtime.revise_plan(self.edit_run["id"], normalized["revision"], "追加正式岗投递")
        child = self.store.get(self.edit_run["id"])
        self.assertEqual(child["parent_id"], self.parent["id"])
        self.assertEqual(child["checkpoint"]["phase"], "action_plan")
        self.assertEqual(self.store.get(self.parent["id"])["checkpoint"]["plan"], self.value)
        await advance(child["checkpoint"]["plan"], self.registry)
        self.assertEqual([name for name, args in self.calls], ["mock_delivery_start"])

    async def test_current_plan_pointer_switch_does_not_revoke_older_task(self):
        self.enter()
        self.context.dialogue = {"tasks": {"plan_id": "unrelated-new-plan"}}
        value = copy.deepcopy(self.value)
        value["steps"] = value["steps"][2:3]
        value["steps"][0]["depends_on"] = []
        await advance(value, self.registry)
        self.assertEqual(value["steps"][0]["status"], "completed")
        self.assertEqual(self.calls[0][0], "mock_delivery_start")

    async def test_actual_cancellation_and_foreign_session_still_stop_execution(self):
        self.enter()
        for foreign in (False, True):
            value = copy.deepcopy(self.value)
            value["steps"] = value["steps"][2:3]; value["steps"][0]["depends_on"] = []
            self.context.cancellation_requested = (lambda: True) if not foreign else (lambda: False)
            if foreign: value["session_id"] = "another-session"
            await advance(value, self.registry)
            self.assertEqual(value["steps"][0]["status"], "blocked")
        self.assertEqual(self.calls, [])

    async def test_resume_change_does_not_silently_switch_attachment(self):
        self.enter()
        value = copy.deepcopy(self.value)
        value["resume_stamp"] = "changed"
        value["steps"] = value["steps"][2:3]; value["steps"][0]["depends_on"] = []
        await advance(value, self.registry)
        self.assertEqual(value["steps"][0]["failure"]["code"], "authority_stale")
        self.assertEqual(self.calls, [])

    async def test_missing_goal_coverage_warns_without_vetoing_requested_actions(self):
        self.enter()
        draft = RequestDraft.model_validate({"steps": [{"id": "read", "tool": "job_stats_read"}], "goals": []})
        plan = validate_draft(draft, self.registry, "看统计", dialogue={}, conditions={}, stamp=STAMP)
        self.assertEqual(plan["status"], "ready")
        self.assertTrue(any(item["code"] == "uncovered_step" and item["severity"] == "warn" for item in preflight_warnings()))

    async def test_unrequested_submission_requires_scope_confirmation(self):
        self.enter()
        draft = RequestDraft.model_validate({"steps": [delivery_step(user_requested=False)], "goals": []})
        # Standalone read of already saved results, no fabricated search dependency.
        draft.steps[0].depends_on = []
        plan = validate_draft(draft, self.registry, "看看有什么岗位", dialogue={}, conditions={}, stamp=STAMP)
        self.assertEqual(plan["status"], "ready")
        await advance(plan, self.registry)
        self.assertEqual(plan["steps"][0]["status"], "waiting_approval")
        self.assertEqual(self.calls, [])

    async def test_uncertain_added_submission_asks_instead_of_repair_loop(self):
        self.enter(); self.search_only()
        plan = self.normalize({"operation": "revise", "steps": [delivery_step(user_requested=False)]})
        self.assertEqual(plan["status"], "revision")
        self.assertFalse(plan["revision"]["add_steps"][0]["user_requested"])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.get(self.parent["id"])["checkpoint"]["plan"], self.value)

    async def test_remove_pending_branch_preserves_progress_and_cannot_be_resumed(self):
        self.enter()
        update = edited_plan(self.value, RevisionDraft(plan_id="plan-1", expected_revision=0, remove_steps=["growth"]),
            self.registry, stamp=STAMP, session_id="default", message="取消成长计划，保留搜索和投递")
        self.assertEqual(update["steps"][:3], self.value["steps"][:3])
        removed = update["steps"][3]
        self.assertTrue(removed["withdrawn"])
        source = {**self.store.get(self.parent["id"]), "checkpoint": {"plan": update}}
        recovered = recover_plan(source, session_id="default", stamp=STAMP)
        self.assertNotIn("growth", recovered.plan.get("recovery_pending", []))

    async def test_cycle_invalid_tools_and_changed_completed_step_remain_internal_errors(self):
        self.enter(); self.search_only()
        for step in (delivery_step(depends_on=["deliver_extra"]), delivery_step(tool="made_up")):
            with self.assertRaises(ValueError):
                self.normalize({"operation": "revise", "steps": [step]})
        running = edited_plan(self.value, RevisionDraft(plan_id="plan-1", expected_revision=0,
            updates=[{"step_id": "search", "arguments": {"result_limit": 20}}]),
            self.registry, stamp=STAMP, session_id="default", message="改数量")
        self.assertTrue(running["paused"])
        self.assertEqual(running["steps"][0]["arguments"], self.value["steps"][0]["arguments"])
        self.assertEqual(running["steps"][0]["staged_arguments"]["result_limit"], 20)
        finished = copy.deepcopy(self.value)
        finished["steps"][0]["status"] = "completed"
        with self.assertRaises(ValueError):
            edited_plan(finished, RevisionDraft(plan_id="plan-1", expected_revision=0,
                updates=[{"step_id": "search", "arguments": {"result_limit": 20}}]),
                self.registry, stamp=STAMP, session_id="default", message="改数量")


class EvidenceReuseContracts(unittest.IsolatedAsyncioTestCase):
    async def test_same_predicate_reuses_verified_jd_evidence_without_model(self):
        async def forbidden(_): raise AssertionError("cached predicate should not call model")
        facts = {"description": "本岗位为正式岗位，由本公司签订劳动合同。"}
        prior = {"facts_digest": filter_facts_digest(facts), "conditions": [{"text": "正式岗位", "status": "pass", "mode": "require",
            "reason": "明确正式", "evidence": [{"field": "description", "quote": "本岗位为正式岗位"}]}]}
        result = await evaluate_filter(forbidden, leaf("正式岗位"), facts, prior_evidence=prior)
        self.assertEqual(result["status"], "pass")
        self.assertTrue(result["conditions"][0]["reused"])

    async def test_changed_jd_or_new_predicate_requires_new_evidence(self):
        calls = []
        async def unknown(request):
            calls.append(request)
            return LLMResult('{"conditions":[{"index":0,"status":"unknown","reason":"未说明","evidence":[]}]}', "scripted", "fixture")
        prior = {"conditions": [{"text": "正式岗位", "status": "pass", "mode": "require",
            "evidence": [{"field": "description", "quote": "本岗位为正式岗位"}]}]}
        for text, description in (("正式岗位", "职位已改为实习"), ("有转正机会", "本岗位为正式岗位")):
            result = await evaluate_filter(unknown, leaf(text), {"description": description}, prior_evidence=prior)
            self.assertEqual(result["status"], "unknown")
        self.assertEqual(len(calls), 2)

    async def test_quote_still_present_but_other_facts_changed_is_not_reused(self):
        facts = {"description": "本岗位为正式岗位，由本公司签订劳动合同。"}
        prior = {"facts_digest": filter_facts_digest(facts), "conditions": [{"text": "正式岗位", "status": "pass",
            "mode": "require", "evidence": [{"field": "description", "quote": "本岗位为正式岗位"}]}]}
        async def unknown(request):
            return LLMResult('{"conditions":[{"index":0,"status":"unknown","reason":"信息矛盾","evidence":[]}]}', "scripted", "fixture")
        facts["description"] += "以上是旧模板，实际合同为实习协议。"
        result = await evaluate_filter(unknown, leaf("正式岗位"), facts, prior_evidence=prior)
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(result["conditions"][0].get("reused"))


class QueueScopeContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.jobs = JobStore(Path(self.temp.name) / "jobs.db")
        self.rows, self.model_calls = [], []
        for index, (status, quote) in enumerate((("pass", "正式岗位"), ("fail", "只招聘实习生"), ("unknown", "招聘类型未说明")), 1):
            row = {"source": "MockJobSite", "source_url": f"http://127.0.0.1:8080/job?id={index}",
                "title": f"合成岗位{index}", "company": "合成公司", "location": "合成城市", "salary": "20-25K",
                "description": quote + "。这里只提供合成招聘事实。", "tags": [], "score": 82,
                "decision": "REVIEW", "reasoning": "合成", "stage": "review", "filter_status": "pass",
                "filter_evidence": {"status": "pass", "conditions": [{"text": "正式岗位", "status": status,
                    "mode": "require", "evidence": [{"field": "description", "quote": quote}]}]}}
            row["filter_evidence"]["facts_digest"] = filter_facts_digest(row)
            ident, _ = self.jobs.upsert_job(row)
            self.rows.append(self.jobs.get_job(ident))
        self.jobs.resume_analysis_evidence = lambda *args, **kwargs: (self.rows, {})
        async def unknown(request):
            self.model_calls.append(request)
            return LLMResult('{"conditions":[{"index":0,"status":"unknown","reason":"信息不足","evidence":[]}]}', "scripted", "fixture")
        self.profile = NS(resume_id="synthetic")
        self.document = ResumeDocumentView(id="synthetic-document", version=1, source_filename="synthetic.pdf")
        self.coordinator = MockWorkflowCoordinator(self.jobs, NS(llm=NS(generate=unknown)),
            NS(supports=lambda url: True), NS(read=lambda: NS(profile=self.profile)), NS(read_document=lambda: self.document))
        self.coordinator._ensure_applicant_bound = AsyncMock(return_value="bound")

    async def asyncTearDown(self):
        self.coordinator = self.jobs = None
        gc.collect(); self.temp.cleanup()

    async def test_semantic_scope_queues_only_pass_and_persists_own_evidence(self):
        result = await self.coordinator.queue_deliveries(session_id="default", task_id="synthetic-search", filters=leaf("正式岗位"))
        self.assertEqual((result["queued"], result["excluded"], result["unknown"]), (1, 1, 1))
        queue = self.jobs.mock_delivery_queue()
        self.assertEqual(len(queue), 1)
        self.assertEqual(json.loads(queue[0]["scope_json"])["filters"]["text"], "正式岗位")
        self.assertEqual(json.loads(queue[0]["scope_evidence_json"])["status"], "pass")
        self.assertEqual(len(self.model_calls), 1)
        self.assertTrue(all(job["filter_status"] == "pass" for job in self.rows))
        TRACES.append({"case": "branch-semantic-scope", "result": result, "queued_job_ids": [item["job_id"] for item in queue]})

    async def test_second_queue_request_does_not_duplicate_or_require_new_score(self):
        first = await self.coordinator.queue_deliveries(session_id="default", filters=leaf("正式岗位"))
        second = await self.coordinator.queue_deliveries(session_id="default", filters=leaf("正式岗位"))
        self.assertEqual(first["queued"], 1)
        self.assertEqual(second["queued"], 0)
        self.assertEqual(len(self.jobs.mock_delivery_queue()), 1)

    async def test_stop_during_semantic_judgment_prevents_late_enqueue(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(request):
            entered.set()
            await release.wait()
            return await self.original_generate(request)
        self.original_generate = self.coordinator.browser.llm.generate
        self.coordinator.browser.llm.generate = delayed
        self.coordinator.delivery.current = lambda: DeliveryTaskView()
        task = asyncio.create_task(self.coordinator.queue_deliveries(session_id="default", filters=leaf("正式岗位")))
        await entered.wait()
        await self.coordinator.cancel_delivery_queue("default")
        release.set()
        with self.assertRaises(PreflightRefused):
            await task
        self.assertEqual(self.jobs.mock_delivery_queue(), [])

    async def test_resume_mutation_during_judgment_prevents_wrong_attachment(self):
        original = self.coordinator.browser.llm.generate
        async def mutate(request):
            self.document.summary = "合成的已修改简历"
            return await original(request)
        self.coordinator.browser.llm.generate = mutate
        with self.assertRaises(PreflightRefused):
            await self.coordinator.queue_deliveries(session_id="default", filters=leaf("正式岗位"))
        self.assertEqual(self.jobs.mock_delivery_queue(), [])


if __name__ == "__main__":
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(cls)
        for cls in (DurableCommandContracts, EvidenceReuseContracts, QueueScopeContracts))
    if "--regression" in sys.argv:
        for name in ("goal_graph_contract", "action_contract", "condition_refresh_contract", "presentation_contract",
                     "salary_revision_contract", "delivery_scheduling_contract", "background_polling_contract",
                     "reporting_contract", "privacy_contract", "history_path_contract"):
            module = importlib.import_module("evaluation." + name)
            # Do not count imported helper TestCases twice.
            for cls in vars(module).values():
                if isinstance(cls, type) and issubclass(cls, unittest.TestCase) and cls.__module__ == module.__name__:
                    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(cls))
    class Recorded(unittest.TextTestResult):
        def addSuccess(self, test):
            super().addSuccess(test)
            TRACES.append({"test": test.id(), "status": "pass"})
    result = unittest.TextTestRunner(verbosity=1, resultclass=Recorded).run(suite)
    output = Path(__file__).resolve().parents[1] / "test-results" / "command-recovery" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    summary = {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
               "regression": "--regression" in sys.argv,
               "external_model_calls": 0, "actual_recruitment_actions": 0,
               "coverage": "Recorded failure shapes, production normalization, encrypted SQLite, reopen, watcher and scoped queue with scripted executors; not real-model accuracy or browser E2E"}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "traces.jsonl").write_text("".join(json.dumps(record, ensure_ascii=False, default=str) + "\n" for record in TRACES), encoding="utf-8")
    (output / "failures.json").write_text(json.dumps([{"test": str(test), "traceback": error} for test, error in result.failures + result.errors], ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), **summary}, ensure_ascii=False))
    raise SystemExit(0 if result.wasSuccessful() else 1)
