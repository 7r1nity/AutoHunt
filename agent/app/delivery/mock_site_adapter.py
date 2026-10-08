from __future__ import annotations

import re
import asyncio
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from playwright.async_api import Page


@dataclass(frozen=True)
class MockApplyObservation:
    authenticated: bool
    intervention_reason: str | None
    resume_ready: bool
    waiting_for_reply: bool
    button_text: str
    hr_reply_text: str | None = None
    application_submitted: bool = False
    direct_apply: bool = False


@dataclass(frozen=True)
class MockSubmitResult:
    confirmed: bool
    duplicate: bool
    detail: str
    await_reply: bool = False


class MockJobSiteAdapter:
    """Fixed DOM adapter for the local ``C:\\job_agent`` recruitment site.

    It never reads or solves MFA codes. Authentication and MFA remain visible
    user actions; automation resumes only after an explicit AutoHunt continue.
    """

    submit_timeout_seconds = 8.0
    selector_version = "mock-job-site-v3"
    allowed_hosts = {"127.0.0.1", "localhost"}
    allowed_ports = {8080}

    @classmethod
    def normalize_job_url(cls, value: str) -> str | None:
        parsed = urlparse(str(value or "").strip())
        if parsed.scheme != "http" or (parsed.hostname or "").lower() not in cls.allowed_hosts:
            return None
        if (parsed.port or 80) not in cls.allowed_ports:
            return None
        job_id: str | None = None
        if parsed.path == "/job.html":
            values = parse_qs(parsed.query).get("id") or []
            job_id = values[0] if values else None
        else:
            match = re.fullmatch(r"/api/jobs/(\d+)", parsed.path.rstrip("/"))
            if match:
                job_id = match.group(1)
        if not job_id or not job_id.isdigit():
            return None
        query = {"id": job_id}
        if (parse_qs(parsed.query).get("flow") or [""])[0] == "direct":
            query["flow"] = "direct"
        return urlunparse((parsed.scheme, parsed.netloc, "/job.html", "", urlencode(query), ""))

    async def inspect(self, page: Page) -> MockApplyObservation:
        raw = await page.evaluate(
            """() => {
              const visible = el => !!el && getComputedStyle(el).display !== 'none' &&
                !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
              const button = document.querySelector('#resume-btn');
              const directButton = document.querySelector('#direct-apply-btn');
              const directStatus = document.querySelector('#direct-status-bar');
              const mfa = document.querySelector('#mfa-row, #mfa-box');
              const captcha = Array.from(document.querySelectorAll('[class*=captcha], [id*=captcha], [class*=verify], [id*=verify]')).find(visible);
              const pageText = document.body?.innerText || '';
              const modal = document.querySelector('#auth-modal');
              const userId = localStorage.getItem('userId') || '';
              const userRole = localStorage.getItem('userRole') || '';
              const hrReplies = Array.from(document.querySelectorAll('.bubble-row:not(.mine) p'))
                .map(node => node.textContent?.trim() || '').filter(Boolean);
              return {
                authenticated: !!userId && userRole === 'applicant',
                authVisible: visible(modal),
                mfaVisible: visible(mfa) && visible(modal),
                captchaVisible: !!captcha || /真人验证|滑块验证|图形验证码|安全验证/.test(pageText),
                buttonExists: !!button,
                buttonDisabled: button ? !!button.disabled : true,
                buttonText: button?.textContent?.trim() || '',
                directButtonExists: !!directButton,
                directButtonDisabled: directButton ? !!directButton.disabled : true,
                directButtonText: directButton?.textContent?.trim() || '',
                directStatusVisible: visible(directStatus),
                gateText: document.querySelector('#gate-text')?.textContent?.trim() || '',
                hrReplyText: hrReplies.at(-1) || ''
              };
            }"""
        )
        authenticated = bool(raw.get("authenticated")) and not any(raw.get(key) for key in ("authVisible", "mfaVisible", "captchaVisible"))
        reason = None
        if not authenticated:
            if raw.get("captchaVisible"):
                reason = "检测到验证码或真人验证，必须由用户人工完成"
            elif raw.get("mfaVisible"):
                reason = "等待用户在模拟招聘网站完成 MFA"
            else:
                reason = "等待用户登录模拟招聘网站；验证码和 MFA 必须人工完成"

        direct_apply = bool(raw.get("directButtonExists"))
        application_submitted = (
            "已投递" in str(raw.get("directButtonText") or "") or "已发送" in str(raw.get("buttonText") or "") or bool(raw.get("directStatusVisible"))
        )
        if direct_apply:
            resume_ready = authenticated and not application_submitted and not bool(raw.get("directButtonDisabled"))
            waiting_for_reply = authenticated and application_submitted and not bool(raw.get("hrReplyText"))
            button_text = str(raw.get("directButtonText") or "")
        else:
            resume_ready = bool(raw.get("buttonExists")) and not bool(raw.get("buttonDisabled"))
            waiting_for_reply = (
                authenticated
                and bool(raw.get("buttonExists"))
                and bool(raw.get("buttonDisabled"))
                and "回复" in str(raw.get("gateText") or "")
            )
            button_text = str(raw.get("buttonText") or "")
        return MockApplyObservation(
            authenticated=authenticated,
            intervention_reason=reason,
            resume_ready=resume_ready,
            waiting_for_reply=waiting_for_reply,
            button_text=button_text,
            hr_reply_text=str(raw.get("hrReplyText") or "") or None,
            application_submitted=application_submitted,
            direct_apply=direct_apply,
        )

    async def prepare(self, page: Page, job_url: str) -> MockApplyObservation:
        if page.url != job_url:
            await page.goto(job_url, wait_until="domcontentloaded", timeout=30_000)
        await page.locator("#job-hero h1").wait_for(state="visible", timeout=10_000)
        observation = await self.inspect(page)
        if not observation.authenticated:
            login = page.locator("#nav-login")
            if await login.count() and await login.first.is_visible() and not await page.locator("#auth-modal").is_visible():
                await login.first.click()
                await page.wait_for_timeout(300)
            return await self.inspect(page)

        if observation.direct_apply:
            await page.locator("#job-hero").get_by_role("button", name="投递后沟通", exact=True).click()
            return await self.inspect(page)
        communicate = page.locator("button.communicate-btn")
        if await communicate.count() and await communicate.first.is_visible():
            await communicate.first.click()
            await page.wait_for_timeout(800)
        return await self.inspect(page)

    async def submit(self, page: Page) -> MockSubmitResult:
        """Click once; reconcile using page evidence, never retry a submission."""
        before = await self.inspect(page)
        if before.intervention_reason:
            return MockSubmitResult(False, False, before.intervention_reason)
        if before.application_submitted:
            return MockSubmitResult(True, True, "页面已存在投递成功证据", before.direct_apply)
        selector = "#direct-apply-btn" if before.direct_apply else "#resume-btn"
        button = page.locator(selector)
        if not await button.count() or await button.is_disabled():
            return MockSubmitResult(False, False, "投递按钮不可用")
        await button.click()
        deadline = time.monotonic() + self.submit_timeout_seconds
        while time.monotonic() < deadline:
            observation = await self.inspect(page)
            if observation.application_submitted:
                return MockSubmitResult(True, False, "页面已确认投递成功", before.direct_apply)
            if observation.intervention_reason:
                return MockSubmitResult(False, False, "提交后出现验证，投递结果待核实")
            await asyncio.sleep(0.1)
        return MockSubmitResult(False, False, "提交后未取得成功证据，禁止自动重试")
