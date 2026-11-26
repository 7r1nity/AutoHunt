"""Offline answer synthesis contracts. Scripted models, no network/browser.

Run: python -m evaluation.presentation_contract
"""
from __future__ import annotations

import asyncio
import copy
import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from app.workflow.presentation import compose
from app.workflow.execution import begin, completion_steps
from evaluation.reporting_contract import RuntimePublicationContracts, fixture as report_fixture

TRACES = []


def fixture():
    _, _, plan, _ = report_fixture()
    plan.update(original="只分析已选中低分高薪岗位，不展示全部职位", reply="", answers=["结合现有技术栈"])
    jobs = [{"id": i, "title": f"合成职位{i}", "company": "合成公司", "salary": "28-33K",
             "score": 88 if i < 6 else 82, "source_url": f"https://example.org/jobs/{i}"} for i in range(1, 11)]
    read = {"id": "read", "tool": "job_results_read", "status": "completed", "depends_on": [],
            "arguments": {}, "result": {"snapshot_id": "synthetic-list", "jobs": jobs}}
    analysis = plan["steps"][-1]
    analysis.update(tool="job_stats_read", depends_on=["read"], result={"id": "synthetic-analysis", "title": "合成岗位分析",
        "message": "已核对指定岗位：简历未体现业务验收实践。", "summary": "简历未体现业务验收实践。", "jobs_analyzed": 5,
        "score_scope": {"min_score": None, "max_score": 84}, "salary_scope": {"salary_min": 25000},
        "job_sources": jobs[5:], "gaps": [{"name": "业务验收", "jd_quotes": [{"job_id": 6, "quote": "制定验收标准"}]}]})
    plan["steps"] = [read, analysis]
    return plan


def section(step="analysis", text="这些岗位的差距是简历未体现业务验收实践。", ids=None, kind="summary"):
    return {"step_id": step, "kind": kind, "text": text, "job_ids": ids or []}


class ScriptedLLM:
    def __init__(self, sections=None, error=None, raw=None, delay=0):
        self.sections = [section(ids=[6])] if sections is None else sections
        self.error, self.raw, self.delay, self.requests = error, raw, delay, []

    async def generate(self, request):
        self.requests.append(request)
        TRACES.append({"request": json.loads(request.context), "sections": self.sections})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return NS(text=self.raw if self.raw is not None else json.dumps({"sections": self.sections}, ensure_ascii=False))


class CompositionContracts(unittest.IsolatedAsyncioTestCase):
    async def test_analysis_not_unfiltered_tool_dump(self):
        plan, llm = fixture(), ScriptedLLM()
        answer = await compose(plan, plan["steps"], llm)
        self.assertEqual(answer.mode, "model")
        self.assertNotIn("匹配 88", answer.text)
        self.assertNotIn("职位简表", answer.text)
        self.assertIn("业务验收实践", answer.text)
        self.assertIn("](<https://example.org/jobs/6>)", answer.text)
        self.assertEqual(answer.snapshot_ids, [])

    async def test_request_amendments_scopes_and_sources_reach_model(self):
        plan, llm = fixture(), ScriptedLLM()
        before = copy.deepcopy(plan)
        await compose(plan, plan["steps"], llm)
        payload = json.loads(llm.requests[0].context)
        self.assertEqual(payload["request"], plan["original"])
        self.assertEqual(payload["amendments"], plan["answers"])
        analysis = payload["results"][1]
        self.assertEqual(analysis["scope"]["score_scope"]["max_score"], 84)
        self.assertTrue(all(item["score"] == 82 for item in analysis["result"]["job_sources"]))
        self.assertEqual(plan, before)
        self.assertTrue(llm.requests[0].json_output)

    async def test_explicit_list_uses_canonical_order_and_snapshot(self):
        plan = fixture()
        plan["steps"] = plan["steps"][:1]
        answer = await compose(plan, plan["steps"], ScriptedLLM([section("read", "", kind="result")]))
        self.assertEqual(answer.snapshot_ids, ["synthetic-list"])
        self.assertIn("职位简表", answer.text)
        self.assertLess(answer.text.index("合成职位1"), answer.text.index("合成职位10"))

    async def test_invalid_scoped_reference_falls_back_without_affecting_plan(self):
        plan = fixture()
        before = copy.deepcopy(plan)
        answer = await compose(plan, plan["steps"], ScriptedLLM([section(ids=[1])]))
        self.assertEqual(answer.mode, "fallback")
        self.assertNotIn("职位简表", answer.text)
        self.assertIn("已核对指定岗位", answer.text)
        self.assertEqual(plan, before)

    async def test_supporting_list_cannot_be_presented_as_deliverable(self):
        plan = fixture()
        answer = await compose(plan, plan["steps"], ScriptedLLM([section("read", "", kind="result")]))
        self.assertEqual(answer.mode, "fallback")
        self.assertNotIn("职位简表", answer.text)

    async def test_independent_list_goal_can_be_shown_beside_analysis(self):
        plan = fixture()
        plan["goals"] = [{"step_ids": ["read"], "quote": "列出全部结果"},
                         {"step_ids": ["read", "analysis"], "quote": "再分析低分岗位"}]
        answer = await compose(plan, plan["steps"], ScriptedLLM([section("read", "", kind="result"), section()]))
        self.assertEqual(answer.mode, "model")
        self.assertEqual(answer.snapshot_ids, ["synthetic-list"])

    async def test_bad_formats_do_not_fail_completed_actions(self):
        for raw in ("Internal Server Error", "{}", '{"sections":[]}',
                    '{"sections":[{"step_id":"missing","kind":"summary","text":"完成"}]}'):
            with self.subTest(raw=raw):
                plan = fixture()
                answer = await compose(plan, plan["steps"], ScriptedLLM(raw=raw))
                self.assertEqual(answer.mode, "fallback")
                self.assertIn("简历未体现业务验收实践", answer.text)
                self.assertTrue(all(item["status"] == "completed" for item in plan["steps"]))

    async def test_duplicate_sections_are_not_repeated(self):
        plan = fixture()
        answer = await compose(plan, plan["steps"], ScriptedLLM([section(), section()]))
        self.assertEqual(answer.mode, "fallback")
        self.assertEqual(answer.text.count("已核对指定岗位"), 1)

    async def test_provider_failure_only_falls_back(self):
        plan, llm = fixture(), ScriptedLLM(error=RuntimeError("synthetic failure"))
        answer = await compose(plan, plan["steps"], llm)
        self.assertEqual(answer.mode, "fallback")
        self.assertEqual(len(llm.requests), 1)
        self.assertIn("未重跑任何操作", answer.text)

    async def test_timeout_only_falls_back(self):
        plan, llm = fixture(), ScriptedLLM(delay=1)
        with patch("app.workflow.presentation.TIMEOUT", .01):
            answer = await compose(plan, plan["steps"], llm)
        self.assertEqual(answer.reason, "TimeoutError")
        self.assertEqual(len(llm.requests), 1)

    async def test_cancellation_is_not_swallowed(self):
        plan = fixture()
        with self.assertRaises(asyncio.CancelledError):
            await compose(plan, plan["steps"], ScriptedLLM(error=asyncio.CancelledError()))

    async def test_failures_and_queue_receipts_cannot_be_omitted(self):
        plan = fixture()
        plan["steps"] += [
            {"id": "queue", "tool": "mock_delivery_start", "status": "completed", "result": {"message": "已入队5个，不是已投递"}},
            {"id": "failed", "tool": "job_stats_read", "status": "failed", "detail": "合成读失败"},
            {"id": "uncertain", "tool": "resume_propose_patch", "status": "outcome_indeterminate"}]
        answer = await compose(plan, plan["steps"], ScriptedLLM())
        self.assertIn("已入队5个，不是已投递", answer.text)
        self.assertIn("合成读失败", answer.text)
        self.assertIn("结果尚未确认", answer.text)

    async def test_progress_needs_no_extra_model_call(self):
        _, _, plan, _ = report_fixture()
        step = plan["steps"][0]
        step["status"] = "waiting_external"
        llm = ScriptedLLM()
        answer = await compose(plan, [step], llm)
        self.assertEqual(llm.requests, [])
        self.assertIn("搜索尚未完成", answer.text)

    async def test_already_published_results_not_sent_again(self):
        _, _, plan, event = report_fixture()
        plan["steps"] = plan["steps"][:2]
        llm = ScriptedLLM()
        answer = await compose(plan, completion_steps(plan, [event]), llm)
        self.assertEqual(answer.text, "")
        self.assertEqual(llm.requests, [])

    async def test_large_evidence_is_bounded_and_marked(self):
        plan, llm = fixture(), ScriptedLLM()
        plan["steps"][0]["result"]["jobs"] *= 15
        plan["steps"][0]["result"]["huge_text"] = "合成" * 100000
        await compose(plan, plan["steps"], llm)
        self.assertLess(len(llm.requests[0].context), 48000)
        self.assertIn("truncated", llm.requests[0].context)

    async def test_unsafe_source_link_is_not_clickable(self):
        plan = fixture()
        plan["steps"][1]["result"]["job_sources"][0]["source_url"] = "javascript:alert(1)"
        answer = await compose(plan, plan["steps"], ScriptedLLM())
        self.assertNotIn("javascript:", answer.text)
        self.assertIn("岗位 ID 6", answer.text)

    async def test_legacy_analysis_without_new_source_fields_still_answers(self):
        plan = fixture()
        for source in plan["steps"][1]["result"]["job_sources"]:
            source.pop("score"); source.pop("salary")
        answer = await compose(plan, plan["steps"], ScriptedLLM())
        self.assertEqual(answer.mode, "model")

    async def test_foreground_uses_shared_synthesis_without_marking_hidden_snapshot(self):
        plan, events = fixture(), []
        with patch("app.workflow.execution.persist"), patch("app.workflow.execution.advance", AsyncMock(return_value=plan["steps"])), \
                patch("app.workflow.execution.emit", side_effect=lambda kind, **data: events.append((kind, data))):
            response = await begin(plan, NS(response_llm=ScriptedLLM()))
        self.assertIn("业务验收实践", response.message)
        selected = next(data for kind, data in events if kind == "plan_response_selection")
        self.assertEqual(selected["displayed_snapshot_ids"], [])
        self.assertEqual(selected["completed_step_ids"], ["read", "analysis"])


class RuntimeSynthesisContracts(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = RuntimePublicationContracts.asyncSetUp
    asyncTearDown = RuntimePublicationContracts.asyncTearDown
    complete = RuntimePublicationContracts.complete

    async def test_async_watcher_synthesizes_and_recovery_does_not_repeat(self):
        llm = ScriptedLLM([section(text="合成异步分析")])
        self.runtime.agent.tool_registry.response_llm = llm
        self.runtime.browser_summary(self.run["id"], self.browser)
        await self.complete()
        await self.runtime.watch_plan(self.run["id"])
        self.assertEqual(len(llm.requests), 1)
        self.assertIn("合成异步分析", list(self.messages.values())[-1])
        self.assertEqual(sum("合成金融职位" in text for text in self.messages.values()), 1)
        self.assertEqual(self.store.get(self.run["id"])["status"], "completed")

    async def test_prepared_answer_recovers_after_publish_failure_without_model_or_tools(self):
        llm = ScriptedLLM([section(text="合成缓存回答")])
        self.runtime.agent.tool_registry.response_llm = llm
        saved_publish = self.jobs.add_conversation_message
        self.jobs.add_conversation_message = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("synthetic publish crash"))
        with self.assertRaises(RuntimeError):
            await self.runtime.publish_plan_answer(self.run["id"], self.plan, self.plan["steps"][-1:])
        self.jobs.add_conversation_message = saved_publish
        await self.runtime.publish_plan_answer(self.run["id"], self.plan, self.plan["steps"][-1:])
        self.assertEqual(len(llm.requests), 1)
        self.assertEqual(len(self.messages), 1)
        self.assertIn("合成缓存回答", next(iter(self.messages.values())))

    async def test_async_model_failure_does_not_change_execution_success(self):
        self.runtime.agent.tool_registry.response_llm = ScriptedLLM(error=RuntimeError("synthetic"))
        self.runtime.browser_summary(self.run["id"], self.browser)
        await self.complete()
        self.assertEqual(self.store.get(self.run["id"])["status"], "completed")
        self.assertIn("回答整理暂不可用", list(self.messages.values())[-1])

    async def test_cancel_during_synthesis_does_not_publish_or_overwrite_cancellation(self):
        store, run_id = self.store, self.run["id"]
        class CancellingModel(ScriptedLLM):
            async def generate(self, request):
                store.update(run_id, cancel_requested=1, status="cancelling")
                return await super().generate(request)
        self.runtime.agent.tool_registry.response_llm = CancellingModel([section(text="不应发布")])
        self.runtime.browser_summary(run_id, self.browser)
        await self.complete()
        self.assertEqual(self.store.get(run_id)["status"], "cancelling")
        self.assertEqual(len(self.messages), 1)
        self.assertFalse(self.store.has_event(run_id, "workflow_summary"))


if __name__ == "__main__":
    # Imported helper TestCase is not part of this suite.
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(cls)
        for cls in (CompositionContracts, RuntimeSynthesisContracts))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    output = Path(__file__).resolve().parents[1] / "test-results" / "presentation-contract" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    summary = {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
               "real_model_calls": 0, "browser_runs": 0, "coverage": "Scripted synthesis, foreground and durable background publication; not model-quality or browser E2E"}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "traces.jsonl").write_text("".join(json.dumps(trace, ensure_ascii=False) + "\n" for trace in TRACES), encoding="utf-8")
    (output / "failures.json").write_text(json.dumps([{"test": str(test), "traceback": error} for test, error in result.failures + result.errors], ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), **summary}, ensure_ascii=False))
    raise SystemExit(0 if result.wasSuccessful() else 1)
