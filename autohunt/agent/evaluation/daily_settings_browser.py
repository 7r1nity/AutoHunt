"""Settings UI -> production API -> isolated SQLite, without executing a task.

Requires the frontend on port 3000. Settings requests are routed to an in-process
API with a temporary data root. Every other API request is synthetic or blocked;
the API lifespan/scheduler is never started. Run as a standalone module:
python -m evaluation.daily_settings_browser
"""
import asyncio
import gc
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import Fernet
from playwright.async_api import async_playwright, expect
from evaluation.job_page_fixture import job_page_response


async def run(root):
    token = "synthetic-settings-api"
    os.environ.update(AUTOHUNT_DATA_ROOT=str(root), AUTOHUNT_API_KEY=token,
        AUTOHUNT_DATA_KEY=Fernet.generate_key().decode(), AUTOHUNT_LLM_PROVIDER="mock",
        AUTOHUNT_LLM_MODEL="mock-autohunt-v1", AUTOHUNT_BROWSER_HEADLESS="true")
    for key in ("DEEPSEEK_API_KEY", "AUTOHUNT_ROUTER_SMALL_API_KEY"):
        os.environ.pop(key, None)
    from app.api.application import app
    from app.core import dependencies as d
    from app.core.models import CandidateProfileView
    from app.jobs.store import JobStore

    d.candidate_state_store.apply_resume_profile(CandidateProfileView(
        resume_id="synthetic", skills=["Python"], role_suggestions=["AI Engineer"]), "AI Engineer")
    d.resume_workspace_store.sync_profile(d.candidate_state_store.read().profile)
    checks, writes, errors, settings_writes, external = [], [], [], [], []
    output = Path(__file__).parent / "results" / "daily-settings-browser" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True, exist_ok=True)
    passed = False
    saved = asyncio.Event()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                                headers={"X-AutoHunt-Key": token}) as client, async_playwright() as pw:
        path = "/api/v1/mock-workflow/settings"
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(service_workers="block")
        async def intercept(route):
            request = route.request
            url = urlsplit(request.url)
            if url.netloc != "127.0.0.1:3000":
                external.append({"method": request.method, "host": url.netloc})
                await route.abort()
            elif url.path == "/api/agent/mock-workflow" and request.method in {"GET", "PUT"}:
                if request.method == "PUT":
                    settings_writes.append(json.loads(request.post_data))
                response = await client.request(request.method, path,
                    json=json.loads(request.post_data) if request.method == "PUT" else None)
                await route.fulfill(status=response.status_code, json=response.json())
                if request.method == "PUT":
                    saved.set()
            elif url.path.startswith("/api/") and request.method not in {"GET", "HEAD"}:
                writes.append(url.path)
                await route.abort()
            elif url.path == "/api/agent/jobs":
                await route.fulfill(json=job_page_response([], url.query))
            elif url.path in {"/api/agent/runs", "/api/agent/resumes", "/api/agent/materials"}:
                await route.fulfill(json=[])
            elif url.path == "/api/agent/conversations":
                await route.fulfill(json=[{"id": "default", "title": "合成设置验收会话"}])
            elif url.path == "/api/agent/chat":
                await route.fulfill(json={"messages": []})
            elif url.path in {"/api/agent/browser", "/api/agent/delivery"}:
                await route.fulfill(json={"status": "idle", "detail": "合成空闲"})
            elif url.path == "/api/agent/stats":
                await route.fulfill(json={"review": 0, "applying": 0, "waiting": 0, "interview": 0,
                    "indeterminate": 0, "stored": 0, "discovered": 0, "matched": 0, "duplicates": 0})
            elif url.path.startswith("/api/"):
                await route.fulfill(status=503, json={"detail": "合成验收已隔离真实 Agent API"})
            else:
                await route.continue_()
        await context.route("**/*", intercept)
        page = await context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            await page.goto("http://127.0.0.1:3000/", wait_until="networkidle")
            await expect(page.get_by_role("heading", name="定时任务设置", exact=True)).to_be_visible()
            search = page.get_by_label("每轮搜索岗位数", exact=True)
            contact = page.get_by_label("每轮最多自动沟通岗位", exact=True)
            # Default placeholders render before persisted settings arrive.
            # Wait for the form to finish loading before editing either count.
            await expect(page.get_by_role("button", name="保存每日任务设置", exact=True)).to_be_enabled()
            await expect(search).to_have_value("30")
            await expect(contact).to_have_value("3")
            assert await contact.get_attribute("max") == "200"
            checks.append("dashboard_loads_defaults_and_expanded_contact_limit")
            await search.fill("40")
            await contact.fill("20")
            await page.get_by_role("button", name="保存每日任务设置", exact=True).click()
            await asyncio.wait_for(saved.wait(), 10)
            await expect(page.locator(".workflow-feedback")).to_contain_text("已保存")
            checks.append("twenty_contacts_saved_through_production_api")
            persisted = JobStore(root / "autohunt.db").mock_workflow_settings()
            assert (persisted["max_search_jobs"], persisted["max_contacts"]) == (40, 20)
            assert not any(persisted[key] for key in ("monitor_enabled", "auto_submit", "daily_enabled"))
            assert not any(settings_writes[-1][key] for key in ("monitor_enabled", "auto_submit", "daily_enabled"))
            checks.append("saving_limits_does_not_enable_automation")
            snapshot = d.daily_workflow.snapshot()
            assert (snapshot["request"]["max_jobs"], snapshot["max_contacts"]) == (40, 20)
            checks.append("reopened_store_and_daily_snapshot_keep_twenty_contacts")
            await page.reload(wait_until="networkidle")
            await expect(search).to_have_value("40")
            await expect(contact).to_have_value("20")
            checks.append("reload_restores_both_limits")
            saved.clear()
            await contact.fill("201")
            await page.get_by_role("button", name="保存每日任务设置", exact=True).click()
            assert not await contact.evaluate("el => el.validity.valid")
            assert not saved.is_set()
            checks.append("over_limit_contact_form_value_is_not_saved")
            await contact.fill("200")
            assert await contact.evaluate("el => el.validity.valid")
            await page.get_by_role("button", name="保存每日任务设置", exact=True).click()
            await asyncio.wait_for(saved.wait(), 10)
            assert (await client.get(path)).json()["max_contacts"] == 200
            checks.append("two_hundred_contacts_accepted_in_form_and_api")
            saved.clear()
            await search.fill("201")
            await page.get_by_role("button", name="保存每日任务设置", exact=True).click()
            assert not await search.evaluate("el => el.validity.valid")
            assert not saved.is_set()
            checks.append("over_limit_search_form_value_is_not_saved")
            body = dict(monitor_enabled=False, auto_submit=False, daily_enabled=False,
                        daily_time="09:00", min_score=75, max_contacts=20, max_search_jobs=40)
            for field in ("max_search_jobs", "max_contacts"):
                for value in (0, 201, 1.5, True, None):
                    assert (await client.put(path, json={**body, field: value})).status_code == 422, (field, value)
                for value in (1, 20, 200):
                    response = await client.put(path, json={**body, field: value})
                    assert response.status_code == 200 and response.json()[field] == value
            checks.append("api_accepts_integer_bounds_and_rejects_invalid_counts")
            body.pop("max_search_jobs")
            response = await client.put(path, json=body)  # Legacy client omits the field.
            assert response.status_code == 200
            assert response.json()["max_search_jobs"] == 40
            assert response.json()["max_contacts"] == 20
            checks.append("legacy_update_preserves_saved_search_limit")
            assert d.run_store.list("default") == []
            assert not writes and not errors and not external, (writes, errors, external)
            checks.append("all_api_requests_isolated_without_runs_or_page_errors")
            passed = True
        finally:
            result = {"passed": passed, "checks": checks, "task_runs": 0, "settings_writes": settings_writes,
                "user_api_writes": writes, "page_errors": errors, "external_hosts": external,
                "external_model_calls": 0, "real_recruitment_actions": 0}
            (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({**result, "output": str(output)}, ensure_ascii=False))
            await context.close()
            await browser.close()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="autohunt-settings-", ignore_cleanup_errors=True) as directory:
        try:
            asyncio.run(run(Path(directory)))
        finally:
            gc.collect()
