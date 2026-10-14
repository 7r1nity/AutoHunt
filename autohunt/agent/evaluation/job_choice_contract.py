"""Clickable target/keep-complement contracts: temporary databases, scripted LLM.

No personal records, recruitment browser actions or external model calls.
"""
from __future__ import annotations

import asyncio
import io
import json
import unittest
from datetime import UTC, datetime
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from app.runtime.manager import AgentRuntime
from app.runtime.models import TargetChoiceDecision, public_run
from app.runtime.store import RunStore
from app.workflow.execution import advance, begin
from app.workflow.planning import RequestDraft, validate_draft
from app.workflow.target_selection import bind_target_dependencies
from app.workflow.validation import PlanValidationError
from .goal_graph_contract import STAMP, draft, step
from .job_target_contract import Contracts as TargetFixtures, TRACES
from .search_delivery_contract import job


class Choices(TargetFixtures):
    # Inherit fixture helpers but run only the new cases in this runner.
    async def asyncSetUp(self):
        await super().asyncSetUp()
        with closing(self.jobs._connect()) as db, db:
            for ident, score in zip(self.ids, (88, 82, 78)):
                db.execute("UPDATE jobs SET company=?, score=? WHERE id=?", ("合成星海人工智能", score, ident))
        self.snapshot_items = [{**item, "company": "合成星海人工智能", "score": self.jobs.get_job(item["id"])["score"],
            "content_hash": self.jobs.review_fingerprint(self.jobs.get_job(item["id"]))} for item in self.snapshot_items]
        self.context.dialogue["snapshots"][-1]["items"] = self.snapshot_items
        self.context.user_message = "投递82分的合成星海人工智能，其余都从看板移除"
        self.document = NS(id="synthetic-document", version=1)
        self.registry.resume_workspace = NS(read_document=lambda: self.document)
        self.runtime_store = RunStore(Path(self.temp.name) / "runtime.db")
        self.run, _ = self.runtime_store.create("default", "synthetic-choice", "chat", {"message": self.context.user_message})
        self.runtime = AgentRuntime(self.runtime_store, NS(candidate_state=self.registry.candidate_state), self.jobs, NS(), None, self.registry.resume_workspace)
        self.scheduled = []
        self.runtime.schedule = lambda run_id, **kw: self.scheduled.append((run_id, kw))

    def plan(self):
        value = validate_draft(RequestDraft.model_validate(draft([
            step("deliver", "mock_delivery_start", {"target_step_id": "keep"}),
            step("remove", "job_remove_selection", {"selector": "其余岗位", "exclude_target_step_id": "keep"}),
            step("keep", "job_target_resolve", {"selector": "82分的合成星海人工智能", "scope": "board"})], ["remove", "deliver"])),
            self.registry, self.context.user_message, dialogue=self.context.dialogue, conditions={}, stamp=STAMP)
        self.context.plan_reader = lambda: value
        for item in value["steps"]:
            item["command_quote"] = self.context.user_message
        return value

    async def waiting(self, *, multiple=False):
        self.context.owner = asyncio.current_task()
        self.model_result = {"status": "ambiguous", "matches": [
            {"ordinal": i, "evidence": [{"field": "company", "quote": "合成星海人工智能"}]} for i in (1, 2, 3)]}
        value = self.plan()
        if multiple:
            next(s for s in value["steps"] if s["id"] == "keep")["arguments"]["selection_mode"] = "matching"
        await advance(value, self.registry)
        self.assertEqual(next(s for s in value["steps"] if s["id"] == "keep")["status"], "waiting_input")
        self.runtime_store.save_plan(self.run["id"], value)
        self.runtime_store.update(self.run["id"], status="waiting_user")
        return value

    def decision(self, value, **changes):
        anchor = next(s for s in value["steps"] if s["id"] == "keep")
        return TargetChoiceDecision(**{"conversation_id": "default", "step_id": "keep", "choice_id": anchor["target_choice"]["id"],
            "expected_revision": value["revision"], "job_ids": [self.ids[1]], **changes})

    async def test_exact_score_facts_and_complement_include_same_company(self):
        self.context.owner = asyncio.current_task()
        self.model_result = {"status": "resolved", "matches": [{"ordinal": 2, "evidence": [{"field": "score", "quote": "82"}]}]}
        value = self.plan()
        await advance(value, self.registry)
        self.assertEqual([row.id for row in self.jobs.list_jobs()], [self.ids[1]])
        self.assertEqual(self.calls[0]["job_ids"], [self.ids[1]])
        payload = self.model_calls[0]
        self.assertEqual({row["score"] for row in payload["candidates"]}, {78, 82, 88})
        self.assertIn("82分", payload["instruction"])
        self.assertEqual(len(self.model_calls), 1)

    async def test_question_appears_once_as_clickable_choice_prompt(self):
        self.context.owner = asyncio.current_task()
        self.model_result = {"status": "ambiguous", "matches": [
            {"ordinal": i, "evidence": [{"field": "company", "quote": "合成星海人工智能"}]} for i in (1, 2)]}
        response = await begin(self.plan(), self.registry)
        self.assertEqual(response.message.count("请点击下方候选选择"), 1)
        self.assertNotIn("待补充的分支", response.message)
        self.assertFalse(self.calls)

    async def test_ambiguity_stops_all_dependent_effects_and_exports_choices(self):
        value = await self.waiting()
        self.assertEqual(len(self.jobs.list_jobs()), 3)
        self.assertFalse(self.calls)
        visible = public_run(self.runtime_store.get(self.run["id"]))["target_choices"]
        self.assertEqual(len(visible), 1)
        self.assertEqual(len(visible[0]["options"]), 3)
        self.assertNotIn("content_hash", visible[0]["options"][0])
        self.assertIn("keep", value["steps"][0]["depends_on"])
        self.assertIn("remove", value["steps"][0]["depends_on"])

    async def test_board_scope_includes_jobs_not_in_recent_chat_list(self):
        self.context.owner = asyncio.current_task()
        self.context.dialogue["snapshots"][-1]["items"] = self.snapshot_items[:1]
        self.model_result = {"status": "resolved", "matches": [{"ordinal": 2, "evidence": [{"field": "score", "quote": "82"}]}]}
        await advance(self.plan(), self.registry)
        self.assertEqual([row.id for row in self.jobs.list_jobs()], [self.ids[1]])

    async def test_large_board_never_silently_truncates_removal(self):
        with patch.object(self.jobs, "list_jobs", return_value=[NS(id=i) for i in range(201)]):
            outcome = await self.call("job_target_resolve", {"selector": "合成星海", "scope": "board"})
        self.assertEqual(outcome.status, "awaiting_user")
        self.assertIn("超过200", outcome.content)
        self.assertFalse(self.calls)
        self.assertFalse(self.model_calls)

    async def test_click_then_resume_removes_complement_and_queues_only_selected(self):
        value = await self.waiting()
        calls = len(self.model_calls)
        answered = await self.runtime.choose_job_target(self.run["id"], self.decision(value))
        value = answered["checkpoint"]["plan"]
        self.context.plan_reader = lambda: value
        await advance(value, self.registry)
        self.assertEqual(len(self.model_calls), calls)
        self.assertEqual([row.id for row in self.jobs.list_jobs()], [self.ids[1]])
        self.assertEqual(self.calls[0]["job_ids"], [self.ids[1]])
        self.assertEqual(len(self.scheduled), 1)

    async def test_duplicate_same_choice_is_idempotent(self):
        value = await self.waiting()
        decision = self.decision(value)
        await self.runtime.choose_job_target(self.run["id"], decision)
        await self.runtime.choose_job_target(self.run["id"], decision)
        self.assertEqual(len(self.scheduled), 1)
        self.assertEqual(len(self.jobs.list_jobs()), 3)  # HTTP decision itself runs no tool.

    async def test_explicit_multiple_choices_keep_selected_set(self):
        value = await self.waiting(multiple=True)
        selected = self.ids[1:]
        answered = await self.runtime.choose_job_target(self.run["id"], self.decision(value, job_ids=selected))
        value = answered["checkpoint"]["plan"]
        self.context.plan_reader = lambda: value
        await advance(value, self.registry)
        self.assertEqual({row.id for row in self.jobs.list_jobs()}, set(selected))
        self.assertEqual(set(self.calls[0]["job_ids"]), set(selected))

    async def test_different_second_choice_rejected(self):
        value = await self.waiting()
        await self.runtime.choose_job_target(self.run["id"], self.decision(value))
        with self.assertRaises(ValueError):
            await self.runtime.choose_job_target(self.run["id"], self.decision(value, job_ids=[self.ids[0]]))

    async def test_foreign_and_stale_decisions_have_no_effect(self):
        value = await self.waiting()
        for change in ({"conversation_id": "foreign"}, {"choice_id": "f"*32}, {"step_id": "foreign"},
                       {"expected_revision": value["revision"]+1}, {"job_ids": [999]}, {"job_ids": self.ids[:2]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                await self.runtime.choose_job_target(self.run["id"], self.decision(value, **change))
        self.assertFalse(self.scheduled)
        self.assertEqual(len(self.jobs.list_jobs()), 3)

    async def test_changed_score_rejected(self):
        value = await self.waiting()
        with closing(self.jobs._connect()) as db, db: db.execute("UPDATE jobs SET score=90 WHERE id=?", (self.ids[1],))
        with self.assertRaises(ValueError): await self.runtime.choose_job_target(self.run["id"], self.decision(value))

    async def test_removed_candidate_rejected(self):
        value = await self.waiting()
        with closing(self.jobs._connect()) as db, db: db.execute("UPDATE jobs SET removed_at=? WHERE id=?", ("synthetic-removed", self.ids[1]))
        with self.assertRaises(ValueError): await self.runtime.choose_job_target(self.run["id"], self.decision(value))

    async def test_selected_content_change_rejected(self):
        value = await self.waiting()
        self.jobs.upsert_job({**job(2), "company": "合成星海人工智能", "title": "NLP算法工程师", "score": 82,
                             "location": "上海", "description": "不同职责"})
        with self.assertRaises(ValueError): await self.runtime.choose_job_target(self.run["id"], self.decision(value))

    async def test_changed_resume_rejected(self):
        value = await self.waiting()
        self.document.version = 2
        with self.assertRaises(ValueError): await self.runtime.choose_job_target(self.run["id"], self.decision(value))

    async def test_board_change_does_not_expand_deletion(self):
        value = await self.waiting()
        self.jobs.upsert_job({**job(4), "external_id": "extra", "source_url": "http://127.0.0.1:8080/job.html?id=extra"})
        with self.assertRaises(ValueError): await self.runtime.choose_job_target(self.run["id"], self.decision(value))
        self.assertEqual(len(self.jobs.list_jobs()), 4)

    async def test_paused_choice_does_not_schedule_or_mutate_board(self):
        value = await self.waiting()
        value["paused"] = True
        self.runtime_store.save_plan(self.run["id"], value)
        answered = await self.runtime.choose_job_target(self.run["id"], self.decision(value))
        self.assertEqual(answered["status"], "waiting_user")
        self.assertFalse(self.scheduled)
        self.assertEqual(len(self.jobs.list_jobs()), 3)

    async def test_restart_preserves_pending_choices(self):
        value = await self.waiting()
        self.runtime.store = RunStore(Path(self.temp.name) / "runtime.db")
        answered = await self.runtime.choose_job_target(self.run["id"], self.decision(value))
        self.assertEqual(answered["checkpoint"]["plan"]["revision"], value["revision"]+1)

    async def test_cancelled_run_hides_and_rejects_choice(self):
        value = await self.waiting()
        self.runtime_store.update(self.run["id"], cancel_requested=True, status="cancelled")
        self.assertEqual(public_run(self.runtime_store.get(self.run["id"]))["target_choices"], [])
        with self.assertRaises(ValueError): await self.runtime.choose_job_target(self.run["id"], self.decision(value))

    async def test_no_pending_choice_rejected(self):
        with self.assertRaises(ValueError):
            await self.runtime.choose_job_target(self.run["id"], TargetChoiceDecision(conversation_id="default", choice_id="a"*32,
                step_id="keep", expected_revision=0, job_ids=[self.ids[0]]))

    async def test_existing_single_delivery_clarification_supports_click(self):
        self.context.owner = asyncio.current_task()
        self.model_result = {"status": "ambiguous", "matches": [
            {"ordinal": i, "evidence": [{"field": "company", "quote": "合成星海人工智能"}]} for i in (1, 2, 3)]}
        value = validate_draft(RequestDraft.model_validate(draft([
            step("keep", "mock_delivery_start", {"selector": "合成星海人工智能"})], ["keep"])),
            self.registry, "投递合成星海人工智能", dialogue=self.context.dialogue, conditions={}, stamp=STAMP)
        self.context.plan_reader = lambda: value
        await advance(value, self.registry)
        self.runtime_store.save_plan(self.run["id"], value)
        self.runtime_store.update(self.run["id"], status="waiting_user")
        model_calls_before_click = len(self.model_calls)
        answered = await self.runtime.choose_job_target(self.run["id"], self.decision(value))
        value = answered["checkpoint"]["plan"]
        self.context.plan_reader = lambda: value
        await advance(value, self.registry)
        self.assertEqual(self.calls[0]["job_ids"], [self.ids[1]])
        self.assertEqual(len(self.jobs.list_jobs()), 3)
        self.assertEqual(len(self.model_calls), model_calls_before_click)

    async def test_legacy_ambiguous_matching_does_not_offer_unsupported_multi_click(self):
        self.model_result = {"status": "ambiguous", "matches": [
            {"ordinal": i, "evidence": [{"field": "company", "quote": "合成星海人工智能"}]} for i in (1, 2)]}
        result = await self.call("mock_delivery_start", {"selector": "这家公司做RAG的", "selection_mode": "matching"})
        self.assertEqual(result.status, "awaiting_user")
        self.assertEqual(result.target_choice["selection_mode"], "single")
        self.assertFalse(self.calls)

    async def test_foreign_target_step_cannot_be_referenced(self):
        with self.assertRaises(PlanValidationError):
            bind_target_dependencies([step("deliver", "mock_delivery_start", {"target_step_id": "foreign"})])

    async def test_text_ordinal_choice_preserves_original_board_scope(self):
        value = await self.waiting()
        keep = next(s for s in value["steps"] if s["id"] == "keep")
        self.context.plan_reader = lambda: value
        self.context.action_authority = {"plan_id": value["id"], "step_id": "keep"}
        result = await self.call("job_target_resolve", {"scope": "board", "selector": "第2个", "snapshot_id": keep["target_choice"]["snapshot_id"]})
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.content["selected_ids"], [self.ids[1]])
        self.assertEqual(len(result.content["selection"]["scope_items"]), 3)


def main():
    suite = unittest.TestSuite(Choices(name) for name in Choices.__dict__ if name.startswith("test_"))
    buffer = io.StringIO()
    result = unittest.TextTestRunner(stream=buffer, verbosity=2).run(suite)
    output = Path(__file__).parent / "results" / "job-choices" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    (output / "tests.log").write_text(buffer.getvalue(), encoding="utf-8")
    (output / "traces.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False, default=str)+"\n" for row in TRACES), encoding="utf-8")
    summary = {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
               "real_model_calls": 0, "real_browser_actions": 0, "coverage": "real registry/DAG/SQLite/runtime decision + scripted target model"}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if not result.wasSuccessful(): print(buffer.getvalue()[-16000:])
    print(json.dumps({"output": str(output), **summary}))
    return int(not result.wasSuccessful())


if __name__ == "__main__": raise SystemExit(main())
