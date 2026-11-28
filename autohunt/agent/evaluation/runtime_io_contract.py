"""Isolated real-store contracts for nonblocking, cancellation-safe observers.

No model, browser, recruitment account or existing runtime data is used.
"""
from __future__ import annotations

import asyncio
import copy
import gc
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from types import SimpleNamespace as NS

from cryptography.fernet import Fernet

from app.jobs.store import JobStore
from app.runtime.manager import AgentRuntime
from app.runtime.store import RunStore


class RuntimeIO(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="autohunt-runtime-io-")
        self.root = Path(self.temp.name)
        self.key = Fernet.generate_key().decode()
        self.store = RunStore(self.root / "runtime.db", self.key)
        self.jobs = JobStore(self.root / "jobs.db")
        self.effects = []
        def forbidden(*args, **kwargs):
            self.effects.append((args, kwargs))
            raise AssertionError("No model, browser or sending is permitted")
        services = NS(current=lambda: NS(id=None), worker=None, start=forbidden, resume=forbidden,
                      stop=forbidden, generate=forbidden)
        self.runtime = AgentRuntime(self.store, NS(llm=services), self.jobs, services, services)
        self.runtime.delivery = services

    async def asyncTearDown(self):
        for task in list(self.runtime.tasks.values()):
            task.cancel()
        await asyncio.gather(*list(self.runtime.tasks.values()), return_exceptions=True)
        self.assertEqual(self.effects, [])
        gc.collect()  # sqlite3 transaction contexts close handles at collection on Windows.
        self.temp.cleanup()

    async def until(self, predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(.005)

    def seed(self, index=0):
        job_id, _ = self.jobs.upsert_job({"source": "MockJobSite", "source_url": f"http://example.invalid/{index}",
            "external_id": str(index), "title": f"Synthetic observer job {index}", "company": "Synthetic only",
            "location": "深圳", "salary": "20-30K", "description": "Isolated contract only", "tags": ["Python"],
            "score": 88, "decision": "REVIEW", "reasoning": "Synthetic", "stage": "applying"})
        delivery_id = f"synthetic-delivery-{index}"
        self.jobs.create_delivery_run(delivery_id, job_id)
        view = self.jobs.update_delivery_run(delivery_id, status="waiting_for_reply", checkpoint="waiting_for_reply")
        run, _ = self.store.create("default", f"observer-{index}", "delivery", {})
        self.store.update(run["id"], status="waiting_reply", checkpoint={"delivery_id": delivery_id, "retained": "evidence"})
        data = {"task": view.model_dump(mode="json"), "status": "waiting_reply", "detail": view.detail}
        return run["id"], delivery_id, data

    def delivery_events(self, run_id):
        return [event for event in self.store.all_events(run_id) if event["kind"] == "delivery"]

    async def test_database_work_releases_loop_and_limits_parallel_workers(self):
        entered, release = threading.Event(), threading.Event()
        lock = threading.Lock()
        active, maximum, started = 0, 0, 0
        def blocked():
            nonlocal active, maximum, started
            with lock:
                active += 1
                started += 1
                maximum = max(maximum, active)
                if active == 4:
                    entered.set()
            release.wait(4)
            with lock:
                active -= 1
        tasks = [asyncio.create_task(self.runtime._background_io(blocked)) for _ in range(12)]
        try:
            await self.until(entered.is_set)
            self.assertEqual(started, 4)
            # This callback runs while all four DB calls remain blocked.
            marker = []
            asyncio.get_running_loop().call_soon(marker.append, True)
            await asyncio.sleep(0)
            self.assertEqual(marker, [True])
        finally:
            release.set()
            await asyncio.gather(*tasks)
        self.assertEqual(maximum, 4)
        self.assertEqual(started, 12)

    async def test_cancel_drains_inflight_write_before_returning(self):
        run_id, delivery_id, data = self.seed()
        entered, release = threading.Event(), threading.Event()
        def blocked_write():
            entered.set()
            release.wait(4)
            return self.store.record_delivery_observation(run_id, delivery_id, data)
        task = asyncio.create_task(self.runtime._background_io(blocked_write))
        try:
            await self.until(entered.is_set)
            task.cancel()
            await asyncio.sleep(.02)
            self.assertFalse(task.done(), "Cancellation must not abandon an in-flight DB write")
            self.store.update(run_id, cancel_requested=1, status="cancelling")
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.store.update(run_id, status="cancelled")
        self.assertEqual(self.store.get(run_id)["status"], "cancelled")
        self.assertEqual(self.delivery_events(run_id), [])

    async def test_repeated_cancel_is_drained_and_db_error_is_consumed(self):
        entered, release = threading.Event(), threading.Event()
        def blocked_error():
            entered.set()
            release.wait(4)
            raise ValueError("Synthetic DB error")
        task = asyncio.create_task(self.runtime._background_io(blocked_error))
        try:
            await self.until(entered.is_set)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(.01)
            self.assertFalse(task.done())
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_cancel_before_lane_admission_does_not_start_call(self):
        for _ in range(4):
            await self.runtime._watch_io_slots.acquire()
        called = []
        task = asyncio.create_task(self.runtime._background_io(called.append, True))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        for _ in range(4):
            self.runtime._watch_io_slots.release()
        self.assertEqual(called, [])

    async def test_observation_event_is_atomic_and_deduplicated_after_reopen(self):
        run_id, delivery_id, data = self.seed()
        self.assertTrue(self.store.record_delivery_observation(run_id, delivery_id, data))
        self.store = RunStore(self.root / "runtime.db", self.key)
        unchanged = copy.deepcopy(data)
        unchanged["task"].update(updated_at="2099-01-01T00:00:00Z", last_checked_at="2099-01-01T00:00:00Z")
        self.assertTrue(self.store.record_delivery_observation(run_id, delivery_id, unchanged))
        self.assertEqual(len(self.delivery_events(run_id)), 1)
        state = self.store.get(run_id)
        self.assertEqual(state["checkpoint"]["delivery_id"], delivery_id)
        self.assertEqual(state["checkpoint"]["retained"], "evidence")
        with self.store.connect() as db:
            raw = db.execute("SELECT checkpoint FROM agent_runs WHERE id=?", (run_id,)).fetchone()[0]
            event = db.execute("SELECT data FROM agent_run_events WHERE run_id=? AND kind='delivery'", (run_id,)).fetchone()[0]
        self.assertTrue(raw.startswith("enc:v1:"))
        self.assertTrue(event.startswith("enc:v1:"))

    async def test_real_state_changes_emit_events_but_repeated_poll_does_not(self):
        run_id, delivery_id, data = self.seed()
        self.store.record_delivery_observation(run_id, delivery_id, data)
        data["task"]["recruiter_reply"] = "Synthetic HR reply"
        data["task"]["detail"] = "Synthetic reply detected"
        for _ in range(3):
            self.assertTrue(self.store.record_delivery_observation(run_id, delivery_id, data))
        self.assertEqual(len(self.delivery_events(run_id)), 2)

    async def test_observation_status_and_event_roll_back_together(self):
        run_id, delivery_id, data = self.seed()
        self.store.update(run_id, status="running")
        before = self.store.get(run_id)
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER fail_synthetic_event BEFORE INSERT ON agent_run_events "
                       "WHEN NEW.kind='delivery' BEGIN SELECT RAISE(ABORT, 'synthetic event write failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.record_delivery_observation(run_id, delivery_id, data)
        self.assertEqual(self.store.get(run_id), before)
        self.assertEqual(self.delivery_events(run_id), [])

    async def test_real_cancel_cleanup_waits_for_background_observation_then_settles(self):
        run_id, _, _ = self.seed()
        entered, release = threading.Event(), threading.Event()
        original = self.runtime._save_delivery_observation
        def blocked_save(*args):
            entered.set()
            release.wait(4)
            return original(*args)
        self.runtime._save_delivery_observation = blocked_save
        self.runtime.schedule(run_id, watch_only=True)
        try:
            await self.until(entered.is_set)
            acknowledgement = await self.runtime.cancel(run_id)
            self.assertEqual(acknowledgement["status"], "cancelling")
            cleanup = self.runtime.cancellations[run_id]
            self.assertFalse(cleanup.done())
        finally:
            release.set()
        await asyncio.wait_for(cleanup, 3)
        self.assertEqual(self.store.get(run_id)["status"], "cancelled")
        self.assertEqual(self.delivery_events(run_id), [])

    async def test_terminal_cancelling_and_requested_cancel_are_not_revived(self):
        for index, fields in enumerate(({"status": "completed"}, {"status": "cancelled"},
                {"status": "cancelling"}, {"cancel_requested": 1})):
            with self.subTest(fields=fields):
                run_id, delivery_id, data = self.seed(index)
                self.store.update(run_id, **fields)
                before = self.store.get(run_id)
                self.assertFalse(self.store.record_delivery_observation(run_id, delivery_id, data))
                self.assertEqual(self.store.get(run_id), before)
                self.assertEqual(self.delivery_events(run_id), [])

    async def test_binding_mismatch_does_not_change_checkpoint_or_publish_event(self):
        run_id, delivery_id, data = self.seed()
        self.store.update(run_id, checkpoint={"delivery_id": "different-bound-delivery"})
        before = self.store.get(run_id)
        self.assertFalse(self.store.record_delivery_observation(run_id, delivery_id, data))
        self.assertEqual(self.store.get(run_id), before)
        self.assertEqual(self.delivery_events(run_id), [])
        with self.assertRaises(ValueError):
            self.store.record_delivery_observation(run_id, "wrong-task", data)

    async def test_recovered_watchers_keep_waiting_without_duplicate_events_or_effects(self):
        run_id, delivery_id, _ = self.seed()
        for _ in range(2):
            self.runtime.schedule(run_id, watch_only=True)
            task = self.runtime.tasks[run_id]
            await self.until(lambda: bool(self.delivery_events(run_id)))
            await asyncio.sleep(.03)
            self.assertFalse(task.done())
            self.assertEqual(self.store.get(run_id)["status"], "waiting_reply")
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(len(self.delivery_events(run_id)), 1)
        self.assertEqual(self.jobs.get_delivery_run(delivery_id).status, "waiting_for_reply")

    async def test_watcher_exits_if_binding_is_replaced(self):
        run_id, _, _ = self.seed()
        self.runtime.schedule(run_id, watch_only=True)
        task = self.runtime.tasks[run_id]
        await self.until(lambda: bool(self.delivery_events(run_id)))
        self.store.update(run_id, checkpoint={"delivery_id": "replacement"})
        await asyncio.wait_for(task, 2)
        self.assertEqual(self.store.get(run_id)["checkpoint"], {"delivery_id": "replacement"})
        self.assertEqual(len(self.delivery_events(run_id)), 1)

    async def test_thread_snapshot_cannot_downgrade_success_after_worker_switch(self):
        run_id, delivery_id, _ = self.seed()
        self.jobs.update_delivery_run(delivery_id, status="running", checkpoint="submit_started")
        read, release = threading.Event(), threading.Event()
        original = self.runtime._read_delivery_observation
        def blocked_snapshot(*args):
            view = original(*args)
            read.set()
            release.wait(4)
            return view
        self.runtime._read_delivery_observation = blocked_snapshot
        self.runtime.schedule(run_id, watch_only=True)
        task = self.runtime.tasks[run_id]
        try:
            await self.until(read.is_set)
            view = self.jobs.update_delivery_run(delivery_id, status="completed", submitted=True, checkpoint="submission_confirmed")
            self.jobs.mark_delivery_completed(view.job_id, "Synthetic confirmed submission")
            # The foreground browser has already moved to the next job while
            # the old RUNNING snapshot is still travelling through the thread.
            self.runtime.delivery.current = lambda: NS(id="different-running-job")
        finally:
            release.set()
        await asyncio.wait_for(task, 3)
        self.assertEqual(self.jobs.get_delivery_run(delivery_id).status, "completed")
        self.assertTrue(self.jobs.get_delivery_run(delivery_id).submitted)
        self.assertEqual(self.jobs.get_job(view.job_id)["stage"], "waiting")
        self.assertEqual(self.store.get(run_id)["status"], "completed")
        with self.jobs._connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM browser_events WHERE event_type='delivery_watchdog'").fetchone()[0], 0)

    async def test_stale_working_snapshot_cannot_override_new_wait_or_new_version(self):
        for index, changes in enumerate(({"status": "waiting_for_reply", "checkpoint": "waiting_for_reply"},
                                         {"status": "running", "checkpoint": "newer_preparing_phase"})):
            with self.subTest(changes=changes):
                _, delivery_id, _ = self.seed(index)
                old = self.jobs.update_delivery_run(delivery_id, status="running", checkpoint="submit_started")
                fresh = self.jobs.update_delivery_run(delivery_id, **changes)
                returned = self.runtime.observe_delivery(delivery_id, view=old)
                self.assertEqual(returned.model_dump(), fresh.model_dump())
                self.assertEqual(self.jobs.get_delivery_run(delivery_id).model_dump(), fresh.model_dump())
                self.assertEqual(self.jobs.get_job(fresh.job_id)["stage"], "applying")

    async def test_store_cas_protects_confirmed_submitted_even_if_status_still_running(self):
        _, delivery_id, _ = self.seed()
        view = self.jobs.update_delivery_run(delivery_id, status="running", checkpoint="submit_started", submitted=True)
        self.jobs.mark_delivery_completed(view.job_id, "Synthetic confirmed receipt")
        returned = self.jobs.record_delivery_worker_loss(view, "worker_lost")
        self.assertTrue(returned.submitted)
        self.assertEqual(returned.status, "running")
        self.assertEqual(self.jobs.get_job(view.job_id)["stage"], "waiting")

    async def test_save_refreshes_receipt_before_settling_stale_terminal_snapshot(self):
        run_id, delivery_id, _ = self.seed()
        stale = self.jobs.update_delivery_run(delivery_id, status="failed", checkpoint="failed_transient_before_submit")
        fresh = self.jobs.update_delivery_run(delivery_id, status="waiting_for_reply", checkpoint="waiting_for_reply")
        saved = await self.runtime._background_io(self.runtime._save_delivery_observation, run_id, delivery_id, stale)
        self.assertEqual(saved.status, fresh.status)
        self.assertEqual(self.store.get(run_id)["status"], "waiting_reply")
        self.assertIsNone(self.store.get(run_id)["result"])

    async def test_real_lost_worker_keeps_uncertain_and_pre_effect_failure_guards(self):
        for index, (checkpoint, expected_status, stage) in enumerate((
                ("submit_started", "outcome_indeterminate", "outcome_indeterminate"),
                ("authorized", "failed", "review"))):
            with self.subTest(checkpoint=checkpoint):
                _, delivery_id, _ = self.seed(index)
                old = self.jobs.update_delivery_run(delivery_id, status="running", checkpoint=checkpoint)
                result = self.runtime.observe_delivery(delivery_id, view=old)
                self.assertEqual(result.status, expected_status)
                self.assertEqual(self.jobs.get_job(old.job_id)["stage"], stage)
                self.assertFalse(result.submitted)
                self.assertEqual(result.checkpoint, "worker_lost")
                repeated = self.runtime.observe_delivery(delivery_id)
                self.assertEqual(repeated.model_dump(), result.model_dump())

    async def test_old_lost_worker_does_not_change_newer_delivery_board_state(self):
        _, delivery_id, _ = self.seed()
        old = self.jobs.update_delivery_run(delivery_id, status="running", checkpoint="submit_started")
        self.jobs.create_delivery_run("newer-synthetic-delivery", old.job_id)
        self.jobs.update_delivery_run("newer-synthetic-delivery", status="completed", submitted=True)
        self.jobs.mark_delivery_completed(old.job_id, "Newer confirmed receipt")
        result = self.jobs.record_delivery_worker_loss(old, "worker_lost")
        self.assertEqual(result.status, "outcome_indeterminate")
        self.assertEqual(self.jobs.get_job(old.job_id)["stage"], "waiting")
        self.assertTrue(self.jobs.get_delivery_run("newer-synthetic-delivery").submitted)

    async def test_worker_loss_transaction_rolls_back_run_and_board_together(self):
        _, delivery_id, _ = self.seed()
        old = self.jobs.update_delivery_run(delivery_id, status="running", checkpoint="submit_started")
        job_before = self.jobs.get_job(old.job_id)
        with self.jobs._connect() as db:
            db.execute("CREATE TRIGGER fail_watchdog_event BEFORE INSERT ON browser_events "
                       "WHEN NEW.event_type='delivery_watchdog' BEGIN SELECT RAISE(ABORT, 'synthetic watchdog event failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.jobs.record_delivery_worker_loss(old, "worker_lost")
        self.assertEqual(self.jobs.get_delivery_run(delivery_id).model_dump(), old.model_dump())
        self.assertEqual(self.jobs.get_job(old.job_id), job_before)

    async def test_async_progress_bypasses_watch_lane_and_execution_slots(self):
        self.seed()
        for _ in range(4):
            await self.runtime._watch_io_slots.acquire()
        for _ in range(2):
            await self.runtime.slots.acquire()
        try:
            result = await asyncio.wait_for(self.runtime.progress_result_async("default", request_id="synthetic-query"), 3)
        finally:
            for _ in range(4):
                self.runtime._watch_io_slots.release()
            for _ in range(2):
                self.runtime.slots.release()
        self.assertEqual(result["snapshot"]["counts"]["waiting_reply"], 1)
        repeated = await self.runtime.progress_result_async("default", request_id="synthetic-query")
        self.assertEqual(result["message_id"], repeated["message_id"])
        self.assertEqual(self.store.get(result["run_id"])["status"], "completed")

    async def test_submit_progress_is_async_and_persists_answer_in_chat(self):
        self.seed()
        run = self.runtime.submit("default", "synthetic-progress-submit", "chat", {"message": "当前结果"})
        self.assertEqual(run["status"], "queued")
        await self.runtime.tasks[run["id"]]
        final = self.store.get(run["id"])
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["result"]["snapshot"]["counts"]["contacted"], 1)
        self.assertTrue(any(row["role"] == "assistant" and row["response"]["run_id"] == run["id"]
                            for row in self.jobs.conversation_history()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
