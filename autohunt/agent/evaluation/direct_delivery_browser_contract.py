"""Real mock-site HTML + production delivery adapter, intercepted synthetic APIs.

Never connects to the running mock backend, CDP, resumes or application history.
"""
from __future__ import annotations

import asyncio
import json
import traceback
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, Error as BrowserError
from app.delivery.mock_site_adapter import MockJobSiteAdapter


async def scenario(browser, name):
    context = await browser.new_context(service_workers="block")
    writes, applications, resumes, errors = [], [], [], []
    payload = {"name": "合成候选人", "education": "本科", "skills": ["合成技能"], "experience": "合成项目"}
    resume = {"id": 991, "applicant_id": 987, **payload}
    if name == "duplicate":
        resumes.append(resume)
        applications.append({"id": 992, "job_id": 999999, "resume_id": 991, "status": "submitted"})
    root = Path(__file__).resolve().parents[3] / "mock-site" / "frontend"
    await context.add_init_script("localStorage.setItem('userId','987');localStorage.setItem('userRole','applicant')")
    async def intercept(route):
        target = urlsplit(route.request.url)
        if target.netloc != "127.0.0.1:8080":
            await route.abort(); return
        assets = {"/job.html": ("job.html", "text/html"),
                  "/static/js/api.js": ("js/api.js", "application/javascript"),
                  "/static/css/style.css": ("css/style.css", "text/css")}
        if target.path in assets:
            file, mime = assets[target.path]
            await route.fulfill(content_type=mime + "; charset=utf-8", body=(root / file).read_text(encoding="utf-8")); return
        data = []
        if target.path == "/api/jobs/999999":
            data = {"id": 999999, "title": "合成岗位", "company_name": "合成企业", "city": "深圳", "category": "数据分析",
                    "department": "合成部门", "employer_id": 986, "salary_min": 15, "salary_max": 20,
                    "experience_required": "不限", "education": "本科", "headcount": 1, "description": "合成职责", "skills": [], "requirements": []}
        elif target.path == "/api/resumes/mine":
            data = resumes
        elif target.path == "/api/resumes" and route.request.method == "POST":
            writes.append({"path": target.path, "body": route.request.post_data_json})
            assert route.request.post_data_json == payload
            resumes.append(resume); data = resume
        elif target.path == "/api/resumes/991":
            data = {**resume, "experience": "错误内容"} if name == "changed_resume" else resume
        elif target.path == "/api/applications/mine":
            data = applications
        elif target.path == "/api/applications/direct" and route.request.method == "POST":
            body = route.request.post_data_json
            writes.append({"path": target.path, "body": body})
            assert body["job_id"] == 999999 and body["resume_id"] == 991
            applications.append({"id": 992, "job_id": 999999, "resume_id": 991, "status": "submitted"})
            if name == "lost_response":
                await route.abort(); return
            data = applications[-1]
        elif target.path != "/api/messages":
            await route.abort(); return
        await route.fulfill(content_type="application/json", body=json.dumps(data, ensure_ascii=False))
    await context.route("**/*", intercept)
    page = await context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        url = "http://127.0.0.1:8080/job.html?id=999999&flow=direct"
        await page.goto(url)
        adapter = MockJobSiteAdapter()
        observed = await adapter.prepare(page, url)
        assert observed.direct_apply and not observed.hr_reply_text
        binding = await adapter.bind_resume(page, payload, applicant_id=987)
        if name == "changed_resume":
            try:
                await adapter.submit(page, binding=binding)
                raise AssertionError("Changed resume was accepted")
            except (ValueError, BrowserError) as exc:
                assert "授权简历内容已变化" in str(exc), exc
        else:
            result = await adapter.submit(page, binding=binding)
            assert result.confirmed, result
            repeated = await adapter.submit(page, binding=binding)
            assert repeated.confirmed and repeated.duplicate, repeated
        submissions = [item for item in writes if item["path"] == "/api/applications/direct"]
        assert len(submissions) == (0 if name in {"duplicate", "changed_resume"} else 1), writes
        assert not errors, errors
        return {"scenario": name, "passed": True, "writes": writes, "hr_reply_required": False}
    finally:
        await context.close()


async def main():
    output = Path(__file__).parent / "results" / "direct-delivery-browser" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            for name in ("success_without_reply", "duplicate", "lost_response", "changed_resume"):
                try:
                    rows.append(await scenario(browser, name))
                except Exception:
                    rows.append({"scenario": name, "passed": False, "error": traceback.format_exc()})
        finally:
            await browser.close()
    (output / "results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "passed": sum(row["passed"] for row in rows), "total": len(rows)}))
    return int(not all(row["passed"] for row in rows))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
