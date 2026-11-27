"""Offline contracts for model-owned predicate repair and user-owned ambiguity.

Uses synthetic requests, in-memory state and scripted model output only. It does
not load runtime databases, call a model provider, search or send applications.
Run from agent/: python -m evaluation.predicate_repair_contract
"""
from __future__ import annotations

import asyncio
import copy
import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS

from app.core.agent_tools import AgentToolOutcome, AgentToolRegistry
from app.core.job_filters import validate_filter
from app.llm.base import LLMRequest, LLMResult
from app.runtime.context import RunContext, current_run
from app.workflow.execution import begin, render_step
from app.workflow.lifecycle import input_waits
from app.workflow.planning import _draft_with_repair, plan_request, validate_draft
from app.workflow.recovery import recover_plan
from app.workflow.validation import PlanValidationError


STAMP = "synthetic-resume:synthetic-document:1"
TRACES: list[dict] = []
MESSAGE = "找深圳的合成岗位，不要外包和第三方，月薪12k至15k；另外查看当前状态。"
CITY_QUESTION = "你希望在哪座城市找工作？"
CITY_MESSAGE = "先问我目标城市再找合成岗位；月薪12k至15k。"


def leaf(text, **fields):
    return {"op": "condition", "text": text, **fields}


def monthly(minimum=12000, maximum=15000):
    return leaf("月薪12k至15k", salary={"unit": "monthly", "minimum": minimum,
                                      "maximum": maximum})


def exclusions():
    return {"op": "all", "children": [
        {"op": "not", "children": [leaf("外包岗位")]},
        {"op": "not", "children": [leaf("第三方岗位")]},
    ]}


def step(ident, tool, arguments=None, **fields):
    return {"id": ident, "tool": tool, "arguments": arguments or {},
            "user_requested": True, **fields}


def search_plan(filters=None, *, groups=None, missing_city=False, include_read=False):
    args = {"queries": ["合成应用工程师"]}
    if filters is not None:
        args["filters"] = copy.deepcopy(filters)
    if groups is not None:
        args["result_groups"] = copy.deepcopy(groups)
    search = step("search", "job_search_start", args)
    if missing_city:
        search.update(missing_slots=["locations"], questions=[CITY_QUESTION])
    nodes = [search]
    if include_read:
        nodes.append(step("read", "context_state_read"))
    return {"goals": [{"quote": MESSAGE, "disposition": "steps",
                       "step_ids": [node["id"] for node in nodes]}], "steps": nodes}


class ScriptedModel:
    def __init__(self, outputs, *, review=None):
        self.outputs = copy.deepcopy(outputs)
        self.requests = []
        self.planning_requests = []
        self.review_requests = []
        self.review = review

    async def generate(self, request):
        self.requests.append(request)
        if request.context == "条件语义核对，不产生执行授权":
            self.review_requests.append(request)
            if isinstance(self.review, Exception):
                raise self.review
            value = self.review
        else:
            self.planning_requests.append(request)
            value = self.outputs[min(len(self.planning_requests) - 1, len(self.outputs) - 1)]
        return LLMResult(json.dumps(value, ensure_ascii=False), "scripted", "predicate-contract",
                         call_id=f"synthetic-call-{len(self.requests)}")

    def feedback(self, index=1):
        return json.loads(self.requests[index].user_message)["validation_feedback"]


class PredicateRepairContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = []
        self.models = []
        self.executed = []
        candidate = NS(read=lambda: NS(profile=NS(resume_id="synthetic-resume")))
        resume = NS(read_document=lambda: NS(id="synthetic-document", version=1))
        self.registry = AgentToolRegistry(jobs=None, browser=None,
            candidate_state=candidate, resume_workspace=resume, memory=None,
            mock_workflow=NS(), interview_preparations=None)
        self.context = RunContext("synthetic-predicate-run", asyncio.current_task(),
            lambda kind, data: self.events.append({"kind": kind, **data}), lambda _: None,
            search_state={"queries": ["合成应用工程师"], "query_source": "session", "source": "mock"},
            dialogue={})
        self.token = current_run.set(self.context)

    async def asyncTearDown(self):
        current_run.reset(self.token)
        TRACES.append({"test": self.id(), "events": self.events,
            "scripted_calls": [len(model.requests) for model in self.models],
            "executed_tools": self.executed})

    async def plan(self, outputs, *, message=MESSAGE, semantic_wait=False, async_validation=False):
        self.context.owner = asyncio.current_task()
        model = ScriptedModel(outputs)
        self.models.append(model)
        request = LLMRequest(instructions="仅修复合成规划，不运行工具。",
            user_message=json.dumps({"message": message,
                "conditions": self.context.search_state}, ensure_ascii=False), context="")
        def validate(draft):
            value = validate_draft(draft, self.registry, message,
                dialogue={}, conditions=self.context.search_state, stamp=STAMP)
            if semantic_wait:
                value["scope_review"] = {"waits": {"search": {"reason": CITY_QUESTION}}}
            return value
        async def validate_after_await(draft):
            # Match plan_request's real async validator: rejection can happen
            # after awaiting scope review, not just at synchronous invocation.
            await asyncio.sleep(0)
            return validate(draft)
        value = await _draft_with_repair(model, request,
            validator=validate_after_await if async_validation else validate)
        return value, model

    @staticmethod
    def search(value):
        return next(node for node in value["steps"] if node["id"] == "search")

    def assert_technical_feedback(self, model, field="filters"):
        feedback = model.feedback()
        issues = feedback.get("technical_issues", [])
        self.assertTrue(issues, "Model repair needs the actual predicate defect, not a generic retry.")
        relevant = [issue for issue in issues if issue.get("step_id") == "search"
                    and issue.get("tool") == "job_search_start" and issue.get("field") == field]
        self.assertTrue(relevant)
        for issue in relevant:
            self.assertTrue(issue.get("code"))
            self.assertTrue(issue.get("reason"))
            self.assertIsInstance(issue.get("path"), list)
            self.assertEqual(issue["path"][0], field)
        self.assertEqual(json.loads(model.requests[1].user_message)["message"],
                         json.loads(model.requests[0].user_message)["message"])
        return relevant

    def assert_repaired(self, value, model, expected):
        self.assertEqual(len(model.requests), 2)
        node = self.search(value)
        self.assertFalse(node.get("technical_issues"))
        self.assertFalse(node.get("unresolved_arguments"))
        self.assertNotIn("filters", node["missing_slots"])
        self.assertEqual(node["arguments"]["filters"],
                         validate_filter(expected).model_dump(mode="json"))
        self.assertFalse(value["questions"])

    async def test_empty_all_gets_precise_repair_before_any_user_question(self):
        invalid = {"op": "all", "children": []}
        expected = exclusions()
        value, model = await self.plan([search_plan(invalid), search_plan(expected)])
        self.assert_technical_feedback(model)
        self.assert_repaired(value, model, expected)

    async def test_not_with_two_children_is_repaired_without_dropping_either_exclusion(self):
        invalid = {"op": "not", "children": [leaf("外包岗位"), leaf("第三方岗位")]}
        expected = exclusions()
        value, model = await self.plan([search_plan(invalid), search_plan(expected)])
        self.assert_technical_feedback(model)
        self.assert_repaired(value, model, expected)
        children = self.search(value)["arguments"]["filters"]["children"]
        self.assertEqual([child["op"] for child in children], ["not", "not"])
        self.assertEqual([child["children"][0]["text"] for child in children],
                         ["外包岗位", "第三方岗位"])

    async def test_combination_mode_defect_is_a_model_error_not_a_user_slot(self):
        invalid = {"op": "all", "mode": "allow", "children": [monthly()]}
        expected = {"op": "all", "children": [monthly()]}
        value, model = await self.plan([search_plan(invalid), search_plan(expected)])
        self.assert_technical_feedback(model)
        self.assert_repaired(value, model, expected)
        self.assertEqual(validate_filter(self.search(value)["arguments"]["filters"]).leaves()[0].mode,
                         "require")

    async def test_nested_salary_bounds_feedback_keeps_the_nested_path(self):
        invalid = {"op": "all", "children": [exclusions(), monthly(15000, 12000)]}
        expected = {"op": "all", "children": [exclusions(), monthly()]}
        value, model = await self.plan([search_plan(invalid), search_plan(expected)])
        issues = self.assert_technical_feedback(model)
        self.assertTrue(any("salary" in item["path"] and "children" in item["path"]
                            for item in issues))
        self.assert_repaired(value, model, expected)

    async def test_group_allow_is_repaired_explicitly_and_retains_its_quota(self):
        invalid = [{"label": "实习", "count": 5, "filters": leaf("实习岗位", mode="allow")}]
        expected = [{"label": "实习", "count": 5, "filters": leaf("实习岗位")}]
        value, model = await self.plan([search_plan(groups=invalid), search_plan(groups=expected)])
        self.assert_technical_feedback(model, "result_groups")
        self.assertEqual(len(model.requests), 2)
        node = self.search(value)
        self.assertEqual(node["arguments"]["result_groups"][0]["count"], 5)
        self.assertEqual(node["arguments"]["result_groups"][0]["filters"]["mode"], "require")
        self.assertFalse(value["questions"])

    async def test_repair_feedback_preserves_the_original_invalid_predicate_as_data(self):
        invalid = {"op": "not", "children": [leaf("外包岗位"), leaf("第三方岗位")]}
        value, model = await self.plan([search_plan(invalid), search_plan(exclusions())])
        feedback = model.feedback()
        self.assertEqual(feedback["candidate"]["steps"][0]["arguments"]["filters"], invalid)
        self.assertEqual(json.loads(model.requests[0].user_message)["conditions"],
                         json.loads(model.requests[1].user_message)["conditions"])
        self.assertTrue(self.search(value)["user_requested"])

    async def test_async_validator_rejection_repairs_without_treating_awaitable_as_plan(self):
        invalid = search_plan(monthly())
        invalid["steps"][0]["arguments"]["synthetic_unknown_argument"] = "not a tool parameter"
        value, model = await self.plan([invalid, search_plan(monthly())], async_validation=True)
        self.assertEqual(len(model.requests), 2)
        feedback = model.feedback()
        self.assertTrue(any(issue["code"] == "invalid_argument_schema" for issue in feedback["issues"]))
        self.assertEqual(feedback["technical_issues"], [])
        self.assertEqual(feedback["candidate"]["steps"][0]["arguments"]["synthetic_unknown_argument"],
                         "not a tool parameter")
        self.assertNotIn("synthetic_unknown_argument", self.search(value)["arguments"])
        self.assertEqual(self.search(value)["status"], "pending")
        self.assertEqual(self.executed, [])

    async def test_async_validator_exhaustion_retains_structured_failure_and_exact_repair_bound(self):
        invalid = search_plan(monthly())
        invalid["steps"][0]["arguments"]["synthetic_unknown_argument"] = "not a tool parameter"
        with self.assertRaises(PlanValidationError) as caught:
            await self.plan([invalid], async_validation=True)
        self.assertEqual(caught.exception.issue.code, "repair_exhausted")
        model = self.models[-1]
        self.assertEqual(len(model.requests), 4)
        self.assertEqual([model.feedback(index)["attempt"] for index in range(1, 4)], [1, 2, 3])
        for index in range(1, 4):
            self.assertTrue(any(issue["code"] == "invalid_argument_schema"
                                for issue in model.feedback(index)["issues"]))
            self.assertEqual(model.feedback(index)["technical_issues"], [])
        self.assertEqual(self.executed, [])

    async def test_repeated_technical_failure_uses_exactly_three_repairs_then_blocks(self):
        invalid = {"op": "all", "children": []}
        value, model = await self.plan([search_plan(invalid)])
        self.assertEqual(len(model.requests), 4)
        self.assertEqual([model.feedback(index)["attempt"] for index in range(1, 4)], [1, 2, 3])
        node = self.search(value)
        self.assertEqual(node["status"], "blocked")
        self.assertEqual(node["failure"]["code"], "predicate_repair_exhausted")
        self.assertIs(node["failure"]["effect_started"], False)
        self.assertIs(node["failure"]["uncertain"], False)
        self.assertEqual(node["unresolved_arguments"]["filters"], invalid)
        self.assertTrue(node["technical_issues"])

    async def test_exhaustion_never_asks_user_to_supply_internal_filter_json(self):
        value, _ = await self.plan([search_plan({"op": "all", "children": []})])
        node = self.search(value)
        self.assertNotIn("filters", node["missing_slots"])
        self.assertFalse(node["questions"])
        self.assertFalse(value["questions"])
        input_waits(value)
        self.assertEqual(node["status"], "blocked")
        text = render_step(node)
        self.assertTrue(text)
        self.assertNotIn("filters", text)
        self.assertNotIn("请补充", text)

    async def test_independent_safe_read_remains_runnable_when_search_repair_is_exhausted(self):
        value, _ = await self.plan([search_plan({"op": "all", "children": []}, include_read=True)])
        input_waits(value)
        self.assertEqual(self.search(value)["status"], "blocked")
        read = next(node for node in value["steps"] if node["id"] == "read")
        self.assertEqual(read["status"], "pending")
        self.assertEqual(read["depends_on"], [])

    async def test_genuine_missing_city_remains_business_input_without_repair_loop(self):
        value, model = await self.plan([search_plan(monthly(), missing_city=True)], message=CITY_MESSAGE)
        self.assertEqual(len(model.requests), 1)
        node = self.search(value)
        input_waits(value)
        self.assertEqual(node["status"], "waiting_input")
        self.assertIn("locations", node["missing_slots"])
        self.assertIn(CITY_QUESTION, node["questions"])
        self.assertFalse(node.get("technical_issues"))

    async def test_technical_repair_does_not_erase_a_real_business_question_on_same_step(self):
        value, model = await self.plan([
            search_plan({"op": "all", "children": []}, missing_city=True),
            search_plan(monthly(), missing_city=True)], message=CITY_MESSAGE)
        self.assert_technical_feedback(model)
        self.assertEqual(len(model.requests), 2)
        node = self.search(value)
        input_waits(value)
        self.assertEqual(node["status"], "waiting_input")
        self.assertIn("locations", node["missing_slots"])
        self.assertNotIn("filters", node["missing_slots"])
        self.assertIn(CITY_QUESTION, node["questions"])
        self.assertFalse(node.get("technical_issues"))

    async def test_exhaustion_removes_only_technical_questions_and_preserves_business_question(self):
        value, _ = await self.plan([search_plan({"op": "all", "children": []}, missing_city=True)],
                                   message=CITY_MESSAGE)
        node = self.search(value)
        self.assertEqual(node["status"], "blocked")
        self.assertIn("locations", node["missing_slots"])
        self.assertNotIn("filters", node["missing_slots"])
        self.assertIn(CITY_QUESTION, node["questions"])
        self.assertIn(CITY_QUESTION, value["questions"])
        self.assertFalse(any("filters" in question or "无法无损解释" in question
                             for question in value["questions"]))

    async def test_semantic_wait_on_same_step_cannot_hide_a_technical_defect(self):
        value, model = await self.plan([
            search_plan({"op": "all", "children": []}, missing_city=True),
            search_plan(monthly(), missing_city=True)],
            message=CITY_MESSAGE, semantic_wait=True)
        self.assertEqual(len(model.requests), 2)
        self.assert_technical_feedback(model)
        self.assertFalse(self.search(value).get("technical_issues"))
        self.assertIn(CITY_QUESTION, self.search(value)["questions"])

    async def test_continue_cannot_turn_quarantined_none_into_unfiltered_search(self):
        value, _ = await self.plan([search_plan({"op": "all", "children": []})])
        original = copy.deepcopy(value)
        run = {"id": "synthetic-failed-run", "session_id": self.context.session_id,
               "checkpoint": {"phase": "plan", "plan": value}, "cancel_requested": False}
        decision = recover_plan(run, session_id=self.context.session_id, stamp=STAMP)
        self.assertNotEqual(decision.action, "resume")
        self.assertIsNone(decision.plan)
        self.assertEqual(value, original)
        self.assertEqual(self.search(value)["status"], "blocked")
        self.assertEqual(self.search(value)["unresolved_arguments"]["filters"],
                         {"op": "all", "children": []})

    async def sibling_plan(self, verdict, *, added_negotiable=False):
        message = ("分别执行两条独立搜索：深圳合成应用岗位排除外包和第三方；"
                   "深圳合成研究岗位月薪12k至15k，不接受面议。")
        invalid = {"op": "all", "children": []}
        good_filter = monthly()
        if added_negotiable:
            good_filter = {"op": "any", "children": [monthly(),
                leaf("薪资面议", salary={"unit": "negotiable"})]}
        output = {"goals": [{"quote": message, "disposition": "steps", "step_ids": ["bad", "good"]}],
            "steps": [
                step("bad", "job_search_start", {"queries": ["合成应用工程师"],
                    "locations": ["深圳"], "filters": invalid}),
                step("good", "job_search_start", {"queries": ["合成研究工程师"],
                    "locations": ["深圳"], "filters": good_filter})]}
        review = verdict if isinstance(verdict, Exception) else {"scopes": [verdict]}
        model = ScriptedModel([output], review=review)
        self.models.append(model)
        self.context.owner = asyncio.current_task()
        plan = await plan_request(model, self.registry, message, dialogue={},
            conditions=self.context.search_state, stamp=STAMP)
        self.assertEqual(len(model.planning_requests), 4)
        self.assertEqual(len(model.review_requests), 1)
        self.assertEqual(len(model.requests), 5)
        payload = json.loads(model.review_requests[0].user_message)
        self.assertEqual([item["step_id"] for item in payload["scopes"]], ["good"])
        self.assertEqual(payload["message"], message)
        nodes = {node["id"]: node for node in plan["steps"]}
        self.assertIn("bad", nodes)
        self.assertIn("good", nodes)
        self.assertEqual(nodes["bad"]["status"], "blocked")
        self.assertEqual(nodes["bad"]["failure"]["code"], "predicate_repair_exhausted")
        self.assertEqual(nodes["bad"]["unresolved_arguments"]["filters"], invalid)
        self.assertTrue(nodes["bad"]["technical_issues"])
        self.assertEqual(nodes["good"]["arguments"]["queries"], ["合成研究工程师"])
        self.assertEqual(nodes["good"]["arguments"]["locations"], ["深圳"])
        return plan, nodes, model

    async def test_failed_predicate_sibling_does_not_bypass_scope_review_for_valid_search(self):
        plan, nodes, _ = await self.sibling_plan({"step_id": "good", "status": "pass",
            "changes": {}, "reason": "合成研究分支符合用户明确给出的月薪范围。"})
        self.assertEqual(plan["scope_review"]["status"], "reviewed")
        self.assertEqual([entry["step_id"] for entry in plan["scope_review"]["verdicts"]], ["good"])
        self.assertEqual(nodes["good"]["status"], "pending")
        self.assertFalse(nodes["good"].get("technical_issues"))
        self.assertEqual(nodes["good"]["arguments"]["filters"],
                         validate_filter(monthly()).model_dump(mode="json"))

    async def test_valid_sibling_scope_correction_merges_without_dropping_blocked_original_step(self):
        plan, nodes, _ = await self.sibling_plan({"step_id": "good", "status": "correct",
            "changes": {"filters": monthly()}, "reason": "用户明确不要面议，移除模型擅自增加的例外。"},
            added_negotiable=True)
        self.assertEqual(plan["scope_review"]["status"], "reviewed")
        self.assertEqual(nodes["good"]["status"], "pending")
        self.assertEqual(nodes["good"]["arguments"]["filters"],
                         validate_filter(monthly()).model_dump(mode="json"))
        self.assertEqual([item.salary.unit for item in validate_filter(
            nodes["good"]["arguments"]["filters"]).leaves()], ["monthly"])
        self.assertEqual(next(item for item in plan["scope_review"]["verdicts"]
                              if item["step_id"] == "good")["status"], "correct")

    async def test_scope_service_unavailable_keeps_valid_sibling_waiting_and_unexecuted(self):
        plan, nodes, _ = await self.sibling_plan(RuntimeError("Synthetic scope review unavailable"))
        self.assertEqual(plan["scope_review"]["status"], "unavailable")
        self.assertEqual(set(plan["scope_review"]["waits"]), {"good"})
        input_waits(plan)
        self.assertEqual(nodes["good"]["status"], "waiting_input")
        self.assertFalse(nodes["good"].get("technical_issues"))
        self.assertTrue(nodes["good"]["questions"])
        self.assertEqual(nodes["bad"]["status"], "blocked")
        async def execute(name, arguments, **kwargs):
            self.executed.append(name)
            raise AssertionError(f"Scope-unreviewed branch reached executor: {name}")
        self.registry.execute = execute
        self.context.owner = asyncio.current_task()
        await begin(plan, self.registry)
        self.assertEqual(self.executed, [])

    async def test_blocked_search_is_reported_in_chat_and_cannot_reach_search_or_delivery(self):
        value, _ = await self.plan([search_plan({"op": "all", "children": []}, include_read=True)])
        async def execute(name, arguments, **kwargs):
            self.executed.append(name)
            if name != "context_state_read":
                raise AssertionError(f"Unexpected operation reached executor: {name}")
            return AgentToolOutcome(name, "ok", {"message": "已读取合成状态。"}, "read")
        self.registry.execute = execute
        self.context.owner = asyncio.current_task()
        response = await begin(value, self.registry)
        self.assertEqual(self.executed, ["context_state_read"])
        self.assertTrue(response.message)
        self.assertNotIn("filters", response.message)
        self.assertNotIn("请补充", response.message)
        blocked = [event for event in response.tool_events
                   if event.tool == "job_search_start" and event.status == "blocked"]
        self.assertEqual(len(blocked), 1)
        self.assertIn(self.search(value)["detail"], response.message)
        saved = self.context.dialogue["instruction_request"]
        stored_search = self.search(saved)
        self.assertEqual(stored_search["failure"]["code"], "predicate_repair_exhausted")
        self.assertTrue(stored_search["technical_issues"])


def main():
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(
        __import__(__name__, fromlist=["*"])))
    output = Path("evaluation/results/predicate-repair") / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    summary = {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
               "external_model_calls": 0, "browser_runs": 0, "submissions": 0}
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (output / "traces.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False, default=str)
        for row in TRACES), encoding="utf-8")
    (output / "failures.json").write_text(json.dumps([{"test": str(test), "detail": detail}
        for test, detail in result.failures + result.errors], ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), **summary}))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
