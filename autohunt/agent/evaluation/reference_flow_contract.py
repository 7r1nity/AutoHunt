"""Reference-to-final-state regressions using real registry/DAG/temp SQLite.

Replays malformed model selectors and mixed actions. No external LLM/browser.
"""
from __future__ import annotations

import asyncio
import copy
import io
import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS

from app.core.job_followups import job_followup_request
from app.runtime.dialogue import apply_operation, resolve_reference
from app.runtime.job_references import instruction_reference
from app.workflow.execution import begin, completion_steps, step_receipt
from app.workflow.planning import RequestDraft, validate_draft
from app.workflow.presentation import compose
from .goal_graph_contract import STAMP, draft, step
from .job_target_contract import Contracts as TargetFixtures, TRACES
from .search_delivery_contract import job
from .reporting_contract import RuntimePublicationContracts


class References(TargetFixtures):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.ids = []
        for index in range(1, 14):
            company = {9: "合成天工开物", 10: "合成星云科技", 13: "合成远景"}.get(index, f"合成企业{index}")
            value = {**job(100+index), "title": f"合成岗位{index}", "company": company, "location": "深圳"}
            self.ids.append(self.jobs.upsert_job(value)[0])
        self.snapshot_items = [{"id": ident, **{key: self.jobs.get_job(ident)[key] for key in ("title", "company", "score")},
            "content_hash": self.jobs.review_fingerprint(self.jobs.get_job(ident))} for ident in self.ids]
        self.state = apply_operation({}, {"op": "snapshot", "topic": "applications", "kind": "job", "source": "synthetic",
            "items": self.snapshot_items, "displayed": True})
        self.context.dialogue = self.state
        self.registry.response_llm = None

    async def test_bare_link_reference_matrix_uses_list_position_not_db_id(self):
        for message, selector in (("给我13的链接", "13"), ("第十三个的链接", "第十三个"),
                                 ("发我 13 的网址", "13"), ("查看13详情", "13"), ("13链接", "13")):
            with self.subTest(message=message):
                self.context.user_message = message
                result = await self.call("job_detail_read", {"selector": selector, "view": "link"})
                self.assertEqual(result.status, "ok", result.content)
                self.assertEqual(result.content["job_id"], self.ids[12])
                self.assertNotEqual(result.content["job_id"], 13)
                self.assertIn("http://127.0.0.1:8080", result.content["message"])
                self.assertIn("](", result.content["message"])

    async def test_explicit_database_id_only_matches_displayed_id(self):
        self.context.user_message = f"给我ID:{self.ids[9]}的链接"
        result = await self.call("job_detail_read", {"selector": str(self.ids[9]), "view": "link"})
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.content["job_id"], self.ids[9])

    async def test_explicit_source_snapshot_survives_reference_normalization(self):
        source = self.state["snapshots"][-1]["id"]
        self.state = apply_operation(self.state, {"op": "snapshot", "topic": "applications", "kind": "job",
            "source": "new-list", "items": self.snapshot_items[:1], "displayed": True})
        self.context.dialogue = self.state
        self.context.user_message = "给我13的链接"
        result = await self.call("job_detail_read", {"selector": "13", "snapshot_id": source, "view": "link"})
        self.assertEqual(result.status, "ok", result.content)
        self.assertEqual(result.content["job_id"], self.ids[12])
        self.assertEqual(result.content["snapshot_id"], source)

    async def test_mixed_original_message_does_not_force_first_ordinal_on_link(self):
        for message in ("移除第九个天工开物，投递10星云科技，并且给我13的链接", "移除第九个天工开物，然后给我13的链接"):
            self.context.user_message = message
            result = await self.call("job_detail_read", {"selector": "13", "view": "link"})
            self.assertEqual(result.status, "ok")
            self.assertEqual(result.content["job_id"], self.ids[12])

    async def test_model_answer_omitting_requested_link_cannot_drop_it(self):
        self.context.user_message = "给我13的链接"
        result = await self.call("job_detail_read", {"selector": "13", "view": "link"})
        steps = [{"id": "link", "tool": "job_detail_read", "status": "completed", "result": result.content},
                 {"id": "stats", "tool": "job_stats_read", "status": "completed", "result": {"count": 13}}]
        async def generate(_):
            return NS(text=json.dumps({"sections": [{"step_id": "stats", "kind": "summary", "text": "合成统计", "job_ids": []}]}))
        answer = await compose({"steps": steps, "goals": []}, steps, NS(generate=generate))
        self.assertEqual(answer.mode, "model")
        self.assertIn(self.jobs.get_job(self.ids[12])["source_url"], answer.text)

    async def test_model_dropped_delivery_ordinal_is_restored_and_name_verified(self):
        self.context.user_message = "投递10星云科技"
        result = await self.call("mock_delivery_start", {"selector": "星云科技"})
        self.assertEqual(result.status, "ok", result.content)
        self.assertEqual(self.calls[0]["job_ids"], [self.ids[9]])
        self.assertFalse(self.model_calls)

    async def test_conflicting_name_and_ordinal_ask_before_delivery(self):
        self.context.user_message = "投递9星云科技"
        result = await self.call("mock_delivery_start", {"selector": "星云科技"})
        self.assertEqual(result.status, "awaiting_user")
        self.assertIn("不一致", result.content)
        self.assertFalse(self.calls)

    async def test_detail_conflict_and_out_of_range_are_input_waits(self):
        for selector in ("第9个星云科技", "99"):
            self.context.user_message = "给我"+selector+"的链接"
            result = await self.call("job_detail_read", {"selector": selector, "view": "link"})
            self.assertEqual(result.status, "awaiting_user")

    async def test_mixed_three_branch_replay_checks_final_database_and_reply(self):
        self.context.owner = asyncio.current_task()
        message = "移除第九个天工开物 投递10星云科技 并且给我13的链接"
        steps = [step("remove", "job_remove_selection", {"selector": "第九个 天工开物"}),
                 step("deliver", "mock_delivery_start", {"selector": "星云科技"}),
                 step("link", "job_detail_read", {"selector": "13", "view": "link"})]
        for value, quote in zip(steps, ("移除第九个天工开物", "投递10星云科技", "给我13的链接")):
            value["command_quote"] = quote
        value = validate_draft(RequestDraft.model_validate(draft(steps, ["remove", "deliver", "link"])),
            self.registry, message, dialogue=self.context.dialogue, conditions={}, stamp=STAMP)
        self.context.plan_reader = lambda: value
        response = await begin(value, self.registry)
        self.assertTrue(self.jobs.get_job(self.ids[8])["removed_at"])
        self.assertFalse(self.jobs.get_job(self.ids[9])["removed_at"])
        self.assertEqual(self.calls[0]["job_ids"], [self.ids[9]])
        self.assertEqual(value["steps"][2]["result"]["job_id"], self.ids[12])
        self.assertTrue(all(s["status"] == "completed" for s in value["steps"]))
        self.assertIn("http://127.0.0.1:8080", response.message)
        selection = next(e["data"] for e in self.events if e["kind"] == "plan_response_selection")
        self.assertEqual(completion_steps(value, [{"kind": "response", "data": selection}]), [])
        self.assertEqual(len(self.calls), 1)

    async def test_name_starting_with_numeral_is_not_forced_to_ordinal(self):
        self.state["snapshots"][-1]["items"][0]["company"] = "三一重工（合成）"
        selected = resolve_reference(self.state, kind="job", selector="三一重工（合成）", read_only=True)
        self.assertEqual(selected["items"][0]["id"], self.ids[0])
        self.assertIsNone(instruction_reference("投递三一重工（合成）"))
        self.assertIsNone(instruction_reference("投递3D工程师"))
        self.assertIsNone(instruction_reference("投递85分以上的"))

    async def test_information_shortcut_never_swallows_mixed_actions(self):
        for message in ("移除9投递10给我13的链接", "投递10星云科技 并且给我13的链接", "移除第9个天工开物，然后给我13的链接"):
            self.assertIsNone(job_followup_request(message, self.state))
            self.assertIsNone(instruction_reference(message))

    async def test_object_first_read_with_no_send_keeps_reference(self):
        self.context.user_message = "10星云科技只给链接，不要投递"
        result = await self.call("job_detail_read", {"selector": "星云科技", "view": "link"})
        self.assertEqual(result.status, "ok", result.content)
        self.assertEqual(result.content["job_id"], self.ids[9])
        self.assertFalse(self.calls)

    async def test_prepared_target_not_rejected_by_legacy_ordinal_guard(self):
        self.context.owner = asyncio.current_task()
        steps = [step("target", "job_target_resolve", {"selector": "第十个星云科技"}),
                 step("delivery", "mock_delivery_start", {"target_step_id": "target"})]
        for item in steps: item["command_quote"] = "自动投递第十个星云科技"
        plan = validate_draft(RequestDraft.model_validate(draft(steps, ["delivery"])), self.registry,
            "自动投递第十个星云科技", dialogue=self.context.dialogue, conditions={}, stamp=STAMP)
        self.context.plan_reader = lambda: plan
        await begin(plan, self.registry)
        self.assertEqual(self.calls[0]["job_ids"], [self.ids[9]])

    async def test_third_party_semantics_is_not_an_ordinal(self):
        from app.runtime.job_references import job_reference
        for text in ("第三方或者外包岗位", "第三方", "第一银行", "三一重工", "85分以上", "15k以上", "15.5k以上"):
            self.assertIsNone(job_reference(text), text)


class Receipts(unittest.TestCase):
    def setUp(self):
        self.step = {"id": "link", "tool": "job_detail_read", "status": "blocked", "execution_id": "attempt-one",
                     "arguments": {"selector": "13"}, "detail": "合成失败", "failure": {"code": "invalid_arguments"}}
        self.plan = {"id": "synthetic", "steps": [self.step]}
        self.event = {"kind": "response", "data": {"plan_id": "synthetic", "presented_step_receipts": [step_receipt(self.step)]}}

    def test_foreground_failure_not_repeated_by_background(self):
        self.assertEqual(completion_steps(self.plan, [self.event]), [])

    def test_new_attempt_with_same_failure_remains_visible(self):
        self.step["execution_id"] = "attempt-two"
        self.assertEqual(completion_steps(self.plan, [self.event]), [self.step])

    def test_changed_outcome_or_other_step_is_not_suppressed(self):
        self.step["detail"] = "不同失败"
        self.assertEqual(completion_steps(self.plan, [self.event]), [self.step])

    def test_other_plan_does_not_suppress_receipt(self):
        self.event["data"]["plan_id"] = "other"
        self.assertEqual(completion_steps(self.plan, [self.event]), [self.step])

    def test_legacy_response_uses_recorded_attempt_not_tool_name_only(self):
        events = [{"kind": "plan_state", "data": {"plan": copy.deepcopy(self.plan)}},
                  {"kind": "response", "data": {"plan_id": "synthetic", "result": {"tool_events": [
                    {"tool": "job_detail_read", "status": "blocked", "detail": "合成失败"}]}}}]
        self.assertEqual(completion_steps(self.plan, events), [])
        self.step["execution_id"] = "attempt-two"
        self.assertEqual(completion_steps(self.plan, events), [self.step])


class Watcher(RuntimePublicationContracts):
    async def test_real_watcher_does_not_publish_already_reported_failure(self):
        failed = {"id": "link", "tool": "job_detail_read", "arguments": {"selector": "13"}, "status": "blocked",
                  "execution_id": "synthetic-attempt", "detail": "合成无效序号", "depends_on": []}
        self.plan["steps"] = [failed]
        self.store.event(self.run["id"], "response", {"plan_id": self.plan["id"], "presented_step_receipts": [step_receipt(failed)]})
        await self.complete()
        self.assertEqual(self.store.get(self.run["id"])["status"], "failed")
        self.assertFalse(self.store.has_event(self.run["id"], "workflow_summary"))
        self.assertEqual(self.messages, {})


def main():
    suite = unittest.TestSuite(cls(name) for cls in (References, Receipts, Watcher) for name in cls.__dict__ if name.startswith("test_"))
    buffer = io.StringIO()
    result = unittest.TextTestRunner(stream=buffer, verbosity=2).run(suite)
    output = Path(__file__).parent / "results" / "reference-flow" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    (output / "tests.log").write_text(buffer.getvalue(), encoding="utf-8")
    (output / "traces.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False, default=str)+"\n" for row in TRACES), encoding="utf-8")
    summary = {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
               "external_model_calls": 0, "real_browser_actions": 0, "coverage": "malformed model draft + real registry/DAG/temp SQLite/final outcomes/watcher"}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if not result.wasSuccessful(): print(buffer.getvalue()[-12000:])
    print(json.dumps({"output": str(output), **summary}))
    return int(not result.wasSuccessful())


if __name__ == "__main__": raise SystemExit(main())
