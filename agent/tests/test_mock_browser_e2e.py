"""Opt-in integration tests against an isolated copy of the local site's database.

Run: AUTOHUNT_MOCK_E2E=1 python -m pytest tests/test_mock_browser_e2e.py
Only the synthetic human driver reads the simulator's demo MFA code.
Production adapters never receive that code.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest
from playwright.async_api import async_playwright, expect

from app.browser.lifecycle import BrowserLifecycle
from app.delivery.mock_site_adapter import MockJobSiteAdapter
from app.delivery.automation import DeliveryAutomationService
from app.core.models import BrowserMode, DeliveryTaskStatus
from app.jobs.store import JobStore

pytestmark = pytest.mark.skipif(
    os.getenv("AUTOHUNT_MOCK_E2E") != "1", reason="Explicit isolated browser E2E opt-in required"
)


@pytest.fixture(scope="module")
def simulator(tmp_path_factory):
    root = Path(os.getenv("AUTOHUNT_MOCK_ROOT", r"C:\job_agent"))
    assert (root / "app/main.py").exists(), f"Simulator missing: {root}"
    folder = tmp_path_factory.mktemp("simulator")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = dict(os.environ, DATABASE_URL="sqlite+aiosqlite:///" + (folder / "site.db").as_posix())
    env.pop("PYTHONPATH", None)
    log = (folder / "server.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        [str(root / ".venv/Scripts/python.exe"), "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    origin = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(base_url=origin, trust_env=False) as client:
            for _ in range(100):
                if process.poll() is not None:
                    pytest.fail((folder / "server.log").read_text(encoding="utf-8"))
                try:
                    if client.get("/api/stats/summary").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.1)
            else:
                pytest.fail("Isolated simulator failed to start")
        yield origin
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        log.close()


@pytest.fixture
async def rig(simulator, tmp_path):
    async with httpx.AsyncClient(base_url=simulator, trust_env=False) as api:
        async def post(path, payload, headers=None):
            response = await api.post(path, json=payload, headers=headers)
            response.raise_for_status()
            return response.json()

        suffix = uuid.uuid4().hex[:10]
        user = await post("/api/auth/register", {
            "role": "applicant", "name": "Browser E2E " + suffix,
            "email": suffix + "@example.com",
        })
        headers = {"X-User-Id": str(user["id"]), "X-User-Role": "applicant"}
        resume = await post("/api/resumes", {
            "name": "E2E Resume", "skills": ["Python", "LLM"], "summary": "Synthetic test only",
        }, headers)
        employer = await post("/api/auth/register", {
            "role": "employer", "name": "E2E HR", "email": "hr-" + suffix + "@example.com",
            "company_name": "E2E Company",
        })
        job = await post("/api/jobs", {
            "title": "E2E AI Agent " + suffix, "category": "人工智能", "city": "深圳",
            "salary_min": 20, "salary_max": 30, "skills": ["Python"], "description": "测试职位",
        }, {"X-User-Id": str(employer["id"]), "X-User-Role": "employer"})

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context()
            await context.tracing.start(screenshots=True, snapshots=True, sources=True)
            page = await context.new_page()
            evidence = BrowserLifecycle(tmp_path / "lifecycle.jsonl")
            evidence.watch_context(context)
            browser.on("disconnected", lambda *_: evidence.record("browser.disconnected"))

            class Adapter(MockJobSiteAdapter):
                allowed_ports = {int(simulator.rsplit(":", 1)[1])}

            adapter = Adapter()
            try:
                yield dict(page=page, browser=browser, context=context, api=api, user=user,
                           headers=headers, job=job, employer=employer, adapter=adapter,
                           origin=simulator, tmp=tmp_path, resume=resume, evidence=evidence)
            finally:
                if not page.is_closed():
                    await page.screenshot(path=str(tmp_path / "last-page.png"))
                await context.tracing.stop(path=str(tmp_path / "trace.zip"))
                await browser.close()


async def human_login(rig, flow="direct"):
    """Synthetic human actor, deliberately separate from production adapter."""
    page, adapter = rig["page"], rig["adapter"]
    url = rig["origin"] + f'/job.html?id={rig["job"]["id"]}' + ("&flow=direct" if flow == "direct" else "")
    observation = await adapter.prepare(page, url)
    assert observation.intervention_reason
    await page.locator("#auth-email").fill(rig["user"]["email"])
    await page.locator("#auth-name").fill(rig["user"]["name"])
    await page.locator("#auth-submit").click()
    await expect(page.locator("#demo-code")).to_contain_text("模拟验证码")
    observation = await adapter.inspect(page)
    assert observation.intervention_reason and "MFA" in observation.intervention_reason
    code = re.search(r"\d{6}", await page.locator("#demo-code").inner_text()).group()
    await page.locator("#auth-code").fill(code)
    await page.locator("#auth-submit").click()
    await expect(page.locator("#auth-modal")).not_to_be_visible()
    await expect(page.locator("#resume-select option")).to_contain_text("E2E Resume")
    return url


async def recruiter_reply(rig):
    """Recruiter uses a separate real browser context and the actual manager UI."""
    ctx = await rig["browser"].new_context()
    page = await ctx.new_page()
    await page.goto(rig["origin"] + "/employer.html")
    await page.locator("#email").fill("autohunt-employer@example.com")
    await page.locator("#name").fill("E2E Manager")
    await page.locator("#login-action").click()
    await expect(page.locator("#mfa-hint")).to_contain_text("验证码")
    code = re.search(r"\d{6}", await page.locator("#mfa-hint").inner_text()).group()
    await page.locator("#mfa-code").fill(code)
    await page.locator("#login-action").click()
    await expect(page.locator("#main")).to_be_visible()
    await page.locator('[data-tab="tab-chat"]').click()
    await page.locator("#conversation-list button").filter(has_text=rig["user"]["name"]).click()
    await page.locator("#chat-input").fill("E2E HR reply: 请补充项目亮点")
    await page.locator('button[onclick="handleSendMessage()"]').click()
    await expect(page.locator("#chat-list")).to_contain_text("E2E HR reply")
    return ctx, page


@pytest.mark.parametrize("flow", ["direct", "gated"])
async def test_actual_browser_apply_and_reply(rig, flow):
    page, adapter = rig["page"], rig["adapter"]
    url = await human_login(rig, flow)
    observation = await adapter.prepare(page, url)
    if flow == "gated":
        assert observation.waiting_for_reply
        ctx, hr = await recruiter_reply(rig)
        await expect(page.locator("#resume-btn")).to_be_enabled(timeout=10000)
    result = await adapter.submit(page)
    assert result.confirmed
    if flow == "direct":
        ctx, hr = await recruiter_reply(rig)
    await expect(page.locator("#chat-list")).to_contain_text("E2E HR reply", timeout=10000)
    assert (await adapter.inspect(page)).application_submitted
    # A second call must observe prior success, not send a second request.
    again = await adapter.submit(page)
    assert again.confirmed and again.duplicate
    applications = (await rig["api"].get("/api/applications/mine", headers=rig["headers"])).json()
    assert len(applications) == 1
    assert applications[0]["resume_id"] == rig["resume"]["id"]
    await hr.locator('[data-tab="tab-apps"]').click()
    row = hr.locator("#application-list tr").filter(has_text=rig["job"]["title"])
    await expect(row).to_contain_text("E2E Resume")
    await row.get_by_role("button", name="面试", exact=True).click()
    if flow == "direct":
        await expect(page.locator("#direct-status-title")).to_contain_text("进入面试", timeout=10000)
    await ctx.close()


async def test_delayed_render_and_auth_overlay(rig):
    page, adapter = rig["page"], rig["adapter"]
    async def delay(route):
        await asyncio.sleep(1.2)
        await route.continue_()
    await page.route("**/api/jobs/*", delay)
    url = await human_login(rig)
    await page.evaluate("document.querySelector('#auth-modal').classList.add('open')")
    # Local identity still exists: visible verification must win over stale storage.
    assert (await adapter.inspect(page)).intervention_reason
    before = (await rig["api"].get("/api/applications/mine", headers=rig["headers"])).json()
    assert before == []
    assert not (await adapter.submit(page)).confirmed


async def test_response_lost_after_commit_reconciles_without_resubmission(rig):
    page, adapter = rig["page"], rig["adapter"]
    url = await human_login(rig)
    await adapter.prepare(page, url)
    calls = []
    async def lose_response(route):
        calls.append(route.request.method)
        await route.fetch()  # Server commits, browser loses the response.
        await route.abort("failed")
    await page.route("**/api/applications/direct", lose_response)
    result = await adapter.submit(page)
    assert result.confirmed  # Site's existing application read-back provides evidence.
    assert calls == ["POST"]
    applications = (await rig["api"].get("/api/applications/mine", headers=rig["headers"])).json()
    assert len(applications) == 1


async def test_unknown_outcome_stops_service_without_retry(rig, monkeypatch):
    page, adapter = rig["page"], rig["adapter"]
    url = await human_login(rig)
    await adapter.prepare(page, url)
    calls = []
    async def lose_everything(route):
        calls.append(route.request.method)
        await route.fetch()
        await route.abort("failed")
    await page.route("**/api/applications/direct", lose_everything)
    await page.route("**/api/applications/mine", lambda route: route.abort("failed"))
    adapter.submit_timeout_seconds = 1.0

    class ExistingTestSession:
        async def reuse(self, _mode):
            return BrowserMode.CDP.value
        async def ensure_cdp_window(self, _url):
            pass
        def page_for_url(self, _url):
            return page

    store = JobStore(rig["tmp"] / "jobs.db")
    job_id, _ = store.upsert_job(dict(
        source="MockJobSite", source_url=url, external_id=str(rig["job"]["id"]),
        title=rig["job"]["title"], company="E2E", location="深圳", salary="20-30K",
        description="Synthetic", tags=["Python"], score=80, decision="REVIEW",
        reasoning="E2E", stage="review",
    ))
    service = DeliveryAutomationService(store, rig["tmp"], headless=True, cdp_url="")
    service.session = ExistingTestSession()
    service.adapter = adapter
    preview = store.create_delivery_preview(job_id)
    await service.authorize_and_start(preview.id)
    await asyncio.wait_for(service.worker, timeout=15)
    assert service.current().status == DeliveryTaskStatus.OUTCOME_INDETERMINATE
    assert store.get_job(job_id)["stage"] == "outcome_indeterminate"
    await service.resume()
    assert calls == ["POST"]


async def test_page_close_is_logged_without_browser_disconnect(rig):
    page = rig["page"]
    await page.goto(rig["origin"])
    await page.close()
    await asyncio.sleep(0.1)
    events = [json.loads(line)["event"] for line in (rig["tmp"] / "lifecycle.jsonl").read_text().splitlines()]
    assert "page.close" in events
    assert "browser.disconnected" not in events
    assert rig["browser"].is_connected()


async def test_delayed_search_returns_new_query_not_previous_cards(rig, monkeypatch):
    from app.browser import mock_site_adapter as search_module
    page = rig["page"]
    await human_login(rig)
    await page.goto(rig["origin"])
    await expect(page.locator("#result-count")).to_contain_text("为你找到")
    async def delay(route):
        await asyncio.sleep(1.0)
        await route.continue_()
    await page.route("**/api/jobs?*", delay)
    adapter = search_module.MockJobSiteSearchAdapter()
    cards = await adapter.search(page, rig["job"]["title"], "深圳")
    assert len(cards) == 1
    assert cards[0]["title"] == rig["job"]["title"]
    assert cards[0]["location"] == "深圳"


async def test_repeated_verification_never_submits(rig):
    page = rig["page"]
    url = await human_login(rig)
    await rig["adapter"].prepare(page, url)
    await page.evaluate("""() => {
        const challenge = document.createElement('div');
        challenge.id = 'captcha-test';
        challenge.textContent = '安全验证：请人工完成';
        document.body.appendChild(challenge);
    }""")
    observation = await rig["adapter"].inspect(page)
    assert "人工" in observation.intervention_reason
    assert not (await rig["adapter"].submit(page)).confirmed
    assert (await rig["api"].get("/api/applications/mine", headers=rig["headers"])).json() == []


async def test_cdp_attach_and_detach_keeps_external_browser_alive(tmp_path):
    from app.browser.session import BrowserSession
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    async with async_playwright() as owner:
        browser = await owner.chromium.launch(headless=True, args=[f"--remote-debugging-port={port}"])
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto("about:blank")
        session = BrowserSession(tmp_path / "profile", headless=True, cdp_url=f"http://127.0.0.1:{port}")
        try:
            await session.open(BrowserMode.CDP)
            assert session.browser.is_connected()
            assert session.pages()
            await session.close()
            assert browser.is_connected()
            assert not page.is_closed()
            events = [json.loads(line)["event"] for line in session.lifecycle.path.read_text().splitlines()]
            assert events.index("cdp.connected") < events.index("session.close_requested")
            assert "driver.stop_requested" in events
        finally:
            await session.close()
            await browser.close()


async def test_full_delivery_service_pauses_then_persists_reply(rig):
    page, adapter = rig["page"], rig["adapter"]
    url = rig["origin"] + f'/job.html?id={rig["job"]["id"]}&flow=direct'
    await page.goto(url)

    class BoundTestSession:
        # Only startup binding is substituted; all site interactions use the real adapter/page.
        async def reuse(self, _mode):
            return BrowserMode.CDP.value
        async def ensure_cdp_window(self, _url):
            pass
        def page_for_url(self, _url):
            return page

    store = JobStore(rig["tmp"] / "pipeline.db")
    job_id, _ = store.upsert_job(dict(
        source="MockJobSite", source_url=url, external_id=str(rig["job"]["id"]),
        title=rig["job"]["title"], company="E2E", location="深圳", salary="20-30K",
        description="Synthetic", tags=["Python"], score=80, decision="REVIEW",
        reasoning="E2E", stage="review",
    ))
    service = DeliveryAutomationService(
        store, rig["tmp"], headless=True, cdp_url="", reply_poll_seconds=0.2
    )
    service.session = BoundTestSession()
    service.adapter = adapter

    async def wait_state(status):
        async def check():
            while service.current().status != status:
                if service.worker.done():
                    raise AssertionError(service.current())
                await asyncio.sleep(0.05)
        await asyncio.wait_for(check(), timeout=15)

    try:
        preview = store.create_delivery_preview(job_id)
        await service.authorize_and_start(preview.id)
        await wait_state(DeliveryTaskStatus.WAITING_FOR_USER)
        await human_login(rig)
        # Logging in does not silently resume the paused task.
        assert service.current().status == DeliveryTaskStatus.WAITING_FOR_USER
        await service.resume()
        await wait_state(DeliveryTaskStatus.WAITING_FOR_REPLY)
        assert service.current().submitted
        assert store.get_job(job_id)["stage"] == "waiting"
        ctx, _hr = await recruiter_reply(rig)
        await asyncio.wait_for(service.worker, timeout=15)
        assert service.current().status == DeliveryTaskStatus.COMPLETED
        assert "E2E HR reply" in store.get_job(job_id)["recruiter_reply"]
        await ctx.close()
    finally:
        if service.worker and not service.worker.done():
            service.worker.cancel()
            await asyncio.gather(service.worker, return_exceptions=True)
