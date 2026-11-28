"""Real runtime HR waiters against isolated SQLite, with no model or browser.

The delivery rows are synthetic, already-communicated waiting records. This
measures live watch_delivery coroutines, encrypted ledger writes, foreground
admission and read-only progress reporting. It does not execute recruitment.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import UTC, datetime
import hashlib
import json
import math
from pathlib import Path
import time
import traceback
from types import SimpleNamespace as NS

from cryptography.fernet import Fernet

from app.jobs.store import JobStore
from app.runtime.manager import AgentRuntime
from app.runtime.store import RunStore


ROOT = Path(__file__).resolve().parents[1]
SOURCES = (
    "app/runtime/manager.py", "app/runtime/store.py", "app/runtime/task_results.py",
    "app/jobs/store.py", "evaluation/delivery_scale_runtime.py",
)


def hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES}


def latency(values):
    """Nearest-rank milliseconds; no implicit service-level threshold."""
    ordered = sorted(values)
    def percentile(ratio):
        return round(ordered[max(0, math.ceil(len(ordered) * ratio) - 1)], 3) if ordered else None
    return {"samples": len(ordered), "p50_ms": percentile(.5), "p95_ms": percentile(.95),
            "max_ms": round(max(ordered), 3) if ordered else None}


class ReadOnlyService:
    worker = None

    def __init__(self, calls):
        self.calls = calls

    def current(self):
        return NS(id=None, status="idle")

    def __getattr__(self, name):
        def forbidden(*_, **__):
            self.calls.append(name)
            raise AssertionError("No browser/model/external action is allowed: " + name)
        return forbidden


async def measure(output: Path, count: int, seconds: float, interval: float):
    before = hashes()
    side_effects, read_ms, lag_ms, admissions_ms, progress_ms, lag_samples = [], [], [], [], [], []
    observations = Counter()
    first_observed, last_observed = {}, {}
    tasks = []
    sampler = None
    runtime = None
    stop_sampler = asyncio.Event()
    report = {"passed": False, "count": count, "requested_observation_seconds": seconds,
              "timer_interval_ms": interval * 1000, "model_calls": 0,
              "browser_actions": 0, "real_recruitment_actions": 0,
              "source_sha256": before,
              "scope": "Real AgentRuntime watch_delivery coroutines, isolated real JobStore and encrypted RunStore. Synthetic pre-existing HR waits; no website, frontend, LLM or sending.",
              "limitations": [
                  "Synthetic waiting rows are seeded before the observation window; greetings/submissions are not performed.",
                  "Foreground admission measures the real RunStore create gate, not LLM execution or HTTP latency.",
                  "Event-loop lag and read timings are measurements on this machine, not a production SLO or long-duration soak test.",
                  "No frontend, SSE transport connections or website HR polling is loaded by this runtime benchmark.",
              ]}
    try:
        jobs = JobStore(output / "jobs.db")
        runtime_key = Fernet.generate_key().decode()
        ledger = RunStore(output / "runtime.db", runtime_key)
        services = ReadOnlyService(side_effects)
        runtime = AgentRuntime(ledger, NS(llm=services), jobs, services, services)
        runtime.delivery = services
        run_ids, delivery_ids = [], []
        seed_started = time.perf_counter()
        for index in range(count):
            job_id, _ = jobs.upsert_job({
                "source": "MockJobSite", "source_url": f"http://127.0.0.1:8080/job.html?id=synthetic-runtime-{index}",
                "external_id": f"synthetic-runtime-{index}", "title": f"合成运行器岗位{index}",
                "company": "合成运行器雇主", "location": "深圳", "salary": "20-30K",
                "description": "仅供隔离运行器测量，不是真实招聘。", "tags": ["Python"],
                "score": 88, "decision": "REVIEW", "reasoning": "合成HR等待记录",
                "stage": "applying", "filter_status": "pass", "filter_evidence": {"status": "pass"},
            })
            delivery_id = f"synthetic-runtime-delivery-{index}"
            jobs.create_delivery_run(delivery_id, job_id, detail="合成的已沟通、等待HR记录")
            jobs.update_delivery_run(delivery_id, status="waiting_for_reply", checkpoint="waiting_for_reply")
            run, created = ledger.create("default", f"synthetic-waiter-{index}", "delivery", {})
            assert created
            # Ordinary sequential dispatch changes each new child to waiting
            # before the next admission. HR waits do not consume the 100 active
            # execution queue slots; no gate or production sleep is bypassed.
            ledger.update(run["id"], status="waiting_reply", checkpoint={"delivery_id": delivery_id})
            run_ids.append(run["id"])
            delivery_ids.append(delivery_id)
        report["seed_seconds"] = round(time.perf_counter() - seed_started, 3)

        original_observe = runtime._read_delivery_observation
        original_save = runtime._save_delivery_observation
        saved_observations = set()
        started = time.perf_counter()
        cpu_started = time.process_time()
        phase = "startup"
        phase_started = started
        phase_seconds = Counter()
        def change_phase(name):
            nonlocal phase, phase_started
            now = time.perf_counter()
            phase_seconds[phase] += now - phase_started
            phase, phase_started = name, now
        def observed(run_id, delivery_id):
            tick = time.perf_counter()
            value = original_observe(run_id, delivery_id)
            read_ms.append((time.perf_counter() - tick) * 1000)
            observations[delivery_id] += 1
            offset = round(time.perf_counter() - started, 6)
            first_observed.setdefault(delivery_id, offset)
            last_observed[delivery_id] = offset
            return value
        def saved(run_id, delivery_id, view):
            accepted = original_save(run_id, delivery_id, view)
            if accepted:
                saved_observations.add(delivery_id)
            return accepted
        runtime._read_delivery_observation = observed
        runtime._save_delivery_observation = saved

        async def sample_lag():
            loop = asyncio.get_running_loop()
            while not stop_sampler.is_set():
                tick = loop.time()
                initial_phase = phase
                await asyncio.sleep(interval)
                value = max(0., (loop.time() - tick - interval) * 1000)
                lag_ms.append(value)
                label = phase if phase == initial_phase else f"transition:{initial_phase}->{phase}"
                lag_samples.append({"phase": label, "lag_ms": value,
                                    "at_seconds": round(time.perf_counter() - started, 6)})

        sampler = asyncio.create_task(sample_lag(), name="scale-event-loop-probe")
        await asyncio.sleep(0)  # Start the timer before the initial ledger burst.
        for run_id in run_ids:
            runtime.schedule(run_id, watch_only=True)
        tasks = [runtime.tasks[run_id] for run_id in run_ids]
        # End startup only after every initial observation and durable event
        # has completed; thread admission must not hide startup work elsewhere.
        startup_deadline = asyncio.get_running_loop().time() + 15
        while len(saved_observations) < count:
            assert asyncio.get_running_loop().time() < startup_deadline, "Initial observations did not all persist"
            await asyncio.sleep(.02)
        assert all(not task.done() for task in tasks), "A real waiting coroutine exited unexpectedly"

        change_phase("foreground_admission")
        for index in range(10):
            tick = time.perf_counter()
            foreground, created = ledger.create("default", f"synthetic-foreground-{index}", "chat",
                {"message": "合成新的前台请求，仅测入队，不执行模型"})
            admissions_ms.append((time.perf_counter() - tick) * 1000)
            assert created and foreground["status"] == "queued"
            ledger.update(foreground["id"], status="completed", result={"admission_probe": True})
            await asyncio.sleep(.03)

        tick = time.perf_counter()
        await runtime.slots.acquire()
        report["foreground_slot_acquire_ms"] = round((time.perf_counter() - tick) * 1000, 3)
        runtime.slots.release()

        # Include a full unchanged production watch interval, separately from
        # progress-control queries and their result verification.
        change_phase("steady_wait")
        await asyncio.sleep(1.1)
        change_phase("progress_queries")
        progress_rows = []
        for index in range(3):
            tick = time.perf_counter()
            result = await runtime.progress_result_async("default", "当前投递进度", request_id=f"synthetic-scale-progress-{index}")
            progress_ms.append((time.perf_counter() - tick) * 1000)
            counts = result["snapshot"]["counts"]
            assert counts["contacted"] == count and counts["waiting_reply"] == count
            assert counts["submitted"] == counts["failed"] == counts["uncertain"] == 0
            assert len(result["snapshot"]["jobs"]) == count
            assert ledger.get(result["run_id"])["status"] == "completed"
            assert any(m.get("response", {}).get("message_id") == result["message_id"]
                       for m in jobs.conversation_history(limit=20))
            progress_rows.append({"counts": counts, "jobs": len(result["snapshot"]["jobs"]),
                                  "run_id": result["run_id"], "message_id": result["message_id"]})
            await asyncio.sleep(.03)

        change_phase("steady_wait")
        await asyncio.sleep(max(0., seconds - (time.perf_counter() - started)))
        # This is a bounded coverage wait, not a claimed latency service level.
        deadline = asyncio.get_running_loop().time() + 15
        while any(observations[key] < 2 for key in delivery_ids):
            assert asyncio.get_running_loop().time() < deadline, "Not every watcher reached its second real observation"
            await asyncio.sleep(.05)
        # Stop the timing probe before the synchronous verification queries;
        # otherwise the test's own post-window assertions inflate timer lag.
        stop_sampler.set()
        await sampler
        sampler = None
        change_phase("verification")
        elapsed = time.perf_counter() - started
        cpu_elapsed = time.process_time() - cpu_started
        assert all(not task.done() for task in tasks)
        assert all(ledger.get(run_id)["status"] == "waiting_reply" for run_id in run_ids)
        assert all(jobs.get_delivery_run(key).status == "waiting_for_reply" for key in delivery_ids)
        delivery_events = [[event for event in ledger.all_events(run_id) if event["kind"] == "delivery"]
                           for run_id in run_ids]
        assert all(len(events) == 1 and events[0]["data"]["status"] == "waiting_reply" for events in delivery_events)
        with ledger.connect() as db:
            encrypted_payload_rows = db.execute("SELECT COUNT(*) FROM agent_runs WHERE payload LIKE 'enc:v1:%'").fetchone()[0]
            total_rows = db.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0]
        assert encrypted_payload_rows == total_rows
        assert not side_effects
        report.update(passed=True, observed_seconds=round(elapsed, 3), process_cpu_seconds=round(cpu_elapsed, 3),
            live_waiter_tasks=count, waiting_reply_ledger_rows=count,
            observation_counts={"minimum": min(observations.values()), "maximum": max(observations.values()),
                                "total": sum(observations.values())},
            observation_read_latency=latency(read_ms), event_loop_lag=latency(lag_ms),
            phase_seconds={name: round(value, 3) for name, value in phase_seconds.items()},
            phase_event_loop_lag={name: latency([sample["lag_ms"] for sample in lag_samples if sample["phase"] == name])
                                  for name in sorted({sample["phase"] for sample in lag_samples})},
            foreground_admission=latency(admissions_ms), foreground_admissions_accepted=len(admissions_ms),
            progress_result_latency=latency(progress_ms), progress_results=progress_rows,
            delivery_events_persisted=sum(len(events) for events in delivery_events),
            encrypted_payload_rows=encrypted_payload_rows, encrypted_runtime=True,
            unexpected_waiter_errors=[], external_side_effect_attempts=side_effects)
        window_counts = dict(observations)
        window_last = dict(last_observed)

        # Independently measure real service startup from the same encrypted
        # durable waits. This is separate from the original schedule-burst
        # measurement above, so restore timing cannot conceal that baseline.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        report["pre_restore_watchers_cancelled"] = len(tasks)
        ledger = RunStore(output / "runtime.db", runtime_key)
        runtime = AgentRuntime(ledger, NS(llm=services), jobs, services, services)
        runtime.delivery = services
        original_observe = runtime._read_delivery_observation
        original_save = runtime._save_delivery_observation
        runtime._read_delivery_observation = observed
        runtime._save_delivery_observation = saved
        saved_observations.clear()
        restore_lags = []
        restore_done = asyncio.Event()
        async def restore_probe():
            loop = asyncio.get_running_loop()
            while not restore_done.is_set():
                tick = loop.time()
                await asyncio.sleep(interval)
                restore_lags.append(max(0., (loop.time() - tick - interval) * 1000))
        restore_sampler = asyncio.create_task(restore_probe())
        try:
            await asyncio.sleep(0)
            tick = time.perf_counter()
            await runtime.start()
            start_return_ms = (time.perf_counter() - tick) * 1000
            tasks = [runtime.tasks[run_id] for run_id in run_ids]
            async with asyncio.timeout(15):
                while len(saved_observations) < count:
                    assert all(not task.done() for task in tasks)
                    await asyncio.sleep(.02)
            restore_ready_ms = (time.perf_counter() - tick) * 1000
        finally:
            restore_done.set()
            await restore_sampler
        assert all(not task.done() for task in tasks)
        assert all(ledger.get(run_id)["status"] == "waiting_reply" for run_id in run_ids)
        assert all(len([e for e in ledger.all_events(run_id) if e["kind"] == "delivery"]) == 1 for run_id in run_ids)
        assert not side_effects
        report["runtime_start_recovery"] = {"start_return_ms": round(start_return_ms, 3),
            "all_waiters_observed_ms": round(restore_ready_ms, 3), "live_waiters": count,
            "event_loop_lag": latency(restore_lags), "duplicate_observation_events": 0,
            "external_side_effect_attempts": []}
        (output / "observations.json").write_text(json.dumps({"counts": window_counts,
            "first_offset_seconds": first_observed, "last_offset_seconds": window_last,
            "restore_observation_counts": {key: observations[key] - window_counts[key] for key in delivery_ids},
            "restore_event_loop_lag_ms": restore_lags,
            "event_loop_lag_ms": lag_ms, "phase_timer_samples": lag_samples,
            "foreground_admission_ms": admissions_ms,
            "progress_result_ms": progress_ms}, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        report["passed"] = False
        report["error"] = traceback.format_exc()
        report["external_side_effect_attempts"] = side_effects
    finally:
        stop_sampler.set()
        if sampler:
            await sampler
        for task in tasks:
            task.cancel()
        if tasks:
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
            report["cleanup"] = {"watchers_cancelled": sum(isinstance(item, asyncio.CancelledError) for item in outcomes),
                "unexpected_results": [repr(item) for item in outcomes if not isinstance(item, asyncio.CancelledError)]}
        changes = [name for name, digest in before.items() if hashes()[name] != digest]
        report["source_changed_during_run"] = changes
        if changes:
            report["passed"] = False
    (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=120)
    parser.add_argument("--seconds", type=float, default=3.2)
    parser.add_argument("--interval", type=float, default=.02)
    args = parser.parse_args()
    if not 1 <= args.count <= 200 or not 2.2 <= args.seconds <= 30 or not .005 <= args.interval <= .1:
        parser.error("count: 1–200; seconds: 2.2–30; interval: .005–.1")
    output = ROOT / "evaluation/results/delivery-scale-runtime" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    report = asyncio.run(measure(output, args.count, args.seconds, args.interval))
    print(json.dumps({"output": str(output), **{key: report.get(key) for key in (
        "passed", "count", "observed_seconds", "live_waiter_tasks", "observation_counts",
        "event_loop_lag", "foreground_admission", "progress_result_latency", "error")}}, ensure_ascii=False))
    return int(not report["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
