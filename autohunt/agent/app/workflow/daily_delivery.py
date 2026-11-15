"""Checkpointed simulator communication and submission for daily runs."""
from __future__ import annotations

import asyncio
import time
import uuid

from ..browser.lock import BrowserExecutionLock
from ..browser.session import BrowserSession
from ..browser.mock_site_adapter import MOCK_SITE_ORIGIN
from ..core.models import BrowserMode, BrowserTaskStatus
from ..delivery.resume_binding import mock_resume_payload
from ..delivery.reconciliation import judge_receipt, resume_fingerprint


class SubmissionRevoked(ValueError):
    pass


async def evidence(page, job_url, applicant_id):
    """Read authoritative local receipts without sending or marking messages read."""
    return await page.evaluate("""async ({jobUrl, applicantId}) => {
      const id = Number(localStorage.getItem('userId'));
      if (!applicantId || id !== applicantId || localStorage.getItem('userRole') !== 'applicant')
        throw new Error('求职者账号与本轮绑定不一致');
      const jobId = Number(new URL(jobUrl).searchParams.get('id'));
      const headers = {'X-User-Id':String(id), 'X-User-Role':'applicant'};
      const read = async path => { const r = await fetch(path, {headers, redirect:'error', signal: AbortSignal.timeout(8000)});
        if (!r.ok) throw new Error('核验失败 HTTP ' + r.status); return r.json(); };
      const account = await read('/api/auth/me');
      const job = await read('/api/jobs/' + jobId);
      const messages = await read('/api/messages/peek?job_id=' + jobId + '&with_user_id=' + job.employer_id);
      const applications = await read('/api/applications/mine');
      const resumes = applications.some(a => a.job_id === jobId) ? await read('/api/resumes/mine') : [];
      return {contacted: messages.some(m => m.sender_role === 'applicant'),
        reply: messages.filter(m => m.sender_role === 'employer').at(-1)?.content || '',
        application: applications.find(a => a.job_id === jobId) || null, account, applications, resumes};
    }""", {"jobUrl": job_url, "applicantId": applicant_id})


async def deliver(service, run_id, cp):
    browser, jobs = service.browser, service.jobs
    adapter = browser.mock_delivery_adapter
    snapshot = cp["snapshot"]
    lock = BrowserExecutionLock(browser.data_root / "private" / "browser-session.lock", run_id)
    lock.acquire()
    try:
        if not cp["selected"]:
            return
        if browser.session is None:
            browser.session = BrowserSession(browser.profile_dir, headless=browser.headless, cdp_url=browser.cdp_url)
        if await browser.session.reuse(BrowserMode.CDP) is None:
            await browser.session.ensure_cdp_browser(MOCK_SITE_ORIGIN)
            await browser.session.open(BrowserMode.CDP)
        jobs.update_task(cp["browser_id"], status=BrowserTaskStatus.RUNNING, detail="每日任务：核对沟通和投递回执")

        def persist(job_id, phase=None, **values):
            item = cp["jobs"][str(job_id)]
            if phase:
                item["phase"] = phase
            item.update(values)
            service.save(run_id, cp, mutation_started=int(any(j["phase"] != "pending" for j in cp["jobs"].values())))
            return item

        def check_binding():
            if service.runs.get(run_id)["cancel_requested"]:
                raise asyncio.CancelledError()
            binding = snapshot["binding"]
            return mock_resume_payload(service.resume_workspace.read_document(), document_id=binding["resume_document_id"],
                version=binding["resume_version"], digest=binding["resume_digest"])

        def can_submit():
            settings = jobs.mock_workflow_settings()
            return bool(snapshot["auto_submit_epoch"] and settings["auto_submit"]
                        and settings["auto_submit_enabled_at"] == snapshot["auto_submit_epoch"])

        for job_id in cp["selected"]:
            item = cp["jobs"][str(job_id)]
            if item["phase"] in {"submitted", "withheld", "failed", "uncertain"}:
                continue
            await browser._respect_pause()
            job = jobs.get_job(job_id)
            url = adapter.normalize_job_url(job["source_url"])
            if not url:
                raise ValueError("每日投递仅支持本地模拟站")
            execution = uuid.uuid4().hex
            started = time.monotonic()
            event = {"tool": "daily_communication", "plan_id": run_id, "revision": 1, "step_id": f"job_{job_id}", "execution_id": execution}
            service.runs.event(run_id, "tool_started", event)
            try:
                page = await browser.session.navigate(url, page=await browser.session.recover_page(url))
                await browser._ensure_mock_access(cp["browser_id"], url, "daily_login_check")
                page = await browser.session.recover_page(url)
                if not snapshot.get("applicant_id"):
                    raise ValueError("本轮没有绑定求职者账号")
                observed = await evidence(page, url, snapshot["applicant_id"])
                if observed["application"]:
                    receipt = judge_receipt({**snapshot["binding"], **item, "target_url": url,
                        "action": "submit_resume", "mock_applicant_id": snapshot["applicant_id"]},
                        applicant_id=snapshot["applicant_id"], account=observed.get("account", {}),
                        applications=observed.get("applications", []), resumes=observed.get("resumes", []))
                    if receipt["status"] == "confirmed":
                        persist(job_id, "submitted")
                        jobs.mark_delivery_completed(job_id, "每日任务：已核实同一绑定简历的投递记录")
                    else:
                        persist(job_id, "uncertain", detail="存在投递记录但无法确认与本轮简历绑定一致")
                        jobs.mark_outcome_indeterminate(job_id, item["detail"])
                    continue
                check_binding()
                if item["phase"] == "submit_started":
                    persist(job_id, "uncertain", detail="提交中断后未找到成功回执，禁止重发，需人工核验")
                    jobs.mark_outcome_indeterminate(job_id, item["detail"])
                    continue
                if item["phase"] == "contact_started" and not observed["contacted"]:
                    persist(job_id, "uncertain", detail="沟通中断后未找到已发送消息，禁止盲目重发")
                    jobs.mark_outcome_indeterminate(job_id, item["detail"])
                    continue
                if item["phase"] == "pending":
                    if "preview_id" not in item:
                        preview = jobs.create_delivery_preview(job_id, **snapshot["binding"])
                        persist(job_id, preview_id=preview.id, delivery_id=f"daily_{run_id}_{job_id}")
                    try:
                        jobs.get_delivery_run(item["delivery_id"])
                    except KeyError:
                        jobs.authorize_delivery_run(item["preview_id"], item["delivery_id"], **snapshot["binding"])
                if not observed["contacted"]:
                    def before_contact():
                        check_binding()
                        persist(job_id, "contact_started")
                    observation = await adapter.prepare(page, url, before_communication=before_contact)
                    if observation.intervention_reason:
                        raise ValueError(observation.intervention_reason)
                    observed = await evidence(page, url, snapshot["applicant_id"])
                    if not observed["contacted"]:
                        persist(job_id, "uncertain", detail="沟通点击后未取得消息回执")
                        jobs.mark_outcome_indeterminate(job_id, item["detail"])
                        continue
                persist(job_id, "waiting_reply", reply_deadline=item.get("reply_deadline") or
                        service.clock().timestamp() + browser.watch_limits.reply)
                if not observed["reply"]:
                    jobs.mark_delivery_waiting_reply(job_id, "每日任务已发起沟通，等待招聘方回复")
                    if not can_submit():
                        persist(job_id, "withheld", detail="自动投递未授权；后续简历发送由用户确认")
                    elif service.clock().timestamp() >= item["reply_deadline"]:
                        persist(job_id, "withheld", detail="等待招聘方回复已超时，停止自动检查；后续投递需人工确认")
                        jobs.mark_job_needs_review(job_id, item["detail"])
                    continue
                jobs.mark_recruiter_reply(job_id, observed["reply"])
                if not can_submit():
                    persist(job_id, "withheld", detail="本轮自动投递授权未开启或已失效，未上传和发送简历")
                    jobs.mark_job_needs_review(job_id, item["detail"])
                    continue
                # The receipt above proves communication already exists; opening
                # the panel only exposes the bound resume controls.
                observation = await adapter.prepare(page, url)
                if observation.intervention_reason or not observation.hr_reply_text:
                    raise ValueError(observation.intervention_reason or "页面尚未显示招聘方回复，未提交")
                payload = check_binding()
                if not can_submit():
                    raise SubmissionRevoked("自动投递授权已撤销，未上传简历")
                binding = await adapter.bind_resume(page, payload, applicant_id=snapshot["applicant_id"],
                                                    resume_id=item.get("mock_resume_id"))
                persist(job_id, mock_resume_id=binding.resume_id, mock_resume_digest=resume_fingerprint(payload))
                def before_submit():
                    check_binding()
                    if not can_submit():
                        raise SubmissionRevoked("自动投递授权已撤销，未继续点击")
                    persist(job_id, "submit_started")
                result = await adapter.submit(page, binding=binding, before_click=before_submit)
                if result.confirmed:
                    persist(job_id, "submitted")
                    jobs.mark_delivery_completed(job_id, "每日任务：招聘方回复后已确认发送绑定简历")
                elif result.attempted or item["phase"] == "submit_started":
                    persist(job_id, "uncertain", detail=result.detail)
                    jobs.mark_outcome_indeterminate(job_id, result.detail)
                else:
                    persist(job_id, "failed", detail=result.detail)
                    jobs.mark_job_needs_review(job_id, result.detail)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                phase = ("uncertain" if item["phase"] in {"submit_started", "contact_started"}
                         else "withheld" if isinstance(exc, SubmissionRevoked) else "failed")
                persist(job_id, phase, detail=f"{type(exc).__name__}: {str(exc)[:250]}")
                if phase == "uncertain":
                    jobs.mark_outcome_indeterminate(job_id, item["detail"])
                else:
                    jobs.mark_job_needs_review(job_id, item["detail"])
            finally:
                status = {"submitted": "completed", "waiting_reply": "waiting_external", "withheld": "blocked",
                          "uncertain": "outcome_indeterminate", "failed": "failed"}.get(item["phase"], "interrupted")
                cp["daily_progress"]["steps"] = [s for s in cp["daily_progress"]["steps"] if s["id"] != event["step_id"]]
                cp["daily_progress"]["steps"].append({"id": event["step_id"], "tool": event["tool"], "status": status,
                                           "execution_id": execution, "detail": item.get("detail", "")})
                service.save(run_id, cp)
                service.runs.event(run_id, "tool_finished", {**event, "status": status, "detail": item.get("detail", ""),
                    "duration_ms": round((time.monotonic() - started) * 1000), "attempts": 1})
                if item.get("delivery_id"):
                    jobs.update_delivery_run(item["delivery_id"], status={"waiting_external": "waiting_for_reply", "blocked": "cancelled",
                        "interrupted": "running"}.get(status, status), detail=item.get("detail", status),
                        submitted=item["phase"] == "submitted", checkpoint=item["phase"],
                        mock_resume_id=item.get("mock_resume_id"), mock_resume_digest=item.get("mock_resume_digest"),
                        mock_applicant_id=snapshot.get("applicant_id"))
        jobs.update_task(cp["browser_id"], status=BrowserTaskStatus.COMPLETED, detail="每日任务本轮检查完成")
    finally:
        lock.release()
