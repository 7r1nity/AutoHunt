"""Typed salary predicates need no redundant natural-language label.

Offline schema/runtime/plan contracts only. No external model or recruitment
operation is invoked. Run: python -m evaluation.typed_filter_contract
"""
from __future__ import annotations

import asyncio
import copy
import json
import unittest
from datetime import UTC, datetime
from pathlib import Path

from app.core.agent_tools import AgentToolRegistry
from app.core.job_filters import (FilterExpression, MAX_FILTER_CHILDREN, MAX_FILTER_DEPTH,
    combine, evaluate_filter, filter_schema, filter_summary, partial_filter_status, validate_filter)
from app.core.salary_conditions import SalaryPredicate, judge_salary, salary_summary
from app.runtime.context import RunContext, current_run
from app.workflow.planning import plan_request
from evaluation.planning_budget_contract import BudgetedModel, STAMP, draft, registry, step


TRACES = []
PREDICATES = [
    {"unit": "monthly", "minimum": 12000},
    {"unit": "monthly", "maximum": 15000},
    {"unit": "monthly", "minimum": 12000, "maximum": 15000, "range_mode": "contained"},
    {"unit": "monthly", "minimum": 12000, "maximum": 15000, "range_mode": "overlap"},
    {"unit": "daily", "minimum": 300, "applies_to": "internship"},
    {"unit": "daily", "minimum": 300, "maximum": 800},
    {"unit": "negotiable"},
]


def typed(predicate, **fields):
    return {"op": "condition", "salary": copy.deepcopy(predicate), **fields}


def schema_accepts(value):
    try:
        AgentToolRegistry.validate_arguments(filter_schema(), value)
        return True
    except ValueError:
        return False


def runtime_accepts(value):
    try:
        validate_filter(value)
        return True
    except ValueError:
        return False


class TypedSalaryContracts(unittest.TestCase):
    def test_typed_leaf_omitted_empty_or_blank_description_agrees_with_schema(self):
        for predicate in PREDICATES:
            for fields in ({}, {"text": ""}, {"text": " \n\t "}):
                with self.subTest(predicate=predicate, fields=fields):
                    value = typed(predicate, **fields)
                    self.assertTrue(schema_accepts(value))
                    leaf = validate_filter(value)
                    self.assertEqual(leaf.text, salary_summary(SalaryPredicate.model_validate(predicate)))
                    self.assertEqual(leaf.salary, SalaryPredicate.model_validate(predicate))

    def test_existing_text_and_quote_are_preserved_exactly(self):
        for text in ("工资12k-15k", "  用户原文保留空白  "):
            value = typed(PREDICATES[2], text=text, quote="原文：工资12k-15k，不要面议")
            self.assertTrue(schema_accepts(value))
            leaf = validate_filter(value)
            self.assertEqual(leaf.text, text)
            self.assertEqual(leaf.quote, value["quote"])

    def test_label_generation_does_not_mutate_input_or_invent_fields(self):
        for predicate in PREDICATES:
            with self.subTest(predicate=predicate):
                value = typed(predicate, quote="只保留来源引用")
                before = copy.deepcopy(value)
                leaf = validate_filter(value)
                self.assertEqual(value, before)
                self.assertEqual(leaf.quote, before["quote"])
                self.assertEqual(leaf.salary.model_dump(), SalaryPredicate.model_validate(predicate).model_dump())

    def test_salary_display_uses_the_same_typed_formatter(self):
        expected = ["月薪区间下限≥12,000元", "月薪区间上限≤15,000元",
            "月薪区间下限≥12,000元、上限≤15,000元",
            "月薪区间与要求有交集（下限≥12,000元、上限≤15,000元）",
            "仅限制实习岗位：日薪区间下限≥300元",
            "日薪区间下限≥300元、上限≤800元", "薪资面议"]
        for predicate, text in zip(PREDICATES, expected):
            with self.subTest(predicate=predicate):
                self.assertEqual(salary_summary(predicate), text)
                self.assertEqual(filter_summary(typed(predicate)), text)
                self.assertEqual(filter_summary(typed(predicate, text="另一段原始说明")), text)

    def test_semantic_leaf_still_requires_nonempty_text(self):
        for value in ({"op": "condition"}, {"op": "condition", "text": ""},
                      {"op": "condition", "text": " \n "},
                      {"op": "condition", "salary": None},
                      {"op": "condition", "salary": None, "text": "\t"}):
            with self.subTest(value=value):
                self.assertFalse(schema_accepts(value))
                self.assertFalse(runtime_accepts(value))
        semantic = {"op": "condition", "text": "非外包且可远程"}
        self.assertTrue(schema_accepts(semantic))
        self.assertEqual(validate_filter(semantic).text, semantic["text"])

    def test_invalid_typed_salary_shape_stays_rejected_by_schema_and_runtime(self):
        for predicate in ({}, {"unit": "monthly"}, {"unit": "daily", "minimum": None},
                          {"unit": "yearly", "minimum": 12000},
                          {"unit": "monthly", "minimum": -1},
                          {"unit": "monthly", "minimum": 10000001},
                          {"unit": "negotiable", "minimum": 12000}):
            with self.subTest(predicate=predicate):
                self.assertFalse(schema_accepts(typed(predicate)))
                self.assertFalse(runtime_accepts(typed(predicate)))

    def test_cross_field_interval_order_is_still_checked_at_runtime(self):
        # Plain JSON Schema cannot compare two sibling numeric values; runtime
        # validates this pre-existing cross-field invariant independently.
        predicate = {"unit": "monthly", "minimum": 15000, "maximum": 12000}
        with self.assertRaises(ValueError):
            SalaryPredicate.model_validate(predicate)
        with self.assertRaises(ValueError):
            validate_filter(typed(predicate))
        with self.assertRaises(ValueError):
            salary_summary(predicate)

    def test_and_or_not_compositions_keep_the_typed_predicates(self):
        monthly, internship, negotiable = typed(PREDICATES[2]), typed(PREDICATES[4]), typed(PREDICATES[6])
        for value in (
            {"op": "any", "children": [monthly, negotiable]},
            {"op": "all", "children": [monthly, {"op": "condition", "text": "正式岗位"}]},
            {"op": "not", "children": [negotiable]},
            {"op": "any", "children": [monthly, {"op": "all", "children": [
                {"op": "condition", "text": "实习岗位"}, internship]}]},
        ):
            with self.subTest(value=value):
                self.assertTrue(schema_accepts(value))
                result = validate_filter(value)
                self.assertEqual(result.op, value["op"])
                self.assertTrue(all(leaf.text.strip() for leaf in result.leaves()))
                self.assertEqual(sum(leaf.salary is not None for leaf in result.leaves()),
                                 json.dumps(value).count('"salary"'))

    def test_combination_rules_are_not_relaxed_by_typed_leaf_support(self):
        leaf = typed(PREDICATES[0])
        invalid = [
            {"op": "all", "children": []},
            {"op": "not", "children": [leaf, leaf]},
            {"op": "condition", "salary": PREDICATES[0], "children": [leaf]},
            {"op": "all", "children": [leaf], "salary": PREDICATES[0]},
            {"op": "all", "children": [leaf], "mode": "allow"},
        ]
        for value in invalid:
            with self.subTest(value=value):
                self.assertFalse(schema_accepts(value))
                self.assertFalse(runtime_accepts(value))

    def test_tree_depth_and_child_limits_are_unchanged(self):
        value = typed(PREDICATES[0])
        for _ in range(MAX_FILTER_DEPTH):
            value = {"op": "not", "children": [value]}
        self.assertFalse(runtime_accepts(value))
        too_many = {"op": "any", "children": [typed(PREDICATES[0])] * (MAX_FILTER_CHILDREN + 1)}
        self.assertFalse(schema_accepts(too_many))
        self.assertFalse(runtime_accepts(too_many))

    def test_monthly_range_boundaries_and_overlap_have_the_same_evaluation(self):
        contained = validate_filter(typed(PREDICATES[2])).salary
        overlap = validate_filter(typed(PREDICATES[3])).salary
        for salary, expected_contained, expected_overlap in (
            ("12-15K", "pass", "pass"), ("11-15K", "fail", "pass"),
            ("12-16K", "fail", "pass"), ("15-16K", "fail", "pass"),
            ("16-20K", "fail", "fail"), ("面议", "unknown", "unknown")):
            with self.subTest(salary=salary):
                self.assertEqual(judge_salary(contained, {"salary": salary})["status"], expected_contained)
                self.assertEqual(judge_salary(overlap, {"salary": salary})["status"], expected_overlap)

    def test_internship_daily_threshold_never_becomes_a_monthly_threshold(self):
        predicate = validate_filter(typed(PREDICATES[4])).salary
        for facts, expected in (
            ({"salary": "300-800元/天", "employment_kind": "internship"}, "pass"),
            ({"salary": "299-800元/天", "employment_kind": "internship"}, "fail"),
            ({"salary": "12-15K", "employment_kind": "internship"}, "unknown"),
            ({"salary": "面议", "employment_kind": "internship"}, "unknown"),
            ({"salary": "12-15K", "employment_kind": "regular"}, "pass"),
            ({"salary": "300元/天"}, "unknown")):
            with self.subTest(facts=facts):
                self.assertEqual(judge_salary(predicate, facts)["status"], expected)

    def test_or_and_not_arithmetic_preserve_unknown_status(self):
        expression = {"op": "any", "children": [typed(PREDICATES[2]), typed(PREDICATES[6])]}
        self.assertEqual(partial_filter_status(expression, {"salary": "面议"}), "pass")
        self.assertEqual(partial_filter_status(expression, {"salary": "12-15K"}), "pass")
        self.assertEqual(partial_filter_status(expression, {"salary": "16-20K"}), "fail")
        negative = {"op": "not", "children": [typed(PREDICATES[0])]}
        self.assertEqual(partial_filter_status(negative, {"salary": "未披露"}), "unknown")
        self.assertEqual(partial_filter_status(negative, {"salary": "10-11K"}), "pass")


class TypedSalaryPlanContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = []
        self.context = RunContext("synthetic-typed-filter", asyncio.current_task(),
            lambda kind, data: self.events.append({"kind": kind, **data}), lambda _: None,
            search_state={"queries": ["合成算法工程师"], "source": "mock"}, dialogue={})
        self.token = current_run.set(self.context)

    async def asyncTearDown(self):
        current_run.reset(self.token)
        TRACES.append({"test": self.id(), "events": self.events})

    async def test_salary_only_filter_is_evaluated_without_any_model_call(self):
        calls = []
        async def generate(request):
            calls.append(request)
            raise AssertionError("A fully typed monthly predicate needs no semantic call")
        result = await evaluate_filter(generate, typed(PREDICATES[2]), {"salary": "12-15K"})
        self.assertEqual(result["status"], "pass")
        self.assertEqual(calls, [])
        self.assertEqual(result["conditions"][0]["text"], salary_summary(PREDICATES[2]))
        self.assertEqual(result["conditions"][0]["evidence"], [{"field": "salary", "quote": "12-15K"}])

    async def test_original_missing_text_salary_plan_is_ready_without_filters_question(self):
        self.context.owner = asyncio.current_task()
        tools = registry()
        message = "帮我找工作，不要外包，base深圳，薪资12k-15k，符合条件的投递。"
        filters = {"op": "all", "children": [
            {"op": "not", "children": [{"op": "condition", "text": "外包岗位"}]},
            typed({"unit": "monthly", "minimum": 12000, "maximum": 15000, "range_mode": "contained"}),
        ]}
        output = draft(message, [step("search", "job_search_start", {
            "queries": ["合成算法工程师"], "locations": ["深圳"], "filters": filters}),
            step("summary", "job_results_read", {"summary": True}, ["search"]),
            step("delivery", "mock_delivery_start", {}, ["search"])])
        model = BudgetedModel(output)
        plan = await plan_request(model, tools, message, dialogue={},
            conditions=self.context.search_state, stamp=STAMP)
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["questions"], [])
        self.assertEqual([item["tool"] for item in plan["steps"]],
                         ["job_search_start", "job_results_read", "mock_delivery_start"])
        for item in plan["steps"]:
            self.assertNotIn("filters", item.get("missing_slots", []))
            self.assertFalse(item.get("unresolved_arguments"))
        normalized = plan["steps"][0]["arguments"]["filters"]
        salary = validate_filter(normalized).leaves()[-1]
        self.assertEqual(salary.salary.minimum, 12000)
        self.assertEqual(salary.salary.maximum, 15000)
        self.assertEqual(salary.text, salary_summary(salary.salary))
        self.assertLess(model.stats[0]["estimated_input_tokens"], 24000)
        self.assertFalse(any(event["kind"] == "plan_repair" for event in self.events))
        TRACES.append({"case": "original_missing_text_plan", "budget": model.stats[0],
                       "plan_status": plan["status"], "questions": plan["questions"]})


def main():
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(__import__(__name__, fromlist=["*"])))
    output = Path("evaluation/results/typed-filter") / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
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
