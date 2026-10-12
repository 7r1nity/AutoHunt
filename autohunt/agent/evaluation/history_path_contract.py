"""Local maintenance regression: synthetic temporary databases only."""
from __future__ import annotations

import json
import gc
import os
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from app.core.config import BACKEND_ROOT, mock_history_path
from app.intent.store import RouterStore
from app.jobs.store import JobStore
from app.runtime.history_reset import HistoryResetter, JOB_TABLES, RUN_TABLES, ROUTER_TABLES, MOCK_TABLES
from app.runtime.store import RunStore


class PathContracts(unittest.TestCase):
    def test_documented_split_layout_path_is_absolute(self):
        self.assertEqual(mock_history_path("../../mock-site/job_site.db"), BACKEND_ROOT.parents[1] / "mock-site" / "job_site.db")

    def test_relative_path_does_not_depend_on_process_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("app.core.config.os.path.abspath", wraps=os.path.abspath):
                before = Path.cwd()
                try:
                    os.chdir(folder)
                    value = mock_history_path("../../mock-site/job_site.db")
                finally:
                    os.chdir(before)
        self.assertEqual(value, BACKEND_ROOT.parents[1] / "mock-site" / "job_site.db")

    def test_explicit_absolute_path_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            value = Path(folder) / "mock.db"
            self.assertEqual(mock_history_path(str(value)), value)

    def test_empty_path_stays_disabled(self):
        for value in (None, "", "  "):
            self.assertIsNone(mock_history_path(value))


class ResetContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="autohunt-reset-path-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "autohunt" / "agent" / "data"
        self.jobs = JobStore(self.root / "autohunt.db")
        self.runs = RunStore(self.root / "runtime.db", Fernet.generate_key().decode())
        self.router = RouterStore(self.root / "router.db")
        self.jobs.add_conversation_message("user", "合成历史", conversation_id="default")
        self.runs.create("default", "synthetic", "chat", {"message": "合成任务"})
        self.mock = Path(self.temp.name).resolve() / "mock-site" / "job_site.db"
        self.mock.parent.mkdir()
        with closing(sqlite3.connect(self.mock)) as db:
            db.executescript("""
                PRAGMA foreign_keys=ON;
                CREATE TABLE users(id INTEGER PRIMARY KEY);
                CREATE TABLE jobs(id INTEGER PRIMARY KEY);
                CREATE TABLE form_templates(id INTEGER PRIMARY KEY);
                CREATE TABLE resumes(id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users);
                CREATE TABLE applications(id INTEGER PRIMARY KEY, resume_id INTEGER REFERENCES resumes, job_id INTEGER REFERENCES jobs);
                CREATE TABLE messages(id INTEGER PRIMARY KEY, application_id INTEGER REFERENCES applications);
                CREATE TABLE form_responses(id INTEGER PRIMARY KEY, application_id INTEGER REFERENCES applications, template_id INTEGER REFERENCES form_templates);
                INSERT INTO users VALUES(1);
                INSERT INTO jobs VALUES(1);
                INSERT INTO form_templates VALUES(1);
                INSERT INTO resumes VALUES(1,1);
                INSERT INTO applications VALUES(1,1,1);
                INSERT INTO messages VALUES(1,1);
                INSERT INTO form_responses VALUES(1,1,1);
            """)
            db.commit()
        self.reset = HistoryResetter(self.root, self.jobs, self.runs, self.router, None, None, None, None, None,
                                    mock_database=mock_history_path(str(self.mock)))
        self.resume = self.root / "private" / "resumes" / "synthetic.enc"
        self.resume.parent.mkdir(parents=True)
        self.resume.write_text("合成简历", encoding="utf-8")
        self.credentials = self.root / "private" / "llm-config.enc"
        self.credentials.write_text("synthetic config", encoding="utf-8")

    def count(self, path, table):
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            return db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]

    def tearDown(self):
        self.reset = self.jobs = self.runs = self.router = None
        gc.collect()

    def test_resolved_relative_configuration_is_available_and_clears_with_backup(self):
        with patch("app.core.config.BACKEND_ROOT", self.root.parent):
            self.reset.mock_database = mock_history_path("../../mock-site/job_site.db")
        self.assertTrue(self.reset.mock_available())
        result = self.reset._clear_files_and_databases(True)
        self.assertTrue(result["cleared"])
        for table in MOCK_TABLES:
            self.assertEqual(self.count(self.mock, table), 0)
        for table in ("users", "jobs", "form_templates"):
            self.assertEqual(self.count(self.mock, table), 1)
        for path, tables in ((self.jobs.path, JOB_TABLES), (self.runs.path, RUN_TABLES), (self.router.path, ROUTER_TABLES)):
            for table in tables:
                self.assertEqual(self.count(path, table), 0)
        backup = self.root / "history-backups" / result["backup_id"]
        self.assertEqual(self.count(backup / "database-3.db", "messages"), 1)
        self.assertEqual(self.count(backup / "database-0.db", "conversation_messages"), 1)
        self.assertEqual((backup / "files" / "private" / "resumes" / "synthetic.enc").read_text(encoding="utf-8"), "合成简历")
        self.assertFalse(self.resume.exists())
        self.assertEqual(self.credentials.read_text(encoding="utf-8"), "synthetic config")

    def test_missing_path_refused_before_any_history_changes(self):
        self.reset.mock_database = None
        self.assertFalse(self.reset.mock_available())
        with self.assertRaisesRegex(ValueError, "未清理任何历史"):
            self.reset._clear_files_and_databases(True)
        self.assertEqual(self.count(self.jobs.path, "conversation_messages"), 1)
        self.assertEqual(self.count(self.mock, "messages"), 1)
        self.assertTrue(self.resume.exists())

    def test_relative_unresolved_path_not_advertised_as_available(self):
        self.reset.mock_database = Path("job_site.db")
        self.assertFalse(self.reset.mock_available())

    def test_wrong_extension_not_advertised_as_available(self):
        other = self.mock.with_suffix(".txt")
        other.write_text("synthetic", encoding="utf-8")
        self.reset.mock_database = other
        self.assertFalse(self.reset.mock_available())

    def test_link_check_preserved_after_normalization(self):
        with patch.object(self.reset, "safe_path", side_effect=ValueError("link detected")):
            self.assertFalse(self.reset.mock_available())
            with self.assertRaises(ValueError):
                self.reset._clear_files_and_databases(True)
        self.assertEqual(self.count(self.mock, "messages"), 1)

    def test_unexpected_mock_schema_rolls_back_agent_changes(self):
        with closing(sqlite3.connect(self.mock)) as db:
            db.execute("DROP TABLE form_responses")
            db.commit()
        with self.assertRaisesRegex(ValueError, "数据库结构不符"):
            self.reset._clear_files_and_databases(True)
        self.assertEqual(self.count(self.jobs.path, "conversation_messages"), 1)
        self.assertTrue(self.resume.exists())

    def test_agent_only_reset_preserves_mock_history(self):
        self.reset.mock_database = None
        result = self.reset._clear_files_and_databases(False)
        self.assertTrue(result["cleared"])
        self.assertEqual(self.count(self.mock, "messages"), 1)

    def test_database_and_backup_connections_are_closed(self):
        original_connect, connections = sqlite3.connect, []
        def track(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            connections.append(connection)
            return connection
        with patch("app.runtime.history_reset.sqlite3.connect", side_effect=track):
            self.reset._clear_files_and_databases(True)
        self.assertEqual(len(connections), 12)
        for connection in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")


if __name__ == "__main__":
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(cls) for cls in (PathContracts, ResetContracts))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    output = Path(__file__).resolve().parents[1] / "test-results" / "history-path-contract" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    summary = {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
               "real_model_calls": 0, "production_databases_modified": 0,
               "coverage": "Path resolution and clear/backup/rollback on synthetic temporary SQLite; no actual user history reset"}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "failures.json").write_text(json.dumps([{"test": str(test), "traceback": error} for test, error in result.failures + result.errors], ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), **summary}, ensure_ascii=False))
    raise SystemExit(0 if result.wasSuccessful() else 1)
