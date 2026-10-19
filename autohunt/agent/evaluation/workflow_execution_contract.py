"""Planner/executor integration beyond the original state matrix; no network."""
from __future__ import annotations

import asyncio
import copy
import gc
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS

from app.core.agent_tools import MissingPlanScope
from app.core.models import BrowserTaskView, BrowserTaskStatus
from app.core.replies import tool_reply
from app.runtime.context import effect_checkpoint
from app.workflow.execution import advance
from app.workflow.lifecycle import activate_staged_searches
from app.workflow.recovery import recover_plan, reconcile_receipts
from app.workflow.revision import RevisionDraft, edited_plan
from app.workflow.mock_coordinator import MockWorkflowCoordinator
from evaluation.stateful_scenarios import Harness, step, STAMP

TRACES = []


class WorkflowExecutionContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="autohunt-workflow-")
        self.h = Harness(self.temp.name)

    async def asyncTearDown(self):
        TRACES.append({"test": self.id(), "trace": self.h.trace()})
        await self.h.close()
        self.h = None
        gc.collect(); self.temp.cleanup()

    def browser(self, task_id="synthetic-search"):
        view = BrowserTaskView(id=task_id, status=BrowserTaskStatus.PAUSED)
        ledger = []
        async def stop():
            ledger.append("joined_old_worker")
            view.status = BrowserTaskStatus.STOPPED
            return view
        browser = NS(current=lambda: view, stop=stop, can_start_search=lambda: True)
        self.h.browser = self.h.runtime.browser = browser
        return ledger

    async def test_staged_search_keeps_old_request_and_joins_before_new_start(self):
        original = {"queries": ["合成分析师"], "result_limit": 15, "salary_min": 15000}
        self.h.install([step("search", "job_search_start", original, status="waiting_external")])
        self.h.edit({"updates": [{"step_id": "search", "arguments": {"result_limit": 20}}]})
        self.assertTrue(self.h.plan["paused"])
        self.assertEqual(self.h.plan["steps"][0]["arguments"], original)
        ledger = self.browser()
        await activate_staged_searches(self.h.plan, self.h.browser)
        self.h.plan["paused"] = False
        async def start(args):
            self.assertEqual(ledger, ["joined_old_worker"])
            self.assertEqual(args["result_limit"], 20)
            return {"id": "new-search", "status": "running"}
        self.h.behavior["job_search_start"] = start
        await self.h.tick()
        search = self.h.plan["steps"][0]
        self.assertEqual(search["task_id"], "new-search")
        self.assertEqual(search["attempt_history"][0]["arguments"], original)
        self.assertEqual(search["attempt_history"][0]["task_id"], "synthetic-search")

    async def test_runtime_continue_activates_staged_live_parent_not_old_browser_resume(self):
        original = {"queries": ["合成分析师"], "result_limit": 15}
        self.h.install([step("search", "job_search_start", original, status="waiting_external")])
        self.h.edit({"updates": [{"step_id": "search", "arguments": {"result_limit": 20}}]})
        ledger = self.browser()
        hold = asyncio.Event()
        self.h.runtime.tasks[self.h.run["id"]] = asyncio.create_task(hold.wait())
        child, _ = self.h.store.create("default", "continue", "chat", {"message": "继续任务"})
        with self.h.context():
            await self.h.runtime.continue_task(child["id"], "继续任务")
        saved = self.h.store.get(self.h.run["id"])["checkpoint"]["plan"]
        self.assertFalse(saved["paused"])
        self.assertEqual(saved["steps"][0]["arguments"]["result_limit"], 20)
        self.assertEqual(saved["steps"][0]["status"], "pending")
        self.assertEqual(ledger, ["joined_old_worker"])

    async def test_staged_search_never_stops_another_browser_owner(self):
        self.h.install([step("search", "job_search_start", {"queries": ["合成分析师"]}, status="waiting_external")])
        self.h.edit({"updates": [{"step_id": "search", "arguments": {"result_limit": 20}}]})
        ledger = self.browser("foreign-search")
        self.h.browser.can_start_search = lambda: False
        with self.assertRaises(ValueError): await activate_staged_searches(self.h.plan, self.h.browser)
        self.assertEqual(ledger, [])
        self.assertTrue(self.h.plan["paused"])

    async def test_binding_wait_automatically_enqueues_same_scope_after_account_signal(self):
        account, checked = {"applicant_id": ""}, asyncio.Event()
        self.h.registry.mock_workflow = NS(settings=lambda: account)
        self.h.install([step("delivery", "mock_delivery_start", {"min_score": 85})])
        async def queue(args):
            return {"queued": 2} if account["applicant_id"] else {"queued": 0, "binding": "searching", "binding_task_id": "login-search"}
        self.h.behavior["mock_delivery_start"] = queue
        await self.h.tick()
        self.assertEqual(self.h.plan["steps"][0]["status"], "waiting_binding")
        def observe(_):
            checked.set()
            return BrowserTaskView(id="login-search", status=BrowserTaskStatus.RUNNING)
        self.h.runtime.observe_browser = observe
        watcher = asyncio.create_task(self.h.runtime.watch_plan(self.h.run["id"]))
        try:
            await asyncio.wait_for(checked.wait(), 2)
            self.assertFalse(self.h.runtime.sessions.get("default", asyncio.Lock()).locked())
            account["applicant_id"] = "synthetic-applicant"
            await asyncio.wait_for(watcher, 4)
        finally:
            watcher.cancel(); await asyncio.gather(watcher, return_exceptions=True)
        saved = self.h.store.get(self.h.run["id"])
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["checkpoint"]["plan"]["steps"][0]["result"]["queued"], 2)
        self.assertEqual([c["arguments"] for c in self.h.calls], [{"min_score": 85}, {"min_score": 85}])

    async def test_failed_binding_becomes_input_wait_not_completed_delivery(self):
        self.h.install([step("delivery", "mock_delivery_start", {"min_score": 85})])
        async def binding(args): return {"queued": 0, "binding": "searching", "binding_task_id": "login"}
        self.h.behavior["mock_delivery_start"] = binding
        self.h.registry.mock_workflow = NS(settings=lambda: {})
        self.h.runtime.observe_browser = lambda _: BrowserTaskView(id="login", status=BrowserTaskStatus.FAILED)
        await self.h.tick()
        await asyncio.wait_for(self.h.runtime.watch_plan(self.h.run["id"]), 3)
        saved = self.h.store.get(self.h.run["id"])
        self.assertEqual(saved["status"], "waiting_user")
        self.assertEqual(saved["checkpoint"]["plan"]["steps"][0]["status"], "waiting_input")
        self.assertEqual(len(self.h.calls), 1)

    async def test_input_wait_preserves_independent_read_and_does_not_poll_replay(self):
        self.h.install([step("growth", "learning_plan_generate"), step("stats", "job_stats_read")])
        async def missing(args): raise MissingPlanScope("请补充分析范围")
        self.h.behavior["learning_plan_generate"] = missing
        await self.h.tick()
        self.assertEqual([s["status"] for s in self.h.plan["steps"]], ["waiting_input", "completed"])
        await asyncio.wait_for(self.h.runtime.watch_plan(self.h.run["id"]), 2)
        self.assertEqual(self.h.store.get(self.h.run["id"])["status"], "waiting_user")
        self.assertEqual(len(self.h.calls), 2)
        self.assertEqual(recover_plan(self.h.store.get(self.h.run["id"]), session_id="default", stamp=STAMP).action, "clarify")

    async def test_filling_input_wait_reschedules_parent_and_executes_once(self):
        self.h.install([step("growth", "learning_plan_generate")])
        async def missing(args):
            if "max_score" not in args: raise MissingPlanScope("请补充分数范围")
            return {"title": "合成计划", "jobs_analyzed": 1}
        self.h.behavior["learning_plan_generate"] = missing
        await self.h.tick()
        await self.h.runtime.watch_plan(self.h.run["id"])
        with self.h.context():
            await self.h.runtime.revise_plan(self.h.run["id"], {"plan_id": "synthetic-plan", "expected_revision": 0,
                "updates": [{"step_id": "growth", "arguments": {"max_score": 84}}]}, "分析85以下")
        await asyncio.wait_for(self.h.runtime.tasks[self.h.run["id"]], 4)
        saved = self.h.store.get(self.h.run["id"])
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["checkpoint"]["plan"]["questions"], [])
        self.assertEqual(len(self.h.calls), 2)

    async def test_control_dependency_preserves_confirmation_scope_but_not_execution_order(self):
        self.h.install([step("delivery", "mock_delivery_start", {"min_score": 85}, requested=False)])
        await self.h.tick()
        old = self.h.plan["steps"][0]["approval_id"]
        with self.h.context():
            await self.h.runtime.revise_plan(self.h.run["id"], {"plan_id": "synthetic-plan", "expected_revision": 0,
                "add_steps": [{"id": "analysis", "tool": "job_stats_read"}],
                "updates": [{"step_id": "delivery", "depends_on": ["analysis"]}]}, "先分析再投")
        self.assertEqual(self.h.store.approval(old)["status"], "pending")
        updated = self.h.store.get(self.h.run["id"])["checkpoint"]["plan"]
        self.assertEqual(updated["steps"][0]["depends_on"], ["analysis"])
        self.assertEqual(self.h.calls, [])

    async def test_cyclic_dependency_revision_is_atomic(self):
        original = self.h.install([step("a", "job_stats_read"), step("b", "job_stats_read", deps=["a"])])
        before = copy.deepcopy(original)
        with self.assertRaises(ValueError):
            edited_plan(original, RevisionDraft(plan_id="synthetic-plan", expected_revision=0,
                updates=[{"step_id": "a", "depends_on": ["b"]}]), self.h.registry, stamp=STAMP, session_id="default", message="插入")
        self.assertEqual(original, before)
        self.assertEqual(self.h.store.get(self.h.run["id"])["checkpoint"]["plan"], before)

    async def test_twenty_steps_use_batches_without_losing_work_or_replaying(self):
        self.h.install([step(f"read{i}", "job_stats_read") for i in range(20)])
        await self.h.tick()
        self.assertEqual(len(self.h.calls), 8)
        await self.h.tick()
        self.assertEqual(len(self.h.calls), 16)
        await self.h.tick()
        self.assertEqual(len(self.h.calls), 20)
        await self.h.tick()
        self.assertEqual(len(self.h.calls), 20)
        self.assertTrue(all(s["status"] == "completed" for s in self.h.plan["steps"]))

    async def test_cancel_descendants_is_successful_withdrawal_not_run_failure(self):
        self.h.install([step("delivery", "mock_delivery_start"), step("growth", "learning_plan_generate"),
            step("report", "job_stats_read", deps=["delivery"])])
        self.h.edit({"remove_steps": ["delivery"]})
        await self.h.tick()
        await self.h.runtime.watch_plan(self.h.run["id"])
        self.assertEqual(self.h.store.get(self.h.run["id"])["status"], "completed")
        self.assertEqual([c["tool"] for c in self.h.calls], ["learning_plan_generate"])

    async def test_unknown_effect_is_not_safe_merely_because_error_says_before_submit(self):
        self.h.install([step("delivery", "mock_delivery_start", {"min_score": 85})])
        async def fail(args): raise TimeoutError("before submit")
        self.h.behavior["mock_delivery_start"] = fail
        await self.h.tick()
        self.assertEqual(self.h.plan["steps"][0]["status"], "outcome_indeterminate")
        self.assertEqual(recover_plan(self.h.store.get(self.h.run["id"]), session_id="default", stamp=STAMP).action, "verify")

    async def test_started_phase_cannot_be_downgraded_by_nested_preparing_marker(self):
        self.h.install([step("delivery", "mock_delivery_start", {"min_score": 85})])
        async def fail(args):
            effect_checkpoint("preparing")
            effect_checkpoint("started")
            effect_checkpoint("preparing")
            raise TimeoutError("synthetic uncertain send")
        self.h.behavior["mock_delivery_start"] = fail
        await self.h.tick()
        self.assertEqual(self.h.plan["steps"][0]["execution_phase"], "started")
        self.assertEqual(self.h.plan["steps"][0]["status"], "outcome_indeterminate")

    async def test_late_receipt_from_old_attempt_never_completes_new_attempt(self):
        plan = self.h.install([step("delivery", "mock_delivery_start", status="claimed")])
        events = [
            {"seq": 1, "kind": "tool_started", "data": {"tool": "mock_delivery_start", "plan_id": plan["id"], "step_id": "delivery", "arguments": {}, "execution_id": "new"}},
            {"seq": 2, "kind": "tool_finished", "data": {"tool": "mock_delivery_start", "plan_id": plan["id"], "step_id": "delivery", "status": "ok", "result": {"queued": 1}, "execution_id": "old"}},
        ]
        reconcile_receipts(plan, events)
        self.assertEqual(plan["steps"][0]["status"], "claimed")

    async def test_foreign_browser_is_resource_wait_not_owned_binding_task(self):
        foreign = BrowserTaskView(id="foreign-task", status=BrowserTaskStatus.RUNNING)
        browser = NS(current=lambda: foreign, can_start_search=lambda: False,
            store=NS(search_recovery=lambda _: {"request": {"conversation_id": "other"}}))
        coordinator = MockWorkflowCoordinator(NS(mock_workflow_settings=lambda: {"applicant_id": ""}),
            browser, NS(), self.h.candidate, self.h.resumes)
        result = await coordinator.queue_deliveries(session_id="default", min_score=85)
        self.assertEqual(result["binding"], "waiting_resource")
        self.assertIsNone(result["binding_task_id"])
        self.assertEqual(result["queued"], 0)

    async def test_same_session_binding_reuses_owned_search_without_new_start(self):
        view = BrowserTaskView(id="owned-task", status=BrowserTaskStatus.RUNNING)
        browser = NS(current=lambda: view, can_start_search=lambda: False,
            store=NS(search_recovery=lambda _: {"request": {"conversation_id": "default"}}))
        coordinator = MockWorkflowCoordinator(NS(mock_workflow_settings=lambda: {"applicant_id": ""}),
            browser, NS(), self.h.candidate, self.h.resumes)
        result = await coordinator.queue_deliveries(session_id="default", min_score=85)
        self.assertEqual(result["binding_task_id"], "owned-task")
        self.assertEqual(result["binding"], "searching")

    async def test_unknown_duplicate_dependency_is_replanned_not_silently_rewired(self):
        self.h.install([step("search", "job_search_start", status="completed")])
        with self.assertRaises(ValueError):
            self.h.edit({"add_steps": [{"id": "search", "tool": "job_stats_read"},
                {"id": "delivery", "tool": "mock_delivery_start", "depends_on": ["search"]}]})
        self.assertEqual(len(self.h.plan["steps"]), 1)

    async def test_binding_presentation_is_wait_not_failure_or_false_success(self):
        message = tool_reply("mock_delivery_start", "waiting_binding", {})
        self.assertIn("条件满足后会继续", message)
        self.assertNotIn("不会自动重试", message)
        self.assertNotIn("已投递", message)


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(WorkflowExecutionContracts))
    output = Path(__file__).resolve().parents[1] / "evaluation/results/workflow-fix" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    summary = {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors), "external_model_calls": 0, "real_submissions": 0}
    (output/"summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (output/"traces.jsonl").write_text("".join(json.dumps(t, ensure_ascii=False, default=str)+"\n" for t in TRACES), encoding="utf-8")
    (output/"failures.json").write_text(json.dumps([{"test": str(t), "traceback": e} for t,e in result.failures+result.errors], ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), **summary}))
    raise SystemExit(0 if result.wasSuccessful() else 1)
