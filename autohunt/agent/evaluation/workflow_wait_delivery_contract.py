"""Background workflow waits/failures reach durable chat; no model or browser.

Run from agent/: python -m evaluation.workflow_wait_delivery_contract
"""
from __future__ import annotations

import asyncio
import copy
import gc
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from app.core.job_targets import TargetClarification
from app.core.models import BrowserTaskView
from app.jobs.store import JobStore
from app.security.decisions import ActionDecision, Decision
from app.security.risk import SecurityRisk
from app.runtime.store import RunStore
from app.runtime.context import effect_checkpoint
from app.workflow.execution import completion_steps
from app.workflow.presentation import Presentation
from evaluation.stateful_scenarios import Harness, step


TRACES = []


class WorkflowWaitDeliveryContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="autohunt-wait-delivery-")
        self.h = Harness(self.temp.name)
        self.jobs = JobStore(Path(self.temp.name) / "jobs.db")
        self.h.jobs = self.h.runtime.jobs = self.h.registry.jobs = self.jobs
        self.run_id = self.h.run["id"]
        self.browser = BrowserTaskView(id="synthetic-search", status="completed", query="合成工程师")
        self.h.runtime.observe_browser = lambda _: self.browser
        self.jobs.search_report = lambda _: {"covered_jobs": 1, "qualified_jobs": 1,
            "constraints": {"result_limit": 20}, "jobs": [{"id": 1, "title": "合成工程师",
                "company": "合成公司", "location": "深圳", "salary": "12-15K", "stage": "review",
                "employment_type": "正式", "jd_summary": "合成研发职责", "content_hash": "synthetic-job-facts"}]}
        self.h.registry.authorize = lambda *args: ActionDecision(
            Decision.ALLOW, "synthetic_scope", SecurityRisk.LOW, "合成明确范围")

    async def asyncTearDown(self):
        TRACES.append({"test": self.id(), "events": self.h.store.all_events(self.run_id),
            "messages": self.jobs.conversation_history(limit=100), "calls": self.h.calls,
            "run": self.h.store.get(self.run_id)})
        await self.h.close()
        self.h = self.jobs = None
        gc.collect()
        self.temp.cleanup()

    def install_chain(self):
        plan = self.h.install([
            step("search", "job_search_start", status="waiting_external"),
            step("summary", "job_results_read", {"summary": True}, deps=["search"]),
            step("delivery", "mock_delivery_start", {"min_score": 85}, deps=["summary"]),
        ])
        async def summary(_):
            return {"search_task": self.browser.model_dump(mode="json"),
                "search_report": self.jobs.search_report(self.browser.id)}
        self.h.behavior["job_results_read"] = summary
        return plan

    def waiting_plan(self, status="waiting_input", detail="请明确本次候选范围"):
        plan = self.h.install([step("delivery", "mock_delivery_start", {"min_score": 85}, status=status)])
        plan["steps"][0].update(detail=detail, wait_reason="business_input")
        if status == "waiting_input":
            plan["steps"][0].update(questions=[detail], missing_slots=["selector"])
            plan["questions"] = [detail]
        self.h.store.save_plan(self.run_id, plan)
        return plan

    def history(self):
        return self.jobs.conversation_history(limit=100)

    def updates(self):
        return [event["data"] for event in self.h.store.all_events(self.run_id)
                if event["kind"] == "workflow_summary"]

    async def until_message(self, minimum=1):
        watcher = asyncio.create_task(self.h.runtime.watch_plan(self.run_id))
        try:
            async with asyncio.timeout(3):
                while len(self.history()) < minimum:
                    if watcher.done():
                        watcher.result()
                        raise AssertionError("Watcher returned before delivering its reason")
                    await asyncio.sleep(.01)
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    async def test_search_finishes_then_added_delivery_input_wait_reaches_chat(self):
        self.install_chain()
        async def needs_target(_):
            raise TargetClarification("本轮只有1个可选岗位，请确认所指岗位")
        self.h.behavior["mock_delivery_start"] = needs_target
        await asyncio.wait_for(self.h.runtime.watch_plan(self.run_id), 3)
        run = self.h.store.get(self.run_id)
        self.assertEqual(run["status"], "waiting_user")
        self.assertEqual([call["tool"] for call in self.h.calls], ["job_results_read", "mock_delivery_start"])
        self.assertEqual(sum("整体薪资与 JD 总结" in row["content"] for row in self.history()), 1)
        self.assertIn("本轮只有1个可选岗位", self.history()[-1]["content"])
        self.assertTrue(self.updates()[-1]["intermediate"])

    async def test_pending_approval_reason_is_visible_without_marking_finished(self):
        plan = self.waiting_plan("waiting_approval", "范围变化：请确认本轮深圳岗位，不沿用旧薪资上限")
        approval = self.h.store.ensure_approval(self.run_id, "default", "delivery", {"synthetic": "scope"})
        plan["steps"][0]["approval_id"] = approval["id"]
        self.h.store.save_plan(self.run_id, plan)
        await self.until_message()
        run = self.h.store.get(self.run_id)
        self.assertEqual(run["status"], "waiting_confirmation")
        self.assertEqual(run["checkpoint"]["plan"]["steps"][0]["status"], "waiting_approval")
        self.assertIn("范围变化", self.history()[0]["content"])
        self.assertIn("旧薪资上限", self.history()[0]["content"])
        self.assertEqual(self.h.calls, [])

    async def test_repeat_poll_and_reopened_store_do_not_duplicate_wait(self):
        self.waiting_plan()
        await self.h.runtime.watch_plan(self.run_id)
        first = self.history()
        self.h.runtime.store = self.h.store = RunStore(self.h.store.path, self.h.key)
        await self.h.runtime.watch_plan(self.run_id)
        await self.h.runtime.watch_plan(self.run_id)
        self.assertEqual(self.history(), first)
        self.assertEqual(len(self.updates()), 1)

    async def test_history_and_event_share_a_durable_message_identity(self):
        self.waiting_plan()
        await self.h.runtime.watch_plan(self.run_id)
        update, row = self.updates()[0], self.history()[0]
        self.assertEqual(update["message_id"], update["result"]["message_id"])
        self.assertEqual(row["run_id"], update["message_id"])
        self.assertEqual(row["response"]["message_id"], update["message_id"])
        self.assertEqual(row["content"], update["message"])

    async def test_changed_wait_reason_gets_a_new_receipt_once(self):
        self.waiting_plan(detail="请确认第一轮候选")
        await self.h.runtime.watch_plan(self.run_id)
        plan = self.h.store.get(self.run_id)["checkpoint"]["plan"]
        plan["steps"][0].update(detail="该岗位已下架，请选择其他岗位", questions=["该岗位已下架，请选择其他岗位"])
        self.h.store.save_plan(self.run_id, plan)
        await self.h.runtime.watch_plan(self.run_id)
        await self.h.runtime.watch_plan(self.run_id)
        self.assertEqual(len(self.history()), 2)
        self.assertNotEqual(self.updates()[0]["message_id"], self.updates()[1]["message_id"])

    async def test_wait_can_complete_later_without_losing_final_answer(self):
        plan = self.waiting_plan()
        await self.h.runtime.watch_plan(self.run_id)
        plan = self.h.store.get(self.run_id)["checkpoint"]["plan"]
        plan.update(questions=[], revision=1)
        plan["steps"][0].update(status="completed", questions=[], missing_slots=[], detail=None,
            result={"message": "已将1个本轮岗位加入队列，尚差19个；不是已投递"})
        self.h.store.save_plan(self.run_id, plan)
        await self.h.runtime.watch_plan(self.run_id)
        self.assertEqual(self.h.store.get(self.run_id)["status"], "completed")
        self.assertEqual(len(self.history()), 2)
        self.assertIn("尚差19个", self.history()[-1]["content"])
        self.assertFalse(self.updates()[-1]["intermediate"])

    async def test_pending_binding_reports_reason_while_preserving_wait(self):
        plan = self.waiting_plan("waiting_binding", "共享浏览器暂被另一只读任务使用，等待释放后绑定账号")
        plan["steps"][0]["wait_reason"] = "waiting_resource"
        self.h.store.save_plan(self.run_id, plan)
        self.h.runtime.browser = type("Browser", (), {"can_start_search": lambda _: False})()
        await self.until_message()
        self.assertIn("共享浏览器", self.history()[0]["content"])
        self.assertEqual(self.h.store.get(self.run_id)["checkpoint"]["plan"]["steps"][0]["status"], "waiting_binding")
        self.assertEqual(self.h.calls, [])

    async def test_failed_delivery_after_search_is_not_only_a_search_report(self):
        self.install_chain()
        async def fail(_):
            effect_checkpoint("preparing")
            raise RuntimeError("合成投递准备失败：账号验证未通过")
        self.h.behavior["mock_delivery_start"] = fail
        await self.h.runtime.watch_plan(self.run_id)
        self.assertEqual(self.h.store.get(self.run_id)["status"], "failed")
        self.assertEqual(sum("整体薪资与 JD 总结" in row["content"] for row in self.history()), 1)
        self.assertIn("失败", self.history()[-1]["content"])

    async def test_partial_failure_is_visible_while_another_branch_waits(self):
        plan = self.h.install([
            step("failure", "mock_delivery_start", status="blocked"),
            step("waiting", "job_target_resolve", status="waiting_input")])
        plan["steps"][0].update(detail="本轮已无可用岗位，未启动投递")
        plan["steps"][1].update(detail="请明确另一分支的岗位", missing_slots=["selector"])
        self.h.store.save_plan(self.run_id, plan)
        await self.h.runtime.watch_plan(self.run_id)
        self.assertIn("本轮已无可用岗位", self.history()[0]["content"])
        self.assertIn("请明确另一分支", self.history()[0]["content"])
        self.assertEqual(self.h.store.get(self.run_id)["status"], "waiting_user")

    async def test_search_verification_wait_is_visible_and_not_reported_complete(self):
        self.h.install([step("search", "job_search_start", status="waiting_external")])
        self.browser = BrowserTaskView(id="synthetic-search", status="waiting_for_user", detail="请完成验证码")
        await self.until_message()
        self.assertEqual(self.h.store.get(self.run_id)["status"], "waiting_user")
        self.assertIn("验证码", self.history()[0]["content"])
        self.assertEqual(self.h.calls, [])

    async def test_approval_completed_in_background_reaches_final_chat_once(self):
        plan = self.waiting_plan("waiting_approval", "确认本轮选择的合成岗位")
        snapshot = {"synthetic": "scope"}
        approval = self.h.store.ensure_approval(self.run_id, "default", "delivery", snapshot)
        plan["steps"][0]["approval_id"] = approval["id"]
        self.h.store.save_plan(self.run_id, plan)
        await self.until_message()
        self.h.store.settle_approval(approval["id"], run_id=self.run_id, session_id="default", approve=True, snapshot=snapshot)
        async def queue(_):
            return {"queued": 1, "message": "本轮1个岗位已入队，缺额19个，不是已投递"}
        self.h.behavior["mock_delivery_start"] = queue
        await self.h.runtime.watch_plan(self.run_id)
        await self.h.runtime.watch_plan(self.run_id)
        self.assertEqual(self.h.store.get(self.run_id)["status"], "completed")
        self.assertEqual(len(self.history()), 2)
        self.assertIn("缺额19个", self.history()[-1]["content"])
        self.assertEqual([call["tool"] for call in self.h.calls], ["mock_delivery_start"])

    async def test_wait_publication_does_not_need_an_available_model_slot(self):
        plan = self.waiting_plan()
        self.h.runtime.slots = asyncio.Semaphore(0)
        await asyncio.wait_for(self.h.runtime.publish_plan_answer(self.run_id, plan, plan["steps"], intermediate=True), 1)
        self.assertEqual(len(self.history()), 1)

    async def test_cancel_during_preparation_suppresses_stale_wait(self):
        plan = self.waiting_plan()
        async def cancelled(*args, **kwargs):
            self.h.store.update(self.run_id, cancel_requested=1, status="cancelling")
            return Presentation("不应发布的旧投递等待")
        with patch("app.workflow.presentation.compose", cancelled):
            await self.h.runtime.publish_plan_answer(self.run_id, plan, plan["steps"], intermediate=True)
        self.assertEqual(self.history(), [])
        self.assertEqual(self.updates(), [])

    async def test_revision_during_preparation_suppresses_old_scope(self):
        plan = self.waiting_plan()
        async def revised(*args, **kwargs):
            changed = copy.deepcopy(plan)
            changed["revision"] = 1
            changed["steps"][0]["arguments"] = {"min_score": 90}
            self.h.store.save_plan(self.run_id, changed)
            return Presentation("不应发布的旧范围")
        with patch("app.workflow.presentation.compose", revised):
            await self.h.runtime.publish_plan_answer(self.run_id, plan, plan["steps"], intermediate=True)
        self.assertEqual(self.history(), [])

    async def test_prepared_wait_recovers_without_synthesis_or_tool_replay(self):
        plan = self.waiting_plan()
        publish = self.jobs.add_conversation_message
        self.jobs.add_conversation_message = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("合成发布中断"))
        with self.assertRaises(RuntimeError):
            await self.h.runtime.publish_plan_answer(self.run_id, plan, plan["steps"], intermediate=True)
        self.jobs.add_conversation_message = publish
        async def forbidden(*args, **kwargs):
            raise AssertionError("Prepared message must be reused")
        with patch("app.workflow.presentation.compose", forbidden):
            await self.h.runtime.publish_plan_answer(self.run_id, plan, plan["steps"], intermediate=True)
        self.assertEqual(len(self.history()), 1)
        self.assertEqual(self.h.calls, [])


def main():
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(WorkflowWaitDeliveryContracts))
    output = Path("evaluation/results/workflow-wait-delivery") / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    summary = {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
        "external_model_calls": 0, "recruitment_actions": 0}
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (output / "traces.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in TRACES), encoding="utf-8")
    (output / "failures.json").write_text(json.dumps([{"test": str(test), "detail": detail}
        for test, detail in result.failures + result.errors], ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), **summary}))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
