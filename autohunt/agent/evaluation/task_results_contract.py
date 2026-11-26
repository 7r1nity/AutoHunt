"""Read-only task snapshots from fictional temporary SQLite records.

No application globals, provider calls, real browser, or user database is loaded.
"""
from __future__ import annotations

import copy
import gc
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography.fernet import Fernet

from app.jobs.store import JobStore
from app.runtime.store import RunStore
from app.runtime.task_results import read_task_snapshot, render_task_snapshot
from evaluation.application_export_contract import vacancy


class TaskResults(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.runs = RunStore(root / "runs.db", Fernet.generate_key().decode())
        self.jobs = JobStore(root / "jobs.db")
        self.counter = 0

    def tearDown(self):
        gc.collect()

    def run_record(self, *, session="default", kind="chat", status="running", checkpoint=None, payload=None):
        self.counter += 1
        run, _ = self.runs.create(session, f"synthetic-{self.counter}", kind, payload or {})
        return self.runs.update(run["id"], status=status, checkpoint=checkpoint or {})

    def search(self, name="synthetic-search", *, count=3, status="running", start=1):
        self.jobs.create_task(name, "mock", "合成数据岗位")
        self.jobs.update_task(name, discovered=count + 4, status=status)
        ids = []
        for index in range(start, start + count):
            ident, _ = self.jobs.upsert_job(vacancy(index))
            ids.append(ident)
            self.jobs.event(name, "job_stored", "合成核对回执", sample={"job_id": ident, "filter_status": "pass"})
        return ids

    def delivery(self, job_id, name="synthetic-delivery", *, status="waiting_for_reply", submitted=False, checkpoint=None):
        self.jobs.create_delivery_run(name, job_id)
        self.jobs.update_delivery_run(name, status=status, submitted=submitted, checkpoint=checkpoint)
        return self.run_record(kind="delivery", checkpoint={"delivery_id": name})

    def read(self, **kwargs):
        return read_task_snapshot(self.runs, self.jobs, "default", **kwargs)

    def test_empty_session_returns_zero_without_global_board_fallback(self):
        self.jobs.upsert_job(vacancy(50))
        snapshot = self.read()
        self.assertEqual(snapshot["status"], "idle")
        self.assertEqual(snapshot["jobs"], [])
        self.assertTrue(all(value == 0 for value in snapshot["counts"].values()))
        self.assertIn("尚无已保存", render_task_snapshot(snapshot))

    def test_partial_running_search_returns_only_persisted_exact_round(self):
        ids = self.search(count=4)
        self.jobs.upsert_job(vacancy(99))
        run = self.run_record(checkpoint={"browser_id": "synthetic-search"})
        snapshot = self.read()
        self.assertEqual(snapshot["run_ids"], [run["id"]])
        self.assertEqual({job["id"] for job in snapshot["jobs"]}, set(ids))
        self.assertEqual(snapshot["counts"]["reviewed"], 4)
        self.assertEqual(snapshot["counts"]["qualified"], 4)
        self.assertEqual(snapshot["counts"]["discovered"], 8)
        self.assertEqual(snapshot["counts"]["submitted"], 0)

    def test_daily_partial_search_before_selection_is_not_empty(self):
        self.search(count=2)
        run = self.run_record(kind="daily", checkpoint={"phase": "daily", "browser_id": "synthetic-search"})
        snapshot = self.read(run_ids=[run["id"]])
        self.assertEqual(snapshot["counts"]["reviewed"], 2)
        self.assertEqual(len(snapshot["jobs"]), 2)

    def test_stopped_daily_includes_current_phases_and_real_submission_receipt(self):
        ids = self.search(count=4)
        self.jobs.create_delivery_run("receipt", ids[0])
        self.jobs.update_delivery_run("receipt", status="completed", submitted=True)
        run = self.run_record(kind="daily", status="cancelled", checkpoint={"browser_id": "synthetic-search",
            "selected": ids, "jobs": {str(ids[0]): {"phase": "submitted", "delivery_id": "receipt"},
                str(ids[1]): {"phase": "waiting_reply"}, str(ids[2]): {"phase": "submit_started"},
                str(ids[3]): {"phase": "pending"}}})
        snapshot = self.read(run_ids=[run["id"]])
        self.assertEqual(snapshot["counts"]["submitted"], 1)
        self.assertEqual(snapshot["counts"]["waiting_reply"], 1)
        self.assertEqual(snapshot["counts"]["uncertain"], 1)
        self.assertEqual(snapshot["counts"]["queued"], 1)
        self.assertEqual(snapshot["status"], "uncertain")
        self.assertIn("已核实投递 1", render_task_snapshot(snapshot, heading="已停止，当前结果"))

    def test_daily_submitted_phase_without_receipt_is_not_success(self):
        ident = self.jobs.upsert_job(vacancy(1))[0]
        run = self.run_record(kind="daily", checkpoint={"jobs": {str(ident): {"phase": "submitted"}}})
        snapshot = self.read(run_ids=[run["id"]])
        self.assertEqual(snapshot["counts"]["submitted"], 0)
        self.assertEqual(snapshot["counts"]["uncertain"], 1)

    def test_completed_delivery_without_submitted_or_confirmed_is_not_success(self):
        ident = self.jobs.upsert_job(vacancy(1))[0]
        run = self.delivery(ident, status="completed", submitted=False)
        self.assertEqual(self.read(run_ids=[run["id"]])["counts"]["submitted"], 0)

    def test_confirmed_reconciliation_is_counted_and_dated(self):
        ident = self.jobs.upsert_job(vacancy(1))[0]
        run = self.delivery(ident, status="completed", submitted=False)
        with self.jobs._connect() as db:
            db.execute("UPDATE delivery_runs SET reconciliation_json=?,reconciled_at=? WHERE id=?",
                (json.dumps({"status": "confirmed", "application_id": 900}), datetime.now(UTC).isoformat(), "synthetic-delivery"))
        snapshot = self.read(run_ids=[run["id"]])
        self.assertEqual(snapshot["counts"]["submitted"], 1)
        self.assertEqual(snapshot["today_submitted"], 1)

    def test_unknown_submission_is_preserved_without_replay(self):
        ident = self.jobs.upsert_job(vacancy(1))[0]
        run = self.delivery(ident, status="outcome_indeterminate", checkpoint="submit_started")
        snapshot = self.read(run_ids=[run["id"]])
        self.assertEqual(snapshot["counts"]["submitted"], 0)
        self.assertEqual(snapshot["counts"]["uncertain"], 1)
        self.assertEqual(snapshot["jobs"][0]["status"], "uncertain")

    def test_confirmed_receipt_wins_over_stale_daily_failure_and_queue_phase(self):
        ident = self.jobs.upsert_job(vacancy(1))[0]
        self.jobs.create_delivery_run("confirmed", ident)
        self.jobs.update_delivery_run("confirmed", status="failed", submitted=True)
        run = self.run_record(kind="daily", checkpoint={"jobs": {str(ident): {
            "phase": "failed", "delivery_id": "confirmed"}}})
        snapshot = self.read(run_ids=[run["id"]])
        self.assertEqual(snapshot["counts"]["submitted"], 1)
        self.assertEqual(snapshot["counts"]["failed"], 0)
        self.assertEqual(snapshot["jobs"][0]["status"], "submitted")

    def test_same_job_multiple_delivery_attempts_are_not_duplicate_submissions(self):
        ident = self.jobs.upsert_job(vacancy(1))[0]
        first = self.delivery(ident, "one", status="completed", submitted=True)
        second = self.delivery(ident, "two", status="completed", submitted=True)
        snapshot = self.read(run_ids=[first["id"], second["id"]])
        self.assertEqual(snapshot["counts"]["submitted"], 1)
        self.assertEqual(len(snapshot["jobs"]), 1)

    def test_unknown_effect_is_not_reported_as_confirmed_failure(self):
        ident = self.jobs.upsert_job(vacancy(1))[0]
        run = self.delivery(ident, status="failed", checkpoint="submission_unconfirmed")
        snapshot = self.read(run_ids=[run["id"]])
        self.assertEqual(snapshot["counts"]["uncertain"], 1)
        self.assertEqual(snapshot["counts"]["failed"], 0)

    def test_newer_control_run_does_not_replace_latest_real_task(self):
        self.search(status="stopped")
        old = self.run_record(status="cancelled", checkpoint={"browser_id": "synthetic-search"})
        for payload, cp in (({"control_kind": "progress"}, {}), ({"message": "停止"}, {}),
                            ({}, {"phase": "control_result"})):
            self.run_record(payload=payload, checkpoint=cp)
        snapshot = self.read()
        self.assertEqual(snapshot["scope"]["selection"], "latest")
        self.assertEqual(snapshot["run_ids"], [old["id"]])
        self.assertEqual(snapshot["counts"]["reviewed"], 3)

    def test_explicit_scope_does_not_fall_back_to_other_task_or_session(self):
        self.search("other", start=40)
        other = self.runs.create_session("合成其他会话")["id"]
        foreign = self.run_record(session=other, checkpoint={"browser_id": "other"})
        self.assertEqual(self.read(run_ids=[foreign["id"]])["jobs"], [])
        self.assertEqual(self.read(run_ids=[])["jobs"], [])

    def test_same_job_and_search_bound_through_multiple_runs_are_deduplicated(self):
        self.search(count=1)
        one = self.run_record(checkpoint={"browser_id": "synthetic-search"})
        self.run_record(kind="browser", checkpoint={"browser_id": "synthetic-search"})
        snapshot = self.read(run_ids=[one["id"]])
        self.assertEqual(len(snapshot["run_ids"]), 2)
        self.assertEqual(snapshot["counts"]["reviewed"], 1)
        self.assertEqual(snapshot["counts"]["discovered"], 5)

    def test_delivery_child_of_latest_round_is_included_without_global_latest(self):
        ids = self.search(count=1, status="completed")
        search_run = self.run_record(status="completed", checkpoint={"browser_id": "synthetic-search"})
        deliver_run = self.delivery(ids[0], status="completed", submitted=True)
        snapshot = self.read(run_ids=[search_run["id"]])
        self.assertIn(deliver_run["id"], snapshot["run_ids"])
        self.assertEqual(snapshot["counts"]["submitted"], 1)

    def test_latest_terminal_observer_recovers_complete_search_root(self):
        ids = self.search(count=4, status="stopped")
        root = self.run_record(status="cancelled", checkpoint={"browser_id": "synthetic-search"})
        child = self.delivery(ids[2], status="completed", submitted=True)
        self.runs.update(child["id"], status="completed")
        snapshot = self.read()
        self.assertEqual(snapshot["scope"]["selection"], "latest")
        self.assertIn(root["id"], snapshot["run_ids"])
        self.assertIn(child["id"], snapshot["run_ids"])
        self.assertEqual(snapshot["counts"]["discovered"], 8)
        self.assertEqual(snapshot["counts"]["reviewed"], 4)
        self.assertEqual(len(snapshot["jobs"]), 4)

    def test_new_explicit_single_job_task_wins_over_older_search_root(self):
        ids = self.search(count=4, status="completed")
        old = self.run_record(status="completed", checkpoint={"browser_id": "synthetic-search"})
        newer = self.run_record(status="completed", checkpoint={"plan": {"id": "new-single-job-task", "steps": [
            {"tool": "mock_delivery_start", "arguments": {}, "result": {"job_ids": [ids[2]]}}]}})
        child = self.delivery(ids[2], status="completed", submitted=True)
        self.runs.update(child["id"], status="completed")
        snapshot = self.read()
        self.assertIn(newer["id"], snapshot["run_ids"])
        self.assertNotIn(old["id"], snapshot["run_ids"])
        self.assertEqual([job["id"] for job in snapshot["jobs"]], [ids[2]])
        self.assertEqual(snapshot["counts"]["submitted"], 1)

    def test_more_than_one_hundred_results_are_not_silently_truncated(self):
        self.search(count=121)
        self.run_record(checkpoint={"browser_id": "synthetic-search"})
        snapshot = self.read()
        self.assertEqual(len(snapshot["jobs"]), 121)
        self.assertEqual(snapshot["counts"]["reviewed"], 121)

    def test_reads_do_not_mutate_databases_and_digest_is_stable(self):
        self.search()
        self.run_record(checkpoint={"browser_id": "synthetic-search"})
        def dump(store, connect):
            with connect() as db:
                return "\n".join(db.iterdump())
        before = (dump(self.jobs, self.jobs._connect), dump(self.runs, self.runs.connect))
        first, second = self.read(), self.read()
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(before, (dump(self.jobs, self.jobs._connect), dump(self.runs, self.runs.connect)))

    def test_atomic_terminal_projection_only_affects_explicit_run_state(self):
        self.search()
        run = self.run_record(checkpoint={"browser_id": "synthetic-search"})
        normal = self.read(run_states={run["id"]: "cancelled"})
        terminal = self.read(run_ids=[run["id"]], run_states={run["id"]: "cancelled"})
        self.assertEqual(normal["status"], "running")
        self.assertEqual(terminal["status"], "stopped")
        self.assertEqual(normal["counts"], terminal["counts"])
        self.assertEqual(self.runs.get(run["id"])["status"], "running")

    def test_missing_history_receipt_date_does_not_invent_daily_total(self):
        ident = self.jobs.upsert_job(vacancy(1))[0]
        self.delivery(ident, status="completed", submitted=True)
        snapshot = self.read()
        self.assertIsNone(snapshot["today_submitted"])
        self.assertTrue(snapshot["limitations"])

    def test_render_preserves_clickable_links_and_export_scope(self):
        self.search(count=1)
        self.run_record(checkpoint={"browser_id": "synthetic-search"})
        snapshot = self.read()
        rendered = render_task_snapshot(snapshot)
        self.assertIn("](http://127.0.0.1:8080/job.html?id=1)", rendered)
        self.assertIn("含其他轮次，不等于本轮清单", rendered)
        self.assertIn("岗位 #1", rendered)

    def test_snapshot_does_not_export_secret_query_or_private_job_payload(self):
        ident = self.jobs.upsert_job(vacancy(1, source_url="http://127.0.0.1:8080/job.html?id=1&api_key=synthetic-secret&email=synthetic%40example.com",
            description="PRIVATE_SYNTHETIC_RESUME_NOT_FOR_RESULTS"))[0]
        run = self.run_record(kind="daily", checkpoint={"selected": [ident], "jobs": {str(ident): {"phase": "pending"}}})
        snapshot = self.read(run_ids=[run["id"]])
        serialized = json.dumps(snapshot)
        self.assertNotIn("synthetic-secret", serialized)
        self.assertNotIn("synthetic%40example", serialized)
        self.assertNotIn("PRIVATE_SYNTHETIC_RESUME_NOT_FOR_RESULTS", serialized)
        self.assertEqual(snapshot["jobs"][0]["source_url"], "http://127.0.0.1:8080/job.html?id=1")

    def test_broken_search_reference_is_not_replaced_by_global_jobs(self):
        self.jobs.upsert_job(vacancy(99))
        self.run_record(checkpoint={"browser_id": "missing-search"})
        snapshot = self.read()
        self.assertEqual(snapshot["jobs"], [])
        self.assertTrue(snapshot["limitations"])


if __name__ == "__main__":
    unittest.main()
