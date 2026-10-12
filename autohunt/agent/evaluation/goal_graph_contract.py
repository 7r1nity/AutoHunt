"""Synthetic goal/DAG contracts. No external model, browser or user data."""
from __future__ import annotations

import asyncio
import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS

from app.core.agent_tools import AgentToolRegistry
from app.llm.base import LLMResult
from app.runtime.context import RunContext, current_run
from app.workflow.execution import begin
from app.workflow.planning import RequestDraft, plan_request, validate_draft
from app.workflow.validation import PlanValidationError, preflight, preflight_warnings
from .condition_refresh_contract import alternatives

TRACES = []
MESSAGE = "更正条件：正式岗月薪至少15k，实习日薪至少300，两类不用同时满足。"
STAMP = "synthetic-resume:synthetic-document:1"


def step(ident, tool, args=None, deps=()):
    return {"id": ident, "tool": tool, "arguments": args or {}, "depends_on": list(deps), "user_requested": True,
            "command_quote": "", "scope_quote": "", "slot_evidence": {}, "missing_slots": []}


def draft(steps, roots):
    return {"goals": [{"quote": MESSAGE, "disposition": "steps", "step_ids": roots}], "steps": steps}


class Model:
    def __init__(self, payload): self.payload, self.requests = payload, []
    async def generate(self, request):
        self.requests.append(json.loads(request.user_message))
        return LLMResult(json.dumps(self.payload, ensure_ascii=False), "scripted", "goal-graph")


class Contracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = []
        self.profile = NS(read=lambda: NS(profile=NS(resume_id="synthetic-resume")))
        self.document = NS(id="synthetic-document", version=1)
        self.registry = AgentToolRegistry(jobs=None, browser=None, candidate_state=self.profile,
            resume_workspace=NS(read_document=lambda: self.document), memory=None, learning_plans=NS(), mock_workflow=NS())
        self.context = RunContext("synthetic-graph-run", asyncio.current_task(), lambda kind,data:self.events.append({"kind":kind,**data}), lambda _:None,
            search_state={"queries":["合成分析师"],"query_source":"session","source":"mock","result_limit":15,"locations":[]}, dialogue={})
        self.token = current_run.set(self.context)

    async def asyncTearDown(self):
        current_run.reset(self.token)
        TRACES.append({"test": self.id(), "events": self.events})

    def owned(self): self.context.owner = asyncio.current_task()
    def validate(self, payload):
        self.owned()
        return validate_draft(RequestDraft.model_validate(payload), self.registry, MESSAGE,
            dialogue=self.context.dialogue, conditions=self.context.search_state, stamp=STAMP)

    async def test_goal_covers_transitive_prerequisites(self):
        steps = [step("update","search_state_update",{"filters":alternatives()}),
                 step("refresh","job_results_refilter",deps=["update"]),
                 step("delivery","mock_delivery_start",{"min_score":85},["refresh"])]
        preflight(steps, draft(steps,["delivery"])["goals"], [MESSAGE], set(self.registry._tools))

    async def test_application_edges_added_before_coverage_check(self):
        steps = [step("update","search_state_update",{"filters":alternatives()}), step("search","job_search_start")]
        plan = self.validate(draft(steps,["search"]))
        self.assertEqual(plan["status"], "ready")
        self.assertIn("update", next(item for item in plan["steps"] if item["id"] == "search")["depends_on"])

    async def test_inserted_refilter_covers_implicit_condition_update(self):
        steps = [step("update","search_state_update",{"filters":alternatives()}), step("delivery","mock_delivery_start",{"min_score":85})]
        plan = self.validate(draft(steps,["delivery"]))
        delivery = next(item for item in plan["steps"] if item["id"] == "delivery")
        refresh = next(item for item in plan["steps"] if item["tool"] == "job_results_refilter")
        self.assertEqual(delivery["evidence_step"], refresh["id"])
        self.assertEqual(refresh["depends_on"], ["update"])

    async def test_clarification_only_updates_state_no_recruitment(self):
        self.owned()
        payload = draft([step("update","search_state_update",{"filters":alternatives()})],["update"])
        model = Model(payload)
        plan = await plan_request(model,self.registry,MESSAGE,dialogue={},conditions=self.context.search_state,stamp=STAMP)
        response = await begin(plan,self.registry)
        self.assertEqual(plan["status"], "finished", response.message)
        self.assertEqual([item["tool"] for item in plan["steps"]], ["search_state_update"])
        self.assertEqual(self.context.search_state["filters"]["op"], "any")
        self.assertEqual(len(model.requests), 1)

    async def test_finished_plan_is_not_presented_as_active(self):
        self.owned()
        for status in ("finished", "completed", "cancelled", "outcome_indeterminate"):
            with self.subTest(status=status):
                self.context.plan_reader = lambda: {"id":"completed-old","status":status,"steps":[]}
                model = Model(draft([step("update","search_state_update",{"filters":alternatives()})],["update"]))
                await plan_request(model,self.registry,MESSAGE,dialogue={},conditions=self.context.search_state,stamp=STAMP)
                self.assertIsNone(model.requests[0]["active_plan"])

    async def test_running_and_paused_plans_remain_available(self):
        self.owned()
        for status, paused in (("running", False), ("waiting", True)):
            with self.subTest(status=status):
                self.context.plan_reader = lambda: {"id":"live-plan","status":status,"paused":paused,"steps":[]}
                model = Model(draft([step("update","search_state_update",{"filters":alternatives()})],["update"]))
                await plan_request(model,self.registry,MESSAGE,dialogue={},conditions=self.context.search_state,stamp=STAMP)
                self.assertEqual(model.requests[0]["active_plan"]["id"], "live-plan")
                self.assertEqual(model.requests[0]["active_plan"]["paused"], paused)

    async def test_implicit_dependencies_do_not_grant_submission_consent(self):
        steps = [step("update","search_state_update",{"filters":alternatives()}),
                 step("delivery","mock_delivery_start",{"min_score":85})]
        steps[1]["user_requested"] = False
        plan = self.validate(draft(steps,["delivery"]))
        self.assertEqual(plan["status"], "clarifying")
        self.assertTrue(plan["questions"])
        self.assertFalse(next(item for item in plan["steps"] if item["id"] == "delivery")["user_requested"])

    async def test_candidate_trace_identifies_graph_not_personal_text(self):
        self.owned()
        model = Model(draft([step("update","search_state_update",{"filters":alternatives()})],["update"]))
        await plan_request(model,self.registry,MESSAGE,dialogue={},conditions=self.context.search_state,stamp=STAMP)
        traces = [event for event in self.events if event["kind"] == "plan_candidate_graph"]
        self.assertEqual(traces[0]["steps"][0]["id"], "update")
        self.assertNotIn("arguments", traces[0]["steps"][0])
        self.assertNotIn("quote", traces[0]["goals"][0])

    async def test_missing_goal_link_does_not_override_submission_confirmation(self):
        steps = [step("search","job_search_start"), step("extra_delivery","mock_delivery_start",{"min_score":85})]
        steps[1]["user_requested"] = False
        plan = self.validate(draft(steps,["search"]))
        self.assertEqual(plan["status"], "clarifying")
        warnings = preflight_warnings()
        self.assertTrue(any(item["code"] == "uncovered_step" and item["severity"] == "warn" for item in warnings))
        self.assertFalse(next(item for item in plan["steps"] if item["id"] == "extra_delivery")["user_requested"])

    async def test_goal_metadata_omission_is_warning_for_requested_steps(self):
        plan = self.validate(draft([step("update", "search_state_update")], ["missing"]))
        self.assertEqual(plan["status"], "ready")
        self.assertTrue(any(item["code"] == "goal_binding" for item in preflight_warnings()))

    async def test_invalid_graphs_remain_rejected(self):
        cases = [([step("x","search_state_read",deps=["missing"])],["x"],"invalid_dependency"),
                 ([step("x","search_state_read",deps=["y"]),step("y","search_state_read",deps=["x"])],["x"],"dependency_cycle"),
                 ([step("x","search_state_read"),step("x","search_state_read")],["x"],"duplicate_step_ids"),
                 ([step("x","invented_tool")],["x"],"unknown_tool")]
        for steps,roots,code in cases:
            with self.subTest(code=code):
                with self.assertRaises(PlanValidationError) as caught: self.validate(draft(steps,roots))
                self.assertEqual(caught.exception.issue.code,code)


def main():
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Contracts))
    output = Path("test-results/goal-graph") / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    summary = {"tests":result.testsRun,"failures":len(result.failures),"errors":len(result.errors),"external_model_calls":0,"browser_runs":0,"submissions":0}
    (output/"summary.json").write_text(json.dumps(summary,indent=2),encoding="utf-8")
    (output/"traces.jsonl").write_text("\n".join(json.dumps(row,ensure_ascii=False) for row in TRACES),encoding="utf-8")
    (output/"failures.json").write_text(json.dumps([{"test":str(test),"detail":detail} for test,detail in result.failures+result.errors],ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"output":str(output),**summary}))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__": raise SystemExit(main())
