"""Application export contracts: fictional temporary databases, no live work.

Verifies artifact contents, complete-history vs board scope, receipt semantics,
formula safety and database immutability. No network, models or browser calls.
"""
from __future__ import annotations

import codecs
import csv
import gc
import io
import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from openpyxl import load_workbook

from app.jobs.application_export import COLUMNS, export_applications
from app.jobs.store import JobStore


HEADERS = {key: title for key, title, _ in COLUMNS}


def vacancy(index: int, **updates):
    return {"title": f"合成数据分析岗位{index}", "company": "合成雇主", "location": "深圳",
        "salary": "15-25K", "description": "合成职责，不是真实职位或简历。", "tags": ["SQL"],
        "source": "MockJobSite", "source_url": f"http://127.0.0.1:8080/job.html?id={index}",
        "external_id": str(index), "score": 88, "decision": "REVIEW", "reasoning": "合成匹配依据",
        "stage": "review", **updates}


def csv_rows(artifact):
    return list(csv.DictReader(io.StringIO(artifact.content_bytes.decode("utf-8-sig"))))


def workbook(artifact):
    return load_workbook(io.BytesIO(artifact.content_bytes), read_only=False, data_only=False)


def xlsx_rows(artifact):
    book = workbook(artifact)
    try:
        output = []
        for sheet in book:
            iterator = sheet.iter_rows(values_only=True)
            headers = next(iterator)
            output += [dict(zip(headers, values)) for values in iterator]
        return output
    finally:
        book.close()


class ApplicationExports(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.store = JobStore(Path(self.temp.name) / "fictional-jobs.db")

    def tearDown(self):
        gc.collect()

    def add_job(self, index=1, **updates):
        return self.store.upsert_job(vacancy(index, **updates))[0]

    def add_run(self, job_id, name="synthetic-run", *, started="2026-01-01T00:00:00+00:00", **values):
        self.store.create_delivery_run(name, job_id)
        with self.store._connect() as db:
            values = {"started_at": started, "updated_at": started, **values}
            db.execute("UPDATE delivery_runs SET " + ",".join(key + "=?" for key in values) + " WHERE id=?",
                       (*values.values(), name))

    def snapshot(self):
        with self.store._connect() as db:
            return "\n".join(db.iterdump())

    def test_default_complete_history_exceeds_100_and_includes_removed_and_hidden(self):
        ids = [self.add_job(index, score=0 if index == 1 else 88) for index in range(1, 138)]
        with self.store._connect() as db:
            db.execute("UPDATE jobs SET board_visible=0 WHERE id IN (?,?)", (ids[0], ids[2]))
            db.execute("UPDATE jobs SET removed_at='2026-01-03T00:00:00+00:00' WHERE id IN (?,?)", (ids[1], ids[2]))
        self.assertEqual(len(self.store.application_export_rows()), 137)
        self.assertEqual(len(self.store.application_export_rows(scope="board")), 134)
        for format, read in (("csv", csv_rows), ("xlsx", xlsx_rows)):
            with self.subTest(format=format):
                artifact = export_applications(self.store, format)
                rows = read(artifact)
                self.assertEqual([int(row[HEADERS["job_id"]]) for row in rows], ids)
                self.assertEqual(str(rows[0][HEADERS["score"]]), "0")
                self.assertEqual(rows[1][HEADERS["removed"]], "是")
                self.assertEqual(rows[0][HEADERS["on_board"]], "否")
                board = read(export_applications(self.store, format, "board"))
                self.assertEqual({int(row[HEADERS["job_id"]]) for row in board}, set(ids[3:]))
                self.assertTrue(artifact.filename.endswith("." + format))

    def test_latest_attempt_not_latest_updated_old_receipt_and_no_join_duplicates(self):
        ident = self.add_job()
        self.add_run(ident, "old", submitted=1, status="completed", updated_at="2026-12-31T00:00:00+00:00",
            reconciliation_json=json.dumps({"status": "confirmed", "application_id": 123}))
        self.add_run(ident, "new", started="2026-01-02T00:00:00+00:00", status="outcome_indeterminate",
            reconciliation_json=json.dumps({"status": "not_observed", "application_id": None}),
            reconciled_at="2026-01-03T00:00:00+00:00")
        rows = self.store.application_export_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["delivery_run_id"], "new")
        self.assertEqual(rows[0]["delivery_run_count"], 2)
        for read, format in ((csv_rows, "csv"), (xlsx_rows, "xlsx")):
            row = read(export_applications(self.store, format))[0]
            self.assertEqual(row[HEADERS["receipt_result"]], "本次未观察到投递记录")
            self.assertEqual(row[HEADERS["receipt_time"]], "2026-01-03T00:00:00+00:00")
            self.assertNotEqual(row[HEADERS["receipt_application_id"]], 123)

    def test_equal_start_time_uses_insertion_order_and_confirmed_receipt(self):
        ident = self.add_job()
        self.add_run(ident, "one", status="failed")
        self.add_run(ident, "two", submitted=1, status="waiting_for_reply",
            reconciliation_json=json.dumps({"status": "confirmed", "application_id": 456}),
            reconciled_at="2026-01-02T00:00:00+00:00")
        self.assertEqual(self.store.application_export_rows()[0]["delivery_run_id"], "two")
        row = xlsx_rows(export_applications(self.store))[0]
        self.assertEqual(row[HEADERS["receipt_result"]], "已核实投递")
        self.assertEqual(row[HEADERS["receipt_application_id"]], 456)

    def test_completed_execution_is_not_a_successful_application_without_submitted(self):
        ident = self.add_job()
        self.add_run(ident, status="completed", submitted=0)
        row = csv_rows(export_applications(self.store, "csv"))[0]
        self.assertEqual(row[HEADERS["delivery_status"]], "执行已完成")
        self.assertEqual(row[HEADERS["receipt_result"]], "未确认投递成功")
        self.assertEqual(row[HEADERS["receipt_time"]], "")

    def test_read_only_sql_and_minimal_projection_no_private_payloads(self):
        ident = self.add_job(description="NEVER_EXPORT_RESUME_PRIVATE", reasoning="NEVER_EXPORT_TOKEN_PRIVATE")
        self.add_run(ident, status="waiting_for_reply", detail="NEVER_EXPORT_DETAIL_PRIVATE",
            resume_document_id="NEVER_EXPORT_RESUME_ID_PRIVATE", resume_digest="NEVER_EXPORT_DIGEST_PRIVATE")
        with self.store._connect() as db:
            db.execute("UPDATE jobs SET recruiter_reply='NEVER_EXPORT_REPLY_PRIVATE',recruiter_reply_at='2026-01-02' WHERE id=?", (ident,))
            db.execute("INSERT INTO mock_delivery_queue(job_id,session_id,resume_stamp,binding_json,job_hash,min_score,status,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (ident, "NEVER_EXPORT_SESSION_PRIVATE", "private", '{"token":"NEVER_EXPORT_BINDING_PRIVATE"}', "private", 85, "dispatched", "2026-01-02"))
        before = self.snapshot()
        statements = []
        connect = self.store._connect
        def observed_connect():
            db = connect()
            db.set_trace_callback(statements.append)
            return db
        with patch.object(self.store, "_connect", side_effect=observed_connect):
            for format, read in (("csv", csv_rows), ("xlsx", xlsx_rows)):
                rows = read(export_applications(self.store, format))
                self.assertNotIn("NEVER_EXPORT", json.dumps(rows, ensure_ascii=False))
                self.assertEqual(rows[0][HEADERS["communication_status"]], "已收到HR回复")
                self.assertEqual(rows[0][HEADERS["queue_status"]], "已调度")
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(statements)
        self.assertTrue(all(re.match(r"\s*(?:SELECT|PRAGMA query_only=ON)", statement, re.I) for statement in statements), statements)

    def test_empty_artifacts_have_only_headers_and_true_formats(self):
        csv_artifact = export_applications(self.store, "csv")
        self.assertTrue(csv_artifact.content_bytes.startswith(codecs.BOM_UTF8))
        csv_data = list(csv.reader(io.StringIO(csv_artifact.content_bytes.decode("utf-8-sig"))))
        self.assertEqual(csv_data, [[title for _, title, _ in COLUMNS]])
        xlsx_artifact = export_applications(self.store)
        self.assertTrue(xlsx_artifact.content_bytes.startswith(b"PK"))
        self.assertEqual(xlsx_artifact.media_type, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        book = workbook(xlsx_artifact)
        try:
            self.assertEqual(list(book.active.values), [tuple(title for _, title, _ in COLUMNS)])
        finally:
            book.close()

    def test_formula_injection_and_control_characters_do_not_become_formulas(self):
        attacks = ["=1+2", "+SUM(1,2)", "-1+2", "@SUM(1,2)", " \t=1+2", "\r\n@SUM(1,2)", "\x00\ufeff=1+2"]
        for index, attack in enumerate(attacks, 1):
            self.add_job(index, title=attack)
        rows = csv_rows(export_applications(self.store, "csv"))
        self.assertTrue(all(row[HEADERS["title"]].startswith("'") for row in rows))
        book = workbook(export_applications(self.store))
        try:
            for row in list(book.active.iter_rows())[1:]:
                self.assertTrue(all(cell.data_type != "f" and cell.hyperlink is None for cell in row))
                self.assertEqual(row[1].data_type, "s")
                self.assertNotRegex(row[1].value, r"[\x00-\x1f\ufeff]")
        finally:
            book.close()

    def test_source_links_are_plain_http_sources_without_contact_or_auth_queries(self):
        links = ["javascript:alert(1)", "file:///C:/private/resume.pdf", "https://user:password@example.invalid/job/1",
                 "https://example.invalid/job?id=123&accessToken=private-token&email=person%40example.invalid#private-fragment"]
        for index, link in enumerate(links, 1):
            self.add_job(index, source_url=link)
        before = self.snapshot()
        for format, read in (("csv", csv_rows), ("xlsx", xlsx_rows)):
            rows = read(export_applications(self.store, format))
            self.assertTrue(all(not row[HEADERS["source_url"]] for row in rows[:3]))
            self.assertEqual(rows[3][HEADERS["source_url"]], "https://example.invalid/job?id=123")
        self.assertEqual(self.snapshot(), before)

    def test_zero_missing_values_and_chinese_salary_dates_are_preserved(self):
        self.add_job(score=0, company="中文合成公司", location="杭州", salary="面议")
        rows = xlsx_rows(export_applications(self.store))
        self.assertEqual(rows[0][HEADERS["score"]], 0)
        self.assertEqual(rows[0][HEADERS["company"]], "中文合成公司")
        self.assertEqual(rows[0][HEADERS["salary"]], "面议")
        self.assertFalse(rows[0][HEADERS["receipt_application_id"]])
        self.assertFalse(rows[0][HEADERS["delivery_started_at"]])
        self.assertEqual(rows[0][HEADERS["delivery_run_count"]], 0)

    def test_invalid_receipt_json_and_non_scalar_ids_do_not_leak_payloads(self):
        ident = self.add_job()
        self.add_run(ident, reconciliation_json="not valid JSON")
        self.assertIsNone(self.store.application_export_rows()[0]["receipt_status"])
        with self.store._connect() as db:
            db.execute("UPDATE delivery_runs SET reconciliation_json=? WHERE job_id=?",
                (json.dumps({"status": "unknown", "application_id": {"resume": "NEVER_EXPORT_NESTED"}}), ident))
        self.assertNotIn("NEVER_EXPORT_NESTED", str(xlsx_rows(export_applications(self.store))))

    def test_oversized_excel_cell_is_explicit_and_csv_does_not_silently_truncate(self):
        text = "合成" * 17_000
        self.add_job(title=text)
        with self.assertRaisesRegex(ValueError, "CSV"):
            export_applications(self.store)
        self.assertEqual(csv_rows(export_applications(self.store, "csv"))[0][HEADERS["title"]], text)

    def test_invalid_options_fail_before_querying_or_writing(self):
        store = Mock()
        for format, scope in (("xls", "all"), ("CSV", "all"), ("csv", "deleted")):
            with self.subTest(format=format, scope=scope), self.assertRaises(ValueError):
                export_applications(store, format, scope)
        store.application_export_rows.assert_not_called()
        with self.assertRaises(ValueError):
            self.store.application_export_rows(scope="anything")

    def test_new_stores_do_not_create_retired_learning_feature_schema(self):
        with self.store._connect() as db:
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','index')")}
        self.assertNotIn("learning_plans", names)
        self.assertNotIn("idx_learning_plans_resume", names)
        for method in ("save_learning_plan", "learning_plan", "learning_plans"):
            self.assertFalse(hasattr(self.store, method))

    def test_opening_legacy_store_preserves_retired_learning_table_and_data(self):
        legacy = Path(self.temp.name) / "fictional-legacy.db"
        with sqlite3.connect(legacy) as db:
            db.execute("CREATE TABLE learning_plans(id TEXT PRIMARY KEY,resume_id TEXT,payload_json TEXT,created_at TEXT,updated_at TEXT)")
            db.execute("CREATE INDEX idx_learning_plans_resume ON learning_plans(resume_id,created_at DESC)")
            db.execute("INSERT INTO learning_plans VALUES (?,?,?,?,?)",
                       ("synthetic-old-plan", "synthetic-old-resume", '{"fictional":true}', "old", "old"))
        JobStore(legacy)
        with sqlite3.connect(legacy) as db:
            self.assertEqual(db.execute("SELECT * FROM learning_plans").fetchall(),
                [("synthetic-old-plan", "synthetic-old-resume", '{"fictional":true}', "old", "old")])
            self.assertIsNotNone(db.execute("SELECT name FROM sqlite_master WHERE name='idx_learning_plans_resume'").fetchone())


if __name__ == "__main__":
    unittest.main()
