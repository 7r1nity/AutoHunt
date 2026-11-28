"""Global retired-dimension ingress; synthetic models and isolated runtime only."""
from __future__ import annotations

import copy
import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS

from app.core.agent_tools import ToolLoopState
from app.security.action_grants import action_binding
from app.security.risk import broad_delivery_scope
from app.llm.base import LLMResult
from app.workflow.planning import RequestDraft, validate_draft, plan_request
from app.workflow.revision import RevisionDraft, edited_plan
from app.workflow.delivery_scope import materialize_delivery_scope
from app.workflow.execution import advance
from app.workflow.scope_review import review
from evaluation import delivery_round_contract as fixtures

TRACES = []


def leaf(text):
    return {"op": "condition", "text": text}


def salary(value=12000):
    return {"op": "condition", "salary": {"unit": "monthly", "minimum": value}}


def mixed():
    return {"op": "all", "children": [salary(), {"op": "not", "children": [leaf("外包岗位")]},
            {"op": "not", "children": [leaf("实习岗位")]}, leaf("工作经验至少3年")]}


def draft(tool, arguments):
    return RequestDraft.model_validate({"steps": [{"id": "work", "tool": tool,
        "arguments": arguments, "user_requested": True}],
        "goals": [{"quote": "执行当前请求", "disposition": "steps", "step_ids": ["work"]}]})


class PlanningIngress(unittest.IsolatedAsyncioTestCase):
    _callTestMethod = fixtures.RoundContracts._callTestMethod
    bind = fixtures.RoundContracts.bind
    snapshot = fixtures.RoundContracts.snapshot

    async def asyncSetUp(self):
        await fixtures.RoundContracts.asyncSetUp(self)

    async def asyncTearDown(self):
        TRACES.append({"test": self.id(), "events": self.events, "queued": self.queued})
        await fixtures.RoundContracts.asyncTearDown(self)

    def compile(self, value, message="执行当前请求", **options):
        return validate_draft(value, self.registry, message, dialogue=self.context.dialogue,
            conditions=options.pop("conditions", {}), stamp=self.stamp, **options)

    async def test_new_search_removes_all_retired_leaves_but_keeps_salary_and_total(self):
        groups = [{"label": "正式岗位", "count": 5, "filters": leaf("正式岗位")},
                  {"label": "实习岗位", "count": 5, "filters": leaf("实习岗位")}]
        plan = self.compile(draft("job_search_start", {"queries": ["实习Python工程师"],
            "locations": ["深圳"], "filters": mixed(), "result_groups": groups}), "找正式5个实习5个，深圳月薪12k以上，不要外包")
        args = plan["steps"][0]["arguments"]
        self.assertEqual(args["queries"], ["Python工程师"])
        self.assertEqual(args["locations"], ["深圳"])
        self.assertEqual(args["result_limit"], 10)
        self.assertIsNone(args["result_groups"])
        self.assertEqual(args["filters"]["salary"]["minimum"], 12000)
        self.assertNotIn("外包", json.dumps(args["filters"], ensure_ascii=False))
        self.assertEqual(plan["status"], "ready")

    async def test_mixed_groups_preserve_city_quota_and_original_total(self):
        groups = [{"label": "深圳正式", "count": 5, "filters": {"op": "all", "children": [
            leaf("工作地点在深圳"), leaf("正式岗位")]}},
            {"label": "实习", "count": 5, "filters": leaf("实习岗位")}]
        plan = self.compile(draft("job_search_start", {"queries": ["Python工程师"], "result_groups": groups}))
        args = plan["steps"][0]["arguments"]
        self.assertEqual(args["result_limit"], 10)
        self.assertEqual(sum(group["count"] for group in args["result_groups"]), 10)
        self.assertEqual(args["result_groups"][0]["filters"]["text"], "工作地点在深圳")
        self.assertIsNone(args["result_groups"][1]["filters"])

    async def test_revision_projects_unexecuted_old_arguments_and_rebinds(self):
        base = self.compile(draft("search_state_update", {"queries": ["Python工程师"]}))
        base["steps"][0]["arguments"].update(filters=mixed(), result_groups=[
            {"label": "实习", "count": 5, "filters": leaf("实习岗位")}])
        before = copy.deepcopy(base)
        changed = edited_plan(base, RevisionDraft(plan_id=base["id"], expected_revision=base["revision"],
            updates=[{"step_id": "work", "arguments": {"locations": ["深圳"]}}]), self.registry,
            stamp=self.stamp, session_id="default", message="改成深圳")
        node = changed["steps"][0]
        self.assertEqual(node["arguments"]["locations"], ["深圳"])
        self.assertEqual(node["arguments"]["filters"]["salary"]["minimum"], 12000)
        self.assertEqual(node["arguments"]["result_limit"], 5)
        self.assertIsNone(node["arguments"]["result_groups"])
        self.assertEqual(node["authority_binding"], action_binding(node["tool"], node["arguments"],
            session_id="default", requested=True))
        self.assertEqual(base, before)

    async def test_revision_addition_projects_before_authority_binding(self):
        base = self.compile(draft("search_state_update", {"locations": ["深圳"]}))
        changed = edited_plan(base, RevisionDraft(plan_id=base["id"], expected_revision=base["revision"],
            add_steps=[{"id": "extra", "tool": "search_state_update", "user_requested": True,
                        "arguments": {"filters": mixed()}}]), self.registry,
            stamp=self.stamp, session_id="default", message="继续按原薪资条件，不要实习")
        node = next(s for s in changed["steps"] if s["id"] == "extra")
        self.assertEqual(node["arguments"]["filters"]["salary"]["minimum"], 12000)
        self.assertEqual(node["authority_binding"], action_binding(node["tool"], node["arguments"],
            session_id="default", requested=True))

    async def test_old_remembered_delivery_scope_cannot_restore_retired_filters(self):
        previous = {"id": "old", "session_id": "default", "resume_stamp": self.stamp,
                    "task_id": "new-round", "filters": mixed(), "min_score": 85}
        before = copy.deepcopy(previous)
        args = materialize_delivery_scope({"limit": 20}, previous,
            session_id="default", stamp=self.stamp, task_id="new-round")
        self.assertEqual(args["filters"]["salary"]["minimum"], 12000)
        self.assertEqual(args["min_score"], 85)
        self.assertEqual(args["limit"], 20)
        self.assertEqual(args["source_task_id"], "new-round")
        self.assertEqual(previous, before)

    async def test_retired_only_delivery_selector_is_not_an_identity_bound(self):
        args = self.registry._effective_arguments("mock_delivery_start", {
            "selector": "实习岗位", "source_task_id": "new-round", "limit": 20})
        self.assertNotIn("selector", args)
        self.assertEqual(args["limit"], 20)
        self.assertFalse(broad_delivery_scope("mock_delivery_start", args))
        unbounded = self.registry._effective_arguments("mock_delivery_start", {"selector": "实习岗位"})
        self.assertTrue(broad_delivery_scope("mock_delivery_start", unbounded))

    async def test_stable_ordinal_and_complete_named_identity_are_preserved(self):
        self.snapshot(count=3)
        self.context.dialogue["snapshots"][-1]["items"][0]["title"] = "Java实习工程师"
        self.assertEqual(self.registry._effective_arguments("mock_delivery_start", {
            "selector": "第3个 实习岗位"})["selector"], "第3个")
        self.assertEqual(self.registry._effective_arguments("mock_delivery_start", {
            "selector": "Java实习工程师"})["selector"], "Java实习工程师")
        reference = {"ordinal": 3, "title": "实习岗位", "company": "合成企业"}
        self.assertEqual(self.registry._effective_arguments("mock_delivery_start", {"selector": reference})["selector"],
            {"ordinal": 3, "company": "合成企业"})

    async def test_observed_company_named_like_retired_category_keeps_identity(self):
        self.snapshot(count=3)
        self.context.dialogue["snapshots"][-1]["items"][0]["company"] = "外包公司"
        args = {"selector": "外包公司", "selection_mode": "single"}
        self.assertEqual(self.registry._effective_arguments("mock_delivery_start", args), args)
        self.assertFalse(self.registry._disabled_removal(args))

    async def test_raw_instruction_cannot_restore_retired_ordinal_qualifier(self):
        snapshot = self.snapshot(count=3)
        self.context.user_message = "投递第3个实习岗位"
        result = await self.registry._mock_delivery_start({"selector": "第3个", "snapshot_id": snapshot,
            "source_task_id": "new-round"})
        self.assertEqual(result["queued"], 1)
        self.assertEqual(self.queued[0]["job_ids"], [3])

    async def test_retired_only_remove_is_successful_noop_without_touching_store(self):
        decision = self.registry.authorize("job_remove_selection", {"selector": "实习岗位"})
        self.assertEqual(decision.decision.value, "ALLOW")
        result = await self.registry._job_remove_selection({"selector": "实习岗位"})
        self.assertEqual(result["removed_ids"], [])
        self.assertTrue(result["ignored_filter"])
        self.assertEqual(self.queued, [])

    async def test_retired_target_and_dependent_mutations_never_expand_to_board(self):
        result = await self.registry._job_target_resolve({"selector": "外包岗位", "scope": "board"})
        self.assertEqual(result["selected_ids"], [])
        self.assertTrue(result["ignored_filter"])
        self.context.plan_reader = lambda: {"id": "plan", "steps": [{"id": "target", "status": "completed", "result": result}]}
        for field in ("target_step_id", "exclude_target_step_id"):
            removed = await self.registry._job_remove_selection({field: "target", "selector": "其余岗位"})
            self.assertEqual(removed["removed_ids"], [])
        delivery = await self.registry._mock_delivery_start({"target_step_id": "target", "limit": 20})
        self.assertEqual(delivery["queued"], 0)
        self.assertEqual(delivery["shortfall"], 20)
        self.assertEqual(self.queued, [])

    async def test_retired_only_scope_needs_no_review_model_call(self):
        async def unavailable(_):
            raise AssertionError("Retired-only scope must not call review")
        value = draft("mock_delivery_start", {"filters": leaf("正式岗位")})
        result, metadata, waits = await review(NS(generate=unavailable), value, message="只投正式岗位",
            live_plan=None, conditions={})
        self.assertIsNone(metadata)
        self.assertEqual(waits, {})

    async def test_reviewer_cannot_reintroduce_retired_condition_or_clear_salary(self):
        async def generate(_):
            return LLMResult(json.dumps({"scopes": [{"step_id": "work", "status": "correct",
                "changes": {"filters": leaf("外包岗位")}, "reason": "合成错误修正"}]}), "scripted", "policy-review")
        value = draft("mock_delivery_start", {"filters": salary()})
        result, metadata, waits = await review(NS(generate=generate), value,
            message="月薪12k以上，不要外包", live_plan=None, conditions={})
        self.assertEqual(result.steps[0].arguments["filters"]["salary"]["minimum"], 12000)
        self.assertEqual(waits, {})

    async def test_reviewer_retired_only_question_cannot_hold_valid_salary_scope(self):
        async def generate(_):
            return LLMResult(json.dumps({"scopes": [{"step_id": "work", "status": "clarify",
                "changes": {}, "reason": "请确认是否外包岗位？", "question": "请确认是否外包岗位？"}]}),
                "scripted", "policy-review")
        result, metadata, waits = await review(NS(generate=generate), draft("mock_delivery_start", {"filters": salary()}),
            message="月薪12k以上，不要外包", live_plan=None, conditions={})
        self.assertEqual(waits, {})
        self.assertEqual(result.steps[0].arguments["filters"]["salary"]["minimum"], 12000)

    async def test_tool_execute_projects_before_action_binding_check(self):
        raw = {"filters": mixed()}
        plan = self.compile(draft("search_state_update", raw))
        step = plan["steps"][0]
        self.bind(plan, step)
        captured = []
        async def executor(arguments):
            captured.append(arguments)
            return {"message": "合成状态保存"}
        self.registry._tools["search_state_update"] = replace(self.registry._tools["search_state_update"], executor=executor)
        result = await self.registry.execute("search_state_update", raw, user_message="保存条件", state=ToolLoopState())
        self.assertEqual(result.status, "ok")
        self.assertEqual(captured[0]["filters"]["salary"]["minimum"], 12000)
        self.assertNotIn("外包", json.dumps(captured, ensure_ascii=False))

    async def test_legacy_unlimited_pending_delivery_is_projected_before_authorize(self):
        plan = self.compile(draft("mock_delivery_start", {
            "source_task_id": "new-round", "min_score": 85, "filters": salary()}))
        step = plan["steps"][0]
        # Simulate a durable pre-policy pending step, without a limit. The
        # executor must project and persist the exact effective binding before
        # the real registry authorization and final queue receipt are checked.
        raw = {"source_task_id": "new-round", "min_score": 85, "filters": mixed()}
        step["arguments"] = copy.deepcopy(raw)
        step["authority_binding"] = action_binding("mock_delivery_start", raw,
            session_id="default", requested=True)
        self.context.dialogue["tasks"] = {"plan_id": plan["id"]}
        self.context.user_message = "按本轮结果投递85分以上的职位"
        observed = await advance(plan, self.registry)
        self.assertEqual(step["status"], "completed", step.get("detail"))
        self.assertEqual(len(self.queued), 1)
        self.assertEqual(self.queued[0]["task_id"], "new-round")
        self.assertEqual(self.queued[0]["min_score"], 85)
        self.assertEqual(self.queued[0]["filters"]["salary"]["minimum"], 12000)
        self.assertEqual(step["authority_binding"], action_binding("mock_delivery_start", step["arguments"],
            session_id="default", requested=True))
        self.assertNotIn("limit", step["arguments"])
        self.assertNotIn("外包", json.dumps(step["arguments"], ensure_ascii=False))
        self.assertEqual(observed[-1]["id"], step["id"])
        stored = self.store.get(self.run["id"])["checkpoint"]["plan"]["steps"][0]
        self.assertEqual(stored["arguments"], step["arguments"])
        self.assertEqual(stored["authority_binding"], step["authority_binding"])

    async def test_nested_action_proposal_saves_only_effective_arguments(self):
        await self.registry._action_propose({"tool": "job_search_start", "description": "合成搜索提案",
            "arguments": {"queries": ["实习Python工程师"], "filters": leaf("外包岗位")}})
        pending = self.context.dialogue["pending"][-1]
        self.assertEqual(pending["arguments"]["queries"], ["Python工程师"])
        self.assertIsNone(pending["arguments"]["filters"])
        self.assertEqual(self.queued, [])

    async def test_direct_bounded_delivery_cannot_restore_retired_selector_or_filter(self):
        self.snapshot(count=25)
        self.context.user_message = "投递20个"
        result = await self.registry._mock_delivery_start({"selector": "实习岗位", "limit": 20,
            "source_task_id": "new-round", "filters": leaf("外包岗位"), "min_score": 85})
        self.assertEqual(result["queued"], 20)
        self.assertEqual(self.queued[0]["limit"], 20)
        self.assertEqual(self.queued[0]["task_id"], "new-round")
        self.assertEqual(self.queued[0]["min_score"], 85)
        self.assertNotIn("filters", self.queued[0])

    async def test_old_mixed_state_reaches_model_and_fresh_valid_scope_can_repair(self):
        model = fixtures.action_fixtures.Model({"steps": [{"id": "work", "tool": "job_search_start",
            "arguments": {"queries": ["Python工程师"], "locations": ["深圳"], "filters": salary(15000)},
            "user_requested": True}]})
        old = {"queries": ["Python工程师"], "filters": leaf("如果是外包则月薪15k，否则要求双休")}
        plan = await plan_request(model, self.registry, "按深圳、月薪15k以上重新搜索",
            dialogue=self.context.dialogue, conditions=old, stamp=self.stamp)
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["steps"][0]["arguments"]["filters"]["salary"]["minimum"], 15000)


def main():
    suite = unittest.TestSuite(PlanningIngress(name) for name in PlanningIngress.__dict__ if name.startswith("test_"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    output = Path("evaluation/results/global-filter-planning") / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    summary = {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
               "external_model_calls": 0, "real_recruitment_actions": 0}
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (output / "traces.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in TRACES), encoding="utf-8")
    print(json.dumps({"output": str(output), **summary}))
    return int(not result.wasSuccessful())


if __name__ == "__main__":
    raise SystemExit(main())
