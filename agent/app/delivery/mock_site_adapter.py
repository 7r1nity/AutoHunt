from __future__ import annotations

import re
import asyncio
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from typing import Callable

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


@dataclass(frozen=True)
class MockResumeBinding:
    resume_id: int
    applicant_id: int
    job_url: str
    payload: dict


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

    @staticmethod
    async def _ensure_demo_resume(page: Page) -> None:
        """Guarantee a sendable resume on the site, as the page itself would.

        The mock site gates sending on having a resume and creates a demo one on
        demand when a human clicks 发送简历. Automation never clicks that button
        until the gate is open, so without this the send button stays disabled and
        every delivery fails with "当前账号没有可用简历或发送按钮不可用".
        Only ever adds a resume when the account has none, and then asks the page to
        re-read its own list so the gate it renders matches the new state.
        """
        try:
            existing = await page.evaluate(
                """async () => {
                  const id = localStorage.getItem('userId') || '';
                  if (!id) return null;
                  const response = await fetch('/api/resumes/mine', { headers: { 'X-User-Id': id, 'X-User-Role': 'applicant' } });
                  if (!response.ok) return null;
                  return await response.json();
                }"""
            )
            if not isinstance(existing, list) or existing:
                return
            created = await page.evaluate(
                """async () => {
                  const id = localStorage.getItem('userId') || '';
                  const response = await fetch('/api/resumes', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-User-Id': id, 'X-User-Role': 'applicant' },
                    body: JSON.stringify({
                      name: '演示简历（自动生成）', education: '本科', experience_years: 3,
                      summary: '本地模拟站演示用简历：用于验证招聘方回复后发送简历的流程，不代表真实候选人资料。',
                      skills: ['沟通', '客户开发', '数据分析'], work_history: []
                    })
                  });
                  return response.ok;
                }"""
            )
            if created:
                # The button is rendered by the page's own state, not by the server.
                await page.evaluate("""() => { if (typeof loadResumes === 'function') return loadResumes(); }""")
                await page.wait_for_timeout(500)
        except Exception:  # noqa: BLE001 - never break a run over resume provisioning
            return

    @staticmethod
    async def _chat_open(page: Page) -> bool:
        try:
            return bool(await page.evaluate(
                """() => {
                  const panel = document.querySelector('#chat-panel');
                  if (!panel) return false;
                  const visible = getComputedStyle(panel).display !== 'none' &&
                    !!(panel.offsetWidth || panel.offsetHeight || panel.getClientRects().length);
                  return visible && panel.classList.contains('open');
                }"""
            ))
        except Exception:  # noqa: BLE001 - a page probe must never break the flow
            return False

    async def _open_communication(self, page: Page) -> None:
        """Open the chat panel once, without waiting 30s on an overlay.

        Once the panel is open it covers the button's hit point, so a second
        Playwright click waits for actionability until it times out. The DOM
        opening message is a side effect too: do not retry an ambiguous click.
        """
        if await self._chat_open(page):
            return
        communicate = page.locator("button.communicate-btn")
        if not await communicate.count() or not await communicate.first.is_visible():
            return
        try:
            await communicate.first.click(timeout=5_000)
        except Exception as exc:
            if not await self._chat_open(page):
                raise RuntimeError("发起沟通未获确认；请核对模拟站会话，不会重复点击") from exc
        await page.wait_for_timeout(800)

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
            if not await self._chat_open(page):
                await page.locator("#job-hero").get_by_role("button", name="投递后沟通", exact=True).click(timeout=5_000)
            await self._ensure_demo_resume(page)
            return await self.inspect(page)
        await self._open_communication(page)
        await self._ensure_demo_resume(page)
        return await self.inspect(page)

    async def bind_resume(self, page: Page, payload: dict, *, applicant_id: int | None = None,
                          resume_id: int | None = None) -> MockResumeBinding:
        """Sync only the previewed document; POST once, never synthesize facts."""
        job_url = self.normalize_job_url(page.url)
        if not job_url:
            raise ValueError("简历同步仅支持本地模拟站职位页面")
        result = await page.evaluate("""async ({payload, applicantId, resumeId}) => {
          const id = Number(localStorage.getItem('userId'));
          if (!id || localStorage.getItem('userRole') !== 'applicant' || (applicantId && id !== applicantId))
            throw new Error('求职者账号与授权绑定不一致');
          if (typeof selectedResumeId !== 'function' || typeof loadResumes !== 'function')
            throw new Error('请刷新模拟站网页，加载支持主简历绑定的版本');
          const headers = {'Content-Type':'application/json', 'X-User-Id':String(id), 'X-User-Role':'applicant'};
          const read = async (path, options = {}) => {
            const response = await fetch(path, {headers, redirect:'error', ...options});
            if (!response.ok) throw new Error('主简历同步失败 HTTP ' + response.status);
            return response.json();
          };
          const equal = resume => Object.keys(payload).every(key => JSON.stringify(resume[key]) === JSON.stringify(payload[key]));
          const mine = await read('/api/resumes/mine');
          let exact = resumeId ? mine.find(r => r.id === resumeId && equal(r)) : mine.find(equal);
          if (resumeId && !exact) throw new Error('已绑定的模拟站简历缺失或发生变化');
          if (!exact) exact = await read('/api/resumes', {method:'POST', body:JSON.stringify(payload)});
          if (!equal(exact) || exact.applicant_id !== id) throw new Error('同步后的简历内容或账号不一致');
          await loadResumes();
          const select = document.querySelector('#resume-select');
          if (!select || !Array.from(select.options).some(o => o.value === String(exact.id)))
            throw new Error('授权简历未出现在页面选项中');
          select.value = String(exact.id);
          window.autohuntResumeBinding = {resumeId:exact.id, applicantId:id};
          return {resume_id:exact.id, applicant_id:id};
        }""", {"payload": payload, "applicantId": applicant_id, "resumeId": resume_id})
        return MockResumeBinding(int(result["resume_id"]), int(result["applicant_id"]), job_url, payload)

    async def _binding_evidence(self, page: Page, binding: MockResumeBinding) -> dict:
        if self.normalize_job_url(page.url) != binding.job_url:
            raise ValueError("投递页面与简历绑定的职位不一致")
        return await page.evaluate("""async ({resumeId, applicantId, payload}) => {
          if (Number(localStorage.getItem('userId')) !== applicantId || localStorage.getItem('userRole') !== 'applicant')
            throw new Error('求职者账号已变化');
          const headers = {'X-User-Id':String(applicantId),'X-User-Role':'applicant'};
          const read = async path => {
            const r = await fetch(path, {headers,redirect:'error'});
            if (!r.ok) throw new Error('无法核实授权简历与投递记录');
            return r.json();
          };
          const resume = await read('/api/resumes/' + resumeId);
          if (resume.applicant_id !== applicantId || !Object.keys(payload).every(k => JSON.stringify(resume[k]) === JSON.stringify(payload[k])))
            throw new Error('授权简历内容已变化');
          const jobId = Number(new URL(location.href).searchParams.get('id'));
          const application = (await read('/api/applications/mine')).find(a => a.job_id === jobId);
          if (application) return {submitted:true, matches:application.resume_id === resumeId};
          const select = document.querySelector('#resume-select');
          const bound = window.autohuntResumeBinding;
          if (!select || select.value !== String(resumeId) || bound?.resumeId !== resumeId || bound?.applicantId !== applicantId)
            throw new Error('页面选择的简历不是已授权简历');
          return {submitted:false,matches:false};
        }""", {"resumeId": binding.resume_id, "applicantId": binding.applicant_id, "payload": binding.payload})

    async def submit(self, page: Page, *, binding: MockResumeBinding | None = None,
                     before_click: Callable[[], object] | None = None) -> MockSubmitResult:
        """Click once; reconcile using page evidence, never retry a submission."""
        if binding is None:
            return MockSubmitResult(False, False, "缺少已授权主简历的模拟站绑定；未点击投递")
        evidence = await self._binding_evidence(page, binding)
        if evidence["submitted"]:
            return MockSubmitResult(bool(evidence["matches"]), bool(evidence["matches"]),
                "已存在同一授权简历的投递记录" if evidence["matches"] else "已存在其他简历的投递记录，需人工核对")
        before = await self.inspect(page)
        if before.intervention_reason:
            return MockSubmitResult(False, False, before.intervention_reason)
        if before.application_submitted:
            return MockSubmitResult(False, False, "页面与投递记录不一致，需人工核对")
        selector = "#direct-apply-btn" if before.direct_apply else "#resume-btn"
        button = page.locator(selector)
        if not await button.count() or await button.is_disabled():
            return MockSubmitResult(False, False, "投递按钮不可用")
        if before_click is not None:
            before_click()
        try:
            await button.click(timeout=8_000)
        except Exception:
            pass  # Click may already have reached the site. Reconcile, never click again.
        deadline = time.monotonic() + self.submit_timeout_seconds
        while time.monotonic() < deadline:
            try:
                evidence = await self._binding_evidence(page, binding)
                observation = await self.inspect(page)
            except Exception:
                return MockSubmitResult(False, False, "提交后无法核实账号、简历或投递记录，禁止自动重试")
            if evidence["submitted"]:
                return MockSubmitResult(bool(evidence["matches"]), False,
                    "模拟站已确认授权简历投递成功" if evidence["matches"] else "投递记录的简历不符，需人工核对", before.direct_apply)
            if observation.intervention_reason:
                return MockSubmitResult(False, False, "提交后出现验证，投递结果待核实")
            await asyncio.sleep(0.1)
        return MockSubmitResult(False, False, "提交后未取得成功证据，禁止自动重试")
