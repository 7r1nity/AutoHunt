"""Search identity and before-write failure regressions; synthetic stores only."""
from __future__ import annotations

import asyncio
import copy
import io
import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS

from app.core.agent_tools import ToolLoopState
from app.core.models import BrowserTaskStatus
from app.runtime.dialogue import apply_operation
from app.runtime.resilience import PreflightRefused
from app.workflow.planning import plan_request
from app.workflow.result_sources import ResultSourceClarification, resolve_search_source
from app.workflow.revision import RevisionDraft, edited_plan
from app.llm.base import LLMResult
from .condition_refresh_contract import Refresh, TRACES
from .goal_graph_contract import STAMP


class Sources(Refresh):
    def source(self, explicit=None):
        self.owned()
        return resolve_search_source(self.jobs, self.context, resume_id=self.profile.resume_id, explicit=explicit)

    def another(self, ident="search-2", *, session="default", resume="synthetic-resume", status=BrowserTaskStatus.COMPLETED):
        self.jobs.create_task(ident, "mock", "合成第二轮", resume_id=resume)
        self.jobs.save_search_request(ident, {"conversation_id": session})
        self.jobs.update_task(ident, status=status)
        return ident

    async def test_chat_id_repaired_to_unique_trusted_search(self):
        self.owned()
        result = await self.registry.execute("job_results_refilter", {"task_id": self.context.run_id},
                                            user_message="重新筛选", state=ToolLoopState())
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.content["task_id"], self.task_id)
        self.assertEqual(result.content["checked"], 5)
        self.assertTrue(any(row["kind"] == "result_source_normalized" for row in self.events))

    async def test_nonexistent_id_without_context_is_input_wait_not_uncertain(self):
        self.owned(); self.context.dialogue = {}
        result = await self.registry.execute("job_results_refilter", {"task_id": "chat-id"},
                                            user_message="重新筛选", state=ToolLoopState())
        self.assertEqual(result.status, "awaiting_user")
        self.assertFalse(result.failure["effect_started"])
        self.assertFalse(result.failure["uncertain"])
        self.assertEqual(self.jobs.get_job(self.ids[0])["filter_status"], "unknown")

    async def test_valid_foreign_resume_or_session_is_not_repaired(self):
        for ident, session, resume in (("other-session", "foreign", self.profile.resume_id),
                                       ("other-resume", "default", "foreign-resume")):
            self.another(ident, session=session, resume=resume)
            with self.assertRaises(ResultSourceClarification): self.source(ident)

    async def test_pending_explicit_search_not_replaced_by_completed_one(self):
        self.another(status=BrowserTaskStatus.RUNNING)
        with self.assertRaises(ResultSourceClarification): self.source("search-2")

    async def test_multiple_known_searches_require_choice_for_invalid_id(self):
        self.another()
        self.context.dialogue = apply_operation(self.context.dialogue, dict(op="snapshot", topic="applications", kind="job",
            source="synthetic", version="search-2", items=[{"id": self.ids[0]}], displayed=True))
        with self.assertRaises(ResultSourceClarification): self.source("chat-id")
        self.assertEqual(self.source("search-2"), "search-2")

    async def test_upstream_receipt_beats_new_ambient_search(self):
        self.another()
        plan = {"steps": [dict(id="producer", tool="job_search_start", task_id=self.task_id, status="completed", depends_on=[]),
                          dict(id="refresh", tool="job_results_refilter", depends_on=["producer"])]}
        self.context.plan_reader = lambda: plan
        self.context.action_authority = {"step_id": "refresh"}
        self.context.dialogue["tasks"]["browser_id"] = "search-2"
        self.assertEqual(self.source("chat-id"), self.task_id)
        with self.assertRaises(ResultSourceClarification): self.source("search-2")

    async def test_multiple_upstream_receipts_allow_explicit_selection(self):
        self.another()
        self.context.plan_reader = lambda: {"steps": [
            dict(id="one", tool="job_search_start", task_id=self.task_id, depends_on=[]),
            dict(id="two", tool="job_search_start", task_id="search-2", depends_on=[]),
            dict(id="refresh", tool="job_results_refilter", depends_on=["one", "two"])]}
        self.context.action_authority = {"step_id": "refresh"}
        with self.assertRaises(ResultSourceClarification): self.source("chat-id")
        self.assertEqual(self.source("search-2"), "search-2")

    async def test_missing_prescribed_receipt_never_falls_back_to_another_search(self):
        self.context.action_authority = {"evidence_task_id": "missing-bound-search"}
        with self.assertRaises(ResultSourceClarification): self.source()
        with self.assertRaises(ResultSourceClarification): self.source(self.task_id)

    async def test_database_read_failure_before_write_is_not_uncertain(self):
        self.owned()
        def failed(*args, **kwargs): raise ConnectionError("synthetic read failure")
        self.jobs.refilter_candidates = failed
        self.context.effect_state = {"phase": "unknown"}
        self.context.action_authority = {"execution_id": "synthetic", "plan_id": "synthetic", "step_id": "refresh", "tool": "job_results_refilter", "requested": True}
        result = await self.registry.execute("job_results_refilter", {}, user_message="重新筛选", state=ToolLoopState())
        self.assertEqual(result.status, "error")
        self.assertEqual(self.context.effect_state["phase"], "preparing")
        self.assertFalse(result.failure["uncertain"])
        self.assertFalse(result.failure["effect_started"])

    async def test_stale_atomic_refresh_validation_has_no_effect(self):
        self.owned()
        rows = self.jobs.refilter_candidates(self.task_id, resume_id=self.profile.resume_id, session_id="default")
        self.jobs.update_job_filter_result(self.ids[-1], {"status": "fail"})
        with self.assertRaises(PreflightRefused):
            self.jobs.save_refilter_results(self.task_id, rows, [{"status": "pass"}] * len(rows), {})
        self.assertEqual(self.jobs.get_job(self.ids[0])["filter_status"], "unknown")

    async def test_successful_refilter_records_preparing_write_and_commit(self):
        self.owned()
        self.context.effect_state = {"phase": "unknown"}
        self.context.action_authority = {"execution_id": "synthetic", "plan_id": "synthetic", "step_id": "refresh", "tool": "job_results_refilter"}
        result = await self.registry._job_results_refilter({})
        self.assertEqual(result["task_id"], self.task_id)
        self.assertEqual(self.context.effect_state["phase"], "committed")
        self.assertEqual([r["phase"] for r in self.events if r["kind"] == "effect_phase"], ["preparing", "started", "committed"])

    async def test_planner_replaces_unexecuted_wrong_tools_instead_of_patching_them(self):
        self.owned()
        old = dict(id="old-plan", original="移除这些，其余投递", answers=[], questions=["这些指谁"],
                   status="clarifying", session_id="default", resume_stamp=STAMP, revision=0, goals=[], steps=[
            dict(id="old-refilter", tool="job_results_refilter", arguments={"task_id": "chat-id"}, depends_on=[],
                 effect="write", status="waiting_input", user_requested=True),
            dict(id="old-delivery", tool="mock_delivery_start", arguments={}, depends_on=["old-refilter"],
                 effect="external", status="pending", user_requested=True)])
        for entry in old["steps"]:
            entry.update(command_quote="移除这些，其余投递", scope_quote="", slot_evidence={}, missing_slots=[], questions=[])
        self.context.plan_reader = lambda: old
        payloads = []
        async def generate(request):
            payload = json.loads(request.user_message); payloads.append(payload)
            self.assertIn("旧计划不是动作选择的标准答案", request.instructions)
            self.assertIn("task_id只能从这些记录中选择", request.instructions)
            self.assertEqual({row["task_id"] for row in payload["result_sources"]}, {self.task_id})
            return LLMResult(json.dumps(dict(operation="revise", questions=[], revision=dict(
                remove_steps=["old-refilter", "old-delivery"], questions=[], add_steps=[
                    dict(id="remove", tool="job_remove_selection", arguments={"selector": "第三方或者外包岗位", "selection_mode": "matching"}, user_requested=True),
                    dict(id="remaining", tool="mock_delivery_start", arguments={"selector": "剩余岗位", "selection_mode": "matching"}, user_requested=True)])), ensure_ascii=False), "scripted", "revision")
        result = await plan_request(NS(generate=generate), self.registry, "移除第三方或者外包，其余投递", dialogue=self.context.dialogue,
                                    conditions=self.context.search_state, stamp=STAMP)
        revised = edited_plan(old, RevisionDraft.model_validate(result["revision"]), self.registry, stamp=STAMP,
                              session_id="default", message="移除第三方或者外包，其余投递")
        steps = {s["id"]: s for s in revised["steps"]}
        self.assertEqual(steps["old-refilter"]["status"], "skipped")
        self.assertEqual(steps["old-delivery"]["status"], "skipped")
        self.assertEqual(steps["remaining"]["depends_on"], ["remove"])
        self.assertFalse(revised["questions"])
        TRACES.append(dict(kind="wrong_plan_replacement", request=payloads, revised=copy.deepcopy(revised)))


def main():
    buffer = io.StringIO()
    result = unittest.TextTestRunner(stream=buffer, verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Sources))
    output = Path(__file__).parent / "results" / "result-sources" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    summary = dict(tests=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                   external_model_calls=0, real_browser_actions=0, coverage="scripted planner + real registry/SQLite/source ownership/phase instrumentation")
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (output / "tests.log").write_text(buffer.getvalue(), encoding="utf-8")
    (output / "traces.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False, default=str)+"\n" for r in TRACES), encoding="utf-8")
    if not result.wasSuccessful(): print(buffer.getvalue()[-18000:])
    print(json.dumps(dict(output=str(output), **summary)))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__": raise SystemExit(main())
