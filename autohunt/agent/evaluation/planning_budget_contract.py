"""Lossless planner-catalog and input-budget regression contracts.

These are offline engineering checks with generated requests and a scripted
provider. They do not measure model understanding or submit job applications.
Run from agent/: python -m evaluation.planning_budget_contract
"""
from __future__ import annotations

import asyncio
import copy
import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS

from app.core.agent_tools import AgentToolRegistry, AgentToolSpec
from app.core.job_filters import filter_schema
from app.llm.base import LLMResult
from app.llm.manager import LLMManager
from app.runtime.context import RunContext, current_run
from app.runtime.context_builder import ContextBuilder, ContextBudgetExceeded, estimate_tokens
from app.runtime.memory_context import compile_context
from app.workflow.contracts import PlannedStep
from app.workflow.planning import INTERNAL_TOOLS, plan_request


STAMP = "synthetic-resume:synthetic-document:1"
TRACES: list[dict] = []


def registry():
    # Building tool specifications does not read these dependencies. The planner
    # sees every currently enabled capability while executors remain unused.
    return AgentToolRegistry(jobs=None, browser=None, candidate_state=None,
        resume_workspace=None, memory=None, mock_workflow=NS(),
        interview_preparations=NS())


def schema_source(tools):
    return [entry for entry in tools.schemas()
            if entry["function"]["name"] not in INTERNAL_TOOLS]


def expand_catalog(document):
    """Independent JSON-Pointer reader; compare meaning against source schemas."""
    def expand(value, ancestors=()):
        if isinstance(value, list):
            return [expand(item, ancestors) for item in value]
        if not isinstance(value, dict):
            return value
        if "$ref" in value:
            pointer = value["$ref"]
            if not isinstance(pointer, str) or not pointer.startswith("#/"):
                raise AssertionError(f"Unexpected nonlocal reference: {pointer!r}")
            if pointer in ancestors:
                raise AssertionError(f"Unexpected cyclic reference: {pointer}")
            target = document
            for segment in pointer[2:].split("/"):
                target = target[segment.replace("~1", "/").replace("~0", "~")]
            resolved = expand(target, (*ancestors, pointer))
            siblings = {key: item for key, item in value.items() if key != "$ref"}
            if siblings:
                # Compaction must never rely on overriding reference targets.
                overlap = set(resolved) & set(siblings)
                if overlap:
                    raise AssertionError(f"Reference changes sibling semantics: {overlap}")
                resolved.update(expand(siblings, ancestors))
            return resolved
        return {key: expand(item, ancestors) for key, item in value.items()}
    return expand(document["tools"])


def condition(text, **fields):
    return {"op": "condition", "text": text, **fields}


def complex_filters():
    return {"op": "all", "children": [
        {"op": "not", "children": [condition("外包或劳务派遣")]},
        {"op": "any", "children": [
            {"op": "all", "children": [condition("正式岗位"),
                condition("月薪12k至15k", salary={"unit": "monthly", "minimum": 12000, "maximum": 15000})]},
            {"op": "all", "children": [condition("实习岗位"),
                condition("实习日薪不低于300", salary={"unit": "daily", "minimum": 300, "applies_to": "internship"})]},
        ]},
    ]}


def step(ident, tool, arguments=None, depends_on=()):
    return {"id": ident, "tool": tool, "arguments": arguments or {},
        "depends_on": list(depends_on), "user_requested": True}


def draft(message, steps):
    return {"goals": [{"quote": message, "disposition": "steps",
                       "step_ids": [entry["id"] for entry in steps]}], "steps": steps}


def model_messages(request):
    # The same complete wire envelope used by LLMManager.generate, including
    # escaping JSON text inside the current user message.
    return [{"role": "user", "content": (
        f"<trusted_context>\n{request.context}\n</trusted_context>\n\n"
        f"<user_request>\n{request.user_message}\n</user_request>")}]


class BudgetedModel:
    def __init__(self, output):
        self.output = output
        self.requests = []
        self.stats = []

    async def generate(self, request):
        fitted, stats = ContextBuilder().model_input(request.instructions,
            model_messages(request), output_tokens=request.max_output_tokens)
        if fitted != model_messages(request):
            raise AssertionError("Current request was changed to fit the budget")
        self.requests.append(request)
        self.stats.append(stats)
        if request.context == "条件语义核对，不产生执行授权":
            payload = json.loads(request.user_message)
            output = {"scopes": [{"step_id": item["step_id"], "status": "pass",
                "reason": "合成数据参数与请求一致", "changes": {}} for item in payload["scopes"]]}
        else:
            output = self.output
        return LLMResult(json.dumps(output, ensure_ascii=False), "scripted", "planning-budget-contract")


class CatalogContracts(unittest.TestCase):
    def catalog(self, schemas):
        from app.llm.schema_catalog import compact_tool_catalog
        return compact_tool_catalog(schemas)

    def test_all_enabled_tool_definitions_round_trip_losslessly(self):
        source = schema_source(registry())
        self.assertEqual(expand_catalog(self.catalog(source)), source)

    def test_compaction_is_deterministic_and_does_not_mutate_registry(self):
        tools = registry()
        source = schema_source(tools)
        before = copy.deepcopy(source)
        first = self.catalog(source)
        second = self.catalog(source)
        self.assertEqual(first, second)
        self.assertEqual(source, before)
        self.assertEqual(schema_source(tools), before)

    def test_common_filter_constraints_and_null_clearing_are_preserved(self):
        source = schema_source(registry())
        expanded = expand_catalog(self.catalog(source))
        named = {item["function"]["name"]: item["function"] for item in expanded}
        for name in ("job_search_start", "search_state_update", "mock_delivery_start"):
            with self.subTest(tool=name):
                supplied = named[name]["parameters"]["properties"]["filters"]
                self.assertIn("null", supplied["type"])
                self.assertIn("oneOf", supplied)
                self.assertFalse(supplied["additionalProperties"])
                salary = supplied["properties"]["salary"]["anyOf"][0]
                self.assertIn("minimum", salary["properties"])
                self.assertIn("maximum", salary["properties"])
        groups = named["job_search_start"]["parameters"]["properties"]["result_groups"]
        self.assertEqual(groups["items"]["properties"]["filters"], filter_schema())

    def test_new_capability_is_included_without_a_second_tool_allowlist(self):
        tools = registry()
        tool = AgentToolSpec("synthetic_new_capability", "未来功能：保留完整约束",
            {"type": "object", "properties": {"scope": filter_schema()},
             "required": ["scope"], "additionalProperties": False}, "read", lambda args: None)
        tools._tools[tool.name] = tool
        source = schema_source(tools)
        expanded = expand_catalog(self.catalog(source))
        self.assertEqual(expanded[-1], tool.openai_schema())

    def test_schema_like_default_and_enum_data_remain_literal(self):
        literal = {"type": "object", "properties": {"a": {"type": "string"}}}
        source = [{"type": "function", "function": {"name": "synthetic_literal",
            "description": "Do not reinterpret application data as schema references.",
            "parameters": {"type": "object", "properties": {
                "default_data": {"default": [literal] * 3},
                "enum_data": {"enum": [literal, literal]},
            }, "additionalProperties": False}}}]
        catalog = self.catalog(source)
        self.assertEqual(expand_catalog(catalog), source)
        self.assertNotIn("$ref", json.dumps(catalog["tools"][0]["function"]["parameters"]["properties"]))

    def test_empty_and_small_catalogs_still_round_trip(self):
        for source in ([], [{"type": "function", "function": {"name": "small",
                "description": "small", "parameters": {"type": "object", "properties": {}}}}]):
            with self.subTest(size=len(source)):
                self.assertEqual(expand_catalog(self.catalog(source)), source)

    def test_existing_reference_scopes_are_preserved_at_their_original_root(self):
        for parameters in (
            {"$defs": {"local": {"type": "string", "minLength": 3}},
             "type": "object", "properties": {"value": {"$ref": "#/$defs/local"}}},
            {"$id": "https://synthetic.invalid/schema", "type": "object",
             "properties": {"value": {"$ref": "child.json"}}},
        ):
            with self.subTest(parameters=parameters):
                source = [{"type": "function", "function": {"name": "scoped",
                    "description": "Synthetic reference scope", "parameters": parameters}}]
                catalog = self.catalog(source)
                self.assertEqual(catalog["tools"], source)
                self.assertEqual(catalog["self_contained_parameters"], ["scoped"])

    def test_mixed_catalog_keeps_local_p0_scope_separate_from_shared_p0(self):
        generated_source = schema_source(registry())
        local_parameters = {"$defs": {"p0": {"type": "integer", "const": 7421}},
            "type": "object", "properties": {"value": {"$ref": "#/$defs/p0"}},
            "required": ["value"], "additionalProperties": False}
        opaque = {"type": "function", "function": {"name": "synthetic_local_scope",
            "description": "An independently rooted parameter document.", "parameters": local_parameters}}
        source = [*generated_source, opaque]
        before = copy.deepcopy(source)
        catalog = self.catalog(source)
        self.assertEqual(catalog["self_contained_parameters"], ["synthetic_local_scope"])
        self.assertEqual(catalog["tools"][-1], opaque)
        self.assertEqual(source, before)
        self.assertIn("p0", catalog["$defs"])
        self.assertNotEqual(catalog["$defs"]["p0"], local_parameters["$defs"]["p0"])
        # Only generated tools resolve against the catalog's shared definitions.
        generated_document = {**catalog, "tools": catalog["tools"][:-1]}
        self.assertEqual(expand_catalog(generated_document), generated_source)
        # The opaque tool's local pointer resolves at its own parameter root.
        local = catalog["tools"][-1]["function"]["parameters"]
        pointer = local["properties"]["value"]["$ref"]
        target = local
        for segment in pointer[2:].split("/"):
            target = target[segment]
        self.assertEqual(target, {"type": "integer", "const": 7421})


class PlannerBudgetContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tools = registry()
        self.events = []
        self.context = RunContext("synthetic-budget-run", asyncio.current_task(),
            lambda kind, data: self.events.append({"kind": kind, **data}), lambda _: None,
            search_state={"queries": ["合成算法工程师"], "query_source": "session", "source": "mock"},
            dialogue={})
        self.token = current_run.set(self.context)

    async def asyncTearDown(self):
        current_run.reset(self.token)
        TRACES.append({"test": self.id(), "events": self.events})

    async def plan(self, message, output, **kwargs):
        self.context.owner = asyncio.current_task()
        model = BudgetedModel(output)
        plan = await plan_request(model, self.tools, message,
            dialogue=kwargs.get("dialogue", self.context.dialogue),
            conditions=kwargs.get("conditions", self.context.search_state), stamp=STAMP)
        return plan, model

    async def test_empty_history_full_capability_request_fits_default_budget(self):
        message = "帮我找20份深圳的算法工程师岗位，不要外包，月薪12k至15k。"
        output = draft(message, [step("search", "job_search_start", {
            "queries": ["合成算法工程师"], "locations": ["深圳"], "result_limit": 20})])
        plan, model = await self.plan(message, output)
        self.assertEqual(plan["status"], "ready")
        self.assertLess(model.stats[0]["estimated_input_tokens"], 24000)
        self.assertLess(model.stats[0]["estimated_input_tokens"], model.stats[0]["input_token_limit"])
        payload = json.loads(model.requests[0].user_message)
        self.assertEqual(payload["message"], message)
        self.assertEqual(expand_catalog(payload["capabilities"]), schema_source(self.tools))
        TRACES.append({"case": "full_catalog_request", "budget": model.stats[0]})

    async def test_actual_llm_manager_fits_catalog_then_invokes_scripted_provider(self):
        message = "帮我找20份深圳的合成算法工程师岗位。"
        output = draft(message, [step("search", "job_search_start", {
            "queries": ["合成算法工程师"], "locations": ["深圳"], "result_limit": 20})])

        class Provider:
            name, model = "scripted", "planning-manager-integration"

            def __init__(self):
                self.requests = []

            async def generate(self, request):
                self.requests.append(request)
                return LLMResult(json.dumps(output, ensure_ascii=False), self.name, self.model)

        provider = Provider()
        llm = LLMManager(NS(), NS())
        llm._provider = lambda: provider
        self.context.owner = asyncio.current_task()
        plan = await plan_request(llm, self.tools, message, dialogue={},
            conditions=self.context.search_state, stamp=STAMP)
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(len(provider.requests), 1)
        request = provider.requests[0]
        payload = json.loads(request.user_message)
        self.assertEqual(payload["message"], message)
        self.assertEqual(expand_catalog(payload["capabilities"]), schema_source(self.tools))
        budgets = [event for event in self.events if event["kind"] == "context_budget"
                   and event.get("purpose") == "model_input"]
        self.assertEqual(len(budgets), 1)
        self.assertEqual(budgets[0]["status"], "ok")
        self.assertLess(budgets[0]["estimated_input_tokens"], 24000)
        self.assertTrue(any(event["kind"] == "model_request" for event in self.events))
        self.assertTrue(any(event["kind"] == "model_response" and event.get("status") == "ok"
                            for event in self.events))
        TRACES.append({"case": "actual_llm_manager", "budget": budgets[0],
                       "provider_calls": len(provider.requests)})

    async def test_complex_grouped_search_and_delivery_keep_all_conditions(self):
        message = "深圳正式5份月薪12k至15k、实习5份日薪至少300；都不要外包，85分以上自动投递。"
        filters = complex_filters()
        groups = [{"label": "正式", "count": 5, "filters": condition("正式岗位")},
                  {"label": "实习", "count": 5, "filters": condition("实习岗位")}]
        output = draft(message, [step("search", "job_search_start", {
            "queries": ["合成算法工程师"], "locations": ["深圳"], "result_limit": 10,
            "filters": filters, "result_groups": groups}),
            step("apply", "mock_delivery_start", {"min_score": 85}, ["search"])])
        plan, model = await self.plan(message, output)
        by_tool = {item["tool"]: item for item in plan["steps"]}
        self.assertEqual(plan["status"], "ready")
        search = by_tool["job_search_start"]["arguments"]
        self.assertEqual(search["locations"], ["深圳"])
        self.assertEqual(search["result_limit"], 10)
        self.assertEqual([group["count"] for group in search["result_groups"]], [5, 5])
        from app.core.job_filters import validate_filter
        self.assertEqual(search["filters"], validate_filter(filters).model_dump(mode="json"))
        self.assertEqual(by_tool["mock_delivery_start"]["arguments"]["min_score"], 85)
        self.assertIn("search", by_tool["mock_delivery_start"]["depends_on"])
        self.assertTrue(all(item["estimated_input_tokens"] < item["input_token_limit"] for item in model.stats))

    async def test_followup_revision_fits_and_keeps_application_version_binding(self):
        message = "搜索结果不变，把后续自动投递改为80分以上。"
        self.context.plan_reader = lambda: {"id": "synthetic-plan", "revision": 7,
            "session_id": "default", "original": "搜索后85分以上自动投递", "status": "running",
            "resume_stamp": STAMP, "steps": [
                {**PlannedStep.model_validate(step("search", "job_search_start", {"queries": ["合成算法工程师"]})).model_dump(), "status": "completed", "effect": "external"},
                {**PlannedStep.model_validate(step("apply", "mock_delivery_start", {"min_score": 85}, ["search"])).model_dump(), "status": "pending", "effect": "external"}]}
        output = {"operation": "revise", "revision": {"updates": [{"step_id": "apply",
            "arguments": {"min_score": 80, "scope_mode": "update"}, "user_requested": True}]}}
        plan, model = await self.plan(message, output)
        self.assertEqual(plan["operation"], "revise")
        self.assertEqual(plan["revision"]["plan_id"], "synthetic-plan")
        self.assertEqual(plan["revision"]["expected_revision"], 7)
        self.assertEqual(plan["revision"]["updates"][0]["arguments"]["min_score"], 80)
        self.assertLess(model.stats[0]["estimated_input_tokens"], 24000)

    async def test_new_tool_is_present_in_actual_planner_input(self):
        synthetic = AgentToolSpec("synthetic_extra", "额外只读功能",
            {"type": "object", "properties": {"filters": filter_schema()}, "additionalProperties": False},
            "read", lambda args: None)
        self.tools._tools[synthetic.name] = synthetic
        message = "只查看额外功能，不搜索或投递"
        _, model = await self.plan(message, {"reply": "合成只读回复", "steps": []})
        catalog = json.loads(model.requests[0].user_message)["capabilities"]
        self.assertIn(synthetic.openai_schema(), expand_catalog(catalog))

    async def test_current_request_and_conditions_are_never_trimmed_to_fit(self):
        message = "请保留本轮条件，不要执行。"
        conditions = {"queries": ["合成算法工程师"], "locations": ["深圳"],
            "result_limit": 20, "filters": complex_filters()}
        _, model = await self.plan(message, {"reply": "合成确认", "steps": []}, conditions=conditions)
        payload = json.loads(model.requests[0].user_message)
        self.assertEqual(payload["message"], message)
        self.assertEqual(payload["conditions"], conditions)

    async def test_real_required_state_overflow_still_stops_before_model_response(self):
        # Fixing duplicated protocol must not silently drop genuine task state.
        message = "完整当前条件：" + "合成约束保持完整" * 12000
        self.context.owner = asyncio.current_task()
        model = BudgetedModel({"steps": [], "reply": "must not be reached"})
        with self.assertRaises(ContextBudgetExceeded):
            await plan_request(model, self.tools, message, dialogue={},
                conditions=self.context.search_state, stamp=STAMP)
        self.assertEqual(model.requests, [])

    async def test_near_limit_required_session_projection_has_room_for_planning(self):
        budget = ContextBuilder().limits.session_tokens
        baseline = estimate_tokens(compile_context({}, self.context.search_state))
        original = "约" * ((budget - baseline - 300) // 2)
        dialogue = compile_context({"task_request": {"original": original}}, self.context.search_state)
        self.assertGreater(estimate_tokens(dialogue), budget - 1000)
        self.assertLessEqual(estimate_tokens(dialogue), budget)
        _, model = await self.plan("查看当前条件，不执行外部操作", {"reply": "合成条件回顾", "steps": []}, dialogue=dialogue)
        payload = json.loads(model.requests[0].user_message)
        self.assertEqual(payload["dialogue"]["task_request"]["original"], original)
        self.assertLess(model.stats[0]["estimated_input_tokens"], model.stats[0]["input_token_limit"])
        TRACES.append({"case": "near_limit_session", "session_estimated_tokens": estimate_tokens(dialogue),
                       "budget": model.stats[0]})


def main():
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(__import__(__name__, fromlist=["*"])))
    output = Path("evaluation/results/planning-budget") / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    summary = {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
        "external_model_calls": 0, "browser_runs": 0, "submissions": 0}
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (output / "traces.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in TRACES), encoding="utf-8")
    (output / "failures.json").write_text(json.dumps([{"test": str(test), "detail": detail}
        for test, detail in result.failures + result.errors], ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), **summary}))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
