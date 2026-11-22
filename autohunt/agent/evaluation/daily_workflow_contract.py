"""Daily launch ownership and auto-submit opt-in, using temporary SQLite/fake pages.

No model calls, network, real browser actions or user data are used.
Run: .venv/Scripts/python.exe -m unittest evaluation.daily_workflow_contract -v
"""
from __future__ import annotations

import asyncio
import gc
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from app.browser.automation import BrowserAutomationService
from app.core.models import BrowserSearchRequest, ResumeDocumentView
from app.jobs.store import JobStore
from app.workflow.delivery_rules import build_rules
from app.workflow.mock_coordinator import BEIJING, MockWorkflowCoordinator
from app.workflow.rules import Observation, RuleEngine


class DailyWorkflow(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="autohunt-daily-")
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(gc.collect)
        self.root = Path(self.temp.name)
        self.jobs = JobStore(self.root / "jobs.db")
        self.settings(auto=False)
        self.document = ResumeDocumentView(id="synthetic", summary="Synthetic candidate", skills=["Python"])
        self.candidate = NS(read=lambda: NS(
            profile=NS(resume_id="synthetic", role_suggestions=["AI Engineer"]),
            preferences=NS(target_roles=NS(source="user_confirmed", values=["AI Engineer"]),
                           locations=NS(values=["深圳"]))
        ))
        self.browser = BrowserAutomationService(self.jobs, None, self.candidate, None, self.root,
            headless=True, resume_workspace=NS(read_document=lambda: self.document))
        self.browser._run_guarded = AsyncMock()
        self.coordinator = MockWorkflowCoordinator(self.jobs, self.browser, NS(worker=None), self.candidate, None)
        self.reply = "Synthetic HR reply"
        self.page = NS(url="http://127.0.0.1:8080/job.html?id=1")
        self.browser.session = NS(recover_page=AsyncMock(return_value=self.page),
                                  navigate=AsyncMock(return_value=self.page), disconnected=asyncio.Event())
        self.clicks = []
        self.on_prepare = self.on_bind = self.on_submit = None

        async def prepare(*args, **kwargs):
            if self.on_prepare:
                self.on_prepare()
            return NS(intervention_reason=None, hr_reply_text=self.reply, application_submitted=False)

        async def bind(*args, **kwargs):
            if self.on_bind:
                self.on_bind()
            return NS(resume_id=1)

        async def submit(*args, before_click, **kwargs):
            if self.on_submit:
                self.on_submit()
            before_click()
            self.clicks.append("submit")
            return NS(confirmed=True)

        self.browser.mock_delivery_adapter.prepare = AsyncMock(side_effect=prepare)
        self.browser.mock_delivery_adapter.bind_resume = AsyncMock(side_effect=bind)
        self.browser.mock_delivery_adapter.submit = AsyncMock(side_effect=submit)
        self.job_id, _ = self.jobs.upsert_job({
            "source": "MockJobSite", "external_id": "1", "source_url": self.page.url,
            "title": "Synthetic AI Engineer", "company": "Synthetic employer", "location": "深圳",
            "salary": "20-30K", "description": "Synthetic Python job", "tags": ["Python"],
            "score": 90, "decision": "REVIEW", "reasoning": "Synthetic match", "stage": "review",
            "filter_status": "pass", "filter_evidence": {"status": "pass"},
        })

    def settings(self, *, auto, daily=True):
        return self.jobs.update_mock_workflow_settings(monitor_enabled=True, auto_submit=auto,
            daily_enabled=daily, daily_time="09:00", min_score=75, max_contacts=3)

    def previous_run(self):
        claim = self.jobs.claim_daily_mock_run("2099-01-01")
        self.assertTrue(self.jobs.record_daily_mock_run("previous", "2099-01-01", claim))

    async def launch(self, *, daily=True):
        request = BrowserSearchRequest(source="mock", query="AI Engineer", dry_run=True,
                                       dry_run_simulate_reply=False)
        task = await self.browser.start(request, daily=daily)
        await self.browser.worker
        return task.id, request

    async def execute(self, launched=None, *, daily=True):
        task_id, request = launched or await self.launch(daily=daily)
        await self.browser._run_mock_delivery_dry_run(task_id, request, [self.job_id])
        return self.jobs.get_task(task_id)

    async def due(self, hour=10):
        with patch("app.workflow.mock_coordinator.datetime") as clock:
            clock.now.return_value = datetime(2099, 1, 2, hour, tzinfo=BEIJING)
            return await self.coordinator._run_daily_if_due()

    def test_failed_launch_releases_day_even_with_previous_success(self):
        self.previous_run()
        claim = self.jobs.claim_daily_mock_run("2099-01-02")
        self.jobs.release_daily_mock_run("2099-01-02", claim)
        reopened = JobStore(self.root / "jobs.db")
        self.assertEqual(reopened.mock_workflow_settings()["last_run_id"], "previous")
        self.assertTrue(reopened.claim_daily_mock_run("2099-01-02"))

    def test_claim_is_exclusive_and_stale_release_cannot_erase_retry(self):
        first = self.jobs.claim_daily_mock_run("2099-01-02")
        self.assertIsNone(self.jobs.claim_daily_mock_run("2099-01-02"))
        self.jobs.release_daily_mock_run("2099-01-02", first)
        second = self.jobs.claim_daily_mock_run("2099-01-02")
        self.jobs.release_daily_mock_run("2099-01-02", first)
        self.assertNotEqual(first, second)
        self.assertIsNone(self.jobs.claim_daily_mock_run("2099-01-02"))
        self.assertFalse(self.jobs.record_daily_mock_run("stale", "2099-01-02", first))
        self.assertTrue(self.jobs.record_daily_mock_run("current", "2099-01-02", second))

    def test_successful_start_cannot_be_released_or_repeated(self):
        claim = self.jobs.claim_daily_mock_run("2099-01-02")
        self.jobs.record_daily_mock_run("current", "2099-01-02", claim)
        self.jobs.release_daily_mock_run("2099-01-02", claim)
        self.assertIsNone(JobStore(self.root / "jobs.db").claim_daily_mock_run("2099-01-02"))

    def test_old_database_gains_nullable_claim_without_losing_settings(self):
        old = self.root / "old.db"
        with sqlite3.connect(old) as db:
            db.execute("CREATE TABLE mock_workflow_settings (id INTEGER PRIMARY KEY, applicant_id INTEGER, "
                "monitor_enabled INTEGER DEFAULT 0, auto_submit INTEGER DEFAULT 0, daily_enabled INTEGER DEFAULT 1, "
                "daily_time TEXT DEFAULT '09:00', min_score INTEGER DEFAULT 80, max_contacts INTEGER DEFAULT 2, "
                "last_run_date TEXT, last_run_id TEXT, last_error TEXT)")
            db.execute("INSERT INTO mock_workflow_settings (id,last_run_id) VALUES (1,'previous')")
        db.close()
        migrated = JobStore(old)
        self.assertEqual(migrated.mock_workflow_settings()["last_run_id"], "previous")
        self.assertIsNone(migrated.mock_workflow_settings()["daily_claim_id"])
        self.assertEqual(migrated.mock_workflow_settings()["max_search_jobs"], 30)
        self.assertEqual(migrated.mock_workflow_settings()["max_contacts"], 2)
        self.assertEqual(migrated.mock_workflow_settings()["min_score"], 80)
        self.assertTrue(migrated.claim_daily_mock_run("2099-01-02"))

    async def test_coordinator_retries_failed_start_then_deduplicates(self):
        self.previous_run()
        self.jobs.update_mock_workflow_settings(monitor_enabled=True, auto_submit=False,
            daily_enabled=True, daily_time="09:00", min_score=75, max_contacts=3, max_search_jobs=12)
        self.browser.start = AsyncMock(side_effect=[RuntimeError("synthetic startup failure"), NS(id="new")])
        with self.assertRaises(RuntimeError):
            await self.due()
        self.assertTrue(await self.due())
        self.assertFalse(await self.due())
        self.assertEqual(self.browser.start.await_count, 2)
        self.assertTrue(self.browser.start.call_args.kwargs["daily"])
        self.assertFalse(self.browser.start.call_args.args[0].dry_run_simulate_reply)
        self.assertEqual(self.browser.start.call_args.args[0].max_jobs, 12)
        self.assertEqual(self.jobs.mock_workflow_settings()["last_run_id"], "new")

    async def test_cancel_before_browser_launch_releases_only_its_claim(self):
        self.previous_run()
        self.browser.start = AsyncMock(side_effect=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError):
            await self.due()
        self.assertTrue(self.jobs.claim_daily_mock_run("2099-01-02"))

    async def test_recording_failure_after_launch_does_not_replay_side_effects(self):
        self.browser.start = AsyncMock(return_value=NS(id="started"))
        with patch.object(self.jobs, "record_daily_mock_run", side_effect=RuntimeError("synthetic db failure")):
            with self.assertRaises(RuntimeError):
                await self.due()
        self.assertFalse(await self.due())
        self.browser.start.assert_awaited_once()

    async def test_before_due_disabled_or_busy_does_not_claim(self):
        self.browser.start = AsyncMock()
        self.assertFalse(await self.due(hour=8))
        self.settings(auto=False, daily=False)
        self.assertFalse(await self.due())
        self.settings(auto=False)
        self.browser.worker = NS(done=lambda: False)
        self.assertFalse(await self.due())
        self.browser.start.assert_not_awaited()
        self.assertIsNone(self.jobs.mock_workflow_settings()["daily_claim_id"])

    async def test_daily_rule_reports_skips_truthfully(self):
        self.coordinator.run_daily_if_due = AsyncMock(return_value=False)
        result = await RuleEngine(build_rules(self.coordinator)).step(Observation(facts={"settings": {"daily_enabled": True}}))
        self.assertIn("未启动", result.result)

    async def test_daily_off_contacts_and_keeps_reply_without_upload_or_submit(self):
        task = await self.execute()
        self.assertEqual(task.dry_run_contacted, 1)
        self.assertEqual(task.dry_run_replied, 1)
        self.assertEqual(task.dry_run_submitted, 0)
        self.assertEqual(task.dry_run_failed, 0)
        self.assertEqual(self.jobs.get_job(self.job_id)["recruiter_reply"], self.reply)
        self.browser.mock_delivery_adapter.bind_resume.assert_not_awaited()
        self.browser.mock_delivery_adapter.submit.assert_not_awaited()
        self.assertIn("未点击发送简历", task.dry_run_report)

    async def test_daily_on_still_waits_for_actual_hr_reply(self):
        self.settings(auto=True)
        self.reply = ""
        task = await self.execute()
        self.assertEqual(task.dry_run_waiting_reply, 1)
        self.assertEqual(self.clicks, [])
        self.browser.mock_delivery_adapter.bind_resume.assert_not_awaited()

    async def test_daily_on_submits_with_bound_resume_and_reply(self):
        self.settings(auto=True)
        task = await self.execute()
        self.assertEqual(self.clicks, ["submit"])
        self.assertEqual(task.dry_run_submitted, 1)
        self.assertEqual(self.jobs.get_job(self.job_id)["stage"], "waiting")

    async def test_disable_while_waiting_for_reply_prevents_resume_upload(self):
        self.settings(auto=True)
        self.on_prepare = lambda: self.settings(auto=False)
        await self.execute()
        self.browser.mock_delivery_adapter.bind_resume.assert_not_awaited()
        self.assertEqual(self.clicks, [])

    async def test_disable_during_upload_prevents_click_without_unknown_outcome(self):
        self.settings(auto=True)
        self.on_bind = lambda: self.settings(auto=False)
        task = await self.execute()
        self.assertEqual(self.clicks, [])
        self.assertEqual(task.dry_run_submitted, 0)
        self.assertEqual(self.jobs.get_job(self.job_id)["stage"], "review")

    async def test_disable_immediately_before_click_is_respected(self):
        self.settings(auto=True)
        self.on_submit = lambda: self.settings(auto=False)
        await self.execute()
        self.assertEqual(self.clicks, [])

    async def test_reenabling_cannot_authorize_an_old_scheduled_run(self):
        self.settings(auto=True)
        launched = await self.launch()
        self.settings(auto=False)
        self.settings(auto=True)
        await self.execute(launched)
        self.assertEqual(self.clicks, [])
        self.browser.mock_delivery_adapter.bind_resume.assert_not_awaited()

    async def test_enabling_after_contact_only_launch_does_not_expand_its_authority(self):
        launched = await self.launch()
        self.settings(auto=True)
        await self.execute(launched)
        self.assertEqual(self.clicks, [])

    async def test_unrelated_setting_update_preserves_current_authority(self):
        self.settings(auto=True)
        launched = await self.launch()
        self.settings(auto=True)
        self.assertEqual((await self.execute(launched)).dry_run_submitted, 1)

    async def test_resume_changed_during_upload_never_clicks(self):
        self.settings(auto=True)
        self.on_bind = lambda: setattr(self.document, "version", 2)
        await self.execute()
        self.assertEqual(self.clicks, [])
        self.assertEqual(self.jobs.get_job(self.job_id)["stage"], "review")

    async def test_explicit_manual_dry_run_retains_its_separate_authorization(self):
        task = await self.execute(daily=False)
        self.assertEqual(self.clicks, ["submit"])
        self.assertEqual(task.dry_run_submitted, 1)

    async def test_revocation_after_first_click_retains_uncertain_outcome(self):
        self.settings(auto=True)
        async def interrupted(*args, before_click, **kwargs):
            before_click()
            self.clicks.append("first")
            self.settings(auto=False)
            before_click()
            self.clicks.append("second")
        self.browser.mock_delivery_adapter.submit = AsyncMock(side_effect=interrupted)
        task = await self.execute()
        self.assertEqual(self.clicks, ["first"])
        self.assertEqual(task.dry_run_failed, 1)
        self.assertEqual(self.jobs.get_job(self.job_id)["stage"], "outcome_indeterminate")


if __name__ == "__main__":
    unittest.main()
