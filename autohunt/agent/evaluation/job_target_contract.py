"""Semantic target/removal integration with scripted models and temporary SQLite.

Exercises the public registry, real graph/revisions, target state, queue exclusions
and cancellation. No live website, personal data or external model invocation.
"""
from __future__ import annotations

import asyncio
import copy
import gc
import io
import json
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS

from app.core.agent_tools import AgentToolRegistry, ToolLoopState
from app.core.job_targets import resolve_job_targets, TargetClarification, is_remaining_selector
from app.jobs.store import JobStore
from app.llm.base import LLMResult
from app.runtime.context import RunContext, current_run
from app.runtime.dialogue import apply_operation
from app.workflow.execution import advance
from app.workflow.planning import RequestDraft, validate_draft
from app.workflow.planning import _draft_with_repair
from app.llm.base import LLMRequest
from app.workflow.revision import RevisionDraft, edited_plan
from app.workflow.mock_coordinator import MockWorkflowCoordinator
from app.workflow.validation import PlanValidationError
from .search_delivery_contract import job
from .goal_graph_contract import STAMP, step, draft, MESSAGE

TRACES = []


class Contracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="autohunt-target-")
        self.jobs = JobStore(Path(self.temp.name) / "jobs.db")
        self.events, self.calls, self.model_calls = [], [], []
        self.ids = []
        for i, city in enumerate(("杭州", "上海", "深圳"), 1):
            value = {**job(i), "title": "NLP算法工程师", "company": f"合成企业{i}", "location": city,
                     "description": f"正式岗位；在{city}开发RAG检索与评测，维护知识库。"}
            self.ids.append(self.jobs.upsert_job(value)[0])
        self.snapshot_items = [{"id": i, "title": self.jobs.get_job(i)["title"], "company": self.jobs.get_job(i)["company"],
                               "content_hash": self.jobs.review_fingerprint(self.jobs.get_job(i))} for i in self.ids]
        self.state = apply_operation({}, {"op": "snapshot", "topic": "applications", "kind": "job", "source": "synthetic",
            "version": "synthetic-search", "items": self.snapshot_items, "displayed": True})
        self.snapshot = self.state["snapshots"][-1]["id"]
        self.model_result = {"status": "resolved", "matches": [{"ordinal": 1, "evidence": [{"field": "location", "quote": "杭州"}]}]}
        async def generate(request):
            self.model_calls.append(json.loads(request.user_message))
            return LLMResult(json.dumps(self.model_result, ensure_ascii=False), "scripted", "target")
        self.provider = NS(generate=generate)
        self.delivery = NS(current=lambda: NS(job_id=None), stop=self.noop)
        self.coordinator = NS(jobs=self.jobs, operations={}, operation_jobs={}, delivery=self.delivery,
                              wakeup=asyncio.Event(), remember_authorization_scope=lambda *a, **kw: None)
        async def remove(items, **kwargs):
            return await MockWorkflowCoordinator.remove_jobs(self.coordinator, items, **kwargs)
        async def enqueue(**kwargs):
            self.calls.append(copy.deepcopy(kwargs))
            ids = kwargs.get("job_ids") or []
            ids = self.jobs.enqueue_mock_deliveries([self.jobs.get_job(i) for i in ids], session_id=kwargs["session_id"],
                resume_stamp=STAMP, binding={}, min_score=kwargs.get("min_score", 0))
            return {"queued": len(ids), "job_ids": ids, "message": "合成队列回执"}
        self.coordinator.remove_jobs, self.coordinator.queue_deliveries = remove, enqueue
        self.registry = AgentToolRegistry(jobs=self.jobs, browser=NS(), memory=None,
            candidate_state=NS(read=lambda: NS(profile=NS(resume_id="synthetic-resume"))),
            resume_workspace=NS(read_document=lambda: NS(id="synthetic-document", version=1)),
            mock_workflow=self.coordinator, response_llm=self.provider)
        self.context = RunContext("synthetic-target-run", asyncio.current_task(),
            lambda kind,data: self.events.append({"kind": kind, "data": copy.deepcopy(data)}), lambda _: None,
            dialogue=self.state, search_state={}, user_message="合成明确指令",
            authorize_action=lambda *a: {"id": "synthetic-authority", "status": "approved", "source": "instruction"},
            mutate_dialogue=self.mutate)
        self.token = current_run.set(self.context)

    async def noop(self): pass

    def mutate(self, operation):
        self.state = apply_operation(self.state, operation)
        return self.state

    async def asyncTearDown(self):
        TRACES.append({"test": self.id(), "calls": self.calls, "model_calls": self.model_calls,
                       "events": self.events, "target_state": self.state.get("job_target_context"),
                       "queue": self.jobs.mock_delivery_queue(active_only=False)})
        current_run.reset(self.token)
        self.registry = self.jobs = self.coordinator = None
        gc.collect(); self.temp.cleanup()

    async def call(self, tool, args):
        self.context.owner = asyncio.current_task()
        return await self.registry.execute(tool, args, user_message=self.context.user_message, state=ToolLoopState())

    async def test_ordinal_delivery_targets_one_without_model(self):
        self.context.user_message = "投递第2个"
        result = await self.call("mock_delivery_start", {"selector": "第2个"})
        self.assertEqual(result.status, "ok")
        self.assertEqual(self.calls[0]["job_ids"], [self.ids[1]])
        self.assertFalse(self.model_calls)

    async def test_unique_company_exact_target_needs_no_model(self):
        result = await self.call("mock_delivery_start", {"selector": "合成企业3"})
        self.assertEqual(result.status, "ok")
        self.assertEqual(self.calls[0]["job_ids"], [self.ids[2]])
        self.assertFalse(self.model_calls)

    async def test_description_targets_city_and_does_not_copy_old_filters(self):
        result = await self.call("mock_delivery_start", {"selector": "杭州做RAG的那个"})
        self.assertEqual(result.status, "ok")
        self.assertEqual(self.calls[0]["job_ids"], [self.ids[0]])
        self.assertNotIn("filters", self.calls[0])
        self.assertEqual(len(self.model_calls), 1)

    async def test_same_title_is_ambiguous_never_first_or_bulk(self):
        self.model_result = {"status": "ambiguous", "matches": [
            {"ordinal": i, "evidence": [{"field": "title", "quote": "NLP算法工程师"}]} for i in (1, 2, 3)]}
        result = await self.call("mock_delivery_start", {"selector": "NLP算法工程师"})
        self.assertEqual(result.status, "awaiting_user")
        self.assertFalse(self.calls)
        self.assertEqual(len(self.context.dialogue["snapshots"][-1]["items"]), 3)
        self.assertIn("1. NLP算法工程师", result.content)

    async def test_false_evidence_never_creates_queue(self):
        self.model_result["matches"][0]["evidence"][0]["quote"] = "不存在的城市"
        result = await self.call("mock_delivery_start", {"selector": "那个描述岗位"})
        self.assertEqual(result.status, "awaiting_user")
        self.assertFalse(self.calls)

    async def test_foreign_ordinal_rejected_before_model(self):
        result = await self.call("mock_delivery_start", {"selector": "第99个"})
        self.assertEqual(result.status, "awaiting_user")
        self.assertFalse(self.model_calls)

    async def test_semantic_matching_explicitly_selects_subset(self):
        self.model_result = {"status": "resolved", "matches": [
            {"ordinal": i, "evidence": [{"field": "title", "quote": "NLP算法工程师"}]} for i in (1, 3)]}
        result = await self.call("mock_delivery_start", {"selector": "那两个岗位", "selection_mode": "matching"})
        self.assertEqual(result.status, "ok")
        self.assertEqual(self.calls[0]["job_ids"], [self.ids[0], self.ids[2]])

    async def test_these_uses_semantic_context_not_every_displayed_job(self):
        self.model_result = {"status": "resolved", "matches": [
            {"ordinal": 2, "evidence": [{"field": "company", "quote": "合成企业2"}]}]}
        result = await self.call("job_remove_selection", {"selector": "这些"})
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.content["removed_ids"], [self.ids[1]])
        self.assertEqual(len(self.jobs.list_jobs()), 2)

    async def test_remaining_reference_cannot_move_to_new_list(self):
        await self.call("job_remove_selection", {"selector": "第1个"})
        self.context.dialogue = self.state = apply_operation(self.state, {"op": "snapshot", "topic": "applications", "kind": "job",
            "source": "new-search", "items": self.snapshot_items[1:], "displayed": True})
        result = await self.call("mock_delivery_start", {"selector": "剩余岗位"})
        self.assertEqual(result.status, "awaiting_user")
        self.assertFalse(self.calls)

    async def test_model_remainder_wording_uses_recorded_removal_not_another_call(self):
        await self.call("job_remove_selection", {"selector": "第2个"})
        for selector in ("移除后的剩余岗位", "移除之后其余的职位", "剩下的岗位", "剩余的"):
            result = await self.call("mock_delivery_start", {"selector": selector, "selection_mode": "matching"})
            self.assertEqual(result.status, "ok")
            self.assertEqual(self.calls[-1]["job_ids"], [self.ids[0], self.ids[2]])
        self.assertFalse(self.model_calls)
        self.assertFalse(is_remaining_selector("剩余的杭州岗位"))

    async def test_matching_distinguishes_set_cardinality_from_actual_ambiguity(self):
        requests = []
        async def generate(request):
            requests.append(request)
            return LLMResult(json.dumps({"status": "ambiguous", "matches": [
                {"ordinal": i, "evidence": [{"field": "company", "quote": f"合成企业{i}"}]} for i in (1, 3)]}), "scripted", "ambiguous")
        with self.assertRaises(TargetClarification):
            await resolve_job_targets("这些", dialogue=self.state, jobs=self.jobs, generate=generate)
        self.assertIn("不表示集合中有多个成员", requests[0].instructions)
        self.assertEqual(json.loads(requests[0].user_message)["selection_mode"], "matching")

    async def test_single_mode_does_not_accept_multiple_matches(self):
        self.model_result = {"status": "resolved", "matches": [
            {"ordinal": i, "evidence": [{"field": "title", "quote": "NLP算法工程师"}]} for i in (1, 2)]}
        result = await self.call("mock_delivery_start", {"selector": "那个NLP岗位"})
        self.assertEqual(result.status, "awaiting_user")
        self.assertFalse(self.calls)

    async def test_remove_then_remaining_delivery_preserves_history_and_order(self):
        before = copy.deepcopy(self.state["snapshots"][0]["items"])
        removed = await self.call("job_remove_selection", {"selector": "合成企业2"})
        self.assertEqual(removed.status, "ok")
        self.assertEqual(removed.content["removed_ids"], [self.ids[1]])
        self.assertEqual(len(self.jobs.list_jobs()), 2)
        self.assertTrue(self.jobs.get_job(self.ids[1])["removed_at"])
        self.assertEqual(self.state["snapshots"][0]["items"], before)
        delivered = await self.call("mock_delivery_start", {"selector": "剩余岗位", "selection_mode": "matching"})
        self.assertEqual(delivered.status, "ok")
        self.assertEqual(delivered.content["job_ids"], [self.ids[0], self.ids[2]])

    async def test_semantic_remove_cancels_only_selected_queue_row(self):
        self.jobs.enqueue_mock_deliveries([self.jobs.get_job(i) for i in self.ids], session_id="default",
                                        resume_stamp=STAMP, binding={}, min_score=0)
        result = await self.call("job_remove_selection", {"selector": "杭州做RAG的那个"})
        self.assertEqual(result.status, "ok")
        rows = {item["job_id"]: item for item in self.jobs.mock_delivery_queue(active_only=False)}
        self.assertEqual(rows[self.ids[0]]["status"], "cancelled")
        self.assertEqual(rows[self.ids[1]]["status"], "queued")
        self.assertEqual(rows[self.ids[2]]["status"], "queued")

    async def test_remove_retired_category_is_noop_without_semantic_reselection(self):
        original = self.provider.generate
        async def generate(request):
            payload = json.loads(request.user_message)
            self.assertEqual(payload["selector"], "第三方或者外包岗位")
            self.assertEqual(payload["selection_mode"], "matching")
            return await original(request)
        self.provider.generate = generate
        with self.jobs._connect() as db:
            db.execute("UPDATE jobs SET description=? WHERE id=?", ("由第三方签劳动合同，为客户企业提供外包研发服务。", self.ids[0]))
        # Display the updated trusted facts, not a stale search snapshot.
        entries = [{**item, "content_hash": self.jobs.review_fingerprint(self.jobs.get_job(item["id"]))} for item in self.snapshot_items]
        self.context.dialogue = self.state = apply_operation(self.state, {"op": "snapshot", "topic": "applications", "kind": "job",
            "source": "synthetic", "items": entries, "displayed": True})
        self.model_result = {"status": "resolved", "matches": [{"ordinal": 1,
            "evidence": [{"field": "description", "quote": "由第三方签劳动合同"}]}]}
        removed = await self.call("job_remove_selection", {"selector": "第三方或者外包岗位", "selection_mode": "matching"})
        self.assertEqual(removed.status, "ok")
        self.assertEqual(removed.content["removed_ids"], [])
        self.assertTrue(removed.content["ignored_filter"])
        self.assertEqual(self.model_calls, [])
        self.assertEqual(len(self.jobs.list_jobs()), 3)
        self.assertEqual(self.jobs.mock_delivery_queue(), [])

    async def test_allowed_semantic_remove_uses_evidence_then_delivers_remaining(self):
        original = self.provider.generate
        async def generate(request):
            payload = json.loads(request.user_message)
            self.assertEqual(payload["selector"], "需要出差的岗位")
            self.assertEqual(payload["selection_mode"], "matching")
            return await original(request)
        self.provider.generate = generate
        with self.jobs._connect() as db:
            db.execute("UPDATE jobs SET description=? WHERE id=?", ("每月需出差两次，负责客户现场RAG检索系统交付。", self.ids[0]))
        entries = [{**item, "content_hash": self.jobs.review_fingerprint(self.jobs.get_job(item["id"]))} for item in self.snapshot_items]
        self.context.dialogue = self.state = apply_operation(self.state, {"op": "snapshot", "topic": "applications", "kind": "job",
            "source": "synthetic", "items": entries, "displayed": True})
        self.model_result = {"status": "resolved", "matches": [{"ordinal": 1,
            "evidence": [{"field": "description", "quote": "每月需出差两次"}]}]}
        removed = await self.call("job_remove_selection", {"selector": "需要出差的岗位", "selection_mode": "matching"})
        self.assertEqual(removed.status, "ok")
        self.assertEqual(removed.content["removed_ids"], [self.ids[0]])
        delivered = await self.call("mock_delivery_start", {"selector": "移除后的剩余岗位", "selection_mode": "matching"})
        self.assertEqual(delivered.status, "ok")
        self.assertEqual(delivered.content["job_ids"], [self.ids[1], self.ids[2]])

    async def test_remove_precondition_failure_never_reports_uncertain_effect(self):
        self.jobs.enqueue_mock_deliveries([self.jobs.get_job(self.ids[0])], session_id="other",
                                         resume_stamp=STAMP, binding={}, min_score=0)
        result = await self.call("job_remove_selection", {"selector": "第1个"})
        self.assertEqual(result.status, "error")
        self.assertFalse(result.failure["effect_started"])
        self.assertFalse(result.failure["uncertain"])
        self.assertEqual(len(self.jobs.list_jobs()), 3)
        self.assertEqual(self.jobs.mock_delivery_queue(session_id="other")[0]["status"], "queued")

    async def test_atomic_remove_on_stale_evidence(self):
        invalid = [{**self.snapshot_items[0]}, {**self.snapshot_items[1], "content_hash": "wrong"}]
        with self.assertRaises(ValueError):
            self.jobs.remove_selection(invalid, session_id="default", run_id="synthetic")
        self.assertEqual(len(self.jobs.list_jobs()), 3)

    async def test_removed_job_cannot_be_requeued_or_previewed(self):
        original = self.jobs.get_job(self.ids[0])
        await self.call("job_remove_selection", {"selector": "第1个"})
        self.assertEqual(self.jobs.enqueue_mock_deliveries([original], session_id="default",
            resume_stamp=STAMP, binding={}, min_score=0), [])
        with self.assertRaises(ValueError): self.jobs.create_delivery_preview(self.ids[0])

    async def test_target_binding_rejects_unknown_snapshot(self):
        result = await self.call("mock_delivery_start", {"selector": "第1个", "snapshot_id": "foreign"})
        self.assertEqual(result.status, "awaiting_user")
        self.assertFalse(self.calls)

    async def test_large_list_keeps_last_candidate_and_bounded_model_context(self):
        facts = {i: {"id": i, "title": f"合成岗位{i}", "company": "合成企业", "location": f"合成城市{i}",
                     "salary": "20-30K", "description": "合成岗位说明" * 800, "tags": "合成标签" * 500} for i in range(1, 201)}
        entries = [{**{key: f[key] for key in ("id", "title", "company")}, "content_hash": JobStore.review_fingerprint(f)} for f in facts.values()]
        state = apply_operation({}, {"op": "snapshot", "topic": "applications", "kind": "job", "source": "synthetic", "items": entries})
        self.model_result = {"status": "resolved", "matches": [{"ordinal": 200, "evidence": [{"field": "location", "quote": "合成城市200"}]}]}
        resolved = await resolve_job_targets("最后那个城市的岗位", dialogue=state,
            jobs=NS(get_job=lambda i: facts[i], review_fingerprint=JobStore.review_fingerprint), generate=self.provider.generate)
        self.assertEqual(resolved["items"][0]["id"], 200)
        self.assertEqual(len(self.model_calls[0]["candidates"]), 200)
        self.assertLessEqual(len(json.dumps(self.model_calls[0], ensure_ascii=False)), 64000)

    async def test_repeat_remove_is_idempotent(self):
        for _ in range(2):
            result = await self.call("job_remove_selection", {"selector": "第1个"})
            self.assertEqual(result.status, "ok")
        self.assertEqual(len(self.jobs.list_jobs()), 2)

    async def test_removed_job_stays_hidden_after_search_upsert(self):
        original = self.jobs.get_job(self.ids[0])
        original["tags"] = json.loads(original["tags"])
        await self.call("job_remove_selection", {"selector": "第1个"})
        self.jobs.upsert_job({**original, "board_visible": True})
        self.assertEqual(len(self.jobs.list_jobs()), 2)
        self.jobs.create_task("new-search", "mock", "NLP算法工程师", resume_id="synthetic-resume")
        admitted = self.jobs.admit_search_result("new-search", original, limit=3, groups=None)
        self.assertEqual(admitted["status"], "user_removed")
        self.assertEqual(len(self.jobs.list_jobs()), 2)

    async def test_cross_session_active_queue_not_cancelled(self):
        self.jobs.enqueue_mock_deliveries([self.jobs.get_job(self.ids[0])], session_id="other",
                                        resume_stamp=STAMP, binding={}, min_score=0)
        with self.assertRaises(ValueError):
            self.jobs.remove_selection([self.snapshot_items[0]], session_id="default", run_id="synthetic")
        self.assertFalse(self.jobs.get_job(self.ids[0])["removed_at"])
        self.assertEqual(self.jobs.mock_delivery_queue(session_id="other")[0]["status"], "queued")

    async def test_remove_started_send_preserves_uncertain_outcome(self):
        from app.core.models import DeliveryTaskStatus
        preview = self.jobs.create_delivery_preview(self.ids[0])
        run = self.jobs.create_delivery_run("uncertain", self.ids[0], preview_id=preview.id)
        self.jobs.enqueue_mock_deliveries([self.jobs.get_job(self.ids[0])], session_id="default", resume_stamp=STAMP, binding={}, min_score=0)
        self.jobs.update_mock_delivery(self.ids[0], "dispatched", "合成发送中", run_id=run.id)
        self.delivery.current = lambda: NS(job_id=self.ids[0])
        async def stop():
            self.jobs.update_delivery_run(run.id, status=DeliveryTaskStatus.OUTCOME_INDETERMINATE, detail="合成点击结果未知")
        self.delivery.stop = stop
        result = await self.call("job_remove_selection", {"selector": "第1个"})
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.content["uncertain_ids"], [self.ids[0]])
        self.assertEqual(self.jobs.get_delivery_run(run.id).status, DeliveryTaskStatus.OUTCOME_INDETERMINATE)
        self.assertIn("待核验", result.content["message"])

    async def test_old_plan_wrong_field_gets_precise_feedback_and_new_plan_can_execute(self):
        self.context.owner = asyncio.current_task()
        old = {"id": "old", "session_id": "default", "resume_stamp": STAMP, "revision": 0,
               "status": "clarifying", "original": "移除这些", "questions": ["这些指谁"], "steps": [], "goals": []}
        self.context.plan_reader = lambda: old
        calls = []
        async def generate(request):
            payload = json.loads(request.user_message)
            calls.append(payload)
            if len(calls) == 1:
                raw = {"operation": "revise", "revision": {"add_steps": [{"id": "bad", "tool": "job_results_refilter", "arguments": {"selector": "第2个"}}]}}
            else:
                issue = payload["validation_feedback"]["issues"][0]
                self.assertEqual(issue["tool"], "job_results_refilter")
                self.assertIn("selector", issue["detail"])
                raw = draft([step("one", "mock_delivery_start", {"selector": "第2个"})], ["one"])
            return LLMResult(json.dumps(raw), "scripted", "repair")
        value = await _draft_with_repair(NS(generate=generate), LLMRequest(instructions="合成", user_message=json.dumps({"message": "投递第2个"}), context=""),
            validator=lambda d: validate_draft(d, self.registry, "投递第2个", dialogue=self.context.dialogue, conditions={}, stamp=STAMP))
        self.context.owner = asyncio.current_task()
        self.context.user_message = "投递第2个"
        await advance(value, self.registry)
        self.assertEqual(self.calls[0]["job_ids"], [self.ids[1]])
        self.assertEqual(len(calls), 2)

    async def test_new_plan_single_target_ignores_unrelated_question(self):
        self.context.owner = asyncio.current_task()
        value = validate_draft(RequestDraft.model_validate(draft([step("one", "mock_delivery_start", {"selector": "第2个"})], ["one"])),
            self.registry, "投递第2个", dialogue=self.context.dialogue, conditions={}, stamp=STAMP)
        self.context.user_message = "投递第2个"
        await advance(value, self.registry)
        self.assertEqual(value["steps"][0]["status"], "completed")
        self.assertEqual(self.calls[0]["job_ids"], [self.ids[1]])

    async def test_remove_and_deliver_dependencies_inserted(self):
        self.context.owner = asyncio.current_task()
        value = validate_draft(RequestDraft.model_validate(draft([
            step("deliver", "mock_delivery_start", {"selector": "剩余岗位"}),
            step("remove", "job_remove_selection", {"selector": "第2个"})], ["remove", "deliver"])),
            self.registry, "移除第2个，其余投递", dialogue=self.context.dialogue, conditions={}, stamp=STAMP)
        self.context.user_message = "移除这个岗位，其余投递"
        await advance(value, self.registry)
        self.assertIn("remove", value["steps"][0]["depends_on"])
        self.assertEqual(self.calls[0]["job_ids"], [self.ids[0], self.ids[2]])

    async def test_revision_confirmation_clears_null_legacy_question(self):
        self.context.owner = asyncio.current_task()
        old = {"id": "old", "session_id": "default", "resume_stamp": STAMP, "revision": 0,
               "status": "clarifying", "original": "移除这些，其余投递", "questions": ["这些指谁"], "steps": [], "goals": [],
               "turn": 0, "expires_at": time.time()+1800, "answers": []}
        self.context.plan_reader = lambda: old
        candidate = RequestDraft.model_validate({"operation": "revise", "questions": [], "revision": {
            "questions": None, "add_steps": [{"id": "remove", "tool": "job_remove_selection", "arguments": {"selector": "第2个"}, "user_requested": True},
                                             {"id": "deliver", "tool": "mock_delivery_start", "arguments": {"selector": "剩余岗位"}, "user_requested": True}]}})
        validated = validate_draft(candidate, self.registry, "确认移除第2个，剩余投递", dialogue={}, conditions={}, stamp=STAMP)
        revised = edited_plan(old, RevisionDraft.model_validate(validated["revision"]), self.registry,
                              stamp=STAMP, session_id="default", message="确认移除第2个，剩余投递")
        self.assertFalse(revised["questions"])
        self.context.user_message = "确认这些，剩余投递"
        await advance(revised, self.registry)
        self.assertEqual(len(self.calls[0]["job_ids"]), 2)

    async def test_revision_error_identifies_tool_and_unknown_field(self):
        old = {"id": "old", "session_id": "default", "resume_stamp": STAMP, "revision": 0,
               "status": "running", "original": "合成", "questions": [], "steps": [], "goals": []}
        with self.assertRaises(PlanValidationError) as raised:
            edited_plan(old, RevisionDraft(plan_id="old", expected_revision=0, add_steps=[
                {"id": "bad", "tool": "job_results_refilter", "arguments": {"selector": "第2个"}}]),
                self.registry, stamp=STAMP, session_id="default", message="合成")
        self.assertEqual(raised.exception.issue.tool, "job_results_refilter")
        self.assertIn("selector", str(raised.exception))
        self.assertIn("task_id", str(raised.exception))


def main():
    buffer = io.StringIO()
    result = unittest.TextTestRunner(stream=buffer, verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Contracts))
    output = Path(__file__).parent / "results" / "job-targets" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    (output / "tests.log").write_text(buffer.getvalue(), encoding="utf-8")
    (output / "traces.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False, default=str)+"\n" for row in TRACES), encoding="utf-8")
    summary = {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
               "real_model_calls": 0, "real_browser_actions": 0, "coverage": "scripted semantic output + real registry/target state/SQLite/graph/revision, not real-model comprehension"}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if not result.wasSuccessful(): print(buffer.getvalue()[-14000:])
    print(json.dumps({"output": str(output), **summary}))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__": raise SystemExit(main())
