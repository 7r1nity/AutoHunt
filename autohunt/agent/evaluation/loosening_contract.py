"""Regression guard for permissive planning without semantic/execution corruption.

Synthetic plans, scripted responses and temporary SQLite only. No network,
personal data, production browser, job search or application submission.
"""
from __future__ import annotations

import asyncio
import copy
import io
import hashlib
import importlib
import itertools
import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from app.core.job_filters import FilterExpression, combine, validate_filter, MAX_FILTER_DEPTH
from app.core.result_groups import normalize_groups
from app.runtime.manager import _approval_detail
from app.security.decisions import ActionPolicy, Decision
from app.security.risk import AlwaysConfirm, grade_action, SecurityRisk
from app.workflow.graph import deduplicate_mutations, depend_on
from app.workflow.lifecycle import input_waits
from app.workflow.planning import RequestDraft, validate_draft
from app.workflow.planning import _draft_with_repair
from app.llm.base import LLMRequest, LLMResult
from app.workflow.revision import RevisionDraft, edited_plan
from app.workflow.validation import PlanValidationError, preflight
from .approval_contract import DurableApprovalContracts
from .goal_graph_contract import Contracts, STAMP, MESSAGE, draft, step
from .salary_revision_contract import task_plan
from . import approval_contract, goal_graph_contract

def leaf(text="合成类别", mode="require"):
    return {"op": "condition", "text": text, "mode": mode}


def raw_truth(tree, values):
    if tree["op"] == "condition":
        value = next(values)
        return value if tree.get("mode", "require") == "require" else None
    outcomes = [raw_truth(child, values) for child in tree["children"]]
    outcomes = [value for value in outcomes if value is not None]
    if not outcomes:
        return None
    if tree["op"] == "not":
        return {"pass": "fail", "fail": "pass", "unknown": "unknown"}[outcomes[0]]
    if tree["op"] == "all":
        return "fail" if "fail" in outcomes else "unknown" if "unknown" in outcomes else "pass"
    return "pass" if "pass" in outcomes else "unknown" if "unknown" in outcomes else "fail"


class BooleanContracts(unittest.TestCase):
    def test_eight_negations_do_not_get_flattened(self):
        value = leaf()
        for _ in range(8):
            value = {"op": "not", "children": [value]}
        with self.assertRaises(ValueError):
            validate_filter(value)
        with self.assertRaises(ValueError):
            FilterExpression.model_validate(value)

    def test_redundant_associative_depth_is_accepted_losslessly(self):
        value = leaf()
        for _ in range(20):
            value = {"op": "all", "children": [value]}
        self.assertEqual(validate_filter(value).op, "condition")

    def test_not_many_children_is_ambiguous_not_an_implicit_and(self):
        with self.assertRaises(ValueError):
            validate_filter({"op": "not", "children": [leaf("A"), leaf("B")]})

    def test_node_budget_is_not_bypassed_or_trimmed(self):
        for mode in ("require", "prefer", "allow"):
            value = {"op": "all", "children": [{"op": "all", "children": [leaf(str(i), mode) for i in range(32)]}
                                                  for _ in range(9)]}
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                validate_filter(value)

    def test_group_soft_conditions_not_silently_upgraded(self):
        for mode in ("allow", "prefer"):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                normalize_groups([{"label": "允许实习", "count": 5, "filters": leaf("实习", mode)}])


# 162 independently named truth-table cases: all/any/NOT, unknown, mixed
# operators, quoted groups, redundant wrapping and ordered duplicate leaves.
for operator in ("all", "any"):
    for index, values in enumerate(itertools.product(("pass", "fail", "unknown"), repeat=4)):
        def check(self, operator=operator, values=values):
            inverse = "any" if operator == "all" else "all"
            tree = {"op": operator, "children": [
                {"op": operator, "children": [leaf("A"), {"op": "not", "children": [leaf("B")]}]},
                {"op": inverse, "quote": "保持这组", "children": [leaf("A"), leaf("C")]}]}
            raw = copy.deepcopy(tree)
            normalized = validate_filter(tree)
            self.assertEqual(tree, raw)
            self.assertEqual([item.text for item in normalized.leaves()], ["A", "B", "A", "C"])
            self.assertEqual(combine(normalized, iter(values)), raw_truth(raw, iter(values)))
            self.assertEqual(validate_filter(normalized).model_dump(), normalized.model_dump())
        setattr(BooleanContracts, f"test_truth_{operator}_{index:03}", check)


class PlanningContracts(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = Contracts.asyncSetUp
    asyncTearDown = Contracts.asyncTearDown
    owned = Contracts.owned
    validate = Contracts.validate

    async def test_stats_follows_search_not_the_reverse(self):
        value = self.validate(draft([step("search", "job_search_start", deps=["grow"]),
                                    step("grow", "job_stats_read")], ["grow"]))
        by_id = {item["id"]: item for item in value["steps"]}
        self.assertNotIn("grow", by_id["search"]["depends_on"])
        self.assertIn("search", by_id["grow"]["depends_on"])
        self.assertEqual(by_id["grow"]["evidence_step"], "search")
        preflight(value["steps"], value["goals"], [MESSAGE], set(self.registry._tools))

    async def test_summary_follows_search(self):
        value = self.validate(draft([step("search", "job_search_start", deps=["report"]),
                                    step("report", "job_results_read", {"summary": True})], ["report"]))
        by_id = {item["id"]: item for item in value["steps"]}
        self.assertEqual(by_id["search"]["depends_on"], [])
        self.assertEqual(by_id["report"]["depends_on"], ["search"])

    async def test_indirect_reverse_needs_replanning_not_edge_deletion(self):
        nodes = [step("search", "job_search_start", deps=["read"]),
                 step("read", "context_state_read", deps=["grow"]), step("grow", "job_stats_read")]
        with self.assertRaises(PlanValidationError):
            depend_on(nodes, nodes[2], nodes[0])
        self.assertEqual(nodes[0]["depends_on"], ["read"])

    async def test_invalid_filter_waits_without_discarding_conditions(self):
        invalid = {"op": "all", "children": []}
        value = self.validate(draft([step("search", "job_search_start", {"filters": invalid}),
                                    step("stats", "context_state_read")], ["search", "stats"]))
        input_waits(value)
        by_id = {item["id"]: item for item in value["steps"]}
        self.assertEqual(by_id["search"]["status"], "waiting_input")
        self.assertEqual(by_id["search"]["unresolved_arguments"]["filters"], invalid)
        self.assertEqual(by_id["stats"]["status"], "pending")
        self.assertNotIn("filters", value["verified_conditions"])

    async def test_soft_quota_waits_only_search_branch(self):
        group = {"label": "实习", "count": 5, "filters": leaf("实习", "allow")}
        value = self.validate(draft([step("search", "job_search_start", {"result_groups": [group]}),
                                    step("read", "context_state_read")], ["search", "read"]))
        input_waits(value)
        self.assertEqual(value["steps"][0]["status"], "waiting_input")
        self.assertEqual(value["steps"][1]["status"], "pending")

    async def test_predicate_defect_gets_model_repair_before_user_question(self):
        payloads = [draft([step("search", "job_search_start", {"filters": {"op": "all", "children": []}})], ["search"]),
                    draft([step("search", "job_search_start", {"filters": leaf("不要外包")})], ["search"])]
        calls = []
        async def generate(request):
            calls.append(request)
            return LLMResult(json.dumps(payloads[min(len(calls)-1, 1)]), "scripted", "repair")
        from types import SimpleNamespace
        self.owned()
        plan = await _draft_with_repair(SimpleNamespace(generate=generate),
            LLMRequest(instructions="合成规划测试", user_message=json.dumps({"message": "合成请求"}), context=""),
            validator=lambda value: validate_draft(value, self.registry, MESSAGE,
                dialogue={}, conditions=self.context.search_state, stamp=STAMP))
        self.assertEqual(len(calls), 2)
        self.assertFalse(plan["questions"])
        self.assertEqual(plan["steps"][0]["arguments"]["filters"]["text"], "不要外包")

    async def test_exhausted_predicate_repair_returns_scoped_wait_not_denial(self):
        payload = draft([step("search", "job_search_start", {"filters": {"op": "all", "children": []}}),
                         step("read", "context_state_read")], ["search", "read"])
        calls = []
        async def generate(request):
            calls.append(request)
            return LLMResult(json.dumps(payload), "scripted", "repair")
        from types import SimpleNamespace
        self.owned()
        plan = await _draft_with_repair(SimpleNamespace(generate=generate),
            LLMRequest(instructions="合成规划测试", user_message=json.dumps({"message": "合成请求"}), context=""),
            validator=lambda value: validate_draft(value, self.registry, MESSAGE,
                dialogue={}, conditions=self.context.search_state, stamp=STAMP))
        input_waits(plan)
        self.assertEqual(len(calls), 4)
        self.assertEqual(plan["steps"][0]["status"], "waiting_input")
        self.assertEqual(plan["steps"][1]["status"], "pending")

    async def test_ambiguous_producer_does_not_select_first(self):
        value = self.validate(draft([step("s1", "job_search_start"), step("s2", "job_search_start"),
                                    step("grow", "job_stats_read")], ["grow"]))
        input_waits(value)
        grow = next(item for item in value["steps"] if item["id"] == "grow")
        self.assertEqual(grow["status"], "waiting_input")
        self.assertNotIn("evidence_step", grow)

    async def test_explicit_producer_kept_even_if_not_first(self):
        value = self.validate(draft([step("s1", "job_search_start"), step("s2", "job_search_start"),
                                    step("grow", "job_stats_read", deps=["s2"])], ["grow"]))
        self.assertEqual(next(item for item in value["steps"] if item["id"] == "grow")["evidence_step"], "s2")

    async def test_dedup_does_not_merge_across_a_read_barrier(self):
        nodes = [step("u1", "search_state_update", {"locations": ["深圳"]}, ["read"]),
                 step("read", "context_state_read", deps=["u2"]),
                 step("u2", "search_state_update", {"locations": ["深圳"]})]
        value = self.validate(draft(nodes, ["u1"]))
        self.assertEqual(len([item for item in value["steps"] if item["tool"] == "search_state_update"]), 2)
        preflight(value["steps"], value["goals"], [MESSAGE], set(self.registry._tools))

    async def test_safe_dedup_updates_goal_and_evidence_references(self):
        nodes = [step("a", "search_state_update", {"locations": ["深圳"]}),
                 step("b", "search_state_update", {"locations": ["深圳"]}),
                 step("read", "context_state_read", deps=["b"])]
        value = self.validate(draft(nodes, ["read", "b"]))
        self.assertNotIn("b", {item["id"] for item in value["steps"]})
        self.assertEqual(value["steps"][-1]["depends_on"], ["a"])
        self.assertNotIn("b", value["goals"][0]["step_ids"])

    async def test_resume_change_is_clarification_not_late_execution_failure(self):
        value = task_plan()
        edit = RevisionDraft(plan_id=value["id"], expected_revision=0, updates=[{"step_id": "inspection", "arguments": {"limit": 20}}])
        with self.assertRaises(PlanValidationError) as raised:
            edited_plan(value, edit, self.registry, stamp="changed", session_id="default", message="合成修改")
        self.assertEqual(raised.exception.issue.category, "clarify")

    async def test_unknown_invalidated_id_does_not_crash_or_claim_success(self):
        value = task_plan()
        result = edited_plan(value, RevisionDraft(plan_id=value["id"], expected_revision=0, invalidated_steps=["missing", "missing"]),
                             self.registry, stamp=STAMP, session_id="default", message="刷新合成结果")
        self.assertIn("missing", "".join(result["questions"]))
        self.assertEqual(result["revision_history"][-1]["invalidated_steps"], [])

    async def test_unknown_cancel_is_reported(self):
        value = task_plan()
        result = edited_plan(value, RevisionDraft(plan_id=value["id"], expected_revision=0, remove_steps=["missing"]),
                             self.registry, stamp=STAMP, session_id="default", message="取消指定步骤")
        self.assertIn("missing", "".join(result["questions"]))

    async def test_repeated_revision_patches_merge_without_losing_fields(self):
        value = task_plan()
        edit = RevisionDraft(plan_id=value["id"], expected_revision=0, updates=[
            {"step_id": "inspection", "arguments": {"limit": 20}},
            {"step_id": "inspection", "arguments": {"stage": "waiting"}}])
        result = edited_plan(value, edit, self.registry, stamp=STAMP, session_id="default", message="合成追加条件")
        args = next(item for item in result["steps"] if item["id"] == "inspection")["arguments"]
        self.assertEqual(args["limit"], 20)
        self.assertEqual(args["stage"], "waiting")

    async def test_conflicting_revision_does_not_choose_first_or_last(self):
        value = task_plan()
        edit = RevisionDraft(plan_id=value["id"], expected_revision=0, updates=[
            {"step_id": "inspection", "arguments": {"limit": 20}},
            {"step_id": "inspection", "arguments": {"limit": 30}}])
        with self.assertRaises(PlanValidationError) as raised:
            edited_plan(value, edit, self.registry, stamp=STAMP, session_id="default", message="合成条件冲突")
        self.assertEqual(raised.exception.issue.category, "clarify")

    async def test_completed_producer_receipt_cannot_be_rewritten(self):
        nodes = [dict(step("search", "job_search_start", deps=["grow"]), status="completed"),
                 step("grow", "job_stats_read")]
        with self.assertRaises(PlanValidationError):
            depend_on(nodes, nodes[1], nodes[0])
        self.assertEqual(nodes[0]["depends_on"], ["grow"])

    async def test_unscoped_legacy_questions_allow_only_safe_reads(self):
        nodes = [dict(step("write", "search_state_update"), effect="write", status="pending"),
                 dict(step("read", "context_state_read"), effect="read", status="pending")]
        value = {"steps": nodes, "questions": ["请确认新任务范围"]}
        input_waits(value)
        self.assertEqual(nodes[0]["status"], "waiting_input")
        self.assertEqual(nodes[1]["status"], "pending")


class ApprovalContracts(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = DurableApprovalContracts.asyncSetUp
    asyncTearDown = DurableApprovalContracts.asyncTearDown
    enter = DurableApprovalContracts.enter
    plan = DurableApprovalContracts.plan
    tick = DurableApprovalContracts.tick

    async def test_aged_approval_waits_and_reminds_once_then_can_be_confirmed(self):
        value = self.plan()
        await self.tick(value)
        watcher = asyncio.create_task(self.runtime.watch_plan(self.run["id"]))
        try:
            with patch("app.runtime.manager.wait_expired", return_value=True):
                for _ in range(200):
                    record = self.store.get(self.run["id"])
                    if any(item["kind"].startswith("approval_reminder:") for item in self.store.public_events(self.run["id"])):
                        break
                    await asyncio.sleep(0.005)
                self.assertEqual(record["checkpoint"]["plan"]["steps"][0]["status"], "waiting_approval")
                self.assertEqual(self.calls, [])
                await self.runtime.decide_approval(self.run["id"], value["steps"][0]["approval_id"], "default", True)
                await asyncio.wait_for(watcher, 4)
            self.assertEqual(len(self.calls), 1)
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    async def test_cancellation_checked_at_shared_tool_gateway(self):
        self.enter()
        self.store.update(self.run["id"], cancel_requested=True)
        self.assertEqual(self.registry.authorize("resume_propose_patch", {"instruction": "补充合成项目描述"}).code, "task_cancelled")
        self.assertEqual(self.registry.authorize("browser_task_stop", {}).decision, Decision.ALLOW)

    async def test_confirmation_card_creates_no_provider_or_background_task(self):
        value = self.plan()
        value["steps"][0]["arguments"]["filters"] = leaf("合成正式岗位")
        self.store.save_plan(self.run["id"], value)
        with patch("app.llm.deepseek_provider.DeepSeekProvider", side_effect=AssertionError("unexpected network provider")):
            await self.tick(value)
        self.assertEqual(value["steps"][0]["status"], "waiting_approval")
        self.assertIn("合成正式岗位", value["steps"][0]["detail"])
        self.assertFalse(self.runtime.tasks)

    async def test_configured_strict_policy_still_applies_to_local_mutations(self):
        self.assertEqual(ActionPolicy().evaluate("resume_propose_patch", {"instruction": "补充合成项目描述"}, "write").decision, Decision.ALLOW)
        self.assertEqual(ActionPolicy(AlwaysConfirm()).evaluate("resume_propose_patch", {"instruction": "补充合成项目描述"}, "write").decision, Decision.REQUIRE_APPROVAL)

    async def test_mock_trial_not_misrepresented_as_no_effect(self):
        self.assertEqual(grade_action("job_search_start", {"dry_run": True, "source": "mock"}, effect="external").risk, SecurityRisk.HIGH)
        self.assertNotIn("不会发送任何消息", _approval_detail("job_search_start", {"dry_run": True, "queries": ["合成岗位"]}))


def main():
    import sys
    output = Path(__file__).parent / "results" / "loosening" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    buffer = io.StringIO()
    # These modules import other test classes; load named classes to avoid counting
    # inherited/imported suites as independent cases.
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(cls)
        for cls in (BooleanContracts, PlanningContracts, ApprovalContracts))
    if "--regression" in sys.argv:
        for name in ("search_delivery_contract", "approval_contract", "goal_graph_contract",
                     "condition_refresh_contract", "command_recovery_contract"):
            suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(importlib.import_module("evaluation." + name)))
    result = unittest.TextTestRunner(stream=buffer, verbosity=2).run(suite)
    (output / "tests.log").write_text(buffer.getvalue(), encoding="utf-8")
    (output / "traces.jsonl").write_text("".join(json.dumps(trace, ensure_ascii=False, default=str) + "\n"
        for trace in [*goal_graph_contract.TRACES, *approval_contract.TRACES]), encoding="utf-8")
    (output / "failures.json").write_text(json.dumps([{"test": str(test), "traceback": error}
        for test, error in result.failures + result.errors], ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {"tests": result.testsRun, "failures": len(result.failures),
        "errors": len(result.errors), "real_model_calls": 0, "real_browser_actions": 0,
        "filter_runtime_depth": MAX_FILTER_DEPTH,
        "coverage": "offline contracts + parameterized Boolean truth tables, not real-model intent quality or production browser E2E",
        "source_sha256": {str(path.relative_to(Path(__file__).parents[1])): hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in (Path(__file__).parents[1] / "app").rglob("*.py")}}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if not result.wasSuccessful():
        print(buffer.getvalue()[-6000:])
    print(json.dumps({"output": str(output), **{key: value for key, value in summary.items() if key != "source_sha256"}}))
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
