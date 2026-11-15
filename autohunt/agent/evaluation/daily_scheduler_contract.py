"""Durable schedule/recovery contracts; isolated DBs and deterministic page receipts."""
from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from app.runtime.store import RunStore
from app.workflow.daily import DailyWorkflow
from app.core.models import BrowserTaskStatus
from evaluation import daily_workflow_contract as fixture


class Scheduler(unittest.IsolatedAsyncioTestCase):
    settings = fixture.DailyWorkflow.settings

    def setUp(self):
        fixture.DailyWorkflow.setUp(self)
        self.runs = RunStore(self.root / "runtime.db")
        self.now = datetime(2099, 1, 12, 2, tzinfo=UTC)
        self.daily = DailyWorkflow(self.jobs, self.runs, self.browser, NS(worker=None), self.candidate,
                                   NS(read_document=lambda: self.document), clock=lambda: self.now)
        self.browser.session.reuse = AsyncMock(return_value="cdp")
        self.browser.session.ensure_cdp_window = AsyncMock()
        self.browser._ensure_mock_access = AsyncMock(return_value=self.page)
        self.remote = {"contacted": False, "reply": "", "application": None}
        self.patcher = patch("app.workflow.daily_delivery.evidence", AsyncMock(side_effect=lambda *a: dict(self.remote)))
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.jobs.bind_mock_applicant(1)
        self.contacts = 0

        async def prepare(*args, before_communication=None):
            if not self.remote["contacted"]:
                if before_communication:
                    before_communication()
                self.contacts += 1
                self.remote["contacted"] = True
            return NS(intervention_reason=None, hr_reply_text=self.remote["reply"], application_submitted=False)
        self.browser.mock_delivery_adapter.prepare = AsyncMock(side_effect=prepare)

    def cursor(self, value):
        with self.jobs._connect() as db:
            db.execute("UPDATE mock_workflow_settings SET daily_next_due_at=? WHERE id=1", (value,))

    def prepared(self, *, auto=True, phase="pending"):
        self.settings(auto=auto)
        run = self.daily.enqueue("synthetic-test")
        snapshot = self.daily.snapshot()
        self.jobs.create_task("search", "mock", "AI Engineer", resume_id="synthetic")
        self.jobs.update_task("search", status=BrowserTaskStatus.COMPLETED)
        self.jobs.event("search", "job_stored", "Synthetic receipt", sample={"job_id": self.job_id, "filter_status": "pass"})
        cp = {"phase": "daily", "snapshot": snapshot, "browser_id": "search", "search_complete": True,
              "selected": [self.job_id], "jobs": {str(self.job_id): {"phase": phase}}, "attempts": 0,
              "daily_progress": {"id": run["id"], "revision": 1, "steps": []}}
        self.daily.save(run["id"], cp)
        return run["id"]

    async def execute(self, ident):
        await self.daily.execute(ident)
        return self.runs.get(ident)

    def test_search_count_is_persisted_independently_and_omitted_updates_preserve_it(self):
        from app.jobs.store import JobStore
        self.assertEqual(self.jobs.mock_workflow_settings()["max_search_jobs"], 30)
        self.jobs.update_mock_workflow_settings(monitor_enabled=True, auto_submit=True,
            daily_enabled=True, daily_time="09:00", min_score=75, max_contacts=5, max_search_jobs=2)
        before = self.jobs.mock_workflow_settings()
        self.assertEqual(self.daily.snapshot()["request"]["max_jobs"], 2)
        self.assertEqual(self.daily.snapshot()["max_contacts"], 5)
        reopened = JobStore(self.root / "jobs.db")
        self.assertEqual(reopened.mock_workflow_settings()["max_search_jobs"], 2)
        self.jobs.update_mock_workflow_settings(monitor_enabled=True, auto_submit=True,
            daily_enabled=True, daily_time="09:00", min_score=80, max_contacts=1)
        after = self.jobs.mock_workflow_settings()
        self.assertEqual(after["max_search_jobs"], 2)
        self.assertEqual(after["max_contacts"], 1)
        self.assertEqual(after["auto_submit_enabled_at"], before["auto_submit_enabled_at"])
        self.assertEqual(after["daily_next_due_at"], before["daily_next_due_at"])

    def test_search_count_rejects_invalid_values_before_saving(self):
        from app.core.models import MockWorkflowUpdate
        from pydantic import ValidationError
        settings = dict(monitor_enabled=True, auto_submit=False, daily_enabled=True,
                        daily_time="09:00", min_score=75, max_contacts=3)
        for value in (0, 201, -1, 1.5, True, "7", None):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    MockWorkflowUpdate(**settings, max_search_jobs=value)
                if value is not None:  # Internal None means keep the stored value.
                    with self.assertRaises(ValueError):
                        self.jobs.update_mock_workflow_settings(**settings, max_search_jobs=value)
        for value in (1, 200):
            self.jobs.update_mock_workflow_settings(**MockWorkflowUpdate(**settings, max_search_jobs=value).model_dump())
            self.assertEqual(self.daily.snapshot()["request"]["max_jobs"], value)

    async def test_all_daily_triggers_pass_saved_search_count_to_request(self):
        self.jobs.update_mock_workflow_settings(monitor_enabled=True, auto_submit=False,
            daily_enabled=True, daily_time="09:00", min_score=75, max_contacts=5, max_search_jobs=7)
        observed = []
        async def search(run_id, cp):
            observed.append(cp["snapshot"]["request"]["max_jobs"])
            self.jobs.create_task(cp["browser_id"], "mock", "Synthetic AI")
            self.jobs.update_task(cp["browser_id"], status=BrowserTaskStatus.COMPLETED)
            cp["search_complete"] = True
            self.daily.save(run_id, cp)
        with patch.object(self.daily, "search", side_effect=search), patch("app.workflow.daily_delivery.deliver", AsyncMock()):
            for trigger in ("manual", "scheduled", "catch_up"):
                run, _ = self.runs.create("default", "synthetic-" + trigger, "daily", {"trigger": trigger})
                self.assertEqual((await self.execute(run["id"]))["status"], "completed")
        self.assertEqual(observed, [7, 7, 7])
        self.assertTrue(all("本轮搜索上限为 7 个" in message["content"] for message in
            self.jobs.conversation_history() if message["run_id"].startswith("daily-search:")))
        self.assertEqual(self.contacts, 0)

    async def test_recovery_keeps_frozen_count_after_settings_change(self):
        self.jobs.update_mock_workflow_settings(monitor_enabled=True, auto_submit=True,
            daily_enabled=True, daily_time="09:00", min_score=75, max_contacts=3, max_search_jobs=7)
        ident = self.prepared()
        self.jobs.update_mock_workflow_settings(monitor_enabled=True, auto_submit=True,
            daily_enabled=True, daily_time="09:00", min_score=75, max_contacts=5, max_search_jobs=18)
        await self.daily.recover()
        result = await self.execute(ident)
        self.assertEqual(result["checkpoint"]["snapshot"]["request"]["max_jobs"], 7)
        self.assertEqual(result["checkpoint"]["snapshot"]["max_contacts"], 3)
        self.assertEqual(self.daily.snapshot()["request"]["max_jobs"], 18)
        self.assertEqual(self.daily.snapshot()["max_contacts"], 5)

    async def test_search_summary_is_bound_and_published_before_hr_reply(self):
        ident = self.prepared()
        await self.execute(ident)
        messages = self.jobs.conversation_history()
        self.assertEqual(len(messages), 2)
        self.assertIn("本轮核对 1 个候选", messages[0]["content"])
        self.assertIn("### 整体薪资与 JD 总结", messages[0]["content"])
        self.assertIn("Synthetic AI Engineer", messages[0]["content"])
        self.assertIn("1 个正在等待招聘方回复", messages[1]["content"])
        state = self.runs.dialogue_state("default")
        self.assertEqual(state["tasks"]["browser_id"], "search")
        self.assertEqual(state["snapshots"][-1]["items"][0]["id"], self.job_id)
        await self.execute(ident)
        self.assertEqual(len(self.jobs.conversation_history()), 2)
        self.remote["reply"] = "Synthetic HR reply"
        await self.execute(ident)
        self.assertIn("投递 1 个", self.jobs.conversation_history()[-1]["content"])
        self.assertEqual(len(self.jobs.conversation_history()), 3)

    async def test_contact_limit_does_not_shrink_search_candidate_pool(self):
        import copy
        ident = self.prepared()
        cp = self.runs.get(ident)["checkpoint"]
        cp["snapshot"]["max_contacts"] = 5
        self.assertEqual(cp["snapshot"]["request"]["max_jobs"], 30)
        cp.pop("selected")
        cp["jobs"] = {}
        self.daily.save(ident, cp)
        report = self.jobs.search_report("search")
        base = report["jobs"][0]
        report.update(covered_jobs=30, qualified_jobs=30,
                      jobs=[{**copy.deepcopy(base), "id": i, "score": 90 if i <= 6 else 70} for i in range(1, 31)])
        with patch.object(self.jobs, "search_report", return_value=report), patch("app.workflow.daily_delivery.deliver", AsyncMock()):
            run = await self.execute(ident)
        self.assertEqual(run["checkpoint"]["selected"], [1, 2, 3, 4, 5])
        message = self.jobs.conversation_history()[0]["content"]
        self.assertIn("本轮核对 30 个候选，其中 6 个达到 75 分", message)
        self.assertIn("自动沟通上限为 5 个，本轮选中 5 个", message)
        self.assertEqual(self.contacts, 0)

    async def test_terminal_recovery_backfills_summary_once_without_reexecution(self):
        ident = self.prepared()
        self.runs.update(ident, status="completed")
        await self.daily.recover()
        await self.daily.recover()
        self.assertEqual(len(self.jobs.conversation_history()), 1)
        self.assertEqual(self.runs.dialogue_state("default")["tasks"]["browser_id"], "search")
        self.assertEqual(self.runs.get(ident)["status"], "completed")
        self.assertEqual(self.daily.records(active=True), [])
        self.assertEqual(self.contacts, 0)
        self.assertEqual(self.clicks, [])

    async def test_backfill_preserves_another_search_and_session_isolation(self):
        ident = self.prepared()
        self.runs.update(ident, status="completed")
        self.runs.mutate_dialogue("default", {"op": "task", "source": "synthetic", "kind": "browser_id", "id": "newer-search", "bind": True})
        await self.daily.recover()
        self.assertEqual(self.runs.dialogue_state("default")["tasks"]["browser_id"], "newer-search")
        self.assertEqual(self.runs.dialogue_state("default")["snapshots"], [])
        other = self.runs.create_session("Synthetic other session")["id"]
        self.assertEqual(self.runs.dialogue_state(other)["tasks"], {})
        self.assertEqual(self.jobs.conversation_history(conversation_id=other), [])

    async def test_recovery_respects_context_reset_epoch(self):
        ident = self.prepared()
        self.runs.update(ident, status="completed")
        self.runs.mutate_dialogue("default", {"op": "reset", "source": "synthetic"})
        await self.daily.recover()
        self.assertEqual(self.jobs.conversation_history(), [])
        self.assertEqual(self.runs.dialogue_state("default")["tasks"], {})

    async def test_search_publication_crash_does_not_duplicate_persisted_message(self):
        ident = self.prepared()
        cp = self.runs.get(ident)["checkpoint"]
        original = self.runs.event
        def event(run_id, kind, data):
            if kind == "browser_summary":
                raise RuntimeError("synthetic crash before event")
            return original(run_id, kind, data)
        with patch.object(self.runs, "event", side_effect=event):
            with self.assertRaises(RuntimeError):
                self.daily.publish_search_summary(ident, cp)
        self.daily.publish_search_summary(ident, cp)
        self.assertEqual(len(self.jobs.conversation_history()), 1)
        self.assertTrue(self.runs.has_event(ident, "browser_summary"))

    async def test_followup_summary_reads_daily_results_with_explicit_local_accounting(self):
        from app.core.main_agent import AutoHuntAgent
        from app.runtime.context import RunContext, current_run
        ident = self.prepared()
        self.daily.publish_search_summary(ident, self.runs.get(ident)["checkpoint"])
        agent = AutoHuntAgent.__new__(AutoHuntAgent)
        agent.job_store = self.jobs
        chat, _ = self.runs.create("default", "summary-followup", "chat", {})
        token = current_run.set(RunContext(chat["id"], asyncio.current_task(),
            lambda kind, data: self.runs.event(chat["id"], kind, data), lambda value: None,
            dialogue=self.runs.dialogue_state("default"),
            mutate_dialogue=lambda value: self.runs.mutate_dialogue("default", value)))
        try:
            result = agent._search_summary_response()
        finally:
            current_run.reset(token)
        self.assertIn("Synthetic AI Engineer", result.message)
        self.assertNotIn("还没有可核对", result.message)
        self.runs.update(chat["id"], status="completed")
        ledger = self.runs.execution_ledger(chat["id"])
        self.assertEqual(ledger["execution_path"]["mode"], "local_rule")
        self.assertEqual(ledger["execution_path"]["source_browser_id"], "search")
        self.assertEqual(ledger["calls"], [])
        self.assertEqual(len(ledger["tools"]), 1)
        self.assertEqual(ledger["tools"][0]["tool"], "job_results_read")
        self.assertIsNotNone(ledger["tools"][0]["duration_ms"])
        self.assertEqual(self.contacts, 0)

    def test_missed_days_coalesce_and_persist_next_occurrence(self):
        self.cursor("2099-01-01T01:00:00+00:00")
        self.daily.materialize_due()
        rows = self.daily.records()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload"]["coalesced_occurrences"], 12)
        self.assertEqual(rows[0]["payload"]["trigger"], "catch_up")
        self.assertEqual(self.jobs.mock_workflow_settings()["daily_next_due_at"], "2099-01-13T01:00:00+00:00")
        self.daily.materialize_due()
        self.assertEqual(len(self.daily.records()), 1)

    def test_crash_between_run_creation_and_cursor_commit_is_idempotent(self):
        self.cursor("2099-01-12T01:00:00+00:00")
        self.now = datetime(2099, 1, 12, 1, 0, tzinfo=UTC)
        with patch.object(self.jobs, "advance_daily_schedule", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.daily.materialize_due()
        self.now += timedelta(minutes=20)
        self.daily.materialize_due()
        self.assertEqual(len(self.daily.records()), 1)

    def test_before_time_and_disabled_do_not_enqueue(self):
        self.cursor("2099-01-12T03:00:00+00:00")
        self.daily.materialize_due()
        self.settings(auto=False, daily=False)
        self.daily.materialize_due()
        self.assertEqual(self.daily.records(), [])

    def test_manual_test_does_not_consume_daily_slot_and_http_retry_is_idempotent(self):
        self.cursor("2099-01-13T01:00:00+00:00")
        one = self.daily.enqueue("same-request")
        self.assertEqual(one["id"], self.daily.enqueue("same-request")["id"])
        self.assertEqual(self.jobs.mock_workflow_settings()["daily_next_due_at"], "2099-01-13T01:00:00+00:00")

    async def test_waiting_reply_survives_restart_without_second_greeting(self):
        ident = self.prepared()
        self.assertEqual((await self.execute(ident))["status"], "waiting_reply")
        self.assertEqual(self.contacts, 1)
        await self.daily.recover()
        self.assertEqual((await self.execute(ident))["status"], "waiting_reply")
        self.assertEqual(self.contacts, 1)

    async def test_reply_submits_once_and_records_tools_in_unified_ledger(self):
        ident = self.prepared()
        await self.execute(ident)
        self.remote["reply"] = "Please send the approved resume"
        result = await self.execute(ident)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.clicks, ["submit"])
        ledger = self.runs.execution_ledger(ident)
        self.assertEqual(len(ledger["tools"]), 2)
        self.assertTrue(ledger["steps"])
        await self.daily.recover()
        self.assertFalse(self.daily.records(active=True))

    async def test_auto_off_contacts_but_never_uploads_or_sends(self):
        ident = self.prepared(auto=False)
        self.remote["reply"] = "Reply"
        run = await self.execute(ident)
        self.assertEqual(run["checkpoint"]["jobs"][str(self.job_id)]["phase"], "withheld")
        self.assertEqual(self.contacts, 1)
        self.assertEqual(self.clicks, [])
        self.browser.mock_delivery_adapter.bind_resume.assert_not_awaited()

    async def test_contact_intent_without_receipt_is_not_replayed(self):
        ident = self.prepared(phase="contact_started")
        run = await self.execute(ident)
        self.assertEqual(run["status"], "outcome_indeterminate")
        self.assertEqual(self.contacts, 0)

    async def test_contact_intent_with_receipt_recovers_without_replay(self):
        ident = self.prepared(phase="contact_started")
        self.remote["contacted"] = True
        self.assertEqual((await self.execute(ident))["status"], "waiting_reply")
        self.assertEqual(self.contacts, 0)

    async def test_submission_intent_without_receipt_does_not_reclick(self):
        ident = self.prepared(phase="submit_started")
        self.assertEqual((await self.execute(ident))["status"], "outcome_indeterminate")
        self.assertEqual(self.clicks, [])

    async def test_submission_intent_with_exact_receipt_is_reconciled(self):
        from app.delivery.resume_binding import mock_resume_payload
        from app.delivery.reconciliation import resume_fingerprint
        ident = self.prepared(phase="submit_started")
        cp = self.runs.get(ident)["checkpoint"]
        cp["jobs"][str(self.job_id)]["mock_resume_id"] = 71
        binding = cp["snapshot"]["binding"]
        payload = mock_resume_payload(self.document, document_id=binding["resume_document_id"],
                                      version=binding["resume_version"], digest=binding["resume_digest"])
        cp["jobs"][str(self.job_id)]["mock_resume_digest"] = resume_fingerprint(payload)
        self.daily.save(ident, cp)
        application = {"id": 91, "job_id": 1, "applicant_id": 1, "resume_id": 71}
        self.remote.update(application=application, applications=[application], account={"id": 1, "role": "applicant"},
                           resumes=[{**payload, "id": 71, "applicant_id": 1}])
        run = await self.execute(ident)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(self.clicks, [])

    async def test_mismatched_receipt_stays_unknown(self):
        ident = self.prepared(phase="submit_started")
        self.remote["application"] = {"resume_id": 72}
        self.assertEqual((await self.execute(ident))["status"], "outcome_indeterminate")

    async def test_cancel_is_durable_and_not_recovered(self):
        ident = self.prepared()
        await self.daily.cancel(ident)
        await self.daily.recover()
        self.assertEqual(self.runs.get(ident)["status"], "cancelled")
        self.assertEqual(self.daily.records(active=True), [])

    async def test_resume_version_change_blocks_actions(self):
        ident = self.prepared()
        self.document.version += 1
        self.assertEqual((await self.execute(ident))["status"], "failed")
        self.assertEqual(self.contacts, 0)
        self.assertEqual(self.clicks, [])

    async def test_turning_auto_off_while_waiting_prevents_upload(self):
        ident = self.prepared()
        await self.execute(ident)
        self.settings(auto=False)
        self.remote["reply"] = "Reply"
        run = await self.execute(ident)
        self.assertEqual(run["checkpoint"]["jobs"][str(self.job_id)]["phase"], "withheld")
        self.assertEqual(self.clicks, [])

    async def test_reply_timeout_is_durable_and_never_resends(self):
        ident = self.prepared()
        await self.execute(ident)
        self.now += timedelta(seconds=self.browser.watch_limits.reply + 1)
        await self.daily.recover()
        run = await self.execute(ident)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["checkpoint"]["jobs"][str(self.job_id)]["phase"], "withheld")
        self.assertEqual(self.contacts, 1)
        self.assertEqual(self.clicks, [])

    async def test_revoke_during_upload_does_not_click(self):
        ident = self.prepared()
        self.remote["reply"] = "Reply"
        self.on_bind = lambda: self.settings(auto=False)
        run = await self.execute(ident)
        self.assertEqual(run["checkpoint"]["jobs"][str(self.job_id)]["phase"], "withheld")
        self.assertEqual(self.clicks, [])

    async def test_reenable_does_not_reauthorize_waiting_daily_run(self):
        ident = self.prepared()
        await self.execute(ident)
        self.settings(auto=False)
        self.settings(auto=True)
        self.remote["reply"] = "Reply"
        await self.execute(ident)
        self.browser.mock_delivery_adapter.bind_resume.assert_not_awaited()
        self.assertEqual(self.clicks, [])

    async def test_ordinary_polling_does_not_take_daily_submission_authority(self):
        ident = self.prepared()
        await self.execute(ident)
        self.jobs.mark_recruiter_reply(self.job_id, "Reply")
        self.coordinator.daily = self.daily
        observation = await self.coordinator.observe(with_site=False)
        self.assertEqual(observation.get("reply_candidates"), [])
        names = [r.name for r in self.coordinator.engine.matching(observation)]
        self.assertIn("daily_report", names)
        self.assertNotIn("submit_authorised_resume", names)

    async def test_crashed_search_reuses_id_and_restores_server_request(self):
        ident = self.prepared(auto=False)
        cp = self.runs.get(ident)["checkpoint"]
        cp.pop("search_complete")
        self.daily.save(ident, cp)
        self.jobs.update_task("search", status=BrowserTaskStatus.STOPPED)
        async def complete(task_id, request):
            self.assertEqual(task_id, "search")
            self.assertFalse(request.dry_run)
            self.jobs.update_task(task_id, status=BrowserTaskStatus.COMPLETED)
        self.browser._run_guarded.side_effect = complete
        run = await self.execute(ident)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["checkpoint"]["browser_id"], "search")
        self.assertTrue(self.jobs.search_recovery("search")["request"])
        self.assertEqual(run["checkpoint"]["daily_progress"]["steps"][0]["status"], "completed")

    async def test_read_failures_have_persistent_bounded_retry_budget(self):
        ident = self.prepared()
        cp = self.runs.get(ident)["checkpoint"]
        cp.pop("search_complete")
        self.daily.save(ident, cp)
        with patch.object(self.daily, "search", AsyncMock(side_effect=ConnectionError("synthetic outage"))):
            for _ in range(2):
                self.assertEqual((await self.execute(ident))["status"], "queued")
                await self.daily.recover()
            run = await self.execute(ident)
        self.assertEqual(run["status"], "failed")
        self.assertIn("synthetic outage", run["error"])
        self.assertEqual(len(self.runs.execution_ledger(ident)["issues"]), 3)
        self.assertEqual(self.contacts, 0)

    async def test_daily_progress_is_visible_but_not_an_editable_chat_plan(self):
        from app.runtime.manager import AgentRuntime
        from app.runtime.models import public_run
        ident = self.prepared()
        run = await self.execute(ident)
        self.assertTrue(public_run(run)["plan_steps"])
        self.assertTrue(self.runs.execution_ledger(ident)["steps"])
        runtime = AgentRuntime.__new__(AgentRuntime)
        runtime.store = self.runs
        self.assertIsNone(runtime.active_plan("default"))
        self.assertIsNone(runtime.plan_for_revision("default", ident))


if __name__ == "__main__":
    unittest.main()
