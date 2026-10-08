import importlib.util
import json
import os
import sqlite3
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "mock_inventory.py"
spec = importlib.util.spec_from_file_location("inventory", SCRIPT)
inventory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inventory)


def test_generated_inventory_is_diverse_deterministic_and_coherent():
    jobs = inventory.generate_jobs()
    assert jobs == inventory.generate_jobs()
    assert len(jobs) == 11520 and len({job["key"] for job in jobs}) == len(jobs)
    assert len({job["category"] for job in jobs}) == 40
    assert len({job["city"] for job in jobs}) == 24
    assert len({job["description"] for job in jobs}) == len(jobs)
    assert all(job["salary_min"] < job["salary_max"] for job in jobs)
    assert all(job["status"] == "open" and len(job["requirements"]) >= 5 for job in jobs)
    assert all("合成测试职位" in job["description"] and "按12个月计薪" in job["description"] for job in jobs)
    assert min(job["salary_min"] for job in jobs) == 4
    assert max(job["salary_max"] for job in jobs) == 80
    assert {job["experience_required"] for job in jobs} == {level[1] for level in inventory.LEVELS}
    for job in jobs:
        role = job["title"].split("（")[0]
        if any(term in role for term in inventory.LEADERSHIP):
            assert job["experience_required"] not in {"在校/实习", "应届毕业生", "1-3年"}
    for role in ("外贸业务员", "国际销售", "海外销售", "外贸销售主管", "国际业务开发"):
        matches = [job for job in jobs if job["title"].startswith(role + "（") and job["salary_min"] >= 12]
        assert len(matches) >= 15
    assert len([job for job in jobs if job["category"] in {"国际贸易", "跨境电商"}
                and job["city"] in {"深圳", "上海", "杭州", "武汉"} and job["salary_min"] >= 12]) >= 15


def create_fixture(database):
    with sqlite3.connect(database) as db:
        db.executescript("""
            CREATE TABLE users(id INTEGER PRIMARY KEY,role TEXT,name TEXT,company_name TEXT);
            CREATE TABLE jobs(id INTEGER PRIMARY KEY,employer_id INTEGER,title TEXT,company_name TEXT,
                department TEXT,category TEXT,city TEXT,salary_min INTEGER,salary_max INTEGER,
                experience_required TEXT,education TEXT,description TEXT,requirements TEXT,skills TEXT,
                headcount INTEGER,status TEXT,created_at TEXT);
            CREATE TABLE applications(id INTEGER PRIMARY KEY);
            CREATE TABLE messages(id INTEGER PRIMARY KEY);
            CREATE TABLE resumes(id INTEGER PRIMARY KEY);
            INSERT INTO jobs(id,title,description,status) VALUES (1,'保留旧职位','不可改写','open');
            INSERT INTO applications VALUES(1);
            INSERT INTO messages VALUES(1);
            INSERT INTO resumes VALUES(1);
        """)
        db.executemany("INSERT INTO users VALUES(?,?,?,?)", [(i, "employer", f"HR{i}", f"公司{i}") for i in range(1, 31)])


def test_append_is_idempotent_and_preserves_existing_records(tmp_path):
    database = tmp_path / "simulator.db"
    create_fixture(database)
    first = inventory.append_inventory(database, tmp_path / "backups")
    assert first["inserted"] == 11520 and first["after"]["jobs"] == 11521
    assert Path(first["backup"]).is_file()
    with sqlite3.connect(first["backup"]) as backup:
        assert backup.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    second = inventory.append_inventory(database, tmp_path / "backups")
    assert second["inserted"] == 0 and second["after"] == first["after"]
    for table in ("users", "applications", "messages", "resumes"):
        assert first["before"][table] == first["after"][table]
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT title,description FROM jobs WHERE id=1").fetchone() == ("保留旧职位", "不可改写")
        assert db.execute("SELECT COUNT(DISTINCT employer_id) FROM jobs WHERE id>1").fetchone()[0] == 30
        assert all(len(json.loads(row[0])) >= 5 for row in db.execute("SELECT requirements FROM jobs WHERE id>1"))


def test_wrong_database_is_not_modified(tmp_path):
    database = tmp_path / "wrong.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE private_data(value TEXT)")
    before = database.read_bytes()
    with pytest.raises(ValueError, match="不是模拟"):
        inventory.append_inventory(database, tmp_path / "backup")
    assert database.read_bytes() == before


@pytest.mark.skipif(os.getenv("AUTOHUNT_INVENTORY_UI") != "1", reason="opt-in read-only local simulator smoke")
async def test_actual_simulator_search_pagination_and_jd():
    from playwright.async_api import async_playwright
    from app.browser.mock_site_adapter import MockJobSiteSearchAdapter

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            mutations = []

            async def read_only(route):
                if route.request.method != "GET":
                    mutations.append(route.request.method)
                    await route.abort()
                else:
                    await route.continue_()

            await context.route("**/*", read_only)
            page = await context.new_page()
            await page.goto("http://127.0.0.1:8080/")
            await page.locator("#result-count").filter(has_text="为你找到").wait_for(timeout=20000)
            adapter = MockJobSiteSearchAdapter()
            cards = await adapter.search(page, "外贸业务员", None, max_cards=30)
            assert len(cards) == 30 and len({card["url"] for card in cards}) == 30
            assert all("外贸业务员" in card["title"] for card in cards)
            await page.goto(cards[0]["url"])
            await adapter.wait_for_job_detail(page)
            detail = await adapter.extract_job_detail(page, cards[0])
            assert "合成测试职位" in detail["description"]
            assert "商务英语" in detail["description"] and "薪资福利" in detail["description"]
            for term in ("法务专员", "机械设计工程师", "国际业务开发"):
                await page.goto("http://127.0.0.1:8080/")
                await page.locator("#result-count").filter(has_text="为你找到").wait_for(timeout=20000)
                matches = await adapter.search(page, term, "杭州", max_cards=5)
                assert matches and all("杭州" in card["location"] for card in matches)
                assert all(term in card["title"] for card in matches)
            assert not mutations
        finally:
            await browser.close()
