"""Real Chromium + production DOM adapters, entirely intercepted fixture pages.

This is browser-adapter integration, NOT the full mock site's backend or Agent E2E.
All browser requests are fulfilled or aborted before network; never attaches CDP.
"""
from __future__ import annotations

from dataclasses import asdict
from playwright.async_api import async_playwright
from app.browser.mock_site_adapter import MockJobSiteSearchAdapter
from app.delivery.mock_site_adapter import MockJobSiteAdapter


SCENARIOS = (
    "logged_out", "logged_in", "recruiter_role", "auth_modal", "captcha", "mfa",
    "waiting_reply", "reply_unlock", "no_resume", "already_submitted", "direct_ready",
    "cards", "closed_page", "communication_overlay",
)


class BrowserFixture:
    def __init__(self):
        self.playwright = self.browser = None

    async def start(self):
        self.playwright = await async_playwright().start()
        try:
            self.browser = await self.playwright.chromium.launch(headless=True)
        except Exception:
            await self.playwright.stop()
            self.playwright = None
            raise

    async def close(self):
        if self.browser: await self.browser.close()
        if self.playwright: await self.playwright.stop()

    async def run(self, scenario):
        context = await self.browser.new_context(service_workers="block")
        requests = []
        page = await context.new_page()
        try:
            async def intercept(route):
                requests.append(route.request.url)
                if route.request.url == "http://127.0.0.1:8080/job.html?id=999999":
                    await route.fulfill(status=200, content_type="text/html", body="<!doctype html><html><body></body></html>")
                else:
                    await route.abort()
            await context.route("**/*", intercept)
            await page.goto("http://127.0.0.1:8080/job.html?id=999999")
            logged_in = scenario != "logged_out"
            await page.evaluate("p => {localStorage.setItem('userId',p.id);localStorage.setItem('userRole',p.role)}",
                {"id": "synthetic-user" if logged_in else "", "role": "recruiter" if scenario == "recruiter_role" else "applicant"})
            disabled = scenario in {"waiting_reply", "no_resume"}
            text = "已发送" if scenario == "already_submitted" else "发送简历"
            html = f'<button id="resume-btn" {"disabled" if disabled else ""}>{text}</button>'
            if scenario in {"auth_modal", "mfa"}: html += '<div id="auth-modal">登录' + ('<div id="mfa-row">MFA</div>' if scenario == "mfa" else '') + '</div>'
            if scenario == "captcha": html += '<div id="captcha">安全验证</div>'
            if scenario == "waiting_reply": html += '<div id="gate-text">等待招聘方回复</div>'
            if scenario == "reply_unlock": html += '<div class="bubble-row"><p>请发送简历</p></div>'
            if scenario == "direct_ready": html += '<button id="direct-apply-btn">直接投递</button>'
            if scenario == "cards":
                html += '<div id="jobs">' + ''.join(f'<div class="modern-job-card" onclick="location.href=\'job.html?id={i}\'"><div class="job-card-top"><h3>合成职位{i}</h3><p>上海 · 社招</p></div><div class="company-line"><b>合成企业</b></div><span class="salary">15-25K</span><div class="job-tags"><span>风险管理</span></div></div>' for i in range(1, 4)) + '</div>'
            if scenario == "communication_overlay":
                html += '<button class="communicate-btn" onclick="window.counter=(window.counter||0)+1;document.querySelector(\'#chat-panel\').classList.add(\'open\');document.querySelector(\'#chat-panel\').style.display=\'block\'">立即沟通</button><div id="chat-panel" style="display:none;width:500px;height:300px;position:fixed;inset:0;background:white">合成聊天面板</div>'
            await page.set_content(html)
            search = MockJobSiteSearchAdapter()
            delivery = MockJobSiteAdapter()
            if scenario == "closed_page":
                await page.close()
                try:
                    await delivery.inspect(page)
                    result = {"closed_error": False}
                except Exception as exc:
                    result = {"closed_error": True, "error_type": type(exc).__name__}
                expected = {"closed_error": True}
            elif scenario == "cards":
                result = {"cards": await search.extract_search_cards(page)}
                expected = {"count": 3}
            elif scenario == "communication_overlay":
                await delivery._open_communication(page)
                await delivery._open_communication(page)
                result = {"clicks": await page.evaluate("window.counter")}
                expected = {"clicks": 1}
            else:
                result = {"search": (await search.inspect(page)).as_dict(), "delivery": asdict(await delivery.inspect(page))}
                expected = {"authenticated": scenario not in {"logged_out", "recruiter_role", "auth_modal", "captcha", "mfa"},
                            "takeover": scenario in {"logged_out", "recruiter_role", "auth_modal", "captcha", "mfa"}}
                if scenario == "waiting_reply": expected["waiting_for_reply"] = True
                if scenario in {"reply_unlock", "direct_ready"}: expected["resume_ready"] = True
                if scenario == "no_resume": expected["resume_ready"] = False
                if scenario == "already_submitted": expected["application_submitted"] = True
            result["intercepted_requests"] = requests
            return result, expected
        finally:
            await context.close()
