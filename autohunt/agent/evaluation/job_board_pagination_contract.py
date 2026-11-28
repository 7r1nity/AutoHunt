"""Complete board pagination: temporary SQLite and isolated authenticated API.

No existing database, candidate material, model, browser or recruitment task is
used. HTTP construction happens in a subprocess without entering app lifespan.
"""
from __future__ import annotations

import gc
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from app.jobs.store import JobSnapshotChanged, JobStore
from evaluation.application_export_contract import vacancy


class PaginationStore(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.store = JobStore(Path(self.temp.name) / "synthetic-jobs.db")
        self.ids = [self.store.upsert_job(vacancy(index))[0] for index in range(250)]
        with self.store._connect() as db:
            db.execute("UPDATE jobs SET last_seen_at='2026-01-01T00:00:00Z'")

    def tearDown(self):
        gc.collect()

    def all_pages(self, **kwargs):
        result, offset, snapshot = [], 0, None
        while True:
            page = self.store.list_jobs_page(offset=offset, snapshot=snapshot, **kwargs)
            self.assertEqual(page["offset"], offset)
            self.assertEqual(page["total"], 250)
            if snapshot is not None:
                self.assertEqual(page["snapshot"], snapshot)
            snapshot = page["snapshot"]
            result.extend(item.id for item in page["items"])
            if page["next_offset"] is None:
                return result
            offset = page["next_offset"]

    def test_250_rows_all_loaded_once_in_stable_order_and_count_is_unbounded(self):
        ids = self.all_pages(limit=100)
        self.assertEqual(ids, list(reversed(self.ids)))
        self.assertEqual(len(set(ids)), 250)
        self.assertEqual(self.store.count_jobs(), 250)
        self.assertEqual(len(self.store.list_jobs()), 100)  # legacy API stays bounded
        self.assertEqual(len(self.store.list_jobs(limit=1000)), 250)

    def test_visible_count_and_pages_exclude_hidden_removed_and_rejected(self):
        extra = [self.store.upsert_job(vacancy(300 + index))[0] for index in range(3)]
        with self.store._connect() as db:
            db.execute("UPDATE jobs SET board_visible=0 WHERE id=?", (extra[0],))
            db.execute("UPDATE jobs SET removed_at='2026-01-02T00:00:00Z' WHERE id=?", (extra[1],))
            db.execute("UPDATE jobs SET stage='rejected' WHERE id=?", (extra[2],))
        self.assertEqual(self.store.count_jobs(), 251)
        self.assertEqual(self.store.count_jobs(include_rejected=False), 250)
        self.assertEqual(self.store.list_jobs_page()["total"], 250)
        rejected = self.store.list_jobs_page(stage="rejected")
        self.assertEqual(rejected["total"], 1)
        self.assertEqual(rejected["items"][0].id, extra[2])

    def test_stage_filter_total_and_page_membership_agree(self):
        with self.store._connect() as db:
            db.execute("UPDATE jobs SET stage='applying' WHERE id<=10")
        page = self.store.list_jobs_page(stage="applying")
        self.assertEqual(page["total"], 10)
        self.assertEqual(self.store.count_jobs(stage="applying"), 10)
        self.assertEqual({row.id for row in page["items"]}, set(self.ids[:10]))

    def test_between_page_stage_insert_remove_and_order_changes_refuse_old_snapshot(self):
        for statement in (
            "UPDATE jobs SET stage='applying' WHERE id=1",
            "UPDATE jobs SET score=99 WHERE id=2",
            "UPDATE jobs SET board_visible=0 WHERE id=3",
            "UPDATE jobs SET removed_at='2026-01-02T00:00:00Z' WHERE id=4",
        ):
            page = self.store.list_jobs_page()
            with self.store._connect() as db:
                db.execute(statement)
            with self.assertRaises(JobSnapshotChanged):
                self.store.list_jobs_page(offset=100, snapshot=page["snapshot"])
        page = self.store.list_jobs_page()
        self.store.upsert_job(vacancy(999))
        with self.assertRaises(JobSnapshotChanged):
            self.store.list_jobs_page(offset=100, snapshot=page["snapshot"])

    def test_monitor_timestamp_does_not_invalidate_unchanged_display(self):
        first = self.store.list_jobs_page()
        with self.store._connect() as db:
            db.execute("UPDATE jobs SET reply_last_checked_at='2026-01-02T00:00:00Z'")
        second = self.store.list_jobs_page(offset=100, snapshot=first["snapshot"])
        self.assertEqual(first["snapshot"], second["snapshot"])

    def test_review_approval_and_its_evidence_change_are_part_of_snapshot(self):
        row = self.store.get_job(self.ids[-1])
        with self.store._connect() as db:
            db.execute("INSERT INTO job_review_decisions VALUES(?,?,?,?,?)",
                       (row["id"], "synthetic-session", "synthetic-run", self.store.review_fingerprint(row), "2026-01-01"))
        first = self.store.list_jobs_page()
        self.assertTrue(first["items"][0].review_approved)
        with self.store._connect() as db:
            db.execute("UPDATE jobs SET filter_evidence=? WHERE id=?", ('{"status":"unknown"}', row["id"]))
        with self.assertRaises(JobSnapshotChanged):
            self.store.list_jobs_page(offset=100, snapshot=first["snapshot"])
        self.assertFalse(self.store.list_jobs_page()["items"][0].review_approved)

    def test_paging_is_read_only_and_does_not_select_jd_or_candidate_material(self):
        with self.store._connect() as db:
            before = "\n".join(db.iterdump())
        original = self.store._connect
        statements = []
        def connect():
            db = original()
            db.set_trace_callback(statements.append)
            return db
        self.store._connect = connect
        self.all_pages()
        with original() as db:
            after = "\n".join(db.iterdump())
        self.assertEqual(before, after)
        sql = " ".join(statements).lower()
        for forbidden in ("description", "resume", "conversation", "select j.*", "insert ", "update ", "delete "):
            self.assertNotIn(forbidden, sql)

    def test_empty_board_and_invalid_or_unbound_page_bounds_are_explicit(self):
        for kwargs in ({"limit": 0}, {"limit": 201}, {"limit": True}, {"offset": -1}, {"offset": True},
                       {"offset": 100}, {"offset": 251, "snapshot": self.store.list_jobs_page()["snapshot"]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.store.list_jobs_page(**kwargs)
        with self.store._connect() as db:
            db.execute("UPDATE jobs SET board_visible=0")
        page = self.store.list_jobs_page()
        self.assertEqual(page["total"], 0)
        self.assertEqual(page["items"], [])
        self.assertIsNone(page["next_offset"])


class PaginationHTTP(unittest.TestCase):
    def test_authenticated_250_row_pages_legacy_compatibility_and_conflicts(self):
        script = r'''
import json
from fastapi.testclient import TestClient
from app.api.application import app
from app.core.dependencies import job_store
from evaluation.application_export_contract import vacancy
key='synthetic-pagination-key'
for index in range(250): job_store.upsert_job(vacancy(index))
client=TestClient(app)
headers={'X-AutoHunt-Key':key}
assert client.get('/api/v1/jobs/page').status_code==401
first=client.get('/api/v1/jobs/page',headers=headers)
assert first.status_code==200, first.text[:200]
first=first.json();assert first['total']==250 and len(first['items'])==100
ids=[item['id'] for item in first['items']]
page=first
while page['next_offset'] is not None:
 page=client.get('/api/v1/jobs/page',headers=headers,params={'offset':page['next_offset'],'snapshot':first['snapshot']}).json()
 ids.extend(item['id'] for item in page['items'])
assert len(ids)==250 and len(set(ids))==250
assert len(client.get('/api/v1/jobs',headers=headers).json())==100
assert len(client.get('/api/v1/jobs?limit=250',headers=headers).json())==250
assert client.get('/api/v1/jobs/page?offset=100',headers=headers).status_code==422
for query in ['limit=0','limit=201','offset=-1','snapshot=broken','stage=unknown']:
 assert client.get('/api/v1/jobs/page?'+query,headers=headers).status_code==422,query
with job_store._connect() as db:db.execute("UPDATE jobs SET stage='applying' WHERE id=1")
conflict=client.get('/api/v1/jobs/page',headers=headers,params={'offset':100,'snapshot':first['snapshot']})
assert conflict.status_code==409 and conflict.json()['detail']['code']=='jobs_snapshot_changed'
print(json.dumps({'passed':True,'checks':13,'business_actions':0,'model_calls':0}))
'''
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as root:
            from cryptography.fernet import Fernet
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith("AUTOHUNT_") and not key.endswith(("_API_KEY", "_TOKEN"))}
            env.update(AUTOHUNT_DATA_ROOT=root, AUTOHUNT_DATA_KEY=Fernet.generate_key().decode(),
                       AUTOHUNT_API_KEY="synthetic-pagination-key", AUTOHUNT_LLM_PROVIDER="mock",
                       AUTOHUNT_LLM_MODEL="mock-autohunt-v1", PYTHONUTF8="1")
            result = subprocess.run([sys.executable, "-X", "utf8", "-c", script],
                cwd=Path(__file__).parents[1], env=env, text=True, capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr[-6000:] + result.stdout[-1500:])
            self.assertTrue(json.loads(result.stdout.strip().splitlines()[-1])["passed"])


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    output = Path(__file__).parent / "results" / "job-board-pagination" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    summary = {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
               "model_calls": 0, "real_recruitment_actions": 0,
               "failure_details": [{"test": str(test), "traceback": error} for test, error in result.failures + result.errors]}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), **{key: summary[key] for key in ("tests", "failures", "errors", "model_calls", "real_recruitment_actions")}}))
    raise SystemExit(int(not result.wasSuccessful()))
