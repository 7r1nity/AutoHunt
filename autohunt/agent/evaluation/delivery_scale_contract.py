"""100+ delivery queue/state engineering contracts in temporary stores.

Uses production queue, SQLite receipts, task state, planner and executor with
synthetic data. No browser, user database, model provider or recruitment send is
used. The separate browser scale runner measures actual browser actions.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import time
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet

from app.core.models import DeliveryTaskView, DeliveryTaskStatus
from app.jobs.store import JobStore
from app.runtime.context import RunContext, current_run
from app.runtime.resilience import PreflightRefused
from app.runtime.store import RunStore
from app.workflow.mock_coordinator import MockWorkflowCoordinator
from app.workflow.delivery_quantity import delivery_quantity, normalize_delivery_quantities
from app.workflow.planning import RequestDraft
from evaluation.delivery_limit_contract import DeliveryLimitContracts
from evaluation.delivery_request_contract import DeliveryUserChain


TRACES = []


class ScaleQueue(DeliveryLimitContracts):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.runtime_key = Fernet.generate_key().decode()
        self.run_store = RunStore(Path(self.temp.name) / "runtime.db", self.runtime_key)
        self.parent, _ = self.run_store.create("default", "scale-root", "chat", {"message": "投递前120个"})
        self.context.run_id = self.parent["id"]

    async def asyncTearDown(self):
        self.run_store = None
        await super().asyncTearDown()

    async def batch(self, limit=120, **kwargs):
        self.context.owner = asyncio.current_task()
        started = time.monotonic()
        result = await self.coordinator.queue_deliveries(
            session_id="default", task_id="current", limit=limit, **kwargs)
        TRACES.append({"test": self.id(), "queue_seconds": round(time.monotonic() - started, 6),
                       "requested": limit, "result": result,
                       "final_queue_count": len(self.jobs.mock_delivery_queue(active_only=False))})
        return result

    async def test_120_current_round_receipts_enqueue_exactly_120(self):
        ids = self.seed(140)
        result = await self.batch()
        self.assertEqual((result["queued"], result["shortfall"], result["remaining_eligible"]), (120, 0, 20))
        self.assertEqual(result["selected_job_ids"], ids[:120])
        self.assertEqual(len(self.jobs.mock_delivery_queue()), 120)
        self.model.assert_not_awaited()

    async def test_200_delivery_boundary_accepts_200_and_201_has_no_effect(self):
        ids = self.seed(200)
        with self.assertRaises(PreflightRefused):
            await self.batch(201)
        self.assertEqual(self.jobs.mock_delivery_queue(), [])
        result = await self.batch(200)
        self.assertEqual(result["job_ids"], ids)
        self.assertEqual((result["queued"], result["shortfall"]), (200, 0))

    async def test_120_shortfall_does_not_use_130_jobs_from_another_round(self):
        self.make_search("other-round")
        other = self.seed(130, task_id="other-round")
        wanted = self.seed(105)
        result = await self.batch()
        self.assertEqual(result["job_ids"], wanted)
        self.assertEqual(result["shortfall"], 15)
        self.assertTrue(set(other).isdisjoint(result["selected_job_ids"]))

    async def test_existing_120_row_queue_is_not_inserted_again(self):
        ids = self.seed(120)
        first, second = await self.batch(), await self.batch()
        self.assertEqual(first["job_ids"], ids)
        self.assertEqual((second["queued"], second["already_delivered"], second["shortfall"]), (0, 120, 120))
        self.assertEqual(len(self.jobs.mock_delivery_queue(active_only=False)), 120)

    async def test_two_overlapping_task_contexts_have_120_unique_queue_rows(self):
        ids = self.seed(120)
        another_store = JobStore(self.path)
        another = MockWorkflowCoordinator(another_store, self.coordinator.browser, self.coordinator.delivery,
                                          self.candidate, self.resume)
        ready, count = asyncio.Event(), 0

        async def account_bound(_):
            nonlocal count
            count += 1
            if count == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), 10)
            return "bound"

        self.coordinator._ensure_applicant_bound = account_bound
        another._ensure_applicant_bound = account_bound

        async def submit(coordinator, label):
            run, _ = self.run_store.create("default", label, "chat", {"message": "投递前120个", "task": label})
            context = RunContext(run["id"], asyncio.current_task(),
                lambda kind, data: self.run_store.event(run["id"], kind, data), lambda _: None,
                session_id="default", dialogue={}, user_message="投递前120个",
                action_authority={"plan_id": label, "step_id": "deliver", "user_requested": True})
            token = current_run.set(context)
            try:
                result = await coordinator.queue_deliveries(session_id="default", task_id="current", limit=120)
                self.run_store.update(run["id"], status="completed", result=result)
                return run["id"], result
            finally:
                current_run.reset(token)

        outcomes = await asyncio.gather(submit(self.coordinator, "authority-task-a"), submit(another, "authority-task-b"))
        self.assertNotEqual(outcomes[0][0], outcomes[1][0])
        self.assertEqual(sum(result["queued"] for _, result in outcomes), 120)
        rows = another_store.mock_delivery_queue(active_only=False)
        self.assertEqual(len(rows), 120)
        self.assertEqual({row["job_id"] for row in rows}, set(ids))
        self.assertTrue(all(another_store.assert_delivery_queue_facts(row) is not None for row in rows))
        TRACES.append({"test": self.id(), "task_ids": [ident for ident, _ in outcomes],
                       "new_rows_per_task": [value["queued"] for _, value in outcomes],
                       "final_unique_rows": len(rows), "duplicate_rows": len(rows) - len({r["job_id"] for r in rows}),
                       "boundary": "Distinct production task contexts and SQL queue dedup; not browser/token consumption E2E"})

    async def test_120_frozen_queue_rows_survive_database_reopen(self):
        ids = self.seed(120)
        await self.batch()
        original = self.jobs.mock_delivery_queue(active_only=False)
        restored = JobStore(self.path).mock_delivery_queue(active_only=False)
        self.assertEqual(original, restored)
        self.assertEqual({row["job_id"] for row in restored}, set(ids))
        for row in restored:
            self.assertEqual(row["scope"]["limit"], 120)
            self.assertEqual(row["scope"]["source_task_id"], "current")
            self.assertIsNotNone(self.jobs.assert_delivery_queue_facts(row))
        self.run_store.update(self.parent["id"], checkpoint={"scale_queue_ids": ids, "source_task_id": "current"},
                              status="waiting_reply")
        reopened = RunStore(self.run_store.path, self.runtime_key)
        self.assertEqual(reopened.get(self.parent["id"])["checkpoint"]["scale_queue_ids"], ids)

    async def test_cancel_120_queued_rows_preserves_other_session(self):
        self.seed(120)
        await self.batch()
        self.make_search("other-session", session_id="other")
        other_ids = self.seed(4, task_id="other-session")
        await self.coordinator.queue_deliveries(session_id="other", task_id="other-session", limit=4)
        self.coordinator.delivery.current = lambda: DeliveryTaskView()
        self.coordinator.delivery.stop = AsyncMock(side_effect=AssertionError("No browser operation exists"))
        result = await self.coordinator.cancel_delivery_queue("default")
        self.assertEqual(result["cancelled_queue"], 120)
        rows = self.jobs.mock_delivery_queue(active_only=False)
        self.assertEqual(sum(row["status"] == "cancelled" for row in rows), 120)
        self.assertEqual({row["job_id"] for row in rows if row["status"] == "queued"}, set(other_ids))
        self.assertEqual((await self.coordinator.cancel_delivery_queue("default"))["cancelled_queue"], 0)
        TRACES.append({"test": self.id(), "cancelled": 120, "other_session_preserved": 4})

    async def test_resume_change_during_120_batch_prevents_any_queue_write(self):
        self.seed(120)
        async def changed(_):
            self.document = self.document.model_copy(update={"version": 2})
            return "bound"
        self.coordinator._ensure_applicant_bound.side_effect = changed
        with self.assertRaises(PreflightRefused):
            await self.batch()
        self.assertEqual(self.jobs.mock_delivery_queue(active_only=False), [])

    async def test_changed_resume_invalidates_all_120_before_browser_start(self):
        self.seed(120)
        await self.batch()
        self.coordinator.browser.worker = self.coordinator.delivery.worker = None
        self.coordinator.delivery.create_preview = AsyncMock(side_effect=AssertionError("Stale bindings must not start"))
        with patch("app.memory.resume_identity.resume_stamp", return_value="different-resume-version"):
            await self.coordinator.process_delivery_queue()
        rows = self.jobs.mock_delivery_queue(active_only=False)
        self.assertEqual(len(rows), 120)
        self.assertTrue(all(row["status"] == "stale" for row in rows))
        self.coordinator.delivery.create_preview.assert_not_awaited()

    async def test_120_waiting_hr_monitors_do_not_exhaust_foreground_admission(self):
        ids = self.seed(120)
        for ident in ids:
            name = f"scale-hr-{ident}"
            self.jobs.create_delivery_run(name, ident)
            self.jobs.update_delivery_run(name, status=DeliveryTaskStatus.WAITING_FOR_REPLY, submitted=0)
            run, _ = self.run_store.create("default", name, "delivery", {"delivery_id": name})
            self.run_store.update(run["id"], status="waiting_reply", checkpoint={"delivery_id": name})
        waiting = self.jobs.delivery_runs_waiting_reply(limit=None)
        self.assertEqual(len(waiting), 120)
        reopened = RunStore(self.run_store.path, self.runtime_key)
        self.assertEqual(sum(run["status"] == "waiting_reply" for run in reopened.list("default", limit=None)), 120)
        new, created = reopened.create("default", "new-foreground", "chat", {"message": "当前进度"})
        self.assertTrue(created)
        self.assertEqual(new["status"], "queued")
        TRACES.append({"test": self.id(), "persistent_hr_waits": 120, "foreground_admitted": True})


class ScaleUserChain(DeliveryUserChain):
    async def test_natural_quantities_and_tool_schema_accept_120_through_200(self):
        spec = self.registry._tools["mock_delivery_start"].parameters["properties"]["limit"]
        self.assertEqual((spec["minimum"], spec["maximum"]), (1, 200))
        for message in ["投递前120个", "投递120份", "投递前一百二十个"]:
            with self.subTest(message=message):
                quantity = delivery_quantity(message)
                self.assertIsNotNone(quantity)
                self.assertEqual(quantity.limit, 120)
                draft = RequestDraft.model_validate({"steps": [{"id": "deliver", "tool": "mock_delivery_start",
                    "user_requested": True, "arguments": {}}]})
                result = normalize_delivery_quantities(draft, message)
                self.assertEqual(result.steps[0].arguments["limit"], 120)
        self.assertEqual(self.registry._delivery_prefix("前120个"), 120)
        self.assertEqual(self.registry._delivery_prefix("前200个"), 200)

    async def test_natural_prefix_120_reaches_production_plan_authorization_queue_and_chat(self):
        ids = self.seed(120)
        self.snapshot(ids)
        started = time.monotonic()
        run = await self.execute("投递前120个")
        self.assertEqual(run["status"], "completed", run.get("error"))
        step = next(s for s in run["checkpoint"]["plan"]["steps"] if s["tool"] == "mock_delivery_start")
        self.assertEqual(step["arguments"]["limit"], 120)
        self.assertEqual(step["arguments"]["source_task_id"], "current")
        self.assertEqual(step["result"]["queued"], 120)
        self.assertEqual(step["result"]["shortfall"], 0)
        self.assertEqual(set(step["result"]["selected_job_ids"]), set(ids))
        self.assertEqual(len(self.jobs.mock_delivery_queue(active_only=False)), 120)
        self.assertTrue(any(a["status"] == "approved" for a in self.store.approvals(run["id"])))
        self.assertIn("120", run["result"]["message"])
        TRACES.append({"test": self.id(), "pipeline_seconds": round(time.monotonic() - started, 6),
                       "queued": 120, "shortfall": 0, "scripted_model_calls": len(self.requests),
                       "natural_language_accuracy_measured": False})

    async def test_paused_120_plan_persists_without_enqueuing(self):
        ids = self.seed(120)
        self.snapshot(ids)
        original = self.agent.llm.generate
        async def paused(request):
            result = await original(request)
            if request.context != "条件语义核对，不产生执行授权":
                value = json.loads(result.text)
                value["pause"] = True
                return replace(result, text=json.dumps(value, ensure_ascii=False))
            return result
        self.agent.llm.generate = paused
        run, _ = self.store.create("default", "scale-paused-120", "chat", {"message": "投递前120个，先暂停不要执行"})
        task = asyncio.create_task(self.runtime.execute(run["id"]))
        try:
            async with asyncio.timeout(20):
                while True:
                    saved = self.store.get(run["id"])
                    plan = (saved.get("checkpoint") or {}).get("plan") or {}
                    if plan.get("paused") and saved["status"] == "waiting_user":
                        break
                    if task.done():
                        await task
                        self.fail(f"Pause did not persist: {saved['status']}")
                    await asyncio.sleep(.02)
            self.assertEqual(plan["steps"][0]["arguments"]["limit"], 120)
            self.assertEqual(self.jobs.mock_delivery_queue(active_only=False), [])
            TRACES.append({"test": self.id(), "paused_limit": 120, "queue_rows": 0,
                           "state": saved["status"], "watcher_intentionally_nonterminal": not task.done()})
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def main():
    output = Path(__file__).parent / "results" / "delivery-scale-contract" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    source_root = Path(__file__).parents[1]
    selected = [source_root / p for p in ["app/jobs/store.py", "app/workflow/mock_coordinator.py",
        "app/workflow/planning.py", "app/workflow/execution.py", "app/workflow/delivery_quantity.py",
        "app/core/agent_tools.py", "app/runtime/store.py", "app/runtime/manager.py",
        "evaluation/delivery_limit_contract.py", "evaluation/delivery_request_contract.py",
        "evaluation/delivery_scale_contract.py"]]
    source = {str(p.relative_to(source_root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in selected}
    cases = []
    class Recorded(unittest.TextTestResult):
        def startTest(self, test):
            self.started = time.monotonic()
            super().startTest(test)
        def stopTest(self, test):
            failed = any(t is test for t, _ in self.failures + self.errors)
            cases.append({"name": test.id(), "passed": not failed,
                          "duration_seconds": round(time.monotonic() - self.started, 6)})
            print(json.dumps(cases[-1]), flush=True)
            super().stopTest(test)
    # Do not collect inherited base fixtures or count their previous tests.
    suite = unittest.TestSuite(cls(name) for cls in (ScaleQueue, ScaleUserChain)
                              for name in cls.__dict__ if name.startswith("test_"))
    log = io.StringIO()
    started = time.monotonic()
    result = unittest.TextTestRunner(stream=log, verbosity=2, resultclass=Recorded).run(suite)
    (output / "tests.log").write_text(log.getvalue(), encoding="utf-8")
    changed = [p for p, digest in source.items() if hashlib.sha256((source_root / p).read_bytes()).hexdigest() != digest]
    summary = {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
        "cases": cases, "elapsed_seconds": round(time.monotonic() - started, 6), "source_sha256": source,
        "source_changed_during_run": changed, "external_model_calls": 0, "browser_actions": 0,
        "real_recruitment_actions": 0,
        "candidate_receipts": "Preloaded through production store with no search count cap; fresh search result_limit and actual web search are not exercised.",
        "hr_runtime_boundary": "HR runs are persisted/reopened and admission checked; this suite does not start 120 watch_delivery coroutines.",
        "scope": "Synthetic production queue/store/runtime engineering contracts; not real-model understanding or browser/submission success rate"}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "traces.jsonl").write_text("".join(json.dumps(t, ensure_ascii=False, default=str) + "\n" for t in TRACES), encoding="utf-8")
    print(json.dumps({"output": str(output), **{k: summary[k] for k in ("tests", "failures", "errors", "elapsed_seconds", "source_changed_during_run")}}))
    if not result.wasSuccessful():
        print(log.getvalue()[-14000:])
    return int(not result.wasSuccessful() or bool(changed))


if __name__ == "__main__":
    raise SystemExit(main())
