"""Settings UI -> production API -> isolated SQLite, without executing a task.

Requires the frontend on port 3000. Settings requests are routed to an in-process
API with a temporary data root. Other API writes and external hosts are blocked;
the API lifespan/scheduler is never started. Run as a standalone module:
python -m evaluation.daily_settings_browser
"""
import asyncio
import gc
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import Fernet
from playwright.async_api import async_playwright, expect


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
    checks, writes, errors = [], [], []
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
                await route.abort()
            elif url.path == "/api/agent/mock-workflow" and request.method in {"GET", "PUT"}:
                response = await client.request(request.method, path,
                    json=json.loads(request.post_data) if request.method == "PUT" else None)
                await route.fulfill(status=response.status_code, json=response.json())
                if request.method == "PUT":
                    saved.set()
            elif request.method not in {"GET", "HEAD"}:
                writes.append(url.path)
                await route.abort()
            elif url.path == "/api/agent/runs":
                await route.fulfill(json=[])
            else:
                await route.continue_()
        await context.route("**/*", intercept)
        page = await context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            await page.goto("http://127.0.0.1:3000/", wait_until="networkidle")
            await page.get_by_role("button", name="模型设置", exact=True).click()
            search = page.get_by_label("每轮搜索岗位数", exact=True)
            contact = page.get_by_label("每轮最多自动沟通岗位", exact=True)
            await expect(search).to_have_value("30")
            await expect(contact).to_have_value("3")
            await search.fill("7")
            await contact.fill("2")
            await page.get_by_role("button", name="保存每日任务设置", exact=True).click()
            await asyncio.wait_for(saved.wait(), 10)
            await expect(page.locator(".workflow-feedback")).to_contain_text("已保存")
            checks.append("independent_values_saved_through_production_api")
            persisted = JobStore(root / "autohunt.db").mock_workflow_settings()
            assert (persisted["max_search_jobs"], persisted["max_contacts"]) == (7, 2)
            snapshot = d.daily_workflow.snapshot()
            assert (snapshot["request"]["max_jobs"], snapshot["max_contacts"]) == (7, 2)
            checks.append("persisted_settings_reach_daily_search_request")
            await page.reload(wait_until="networkidle")
            await page.get_by_role("button", name="模型设置", exact=True).click()
            await expect(search).to_have_value("7")
            await expect(contact).to_have_value("2")
            checks.append("reload_restores_both_limits")
            saved.clear()
            await search.fill("201")
            await page.get_by_role("button", name="保存每日任务设置", exact=True).click()
            assert not await search.evaluate("el => el.validity.valid")
            assert not saved.is_set()
            checks.append("invalid_form_value_is_not_saved")
            body = dict(monitor_enabled=False, auto_submit=False, daily_enabled=False,
                        daily_time="09:00", min_score=75, max_contacts=5)
            for value in (0, 201, 1.5, True, None):
                assert (await client.put(path, json={**body, "max_search_jobs": value})).status_code == 422
            response = await client.put(path, json=body)  # Legacy client omits the field.
            assert response.status_code == 200
            assert response.json()["max_search_jobs"] == 7
            assert response.json()["max_contacts"] == 5
            checks.append("api_validation_and_legacy_update_preserve_limit")
            assert d.run_store.list("default") == []
            assert not writes and not errors, (writes, errors)
            print(json.dumps({"passed": True, "checks": checks, "task_runs": 0,
                              "user_api_writes": writes, "page_errors": errors, "external_model_calls": 0}))
        finally:
            await context.close()
            await browser.close()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="autohunt-settings-") as directory:
        asyncio.run(run(Path(directory)))
        gc.collect()
