"""Synthetic contracts for silent reads and existing-browser attachment."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

import httpx

from app.browser.session import BrowserSession
from app.core.models import BrowserMode, DeliveryTaskView
from app.delivery.automation import DeliveryAutomationService
from app.delivery.mock_site_adapter import MockApplyObservation, MockSubmitResult
from evaluation.delivery_scheduling_contract import ReplyTurns


class BackgroundPolling(unittest.IsolatedAsyncioTestCase):
    async def test_http_sync_receives_reply_without_browser_or_marking_read(self):
        coordinator, _, calls = ReplyTurns().coordinator()
        job = {"stage": "applying", "score": 90, "recruiter_reply": ""}
        coordinator.settings = lambda: {"applicant_id": 31, "auto_submit": False}
        coordinator.jobs.job_id_for_source_url = lambda _: 2
        coordinator.jobs.get_job = lambda _: job
        coordinator.jobs.latest_delivery_status_for_job = lambda _: "waiting_for_reply"
        coordinator.jobs.mark_recruiter_reply = lambda _, reply: job.update(recruiter_reply=reply)
        requests = []
        def respond(request):
            requests.append((request.method, request.url.path))
            self.assertEqual(request.headers["X-User-Id"], "31")
            data = {
                "/api/messages/conversations": [{"job_id": 102, "user_id": 41}],
                "/api/applications/mine": [],
                "/api/messages/peek": [
                    {"sender_id": 31, "sender_role": "applicant", "created_at": "2026-10-11T01:00:00Z", "content": "synthetic greeting"},
                    {"sender_id": 41, "sender_role": "employer", "created_at": "2026-10-11T01:01:00Z", "content": "synthetic HR reply"}],
            }
            return httpx.Response(200, json=data[request.url.path])
        client_class = httpx.AsyncClient
        with patch("app.workflow.mock_coordinator.httpx.AsyncClient", side_effect=lambda **kw:
                   client_class(**kw, transport=httpx.MockTransport(respond))):
            outcome = await coordinator.sync_once()
        self.assertEqual(outcome["replied"], 1)
        self.assertEqual(job["recruiter_reply"], "synthetic HR reply")
        self.assertEqual(calls, [])
        self.assertEqual(requests, [("GET", "/api/messages/conversations"),
                                    ("GET", "/api/applications/mine"), ("GET", "/api/messages/peek")])

    async def test_no_reply_never_resumes_browser_worker(self):
        coordinator, rows, calls = ReplyTurns().coordinator()
        coordinator.jobs.get_job = lambda _: {"recruiter_reply": None}
        for _ in range(5):
            self.assertEqual(await coordinator.check_reply_queue(), {"checked": 0, "submitted": 0})
        self.assertEqual(calls, [])
        self.assertEqual(len(rows), 3)

    async def test_only_replied_waiter_gets_browser_slot(self):
        coordinator, _, calls = ReplyTurns().coordinator()
        coordinator.jobs.get_job = lambda i: {"recruiter_reply": "synthetic reply" if i == 2 else ""}
        self.assertEqual((await coordinator.check_reply_queue())["checked"], 1)
        self.assertEqual(calls, ["wait-2"])

    async def test_cancelled_replied_waiter_never_runs(self):
        coordinator, rows, calls = ReplyTurns().coordinator()
        coordinator.jobs.get_job = lambda _: {"recruiter_reply": "synthetic reply"}
        coordinator.jobs.mock_delivery_queue = lambda **_: [
            {"run_id": row["id"], "status": "cancelled"} for row in rows]
        await coordinator.check_reply_queue()
        self.assertEqual(calls, [])

    async def test_existing_browser_is_reused_without_launch_or_activation(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = BrowserSession(Path(tmp), headless=False, cdp_url="http://127.0.0.1:9223")
            session.mode = BrowserMode.CDP.value
            session.browser = NS(is_connected=lambda: True)
            session.context = object()
            session._launch_local_cdp_chrome = Mock(side_effect=AssertionError("no launch"))
            session.ensure_cdp_window = AsyncMock(side_effect=AssertionError("no focus"))
            self.assertEqual(await session.attach_existing_cdp(), BrowserMode.CDP.value)
            session._launch_local_cdp_chrome.assert_not_called()
            session.ensure_cdp_window.assert_not_called()

    async def test_missing_browser_is_reported_without_relaunch(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = BrowserSession(Path(tmp), headless=False, cdp_url="http://127.0.0.1:9223")
            session._resolve_cdp_websocket = Mock(side_effect=ConnectionError("synthetic closed"))
            session._launch_local_cdp_chrome = Mock(side_effect=AssertionError("no launch"))
            self.assertIsNone(await session.attach_existing_cdp())
            self.assertIsNone(session.playwright)
            session._launch_local_cdp_chrome.assert_not_called()

    async def test_disconnected_browser_reconnects_without_launch_permission(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = BrowserSession(Path(tmp), headless=False, cdp_url="http://127.0.0.1:9223")
            session.playwright = object()
            session._resolve_cdp_websocket = Mock(return_value="ws://127.0.0.1:9223/synthetic")
            session._connect_cdp_with_retry = AsyncMock(return_value=object())
            self.assertEqual(await session.attach_existing_cdp(), BrowserMode.CDP.value)
            session._connect_cdp_with_retry.assert_awaited_once_with(allow_launch=False)

    async def test_connection_retry_never_launches_closed_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = BrowserSession(Path(tmp), headless=False, cdp_url="http://127.0.0.1:9223")
            session._resolve_cdp_websocket = Mock(side_effect=ConnectionError("synthetic closed"))
            session._ensure_cdp_websocket = AsyncMock(side_effect=AssertionError("no launch"))
            with patch("app.browser.session.asyncio.sleep", new=AsyncMock()):
                with self.assertRaises(RuntimeError):
                    await session._connect_cdp_with_retry(allow_launch=False)
            session._ensure_cdp_websocket.assert_not_awaited()


class DeliveryForeground(unittest.IsolatedAsyncioTestCase):
    def service(self, directory, *, connected=True, page_present=True, reply=True):
        service = DeliveryAutomationService.__new__(DeliveryAutomationService)
        service.data_root = Path(directory)
        (service.data_root / "private").mkdir()
        url = "http://127.0.0.1:8080/job.html?id=1"
        service.state = DeliveryTaskView(id="synthetic", job_id=1, target_url=url,
            mock_applicant_id=31, status="waiting_for_reply", checkpoint="waiting_for_reply")
        service.gate = asyncio.Event(); service.gate.set()
        service._authorized_resume_payload = lambda: {"name": "Synthetic"}
        service._retry_or_fail = Mock(side_effect=AssertionError("no retry"))
        service.jobs = NS(get_job=lambda _: {"source_url": url}, mock_workflow_settings=lambda: {"applicant_id": 31},
            mark_delivery_waiting_reply=Mock(), mark_recruiter_reply=lambda *args: datetime.now(UTC).isoformat(),
            mark_delivery_completed=Mock(), mark_delivery_failed=Mock(), mark_outcome_indeterminate=Mock())
        def update(status, detail, **changes):
            service.state.status = status
            service.state.detail = detail
            for source, target in (("checkpoint", "checkpoint"), ("submitted", "submitted"), ("reason", "takeover_reason")):
                if source in changes: setattr(service.state, target, changes[source])
        service._update = update
        page = NS(url=url, evaluate=AsyncMock(return_value={"id":31, "role":"applicant"}))
        service.session = NS(attach_existing_cdp=AsyncMock(return_value="cdp" if connected else None),
            page_for_url=lambda _: page if page_present else None,
            ensure_cdp_browser=AsyncMock(), ensure_cdp_window=AsyncMock(), open=AsyncMock())
        observation = MockApplyObservation(True, None, reply, not reply, "send",
            hr_reply_text="synthetic reply" if reply else None)
        service.adapter = NS(normalize_job_url=lambda _: url, prepare=AsyncMock(return_value=observation),
            bind_resume=AsyncMock(return_value=NS(resume_id=1, applicant_id=31)),
            inspect=AsyncMock(return_value=observation),
            submit=AsyncMock(return_value=MockSubmitResult(True, False, "synthetic receipt")))
        return service

    async def test_reply_submission_does_not_activate_existing_tab(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self.service(tmp)
            await service._run_once(1, require_reply=True)
            self.assertEqual(service.state.status, "completed")
            self.assertTrue(service.state.submitted)
            service.adapter.submit.assert_awaited_once()
            service.session.ensure_cdp_window.assert_not_awaited()
            service.session.ensure_cdp_browser.assert_not_awaited()

    async def test_closed_browser_or_tab_waits_for_user_without_opening_window(self):
        for connected, page_present in ((False, False), (True, False)):
            with tempfile.TemporaryDirectory() as tmp:
                service = self.service(tmp, connected=connected, page_present=page_present)
                with patch("app.delivery.automation.asyncio.sleep", new=AsyncMock()):
                    await service._run_once(1, require_reply=True)
                self.assertEqual(service.state.status, "waiting_for_user")
                self.assertIn(service.state.takeover_reason, {"browser_unavailable", "page_unavailable"})
                service.session.ensure_cdp_window.assert_not_awaited()
                service.session.ensure_cdp_browser.assert_not_awaited()
                service.adapter.prepare.assert_not_awaited()
                service.adapter.submit.assert_not_awaited()

    async def test_live_reply_disappeared_never_submits_or_focuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self.service(tmp, reply=False)
            await service._run_once(1, require_reply=True)
            self.assertEqual(service.state.status, "waiting_for_reply")
            service.adapter.submit.assert_not_awaited()
            service.session.ensure_cdp_window.assert_not_awaited()

    async def test_first_start_can_open_login_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self.service(tmp, connected=False)
            service.state.mock_applicant_id = None
            await service._run_once(1, require_reply=True)
            service.session.ensure_cdp_browser.assert_awaited_once()
            service.session.ensure_cdp_window.assert_awaited_once()
            service.session.open.assert_awaited_once_with(BrowserMode.CDP)
            self.assertEqual(service.state.status, "completed")

    async def test_first_start_with_existing_page_is_also_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self.service(tmp)
            service.state.mock_applicant_id = None
            await service._run_once(1, require_reply=True)
            service.session.ensure_cdp_browser.assert_not_awaited()
            service.session.ensure_cdp_window.assert_not_awaited()
            self.assertEqual(service.state.status, "completed")

    async def test_verification_focuses_only_for_explicit_takeover(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self.service(tmp)
            verified = service.adapter.prepare.return_value
            service.adapter.prepare.side_effect = [MockApplyObservation(False, "verification", False, False, ""), verified]
            async def focus(_):
                asyncio.get_running_loop().call_soon(service.gate.set)
            service.session.ensure_cdp_window.side_effect = focus
            await service._run_once(1, require_reply=True)
            self.assertEqual(service.state.status, "completed")
            service.session.ensure_cdp_window.assert_awaited_once()

    async def test_page_recovery_does_not_relaunch_closed_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = BrowserSession(Path(tmp), headless=False, cdp_url="http://127.0.0.1:9223")
            session._resolve_cdp_websocket = Mock(side_effect=ConnectionError("synthetic closed"))
            session._ensure_cdp_websocket = AsyncMock(side_effect=AssertionError("no launch"))
            with patch("app.browser.session.asyncio.sleep", new=AsyncMock()):
                with self.assertRaises(RuntimeError):
                    await session._recover_cdp_page("http://127.0.0.1:8080/")
            session._ensure_cdp_websocket.assert_not_awaited()


def main():
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(
        unittest.defaultTestLoader.loadTestsFromTestCase(cls) for cls in (BackgroundPolling, DeliveryForeground)))
    output = Path("test-results/background-polling") / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    summary = {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
               "model_calls": 0, "browser_runs": 0, "real_submissions": 0}
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (output / "failures.json").write_text(json.dumps([
        {"test": str(test), "detail": detail} for test, detail in result.failures + result.errors], indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), **summary}))
    return int(not result.wasSuccessful())


if __name__ == "__main__":
    raise SystemExit(main())
