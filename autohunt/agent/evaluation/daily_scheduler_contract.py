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
        cp = {"phase": "daily", "snapshot": snapshot, "browser_id": "search", "search_complete": True,
              "selected": [self.job_id], "jobs": {str(self.job_id): {"phase": phase}}, "attempts": 0,
              "daily_progress": {"id": run["id"], "revision": 1, "steps": []}}
        self.daily.save(run["id"], cp)
        return run["id"]

    async def execute(self, ident):
        await self.daily.execute(ident)
        return self.runs.get(ident)

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
