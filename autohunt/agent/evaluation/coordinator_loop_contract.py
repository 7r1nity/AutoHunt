"""Actual coordinator loop + rule engine scheduling, with synthetic adapters.

The real 30-second polling interval and 0.25-second ready delay are unchanged.
No browser, model, existing data, network or recruitment operation is used.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS
import time
import unittest

from app.workflow.mock_coordinator import MockWorkflowCoordinator


class LoopFixture:
    def __init__(self, count=3, *, replied=True):
        self.rows = [{"id": f"synthetic-wait-{i}", "job_id": i + 1, "preview_id": f"synthetic-preview-{i}",
            "action": "submit_resume", "status": "waiting_for_reply", "submitted": False,
            "last_checked_at": None, "observed_reply": "Synthetic HR reply" if replied else ""}
            for i in range(count)]
        self.queue = [{"job_id": row["job_id"], "run_id": row["id"], "status": "dispatched", "session_id": "synthetic",
            "binding": {"resume_document_id": "synthetic-master", "resume_version": 1, "resume_digest": "synthetic-digest"}}
            for row in self.rows]
        self.job_rows = {row["job_id"]: {"id": row["job_id"], "recruiter_reply": row["observed_reply"],
            "recruiter_reply_at": (datetime.now(UTC) - timedelta(minutes=2)).isoformat()} for row in self.rows}
        self.calls, self.errors, self.decisions, self.delays, self.limits = [], [], [], [], []
        settings = {"applicant_id": 1, "daily_enabled": False, "last_run_id": None, "last_reported_run_id": None}
        def waiting(*, limit=50):
            self.limits.append(limit)
            result = [dict(row) for row in self.rows if row["status"] == "waiting_for_reply" and not row["submitted"]]
            return result if limit is None else result[:limit]
        def queue(**kw):
            return [dict(row) for row in self.queue if not kw.get("active_only", True) or row["status"] in {"queued", "dispatched"}]
        def update_run(ident, **fields):
            row = next(row for row in self.rows if row["id"] == ident)
            row.update({key: value.isoformat() if isinstance(value, datetime) else value for key, value in fields.items()})
            return NS(**row)
        def update_queue(ident, status, detail, **kw):
            next(row for row in self.queue if row["job_id"] == ident).update(status=status, **kw)
        self.jobs = NS(mock_workflow_settings=lambda: dict(settings), mock_delivery_queue=queue,
            all_delivery_runs=lambda: [dict(row) for row in self.rows], delivery_runs_waiting_reply=waiting,
            delivery_runs_failed_before_submit=lambda **kw: [], deliveries_to_reconcile=lambda **kw: [],
            get_job=lambda ident: self.job_rows[ident], get_delivery_run=lambda ident: NS(**next(row for row in self.rows if row["id"] == ident)),
            update_delivery_run=update_run, update_mock_delivery=update_queue,
            record_mock_workflow_error=self.errors.append, record_automation_decision=lambda **kw: self.decisions.append(kw),
            add_conversation_message=lambda *args, **kw: None)
        async def resume(ident, **binding):
            assert binding == self.queue[0]["binding"]
            self.calls.append(ident)
            return update_run(ident, status="completed", submitted=True)
        self.delivery = NS(worker=None, resume_authorized_run=resume)
        self.coordinator = MockWorkflowCoordinator(self.jobs, NS(worker=None), self.delivery, None, None, poll_seconds=30)
        self.sync_calls = 0
        async def sync():
            self.sync_calls += 1
            return {"replied": count if replied else 0, "submitted": len(self.calls)}
        self.coordinator.sync_once = sync
        # Only transport facts are synthetic; actual observe/board facts,
        # rules/engine, loop, wait timeout and check_reply_queue remain real.
        original_observe = self.coordinator.observe
        async def local_observe(settings=None, *, with_site=True):
            return await original_observe(settings, with_site=False)
        self.coordinator.observe = local_observe
        original_delay = self.coordinator._next_poll_delay
        async def delay(settings):
            value = await original_delay(settings)
            self.delays.append(value)
            return value
        self.coordinator._next_poll_delay = delay

    async def start(self):
        await self.coordinator.start()

    async def close(self):
        await self.coordinator.stop()


class CoordinatorLoop(unittest.IsolatedAsyncioTestCase):
    async def until(self, predicate):
        async with asyncio.timeout(4):
            while not predicate():
                await asyncio.sleep(.005)

    async def test_real_loop_serves_known_replies_after_fair_sync_without_30_second_idle(self):
        fixture = LoopFixture()
        # Force a fair site-sync turn before the already-ready submissions.
        # sync returns no new evidence/wakeup, reproducing the observed stall.
        fixture.coordinator.engine.last_served["submit_authorised_resume"] = 1
        try:
            await fixture.start()
            await self.until(lambda: len(fixture.calls) == 3 and bool(fixture.delays) and fixture.delays[-1] == 30)
            self.assertEqual(fixture.sync_calls, 1)
            self.assertEqual(len(set(fixture.calls)), 3)
            self.assertEqual([row["rule"] for row in fixture.decisions],
                             ["sync_with_site"] + ["submit_authorised_resume"] * 3)
            self.assertIn(.25, fixture.delays)
            self.assertEqual(fixture.delays[-1], 30)
            self.assertEqual(fixture.errors, [])
        finally:
            await fixture.close()

    async def test_no_reply_keeps_normal_polling_without_fast_empty_cycles(self):
        fixture = LoopFixture(replied=False)
        try:
            await fixture.start()
            await self.until(lambda: bool(fixture.delays))
            await asyncio.sleep(.3)
            self.assertEqual(fixture.delays, [30])
            self.assertEqual(fixture.sync_calls, 1)
            self.assertEqual(fixture.calls, [])
        finally:
            await fixture.close()

    async def test_recent_check_without_new_reply_respects_per_run_backoff(self):
        fixture = LoopFixture(1)
        fixture.rows[0]["last_checked_at"] = datetime.now(UTC).isoformat()
        try:
            await fixture.start()
            await self.until(lambda: bool(fixture.delays))
            await asyncio.sleep(.3)
            self.assertEqual(fixture.delays, [30])
            self.assertEqual(fixture.calls, [])
            observation = await fixture.coordinator.observe(with_site=False)
            self.assertFalse(observation.get("reply_candidates")[0]["ready_for_check"])
        finally:
            await fixture.close()

    async def test_new_reply_can_wake_same_original_authorization_before_backoff_end(self):
        fixture = LoopFixture(1)
        fixture.rows[0]["last_checked_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        fixture.job_rows[1]["recruiter_reply_at"] = datetime.now(UTC).isoformat()
        try:
            await fixture.start()
            await self.until(lambda: len(fixture.calls) == 1 and fixture.queue[0]["status"] == "completed")
            self.assertEqual(fixture.calls, [fixture.rows[0]["id"]])
            self.assertEqual(fixture.queue[0]["status"], "completed")
        finally:
            await fixture.close()

    async def test_invalid_first_60_waits_do_not_hide_a_ready_61st(self):
        fixture = LoopFixture(61)
        for row in fixture.rows[:-1]:
            row["preview_id"] = None
        try:
            await fixture.start()
            await self.until(lambda: len(fixture.calls) == 1)
            self.assertEqual(fixture.calls, [fixture.rows[-1]["id"]])
            self.assertTrue(all(limit is None for limit in fixture.limits))
        finally:
            await fixture.close()

    async def test_cancelled_missing_binding_stopping_and_uncertain_work_never_ready(self):
        for mode in ("cancelled", "binding", "stopping", "uncertain"):
            with self.subTest(mode=mode):
                fixture = LoopFixture(1)
                if mode == "cancelled":
                    fixture.queue[0]["status"] = "cancelled"
                elif mode == "binding":
                    fixture.queue[0]["binding"]["resume_digest"] = None
                elif mode == "stopping":
                    fixture.coordinator.stopping_sessions.add("synthetic")
                else:
                    fixture.rows[0]["status"] = "outcome_indeterminate"
                try:
                    await fixture.start()
                    await self.until(lambda: bool(fixture.delays))
                    self.assertEqual(fixture.delays, [30])
                    self.assertEqual(fixture.calls, [])
                    self.assertEqual(await fixture.coordinator.check_reply_queue(), {"checked": 0, "submitted": 0})
                finally:
                    await fixture.close()

    async def test_rule_cooldown_still_prevents_fast_spin(self):
        fixture = LoopFixture(1)
        for rule in fixture.coordinator.engine.rules:
            fixture.coordinator.engine.cooldowns[rule.name] = time.monotonic() + 30
        self.assertEqual(await fixture.coordinator._next_poll_delay(fixture.coordinator.settings()), 30)

    async def test_busy_worker_has_normal_delay_and_is_not_reentered(self):
        fixture = LoopFixture(1)
        worker = asyncio.create_task(asyncio.Event().wait())
        fixture.delivery.worker = worker
        try:
            self.assertEqual(await fixture.coordinator._next_poll_delay(fixture.coordinator.settings()), 30)
            self.assertEqual(await fixture.coordinator.check_reply_queue(), {"checked": 0, "submitted": 0})
            self.assertEqual(fixture.calls, [])
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
