"""Removed learning capability contracts; synthetic objects, no network or state."""
from __future__ import annotations

import inspect
import json
import copy
import unittest
import asyncio
from unittest.mock import AsyncMock
from types import SimpleNamespace as NS

from app.core.agent_tools import AgentToolRegistry
from app.core.main_agent import AutoHuntAgent
from app.core.models import AgentLane
from app.core.task_semantics import understand
from app.intent.catalog import INTENT_SPECS, FEWSHOT_EXAMPLES
from app.workflow.contracts import EVIDENCE_TOOLS
from app.workflow.planning import RequestDraft, validate_draft
from app.workflow.validation import PlanValidationError
from app.workflow.recovery import recover_plan
from app.runtime.context import RunContext, current_run
from app.core.agent_tools import AgentToolOutcome


class RemovedGrowthContracts(unittest.TestCase):
    def setUp(self):
        self.registry = AgentToolRegistry(jobs=None, browser=None,
            candidate_state=NS(read=lambda: NS(profile=NS(resume_id="synthetic-resume"))),
            resume_workspace=NS(read_document=lambda: NS(id="synthetic-document", version=1)),
            memory=None, mock_workflow=NS(), interview_preparations=NS())

    def test_no_learning_tool_or_context_topic_is_exposed(self):
        schema = json.dumps(self.registry.schemas(), ensure_ascii=False)
        self.assertNotIn("learning_plan", schema)
        self.assertNotIn('"learning"', schema)
        self.assertNotIn("成长计划", schema)
        self.assertNotIn("学习计划", schema)

    def test_no_learning_router_label_or_demonstrations(self):
        self.assertNotIn("learning_plan", INTENT_SPECS)
        self.assertTrue(all(intent != "learning_plan" for _, intent in FEWSHOT_EXAMPLES))
        self.assertNotIn("GROWTH", AgentLane.__members__)

    def test_constructor_does_not_require_removed_service(self):
        self.assertNotIn("learning_plans", inspect.signature(AgentToolRegistry).parameters)
        self.assertNotIn("learning_plans", inspect.signature(AutoHuntAgent).parameters)

    def test_removed_tool_never_receives_authority(self):
        decision = self.registry.authorize("learning_plan_generate", {})
        self.assertEqual(decision.code, "unknown_tool")

    def test_removed_tool_cannot_be_compiled_into_new_plan(self):
        draft = RequestDraft.model_validate({"steps": [{"id": "removed", "tool": "learning_plan_generate"}]})
        with self.assertRaises(PlanValidationError):
            validate_draft(draft, self.registry, "生成学习计划", dialogue={}, conditions={}, stamp="synthetic")
        self.assertNotIn("learning_plan_generate", EVIDENCE_TOOLS)

    def test_removed_action_is_not_a_routing_or_authorization_shortcut(self):
        self.assertFalse(understand("生成成长计划").bind("learning_plan_generate", "生成成长计划"))

    def test_search_delivery_resume_and_interview_capabilities_remain(self):
        names = {schema["function"]["name"] for schema in self.registry.schemas()}
        self.assertTrue({"job_search_start", "job_results_read", "mock_delivery_start",
            "resume_propose_patch", "interview_preparation_generate"} <= names)

    def test_failed_mixed_history_retires_learning_without_rewriting_or_replaying_receipts(self):
        completed = {"id": "search", "tool": "job_search_start", "arguments": {}, "effect": "external",
            "status": "completed", "result": {"id": "synthetic-search"}, "depends_on": []}
        legacy = {"id": "legacy", "tool": "learning_plan_generate", "arguments": {}, "effect": "write",
            "status": "failed", "depends_on": ["search"],
            "failure": {"code": "timeout", "effect_started": False, "uncertain": False}}
        pending = {"id": "stats", "tool": "job_stats_read", "arguments": {}, "effect": "read",
            "status": "pending", "depends_on": ["search"]}
        run = {"id": "legacy-failed-run", "session_id": "default", "status": "failed", "checkpoint": {
            "phase": "action_plan", "plan": {"id": "legacy-plan", "resume_stamp": "synthetic",
                "status": "failed", "questions": [], "steps": [completed, legacy, pending]}}}
        original = copy.deepcopy(run)
        decision = recover_plan(run, session_id="default", stamp="synthetic")
        self.assertEqual(decision.action, "resume")
        self.assertEqual(decision.plan["recovery_pending"], ["stats"])
        self.assertEqual(decision.plan["steps"][1]["status"], "skipped")
        self.assertEqual(decision.plan["steps"][1]["failure"]["code"], "feature_removed")
        self.assertEqual(decision.plan["steps"][0]["result"], completed["result"])
        self.assertTrue(decision.plan["steps"][0]["retained_result"])
        self.assertEqual(run, original)


class RetainedInterviewConfirmation(unittest.IsolatedAsyncioTestCase):
    async def test_interview_proposal_and_confirmation_keep_a_valid_context_route(self):
        registry = AgentToolRegistry(jobs=None, browser=None,
            candidate_state=NS(read=lambda: NS(profile=NS(resume_id="synthetic-resume"))),
            resume_workspace=NS(read_document=lambda: NS(id="synthetic-document", version=1)),
            memory=None, mock_workflow=NS(), interview_preparations=NS())
        context = RunContext("synthetic-confirmation", asyncio.current_task(), lambda *a: None,
                             lambda *a: None, dialogue={}, search_state={"revision": 1})
        token = current_run.set(context)
        try:
            proposed = await registry._action_propose({"tool": "interview_preparation_generate",
                "arguments": {"selector": "第1个"}, "description": "生成所选岗位的面试准备"})
            self.assertEqual(proposed["status"], "pending")
            self.assertEqual(context.dialogue["active_intent"], "applications")
            execute = AsyncMock(return_value=AgentToolOutcome("interview_preparation_generate", "ok", {"message": "合成准备已保存"}, "write"))
            worker = NS(tool_registry=NS(execute=execute), _lane_from_tools=lambda *args: AgentLane.APPLICATION)
            await AutoHuntAgent._resolve_context_confirmation(worker, "确认")
            execute.assert_awaited_once()
            self.assertEqual(execute.await_args.args[0], "interview_preparation_generate")
            self.assertEqual(execute.await_args.kwargs["user_message"], "生成面试准备")
            self.assertEqual(context.dialogue["pending"][-1]["status"], "consumed")
        finally:
            current_run.reset(token)


if __name__ == "__main__":
    unittest.main()
