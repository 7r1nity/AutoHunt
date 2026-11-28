"""Explicit correction of an unexecuted predicate failure, never blind retry.

Offline generated plans, local scripted responses and no recruitment actions.
Run: python -m evaluation.predicate_revision_contract
"""
from __future__ import annotations

import asyncio
import copy
import json
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS

from app.core.job_filters import validate_filter
from app.llm.base import LLMResult
from app.runtime.context import RunContext, current_run
from app.workflow.contracts import PlannedStep
from app.workflow.lifecycle import input_waits
from app.workflow.planning import plan_request
from app.workflow.recovery import recover_plan
from app.workflow.revision import RevisionDraft, edited_plan
from evaluation.planning_budget_contract import BudgetedModel, STAMP, registry


TRACES = []
MESSAGE = "把失败搜索的条件修正为月薪12k至15k，保留其他任务。"


def valid_filter():
    return {"op": "condition", "salary": {"unit": "monthly", "minimum": 12000, "maximum": 15000}}


def node(ident, tool, arguments=None, **fields):
    value = PlannedStep(id=ident, tool=tool, arguments=arguments or {}, user_requested=True).model_dump()
    return {**value, "effect": "read" if tool == "job_stats_read" else "external", "status": "pending", **fields}


def sample_plan():
    return {"id": "synthetic-predicate-plan", "revision": 3, "original": "找合成算法岗位，月薪12k至15k，另一个搜索继续。",
        "session_id": "default", "resume_stamp": STAMP, "expires_at": time.time() + 600,
        "answers": [], "questions": [], "status": "running", "paused": False,
        "goals": [{"quote": "合成独立任务", "disposition": "steps", "step_ids": ["done", "broken", "other"]}],
        "steps": [
            node("done", "job_stats_read", status="completed", result={"count": 7},
                 execution_id="completed-receipt", execution_phase="committed"),
            node("broken", "job_search_start", {"queries": ["合成算法工程师"], "filters": None},
                 status="blocked", failure={"code": "predicate_repair_exhausted", "effect_started": False, "uncertain": False},
                 unresolved_arguments={"filters": {"op": "all", "children": []}},
                 technical_issues=[{"field": "filters", "path": ["filters"], "code": "value_error", "reason": "合成缺陷"}],
                 technical_questions=["系统尚未完成筛选条件结构整理，相关分支尚未执行。"],
                 detail="合成技术失败"),
            node("other", "job_search_start", {"queries": ["合成数据工程师"]},
                 status="waiting_external", task_id="other-search-receipt"),
        ]}


def change(plan, arguments, **fields):
    return RevisionDraft(plan_id=plan["id"], expected_revision=plan["revision"],
        updates=[{"step_id": "broken", "arguments": arguments, **fields}])


class PredicateRevisionContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tools = registry()
        self.events = []
        self.context = RunContext("synthetic-predicate-revision", asyncio.current_task(),
            lambda kind, data: self.events.append({"kind": kind, **data}), lambda _: None,
            search_state={"queries": ["合成算法工程师"], "source": "mock"}, dialogue={})
        self.token = current_run.set(self.context)

    async def asyncTearDown(self):
        current_run.reset(self.token)
        TRACES.append({"test": self.id(), "events": self.events})

    def edit(self, plan, arguments, **fields):
        self.context.owner = asyncio.current_task()
        return edited_plan(plan, change(plan, arguments, **fields), self.tools,
            stamp=STAMP, session_id="default", message=MESSAGE)

    @staticmethod
    def broken(plan):
        return next(item for item in plan["steps"] if item["id"] == "broken")

    async def test_valid_explicit_correction_reopens_only_unexecuted_failed_node(self):
        plan = sample_plan()
        before = copy.deepcopy(plan)
        updated = self.edit(plan, {"filters": valid_filter()})
        corrected = self.broken(updated)
        self.assertEqual(corrected["status"], "pending")
        self.assertEqual(corrected["arguments"]["filters"], validate_filter(valid_filter()).model_dump(mode="json"))
        self.assertFalse(corrected.get("unresolved_arguments"))
        self.assertFalse(corrected.get("technical_issues"))
        self.assertNotIn("failure", corrected)
        self.assertNotIn("detail", corrected)
        self.assertEqual(corrected["attempt_history"][-1]["failure"]["code"], "predicate_repair_exhausted")
        self.assertEqual(updated["revision"], plan["revision"] + 1)
        self.assertEqual(plan, before)

    async def test_other_parameter_or_request_flag_does_not_count_as_condition_correction(self):
        for arguments, fields in (({}, {}), ({"locations": ["深圳"]}, {}), ({}, {"user_requested": True})):
            with self.subTest(arguments=arguments, fields=fields), self.assertRaises(ValueError):
                self.edit(sample_plan(), arguments, **fields)

    async def test_every_quarantined_field_requires_an_explicit_replacement(self):
        plan = sample_plan()
        broken = self.broken(plan)
        broken["arguments"]["result_groups"] = None
        broken["unresolved_arguments"]["result_groups"] = [{"label": "无效", "count": 0, "filters": {}}]
        broken["technical_issues"].append({"field": "result_groups", "code": "value_error"})
        with self.assertRaises(ValueError):
            self.edit(plan, {"filters": valid_filter()})
        groups = [{"label": "五险一金", "count": 5, "filters": {"op": "condition", "text": "提供五险一金"}}]
        updated = self.edit(plan, {"filters": valid_filter(), "result_groups": groups})
        self.assertEqual(self.broken(updated)["status"], "pending")
        self.assertEqual(self.broken(updated)["arguments"]["result_groups"][0]["count"], 5)

    async def test_invalid_new_tree_or_reversed_bounds_never_reopens_node(self):
        for filters in ({"op": "all", "children": []},
                        {"op": "condition", "salary": {"unit": "monthly", "minimum": 15000, "maximum": 12000}}):
            plan = sample_plan()
            before = copy.deepcopy(plan)
            with self.subTest(filters=filters), self.assertRaises(ValueError):
                self.edit(plan, {"filters": filters})
            self.assertEqual(plan, before)

    async def test_invalid_group_mode_is_rejected_by_runtime_group_validation(self):
        plan = sample_plan()
        broken = self.broken(plan)
        broken["arguments"] = {"queries": ["合成算法工程师"], "result_groups": None}
        broken["unresolved_arguments"] = {"result_groups": []}
        broken["technical_issues"] = [{"field": "result_groups", "code": "value_error"}]
        with self.assertRaises(ValueError):
            self.edit(plan, {"result_groups": [{"label": "餐补", "count": 5,
                "filters": {"op": "condition", "text": "提供餐补", "mode": "allow"}}]})

    async def test_null_or_empty_group_cannot_clear_failed_scope_as_format_repair(self):
        with self.assertRaises(ValueError):
            self.edit(sample_plan(), {"filters": None})
        for clear in (None, []):
            plan = sample_plan()
            broken = self.broken(plan)
            broken["arguments"] = {"queries": ["合成算法工程师"], "result_groups": None}
            broken["unresolved_arguments"] = {"result_groups": []}
            broken["technical_issues"] = [{"field": "result_groups", "code": "value_error"}]
            with self.subTest(clear=clear), self.assertRaises(ValueError):
                self.edit(plan, {"result_groups": clear})

    async def test_other_blocked_failures_are_not_made_editable(self):
        for code in ("authority_revoked", "invalid_arguments", "permission_denied", "worker_lost"):
            plan = sample_plan()
            self.broken(plan)["failure"]["code"] = code
            with self.subTest(code=code), self.assertRaises(ValueError):
                self.edit(plan, {"filters": valid_filter()})

    async def test_uncertain_started_or_missing_no_effect_evidence_stays_blocked(self):
        for failure in (
            {"code": "predicate_repair_exhausted", "effect_started": True, "uncertain": False},
            {"code": "predicate_repair_exhausted", "effect_started": False, "uncertain": True},
            {"code": "predicate_repair_exhausted", "uncertain": False},
            {"code": "predicate_repair_exhausted", "effect_started": False},
        ):
            plan = sample_plan()
            self.broken(plan)["failure"] = failure
            with self.subTest(failure=failure), self.assertRaises(ValueError):
                self.edit(plan, {"filters": valid_filter()})

    async def test_contradictory_effect_phase_and_withdrawn_node_stay_blocked(self):
        for fields in ({"execution_phase": "started"}, {"execution_phase": "committed"}, {"withdrawn": True}):
            plan = sample_plan()
            self.broken(plan).update(fields)
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.edit(plan, {"filters": valid_filter()})

    async def test_completed_receipt_and_other_active_search_are_not_replayed_or_replaced(self):
        plan = sample_plan()
        updated = self.edit(plan, {"filters": valid_filter()})
        for ident in ("done", "other"):
            self.assertEqual(next(item for item in updated["steps"] if item["id"] == ident),
                             next(item for item in plan["steps"] if item["id"] == ident))
        blocked_done = copy.deepcopy(plan)
        self.broken(blocked_done)["status"] = "completed"
        with self.assertRaises(ValueError):
            self.edit(blocked_done, {"filters": valid_filter()})

    async def test_real_business_question_survives_technical_diagnostic_cleanup(self):
        plan = sample_plan()
        broken = self.broken(plan)
        question = "你希望在哪座城市找工作？"
        broken["questions"] = [question, "请补充筛选条件。", *broken["technical_questions"]]
        broken["missing_slots"] = ["locations", "filters"]
        plan["questions"] = list(broken["questions"])
        updated = self.edit(plan, {"filters": valid_filter()})
        self.assertEqual(self.broken(updated)["questions"], [question])
        self.assertEqual(self.broken(updated)["missing_slots"], ["locations"])
        self.assertEqual(updated["questions"], [question])
        input_waits(updated)
        self.assertEqual(self.broken(updated)["status"], "waiting_input")

    async def test_existing_unrequested_action_does_not_gain_authority_from_correction(self):
        plan = sample_plan()
        self.broken(plan)["user_requested"] = False
        updated = self.edit(plan, {"filters": valid_filter()})
        self.assertFalse(self.broken(updated)["authority_binding"]["user_requested"])

    async def test_continue_only_recovers_other_work_and_keeps_bad_scope_blocked(self):
        plan = sample_plan()
        self.context.owner = asyncio.current_task()
        run = {"session_id": "default", "checkpoint": {"plan": plan}, "cancel_requested": False}
        decision = recover_plan(run, session_id="default", stamp=STAMP)
        self.assertEqual(decision.action, "resume")
        self.assertEqual(self.broken(decision.plan)["status"], "blocked")
        self.assertNotIn("broken", decision.plan["recovery_pending"])

    async def test_repaired_revision_still_enters_scope_review_before_it_can_run(self):
        plan = sample_plan()
        self.context.owner = asyncio.current_task()
        self.context.plan_reader = lambda: plan
        output = {"operation": "revise", "revision": {"updates": [
            {"step_id": "broken", "arguments": {"filters": valid_filter()}, "user_requested": True}]}}

        class ReviewModel(BudgetedModel):
            async def generate(self, request):
                if request.context == "条件语义核对，不产生执行授权":
                    self.requests.append(request)
                    payload = json.loads(request.user_message)
                    return LLMResult(json.dumps({"scopes": [{"step_id": entry["step_id"],
                        "status": "clarify", "changes": {}, "reason": "合成范围待确认",
                        "question": "请确认本次修正的薪资范围。"} for entry in payload["scopes"]]}, ensure_ascii=False),
                        "scripted", "predicate-revision-review")
                return await super().generate(request)

        model = ReviewModel(output)
        revised = await plan_request(model, self.tools, MESSAGE, dialogue={},
            conditions=self.context.search_state, stamp=STAMP)
        self.assertEqual(revised["operation"], "revise")
        self.assertEqual(len(model.requests), 2)
        self.assertEqual(revised["revision"]["scope_review"]["status"], "reviewed")
        updated = edited_plan(plan, RevisionDraft.model_validate(revised["revision"]), self.tools,
            stamp=STAMP, session_id="default", message=MESSAGE)
        self.assertEqual(self.broken(updated)["status"], "waiting_input")
        self.assertIn("请确认本次修正的薪资范围", [question.rstrip("。") for question in self.broken(updated)["questions"]])


def main():
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(
        __import__(__name__, fromlist=["*"])))
    output = Path("evaluation/results/predicate-revision") / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
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
