"""Offline round/ordinal/authorization contracts; never sends recruitment data."""
from __future__ import annotations

import copy
import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS

from app.core.agent_tools import ToolLoopState
from app.core.job_targets import TargetClarification
from app.runtime.context import dialogue_change
from app.runtime.dialogue import resolve_reference
from app.workflow.execution import persist
from app.workflow.planning import RequestDraft, validate_draft
from app.workflow.result_sources import ResultSourceClarification
from evaluation import action_contract as action_fixtures


TRACES = []


class RoundContracts(unittest.IsolatedAsyncioTestCase):
    _callTestMethod = action_fixtures.Contracts._callTestMethod
    bind = action_fixtures.Contracts.bind

    async def asyncSetUp(self):
        await action_fixtures.Contracts.asyncSetUp(self)
        self.rows = {i: {"id": i, "title": f"合成工程师{i}", "company": "合成企业", "location": "深圳",
            "salary": "19-23K", "score": 90, "source_url": f"http://127.0.0.1:8080/job.html?id={i}"}
            for i in range(1, 26)}
        self.sources = {name: {"task_id": name, "status": "completed", "session_id": "default",
            "resume_id": "synthetic-resume"} for name in ("old-round", "new-round")}
        self.jobs = NS(search_source=lambda ident: copy.deepcopy(self.sources.get(ident)),
            get_job=lambda ident: copy.deepcopy(self.rows[ident]), review_fingerprint=lambda job: f"hash-{job['id']}")
        self.registry.jobs = self.runtime.jobs = self.jobs
        self.context.mutate_dialogue = lambda operation: self.store.mutate_dialogue("default", operation)
        self.queued = []
        async def queue(**kwargs):
            self.queued.append(copy.deepcopy(kwargs))
            count = min(len(kwargs.get("job_ids", self.rows)), kwargs.get("limit", 200))
            return {"queued": count, "reason": "queued" if count else "no_candidates", "message": "合成入队回执",
                    "requested_limit": kwargs.get("limit"), "shortfall": max(0, kwargs.get("limit", count) - count)}
        self.registry.mock_workflow = NS(queue_deliveries=queue, remember_authorization_scope=lambda *a, **kw: None)

    async def asyncTearDown(self):
        TRACES.append({"test": self.id(), "events": self.events, "queued": self.queued})
        await action_fixtures.Contracts.asyncTearDown(self)

    def snapshot(self, source="new-round", count=1):
        state = dialogue_change("snapshot", topic="applications", kind="job", source="search-results", version=source,
            displayed=True, items=[{**self.rows[i], "content_hash": f"hash-{i}"} for i in range(1, count + 1)])
        return state["snapshots"][-1]["id"]

    def old_scope(self):
        dialogue_change("delivery_scope", scope={"id": "old-batch", "session_id": "default",
            "resume_stamp": self.stamp, "task_id": "old-round", "min_score": 85,
            "filters": {"op": "condition", "text": "月薪12至15K", "salary": {
                "unit": "monthly", "minimum": 12000, "maximum": 15000}}})

    def plan(self, args, *, source="new-round", requested=True):
        payload = {"steps": [{"id": "search", "tool": "job_search_start",
            "arguments": {"queries": ["合成工程师"]}, "user_requested": True},
            {"id": "deliver", "tool": "mock_delivery_start", "arguments": args,
             "depends_on": ["search"], "user_requested": requested}],
            "goals": [{"quote": "按本轮结果投递", "disposition": "steps", "step_ids": ["search", "deliver"]}]}
        value = validate_draft(RequestDraft.model_validate(payload), self.registry,
            "按本轮结果最多投递20个", dialogue=self.context.dialogue, conditions=self.context.search_state, stamp=self.stamp)
        producer = next(step for step in value["steps"] if step["id"] == "search")
        producer.update(status="completed", task_id=source)
        delivery = next(step for step in value["steps"] if step["id"] == "deliver")
        self.bind(value, delivery)
        return value, delivery

    async def test_prefix_short_list_is_bounded_batch_not_out_of_range(self):
        snapshot = self.snapshot(count=1)
        self.context.user_message = "投递前20个"
        result = await self.registry._mock_delivery_start({"selector": "前20个", "limit": 20,
            "source_task_id": "new-round", "snapshot_id": snapshot})
        self.assertEqual(result["queued"], 1)
        self.assertEqual(result["shortfall"], 19)
        self.assertEqual(self.queued[0]["job_ids"], [1])
        self.assertEqual(self.queued[0]["limit"], 20)
        self.assertNotIn("allow_unverified", self.queued[0]["selection_evidence"])

    async def test_bare_count_uses_same_fixed_snapshot_universe(self):
        snapshot = self.snapshot(count=25)
        self.context.user_message = "投递20个"
        await self.registry._mock_delivery_start({"limit": 20, "source_task_id": "new-round", "snapshot_id": snapshot})
        self.assertEqual(self.queued[0]["limit"], 20)
        self.assertEqual(self.queued[0]["job_ids"], list(range(1, 26)))

    async def test_prefix_without_model_limit_still_sets_cap(self):
        snapshot = self.snapshot(count=25)
        self.context.user_message = "投递前二十个"
        await self.registry._mock_delivery_start({"selector": "前二十个", "source_task_id": "new-round", "snapshot_id": snapshot})
        self.assertEqual(self.queued[0]["limit"], 20)

    async def test_empty_prefix_round_reports_zero_and_preserves_limit(self):
        snapshot = self.snapshot(count=0)
        self.context.user_message = "投递前20个"
        result = await self.registry._mock_delivery_start({"selector": "前20个", "limit": 20,
            "source_task_id": "new-round", "snapshot_id": snapshot})
        self.assertEqual(result["queued"], 0)
        self.assertEqual(result["shortfall"], 20)
        self.assertEqual(self.queued[0]["job_ids"], [])

    async def test_exact_twentieth_stays_strict(self):
        snapshot = self.snapshot(count=1)
        self.context.user_message = "投递第20个"
        with self.assertRaises(TargetClarification):
            await self.registry._mock_delivery_start({"selector": "第20个", "snapshot_id": snapshot,
                "source_task_id": "new-round"})
        self.assertEqual(self.queued, [])

    async def test_generic_prefix_reference_for_removal_remains_strict(self):
        snapshot = self.snapshot(count=1)
        with self.assertRaises(ValueError):
            resolve_reference(self.context.dialogue, kind="job", selector="前20个", snapshot_id=snapshot, read_only=True)

    async def test_new_displayed_round_cannot_rebind_frozen_batch(self):
        original = self.snapshot(count=1)
        prepared = self.registry._delivery_arguments({"selector": "前20个", "limit": 20,
            "source_task_id": "new-round"})
        self.assertEqual(prepared["snapshot_id"], original)
        self.snapshot("old-round", count=25)
        self.context.user_message = "投递前20个"
        await self.registry._mock_delivery_start(prepared)
        self.assertEqual(self.queued[0]["task_id"], "new-round")
        self.assertEqual(self.queued[0]["job_ids"], [1])

    async def test_missing_frozen_source_never_falls_back_to_other_round(self):
        # No displayed snapshot exists yet, so source identity is the binding.
        prepared = self.registry._delivery_arguments({"limit": 20, "source_task_id": "new-round"})
        self.assertNotIn("snapshot_id", prepared)
        del self.sources["new-round"]
        dialogue_change("task", kind="browser_id", id="old-round", status="completed")
        self.snapshot("old-round", count=25)
        with self.assertRaises(ResultSourceClarification):
            await self.registry._mock_delivery_start(prepared)
        self.assertEqual(self.queued, [])

    async def test_generic_unknown_model_source_repair_remains_available(self):
        from app.workflow.result_sources import resolve_search_source
        dialogue_change("task", kind="browser_id", id="new-round", status="completed")
        # Legacy callers may still repair a hallucinated model ID when there
        # is one trusted candidate; bounded frozen actions opt out explicitly.
        del self.sources["old-round"]
        self.assertEqual(resolve_search_source(self.jobs, self.context,
            resume_id="synthetic-resume", explicit="model-guessed-id"), "new-round")

    async def test_successful_prefix_batch_replaces_previous_remembered_scope(self):
        self.old_scope()
        snapshot = self.snapshot(count=1)
        filters = {"op": "condition", "text": "工作地点在深圳"}
        result = await self.registry._mock_delivery_start({"selector": "前20个", "limit": 20,
            "source_task_id": "new-round", "snapshot_id": snapshot, "min_score": 90,
            "filters": filters, "scope_mode": "new"})
        self.assertEqual(result["queued"], 1)
        saved = self.context.dialogue["delivery_scope"]
        self.assertEqual(saved["task_id"], "new-round")
        self.assertEqual(saved["min_score"], 90)
        self.assertEqual(saved["filters"], filters)

    async def test_source_snapshot_conflict_does_not_switch_rounds(self):
        snapshot = self.snapshot("old-round", count=1)
        with self.assertRaises(ResultSourceClarification):
            await self.registry._mock_delivery_start({"limit": 20, "source_task_id": "new-round", "snapshot_id": snapshot})
        self.assertEqual(self.queued, [])

    async def test_foreign_or_other_resume_source_never_reaches_queue(self):
        for field, value in (("session_id", "foreign"), ("resume_id", "different-resume"), ("status", "running")):
            self.sources["new-round"][field] = value
            with self.subTest(field=field), self.assertRaises(ResultSourceClarification):
                await self.registry._mock_delivery_start({"limit": 20, "source_task_id": "new-round"})
            self.sources["new-round"] = {"task_id": "new-round", "session_id": "default",
                "resume_id": "synthetic-resume", "status": "completed"}
        self.assertEqual(self.queued, [])

    async def test_current_round_new_requested_batch_does_not_compare_old_salary_scope(self):
        self.old_scope()
        _, step = self.plan({"limit": 20, "source_task_id": "new-round", "scope_mode": "new"})
        decision = self.registry.authorize("mock_delivery_start", step["arguments"])
        self.assertEqual(decision.decision, "ALLOW")
        self.assertEqual(decision.approval["status"], "approved")

    async def test_same_round_expansion_still_needs_confirmation(self):
        self.old_scope()
        _, step = self.plan({"limit": 20, "source_task_id": "old-round", "scope_mode": "new"}, source="old-round")
        decision = self.registry.authorize("mock_delivery_start", step["arguments"])
        self.assertEqual(decision.decision, "REQUIRE_APPROVAL")
        self.assertEqual(decision.approval["code"], "delivery_scope_change_confirmation_required")

    async def test_unrequested_new_round_does_not_auto_approve(self):
        self.old_scope()
        _, step = self.plan({"limit": 20, "source_task_id": "new-round", "scope_mode": "new"}, requested=False)
        self.assertEqual(self.registry.authorize("mock_delivery_start", step["arguments"]).decision, "REQUIRE_APPROVAL")

    async def test_existing_human_only_snapshot_stays_pending_but_new_request_gets_fresh_grant(self):
        from app.security.action_grants import action_binding
        self.old_scope()
        self.sources["new-round"]["status"] = "running"
        plan, step = self.plan({"limit": 20, "source_task_id": "new-round", "scope_mode": "new"})
        first = self.registry.authorize("mock_delivery_start", step["arguments"])
        self.assertEqual(first.decision, "REQUIRE_APPROVAL")
        self.sources["new-round"]["status"] = "completed"
        self.assertEqual(self.registry.authorize("mock_delivery_start", step["arguments"]).decision, "REQUIRE_APPROVAL")
        # A later explicit request is represented by a different compiled action
        # snapshot; the old human-only approval must not authorize it by reuse.
        step["arguments"]["limit"] = 10
        step["authority_binding"] = action_binding(step["tool"], step["arguments"], session_id="default", requested=True)
        self.bind(plan, step)
        second = self.registry.authorize("mock_delivery_start", step["arguments"])
        self.assertEqual(second.decision, "ALLOW")
        self.assertNotEqual(first.approval["id"], second.approval["id"])

    async def test_empty_eligibility_returns_shortfall_instead_of_internal_slots(self):
        self.snapshot(count=1)
        async def insufficient(**kwargs):
            return {"queued": 0, "reason": "insufficient_evidence", "shortfall": 20,
                    "requested_limit": 20, "message": "本轮暂未有条件已核实的岗位，尚差20个。"}
        self.registry.mock_workflow.queue_deliveries = insufficient
        result = await self.registry._mock_delivery_start({"limit": 20, "source_task_id": "new-round"})
        self.assertEqual(result["shortfall"], 20)
        self.assertEqual(result["queued"], 0)
        self.assertIsNone(self.context.dialogue.get("delivery_scope"))

    async def test_current_document_change_never_auto_approves_old_stamp(self):
        self.old_scope()
        _, step = self.plan({"limit": 20, "source_task_id": "new-round", "scope_mode": "new"})
        self.document.version = 2
        self.assertEqual(self.registry.authorize("mock_delivery_start", step["arguments"]).decision, "REQUIRE_APPROVAL")

    async def test_new_round_proof_rejects_wrong_owner_or_resume(self):
        self.old_scope()
        for field, value in (("session_id", "foreign"), ("resume_id", "different")):
            self.sources["new-round"][field] = value
            _, step = self.plan({"limit": 20, "source_task_id": "new-round", "scope_mode": "new"})
            self.assertEqual(self.registry.authorize("mock_delivery_start", step["arguments"]).decision, "REQUIRE_APPROVAL")
            self.sources["new-round"][field] = "default" if field == "session_id" else "synthetic-resume"

    async def test_bounded_limit_without_filters_is_not_unbounded(self):
        from app.security.risk import broad_delivery_scope
        self.assertFalse(broad_delivery_scope("mock_delivery_start", {"limit": 20, "source_task_id": "new-round"}))
        for args in ({"limit": 20}, {"limit": True, "source_task_id": "new-round"}, {"limit": 0, "source_task_id": "new-round"}):
            self.assertTrue(broad_delivery_scope("mock_delivery_start", args))


def main():
    suite = unittest.TestSuite(RoundContracts(name) for name in RoundContracts.__dict__ if name.startswith("test_"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    output = Path("evaluation/results/delivery-round") / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    summary = {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
        "external_model_calls": 0, "recruitment_actions": 0}
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (output / "traces.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in TRACES), encoding="utf-8")
    print(json.dumps({"output": str(output), **summary}))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
