"""Offline simulator inventory checks; no models, recruitment searches or submissions."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from mock_inventory import append_inventory, catalog
from mock_special_salary_inventory import BATCH, DAILY_RATES, generate_special_salary_jobs, summary


class SpecialSalaryContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.jobs = generate_special_salary_jobs()

    def test_exact_counts_and_reproducibility(self):
        self.assertEqual(self.jobs, generate_special_salary_jobs())
        self.assertEqual(len(self.jobs), 3000)
        self.assertEqual(len({job["key"] for job in self.jobs}), 3000)
        self.assertEqual(summary(self.jobs)["kinds"], {"negotiable": 1500, "daily": 1500})

    def test_cross_industry_and_city_coverage(self):
        for kind in ("negotiable", "daily"):
            selected = [job for job in self.jobs if job["metadata"]["salary_unit"] == kind]
            self.assertEqual(len({job["category"] for job in selected}), 40)
            self.assertEqual(len({job["city"] for job in selected}), 24)
            suffix = "方向实习生" if kind == "daily" else ""
            self.assertEqual({job["title"].split("（")[0] for job in selected},
                             {role + suffix for industry in catalog() for role, _ in industry["roles"]})

    def test_visible_pay_and_no_monthly_conversion(self):
        daily_rates = set()
        for job in self.jobs:
            self.assertEqual((job["salary_min"], job["salary_max"]), (0, 0))
            self.assertIn("合成测试职位", job["description"])
            if job["metadata"]["salary_unit"] == "daily":
                low, high = job["metadata"]["daily_min"], job["metadata"]["daily_max"]
                self.assertTrue(150 <= low <= high <= 2000)
                self.assertIn(f"实习日薪 {low}-{high}元/天", job["description"])
                self.assertEqual(job["experience_required"], "在校/实习")
                daily_rates.add(low)
            else:
                self.assertIn("【薪资福利】薪资面议", job["description"])
        self.assertEqual(daily_rates, set(DAILY_RATES))

    def test_append_only_and_rerun_idempotence(self):
        with tempfile.TemporaryDirectory(prefix="mock-salary-contract-") as directory:
            database = Path(directory) / "simulator.db"
            with closing(sqlite3.connect(database)) as db, db:
                db.executescript("""
                    CREATE TABLE users (id INTEGER PRIMARY KEY, role TEXT, name TEXT,
                        email TEXT, company_name TEXT, created_at TEXT);
                    CREATE TABLE jobs (id INTEGER PRIMARY KEY, employer_id INTEGER,
                        title TEXT, company_name TEXT, department TEXT, category TEXT, city TEXT,
                        salary_min INTEGER, salary_max INTEGER, experience_required TEXT,
                        education TEXT, description TEXT, requirements TEXT, skills TEXT,
                        headcount INTEGER, status TEXT, created_at TEXT);
                    CREATE TABLE applications (id INTEGER PRIMARY KEY, detail TEXT);
                    CREATE TABLE messages (id INTEGER PRIMARY KEY, detail TEXT);
                    CREATE TABLE resumes (id INTEGER PRIMARY KEY, detail TEXT);
                    INSERT INTO jobs (id,title) VALUES (1,'preserve-existing');
                    INSERT INTO applications VALUES (1,'preserve-application');
                    INSERT INTO messages VALUES (1,'preserve-message');
                    INSERT INTO resumes VALUES (1,'preserve-resume');
                """)
                db.executemany("INSERT INTO users (id,role,name,company_name) VALUES (?,'employer',?,?)",
                               [(index, f"HR {index}", f"Company {index}") for index in range(1, 31)])
                before = {table: db.execute(f"SELECT * FROM {table}").fetchall()
                          for table in ("jobs", "users", "applications", "messages", "resumes")}
            first = append_inventory(database, Path(directory) / "backups", jobs=self.jobs, batch=BATCH)
            second = append_inventory(database, Path(directory) / "backups", jobs=self.jobs, batch=BATCH)
            self.assertEqual(first["inserted"], 3000)
            self.assertEqual(second["inserted"], 0)
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(db.execute("SELECT * FROM jobs WHERE id=1").fetchall(), before["jobs"])
                for table in ("users", "applications", "messages", "resumes"):
                    self.assertEqual(db.execute(f"SELECT * FROM {table}").fetchall(), before[table])


if __name__ == "__main__":
    unittest.main(verbosity=2)
