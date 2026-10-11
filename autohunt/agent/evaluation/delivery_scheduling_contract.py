"""Offline scheduling contracts; no model, site, real messages or resume writes."""
from __future__ import annotations

import asyncio
import json
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from app.workflow.delivery_rules import build_rules
from app.workflow.mock_coordinator import MockWorkflowCoordinator
from app.workflow.rules import Observation, Rule, RuleEngine

TRACES = []


class Clock:
    value = 1000.0
    def now(self): return self.value


class Coordinator:
    def __init__(self):
        self.calls = []
        self.jobs = NS(mark_job_needs_review=lambda *args: self.calls.append("escalate"))
    async def check_reply_queue(self):
        self.calls.append("reply"); return {"checked": 1, "submitted": 0}
    async def process_delivery_queue(self): self.calls.append("dispatch")
    async def recover_failed_starts(self): self.calls.append("recover"); return {}
    async def sync_once(self): self.calls.append("sync"); return {}
    async def run_daily_if_due(self): self.calls.append("daily")


def facts(**extra):
    return Observation(facts={"queue_depth": 4, "delivery_idle": True, "browser_idle": True,
        "reply_candidates": [{"id": "waiting"}], **extra})


class Scheduling(unittest.IsolatedAsyncioTestCase):
    async def test_waiting_hr_never_starves_dispatch_at_any_poll_interval(self):
        for interval in (25, 30, 60, 300):
            clock, coordinator = Clock(), Coordinator()
            engine = RuleEngine(build_rules(coordinator))
            with patch("app.workflow.rules.time.monotonic", clock.now):
                chosen = []
                for _ in range(10):
                    chosen.append((await engine.step(facts())).rule)
                    clock.value += interval
            self.assertEqual(chosen, ["submit_authorised_resume", "dispatch_delivery_queue"] * 5)
            TRACES.append({"interval": interval, "chosen": chosen})

    async def test_all_normal_work_gets_turn_despite_persistent_higher_priority(self):
        coordinator, clock = Coordinator(), Clock()
        engine = RuleEngine(build_rules(coordinator))
        observation = facts(applicant_id=31, settings={"daily_enabled": True},
                            stalled_replied_runs=[{"job_id": 99}])
        with patch("app.workflow.rules.time.monotonic", clock.now):
            for _ in range(10):
                await engine.step(observation); clock.value += 60
        self.assertEqual(coordinator.calls, ["recover", "reply", "dispatch", "sync", "daily"] * 2)

    async def test_busy_cycle_does_not_consume_dispatch_turn(self):
        coordinator, clock = Coordinator(), Clock()
        engine = RuleEngine(build_rules(coordinator))
        with patch("app.workflow.rules.time.monotonic", clock.now):
            await engine.step(facts()); clock.value += 30
            self.assertIsNone((await engine.step(facts(), busy=True)).rule)
            self.assertEqual((await engine.step(facts())).rule, "dispatch_delivery_queue")

    async def test_fault_escalation_preempts_normal_rotation(self):
        coordinator, clock = Coordinator(), Clock()
        engine = RuleEngine(build_rules(coordinator))
        with patch("app.workflow.rules.time.monotonic", clock.now):
            await engine.step(facts()); clock.value += 30
            selected = await engine.step(facts(exhausted_runs=[{"job_id": 99, "attempts": 3}]))
        self.assertEqual(selected.rule, "escalate_exhausted_start")

    async def test_action_failure_yields_to_other_ready_work(self):
        async def fail(_): raise TimeoutError("synthetic")
        async def success(_): return "ok"
        clock = Clock()
        engine = RuleEngine([Rule("fault", 80, lambda _: True, fail),
                             Rule("dispatch", 60, lambda _: True, success)])
        with patch("app.workflow.rules.time.monotonic", clock.now):
            self.assertIn("TimeoutError", (await engine.step(facts())).result)
            clock.value += 30
            self.assertEqual((await engine.step(facts())).rule, "dispatch")

    async def test_busy_browser_and_cancelled_queue_do_not_dispatch(self):
        coordinator = Coordinator()
        engine = RuleEngine(build_rules(coordinator))
        self.assertIsNone((await engine.step(facts(browser_idle=False))).rule)
        self.assertIsNone((await engine.step(facts(queue_depth=0, reply_candidates=[]))).rule)
        self.assertEqual(coordinator.calls, [])

    async def test_reply_log_reports_actual_counts_not_claimed_submission(self):
        coordinator = Coordinator()
        decision = await RuleEngine(build_rules(coordinator)).step(facts())
        self.assertIn("1", decision.result)
        self.assertIn("0", decision.result)


class ReplyTurns(unittest.IsolatedAsyncioTestCase):
    def coordinator(self, *, invalid_first=False):
        old = (datetime.now(UTC) - timedelta(days=365)).isoformat()
        rows = [{"id": f"wait-{i}", "job_id": i, "preview_id": f"preview-{i}", "action": "submit_resume",
                 "resume_document_id": "synthetic", "resume_version": 1, "resume_digest": "digest",
                 "last_checked_at": old} for i in range(3)]
        if invalid_first: rows[0]["preview_id"] = None
        calls = []
        async def resume(ident, **binding):
            calls.append(ident)
            next(row for row in rows if row["id"] == ident)["last_checked_at"] = datetime.now(UTC).isoformat()
            return NS(submitted=False)
        jobs = NS(delivery_runs_waiting_reply=lambda **kw: sorted(rows, key=lambda r: r["last_checked_at"]),
                  mock_delivery_queue=lambda **kw: [], record_mock_workflow_error=lambda text: self.fail(text))
        def update(ident, **changes):
            next(row for row in rows if row["id"] == ident).update({key: value.isoformat() if isinstance(value, datetime) else value
                                                               for key, value in changes.items()})
        jobs.update_delivery_run = update
        coordinator = MockWorkflowCoordinator(jobs, NS(worker=None), NS(worker=None, resume_authorized_run=resume), None, None)
        coordinator.delivery_owner = lambda _: "synthetic-session"
        return coordinator, rows, calls

    async def test_one_reply_check_per_turn_rotates_same_runs(self):
        coordinator, rows, calls = self.coordinator()
        for _ in range(3):
            outcome = await coordinator.check_reply_queue()
            self.assertEqual(outcome["checked"], 1)
        self.assertEqual(calls, [row["id"] for row in rows])
        self.assertEqual((await coordinator.check_reply_queue())["checked"], 0)

    async def test_invalid_binding_does_not_starve_valid_waiters(self):
        coordinator, _, calls = self.coordinator(invalid_first=True)
        await coordinator.check_reply_queue()
        self.assertEqual(calls, ["wait-1"])

    async def test_worker_busy_preserves_wait_without_replay(self):
        coordinator, rows, calls = self.coordinator()
        worker = asyncio.create_task(asyncio.Event().wait())
        coordinator.delivery.worker = worker
        try:
            self.assertEqual((await coordinator.check_reply_queue())["checked"], 0)
            self.assertEqual(calls, [])
            self.assertEqual(len(rows), 3)
        finally:
            worker.cancel(); await asyncio.gather(worker, return_exceptions=True)

    async def test_failed_check_does_not_starve_other_waiters(self):
        coordinator, _, calls = self.coordinator()
        errors = []
        coordinator.jobs.record_mock_workflow_error = errors.append
        original = coordinator.delivery.resume_authorized_run
        async def resume(ident, **binding):
            if ident == "wait-0": raise TimeoutError("synthetic read failure")
            return await original(ident, **binding)
        coordinator.delivery.resume_authorized_run = resume
        await coordinator.check_reply_queue()
        await coordinator.check_reply_queue()
        self.assertEqual(calls, ["wait-1"])
        self.assertEqual(len(errors), 1)

    async def test_check_joins_worker_and_uses_final_submission_receipt(self):
        coordinator, _, _ = self.coordinator()
        finished = []
        async def resume(ident, **binding):
            async def work():
                await asyncio.sleep(0); finished.append(ident)
            coordinator.delivery.worker = asyncio.create_task(work())
            return NS(submitted=False)
        coordinator.delivery.resume_authorized_run = resume
        coordinator.jobs.get_delivery_run = lambda ident: NS(submitted=ident in finished)
        outcome = await coordinator.check_reply_queue()
        self.assertEqual(outcome, {"checked": 1, "submitted": 1})
        self.assertTrue(coordinator.delivery.worker.done())


class Batch(unittest.IsolatedAsyncioTestCase):
    async def test_five_jobs_open_once_without_waiting_for_any_hr_reply(self):
        queue = [{"job_id": i, "session_id": "synthetic", "resume_stamp": "stamp", "min_score": 85,
                  "job_hash": "hash", "binding": {}, "status": "queued", "run_id": None} for i in range(5)]
        runs, contacts, checks = {}, [], []
        delivery = NS(worker=None, supports=lambda _: True)
        jobs = NS(mock_delivery_queue=lambda **kw: [r for r in queue if not kw.get("active_only", True) or r["status"] in {"queued", "dispatched"}],
            get_job=lambda i: {"id": i, "score": 90, "filter_status": "pass", "stage": "review", "source_url": "http://synthetic/job"},
            review_fingerprint=lambda _: "hash", latest_delivery_status_for_job=lambda i: runs.get(f"run-{i}", {}).get("status"),
            create_delivery_preview=lambda i, **kw: NS(id=str(i)))
        def update(i, status, detail, run_id=None):
            queue[i].update(status=status)
            if run_id: queue[i]["run_id"] = run_id
        jobs.update_mock_delivery = update
        def claim(i):
            if queue[i]["status"] != "queued": return False
            queue[i]["status"] = "dispatched"; return True
        jobs.claim_mock_delivery = claim
        jobs.delivery_runs_waiting_reply = lambda **kw: sorted([r for r in runs.values() if r["status"] == "waiting_for_reply"], key=lambda r: r["last_checked_at"])
        def update_run(ident, **kw):
            runs[ident].update({k:v.isoformat() if isinstance(v, datetime) else v for k,v in kw.items()})
        jobs.update_delivery_run = update_run
        jobs.get_delivery_run = lambda ident: NS(**runs[ident])
        jobs.record_mock_workflow_error = lambda text: self.fail(text)
        async def start(preview, **kw):
            i = int(preview); contacts.append(i)
            run = {"id":f"run-{i}","job_id":i,"preview_id":f"preview-{i}","action":"submit_resume",
                   "resume_document_id":"synthetic","resume_version":1,"resume_digest":"digest",
                   "status":"waiting_for_reply","submitted":False,"last_checked_at":""}
            runs[run["id"]] = run
            delivery.worker = asyncio.create_task(asyncio.sleep(0))
            return None, i, None, NS(**run)
        async def check(ident, **kw):
            checks.append(ident)
            delivery.worker = asyncio.create_task(asyncio.sleep(0))
            return NS(**runs[ident])
        delivery.authorize_and_start, delivery.resume_authorized_run = start, check
        coordinator = MockWorkflowCoordinator(jobs, NS(worker=None), delivery, None, NS(read_document=lambda:None))
        clock = Clock()
        coordinator.engine = RuleEngine(build_rules(coordinator), clock=clock.now)
        with patch("app.growth.requests.resume_stamp", return_value="stamp"):
            for _ in range(12):
                await coordinator.engine.step(facts(queue_depth=sum(r["status"] == "queued" for r in queue),
                                                   reply_candidates=list(runs.values())))
                if delivery.worker: await delivery.worker
                clock.value += 30
        self.assertEqual(contacts, list(range(5)))
        self.assertTrue(checks)
        self.assertTrue(all(r["status"] == "waiting_for_reply" and not r["submitted"] for r in runs.values()))
        TRACES.append({"batch":5,"contacts":contacts,"checks":checks,"submitted":0})


def main():
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(cls) for cls in (Scheduling, ReplyTurns, Batch))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    output = Path("test-results/delivery-scheduling") / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    summary = {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
               "model_calls": 0, "browser_runs": 0, "real_submissions": 0}
    (output/"summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (output/"traces.jsonl").write_text("\n".join(json.dumps(row) for row in TRACES), encoding="utf-8")
    (output/"failures.json").write_text(json.dumps([{"test":str(test),"detail":detail} for test,detail in result.failures+result.errors],indent=2),encoding="utf-8")
    print(json.dumps({"output": str(output), **summary}))
    return int(not result.wasSuccessful())


if __name__ == "__main__": raise SystemExit(main())
