from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

from ..browser.lock import BrowserExecutionLock, BrowserLockBusy
from ..browser.session import BrowserSession
from ..core.models import BrowserMode, DeliveryTaskStatus, DeliveryTaskView
from ..jobs.store import JobStore
from .mock_site_adapter import MockJobSiteAdapter
from .resume_binding import mock_resume_payload
from ..memory.resume_workspace import ResumeWorkspaceStore
from ..runtime.resilience import WatchLimits, WatchdogExpired, guarded_operation, wait_expired


class UnsupportedDeliverySite(ValueError):
    pass

_TERMINAL = {
    DeliveryTaskStatus.COMPLETED,
    DeliveryTaskStatus.FAILED,
    DeliveryTaskStatus.OUTCOME_INDETERMINATE,
    DeliveryTaskStatus.CANCELLED,
}


class DeliveryAutomationService:
    """One-job, one-authorization browser delivery state machine."""

    def __init__(self, jobs: JobStore, data_root, *, headless: bool, cdp_url: str, reply_poll_seconds: float | None = 5.0,
                 resume_workspace: ResumeWorkspaceStore | None = None) -> None:
        self.jobs = jobs
        self.data_root = data_root
        self.profile_dir = data_root / "browser-profile"
        self.headless = headless
        self.cdp_url = cdp_url
        self.reply_poll_seconds = reply_poll_seconds
        self.resume_workspace = resume_workspace
        # A start that failed before any click cannot have sent anything, so it may
        # be retried this many times without a fresh authorisation.
        self.retry_limit = 3
        self.adapter = MockJobSiteAdapter()
        self.session: BrowserSession | None = None
        self.worker: asyncio.Task | None = None
        self.gate = asyncio.Event()
        self.gate.set()
        self.monitor_wakeup = asyncio.Event()
        self.state = self.jobs.latest_delivery_run()
        self.lifecycle_lock = asyncio.Lock()
        self.watch_limits = WatchLimits.configured()

    def current(self) -> DeliveryTaskView:
        return self.state.model_copy(deep=True)

    def reload_persisted_state(self) -> DeliveryTaskView:
        self.state = self.jobs.latest_delivery_run()
        return self.current()

    def supports(self, source_url: str) -> bool:
        return self.adapter.normalize_job_url(source_url) is not None

    def _authorized_resume_payload(self) -> dict:
        if self.resume_workspace is None or not self.resume_workspace.candidate_state.read().profile.resume_id:
            raise ValueError("没有启用的主简历；不会发送演示简历或其他账号的简历")
        return mock_resume_payload(self.resume_workspace.read_document(), document_id=self.state.resume_document_id,
            version=self.state.resume_version, digest=self.state.resume_digest)

    async def authorize_and_start(
        self, preview_id: str, *, resume_document_id: str | None = None,
        resume_version: int | None = None, resume_digest: str | None = None,
        monitor_reply: bool = True,
    ):
        """Atomically issue, consume and dispatch a one-job capability."""
        async with self.lifecycle_lock:
            if self.worker and not self.worker.done():
                raise RuntimeError("已有投递任务正在运行")
            job_id = self.jobs.get_delivery_preview_job_id(preview_id)
            job = self.jobs.get_job(job_id)
            if not self.supports(str(job.get("source_url") or "")):
                raise UnsupportedDeliverySite("当前自动投递执行器仅支持本地模拟招聘网站；BOSS Adapter 尚未开放真实提交")
            run_id = f"delivery_{uuid.uuid4().hex}"
            authorization_id, job_id, expires_at, self.state = self.jobs.authorize_delivery_run(
                preview_id,
                run_id,
                resume_document_id=resume_document_id,
                resume_version=resume_version,
                resume_digest=resume_digest,
                action="submit_resume",
            )
            self.gate.set()
            self.worker = asyncio.create_task(self._run_until_terminal(job_id) if monitor_reply else self._run_once_guarded(job_id, require_reply=True), name=f"delivery-{job_id}")
            return authorization_id, job_id, expires_at, self.current()

    async def resume_authorized_run(self, run_id: str, **binding) -> DeliveryTaskView:
        """Check one waiting authorized run without minting/replaying a capability."""
        async with self.lifecycle_lock:
            if self.worker and not self.worker.done():
                raise RuntimeError("已有投递任务正在运行")
            saved = self.jobs.get_delivery_run(run_id)
            if saved.status != DeliveryTaskStatus.WAITING_FOR_REPLY or saved.submitted:
                raise ValueError("只有等待招聘方回复的已授权任务可以检查；终态不会重试")
            if any(getattr(saved, key) != value for key, value in binding.items()):
                raise ValueError("当前简历与投递授权绑定不一致")
            if not self.supports(saved.target_url or ""):
                raise UnsupportedDeliverySite("仅支持本地模拟招聘网站")
            self.state = saved
            self.gate.set()
            self.worker = asyncio.create_task(self._run_once_guarded(saved.job_id, require_reply=True), name=f"delivery-check-{saved.job_id}")
            return self.current()
    async def start(self, authorization_id: str, token: str, job_id: int) -> DeliveryTaskView:
        async with self.lifecycle_lock:
            if self.worker and not self.worker.done():
                raise RuntimeError("已有投递任务正在运行")
            job = self.jobs.get_job(job_id)
            if not self.supports(str(job.get("source_url") or "")):
                raise UnsupportedDeliverySite("当前自动投递执行器仅支持本地模拟招聘网站；BOSS Adapter 尚未开放真实提交")
            consumed_job_id = self.jobs.consume_delivery_authorization(authorization_id, token)
            if consumed_job_id != job_id:
                raise ValueError("投递授权与职位不匹配")
            run_id = f"delivery_{uuid.uuid4().hex}"
            self.state = self.jobs.create_delivery_run(run_id, job_id)
            self.gate.set()
            self.worker = asyncio.create_task(self._run_until_terminal(job_id), name=f"delivery-{job_id}")
            return self.current()

    async def resume(self) -> DeliveryTaskView:
        async with self.lifecycle_lock:
            if self.state.status == DeliveryTaskStatus.WAITING_FOR_USER:
                self.gate.set()
                self._update(
                    DeliveryTaskStatus.STARTING,
                    "正在重新检查登录和 MFA 状态",
                    reason=None,
                    checkpoint="user_resume_requested",
                )
                if (not self.worker or self.worker.done()) and self.state.job_id is not None:
                    self.worker = asyncio.create_task(
                        self._run_until_terminal(self.state.job_id),
                        name=f"delivery-resume-{self.state.job_id}",
                    )
                return self.current()
            if self.state.status == DeliveryTaskStatus.WAITING_FOR_REPLY and self.state.job_id is not None:
                if self.worker and not self.worker.done():
                    self._update(
                        DeliveryTaskStatus.WAITING_FOR_REPLY,
                        "正在立即检查招聘方回复和简历发送按钮",
                        checkpoint="reply_check_requested",
                    )
                    self.monitor_wakeup.set()
                else:
                    self._update(
                        DeliveryTaskStatus.STARTING,
                        "正在重新连接浏览器并检查招聘方回复",
                        checkpoint="reply_check_requested",
                    )
                    self.worker = asyncio.create_task(self._run_until_terminal(self.state.job_id), name=f"delivery-resume-{self.state.job_id}")
            return self.current()

    async def stop(self) -> DeliveryTaskView:
        async with self.lifecycle_lock:
            if self.state.status in {DeliveryTaskStatus.COMPLETED, DeliveryTaskStatus.FAILED,
                                      DeliveryTaskStatus.OUTCOME_INDETERMINATE, DeliveryTaskStatus.CANCELLED,
                                      DeliveryTaskStatus.IDLE}:
                return self.current()
            self.gate.set()
            self.monitor_wakeup.set()
            if self.worker and not self.worker.done():
                self.worker.cancel()
                await asyncio.gather(self.worker, return_exceptions=True)
            if not self.state.submitted and self.state.checkpoint in {"submit_started", "submission_unconfirmed"}:
                detail = "发送简历期间被中断，结果需人工核验；不会自动重试"
                self.jobs.mark_outcome_indeterminate(self.state.job_id, detail)
                self._update(DeliveryTaskStatus.OUTCOME_INDETERMINATE, detail)
            else:
                detail = "监测已停止，已确认的投递保留" if self.state.submitted else "投递任务已停止；已经发送的沟通消息保留"
                if not self.state.submitted and self.state.job_id is not None:
                    self.jobs.mark_delivery_failed(self.state.job_id, detail)
                self._update(DeliveryTaskStatus.CANCELLED, detail)
            return self.current()

    async def shutdown(self) -> None:
        self.gate.set()
        self.monitor_wakeup.set()
        if self.worker and not self.worker.done():
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        if self.session:
            await self.session.close()
            self.session = None

    def _update(
        self, status: DeliveryTaskStatus, detail: str, *, url: str | None = None,
        reason: str | None = None, submitted: bool | None = None,
        checkpoint: str | None = None,
    ) -> None:
        self.state.status = status
        self.state.detail = detail
        self.state.takeover_reason = reason
        if url is not None:
            self.state.current_url = url
        if submitted is not None:
            self.state.submitted = submitted
        if checkpoint is not None:
            self.state.checkpoint = checkpoint
        self.state.updated_at = datetime.now(UTC)
        if self.state.id:
            self.state = self.jobs.update_delivery_run(
                self.state.id,
                status=self.state.status,
                checkpoint=self.state.checkpoint,
                detail=self.state.detail,
                takeover_reason=self.state.takeover_reason,
                current_url=self.state.current_url,
                submitted=self.state.submitted,
                recruiter_reply=self.state.recruiter_reply,
                recruiter_reply_at=self.state.recruiter_reply_at,
                last_checked_at=self.state.last_checked_at,
                mock_resume_id=self.state.mock_resume_id,
                mock_applicant_id=self.state.mock_applicant_id,
                attempts=self.state.attempts,
            )

    async def _run_until_terminal(self, job_id: int) -> None:
        """Open the conversation, then hand the wait to the periodic reply check.

        The single delivery worker must not be held for the whole reply window: one
        job waiting on a recruiter blocked every later job for ~530s. Dispatch now
        stops as soon as the run enters waiting_for_reply, and
        MockWorkflowCoordinator.check_reply_queue resumes it when a reply lands.
        """
        batch = any(item["run_id"] == self.state.id for item in self.jobs.mock_delivery_queue())
        await self._run_once_guarded(job_id, require_reply=batch)
        if self.state.status != DeliveryTaskStatus.WAITING_FOR_REPLY:
            return
        # The coordinator finds batch rows or an active, individually authorized run.
        self._update(DeliveryTaskStatus.WAITING_FOR_REPLY,
            "已发起沟通；后台按周期检查招聘方回复，收到回复后自动发送已授权简历")

    def expire_reply_wait(self, job_id: int) -> None:
        detail = "招聘方回复等待已过期，监测已停止；已确认的投递保留，未投递需重新核对授权"
        if self.state.submitted:
            self._update(DeliveryTaskStatus.COMPLETED, detail, checkpoint="reply_timeout")
        else:
            self.jobs.mark_delivery_failed(job_id, detail)
            self._update(DeliveryTaskStatus.FAILED, detail, checkpoint="reply_timeout")

    async def _run_once_guarded(self, job_id: int, *, require_reply: bool = False) -> None:
        if wait_expired(self.state.started_at, self.watch_limits.reply):
            self.expire_reply_wait(job_id)
            return
        def observe():
            view = self.state
            return ((view.status, view.checkpoint, view.current_url, view.submitted,
                     view.recruiter_reply), view.status == DeliveryTaskStatus.WAITING_FOR_USER)
        try:
            await guarded_operation(self._run_once(job_id, require_reply=require_reply),
                                    observe, limits=self.watch_limits)
        except BrowserLockBusy as exc:
            # Transient contention (a search or another delivery holds the browser).
            # The action never started, so this must not consume the authorization:
            # keep the run waiting and try again on the next check cycle.
            self._update(DeliveryTaskStatus.WAITING_FOR_REPLY,
                         f"浏览器忙，稍后自动重试检查招聘方回复（{exc}）",
                         checkpoint="waiting_for_reply")
        except WatchdogExpired as exc:
            # Never retry a click whose outcome is unknown, even if the timeout
            # happened after submit() returned but before persistence finished.
            if self.state.checkpoint in {"submit_started", "submission_unconfirmed"}:
                self.jobs.mark_outcome_indeterminate(job_id, str(exc))
                self._update(DeliveryTaskStatus.OUTCOME_INDETERMINATE, str(exc), checkpoint="submission_unconfirmed")
            elif self.state.submitted:
                self._update(DeliveryTaskStatus.COMPLETED, str(exc), checkpoint=exc.code)
            else:
                # The watchdog aborted a run that had not reached the submit click, so
                # nothing was sent and a bounded retry cannot duplicate anything. This
                # is the path a transient browser error actually takes; retrying only
                # in _run_once's own handler never fired.
                self._retry_or_fail(job_id, str(exc))

    def _retry_or_fail(self, job_id: int, detail: str) -> None:
        """Bounded retry for a run that failed before the submit click.

        Nothing was sent, so retrying cannot duplicate a submission — the checkpoint
        proves the action never began. The attempt count is persisted, so a restart
        cannot reset the budget and loop forever.
        """
        attempts = self.jobs.bump_delivery_attempts(self.state.id) if self.state.id else self.retry_limit
        if attempts <= self.retry_limit:
            self._update(DeliveryTaskStatus.WAITING_FOR_REPLY,
                         f"本次提交未能开始（{detail[:160]}）；第 {attempts}/{self.retry_limit} 次自动重试，无需重新授权",
                         checkpoint="waiting_for_reply")
        else:
            self.jobs.mark_delivery_failed(
                job_id, f"{detail[:300]}；已自动重试 {self.retry_limit} 次仍未成功，需人工核对")
            self._update(DeliveryTaskStatus.FAILED, f"已自动重试 {self.retry_limit} 次仍未成功",
                         checkpoint="retry_exhausted")

    async def _run_once(self, job_id: int, *, require_reply: bool = False) -> None:
        lock = BrowserExecutionLock(self.data_root / "private" / "browser-session.lock", self.state.id or f"delivery:{job_id}")
        submit_started = False
        try:
            lock.acquire()
            self._authorized_resume_payload()
            job = self.jobs.get_job(job_id)
            job_url = self.adapter.normalize_job_url(str(self.state.target_url or job.get("source_url") or ""))
            if not job_url:
                raise ValueError("职位来源不是受支持的本地模拟招聘网站")
            if self.session is None:
                self.session = BrowserSession(self.profile_dir, headless=self.headless, cdp_url=self.cdp_url)
            mode = await self.session.reuse(BrowserMode.CDP)
            if mode is None:
                await self.session.ensure_cdp_browser()
                await self.session.ensure_cdp_window(job_url)
                await self.session.open(BrowserMode.CDP)
            else:
                await self.session.ensure_cdp_window(job_url)
            page = self.session.page_for_url(job_url)
            if page is None:
                for _ in range(15):
                    await asyncio.sleep(0.2)
                    page = self.session.page_for_url(job_url)
                    if page is not None:
                        break
            if page is None:
                raise RuntimeError("无法绑定模拟招聘网站标签页")
            self._update(DeliveryTaskStatus.RUNNING, "正在检查投递页面", url=page.url)

            while True:
                # Communication sends a greeting too. Bind the account BEFORE
                # prepare() can open chat/send, not only before resume submission.
                identity = await page.evaluate("({id:Number(localStorage.getItem('userId')),role:localStorage.getItem('userRole')})")
                if identity.get("role") == "applicant" and identity.get("id"):
                    applicant_id = identity["id"]
                    expected_applicant = self.state.mock_applicant_id or self.jobs.mock_workflow_settings().get("applicant_id")
                    if not isinstance(applicant_id, int) or applicant_id < 1 or expected_applicant and applicant_id != expected_applicant:
                        raise ValueError("当前模拟站求职者账号与授权任务不一致")
                    self.state.mock_applicant_id = applicant_id
                    self._update(DeliveryTaskStatus.RUNNING, "已核对求职者账号", checkpoint="applicant_bound")
                observation = await self.adapter.prepare(page, job_url)
                self.state.last_checked_at = datetime.now(UTC)
                self._update(DeliveryTaskStatus.RUNNING, "已读取投递页面", url=page.url)
                if observation.intervention_reason:
                    self.gate.clear()
                    self._update(
                        DeliveryTaskStatus.WAITING_FOR_USER,
                        "投递已暂停，请在 AutoHunt Chrome 中完成人工验证",
                        url=page.url,
                        reason=observation.intervention_reason,
                        checkpoint="waiting_for_user",
                    )
                    await self.gate.wait()
                    page = self.session.page_for_url(job_url)
                    if page is None:
                        raise RuntimeError("人工接管后模拟招聘网站页面已关闭")
                    continue
                if observation.waiting_for_reply and not observation.application_submitted:
                    detail = "已发起职位沟通；后台将持续监测招聘方回复，按钮解锁后自动发送已授权简历"
                    self.jobs.mark_delivery_waiting_reply(job_id, detail)
                    self._update(DeliveryTaskStatus.WAITING_FOR_REPLY, detail, url=page.url, checkpoint="waiting_for_reply", submitted=observation.application_submitted)
                    return
                if require_reply and not observation.hr_reply_text and not observation.application_submitted:
                    raise RuntimeError("本次批量授权要求先收到招聘方回复；此页面尚无明确回复，不会直接发送简历，请人工核对")
                if observation.hr_reply_text:
                    reply_at = self.jobs.mark_recruiter_reply(job_id, observation.hr_reply_text)
                    self.state.recruiter_reply = observation.hr_reply_text
                    self.state.recruiter_reply_at = datetime.fromisoformat(reply_at)
                    self._update(DeliveryTaskStatus.RUNNING, f"已收到招聘方回复：{observation.hr_reply_text}；正在发送简历", url=page.url)
                break

            # Recheck the actual workspace immediately before exporting and again
            # before the click. Waiting/MFA/resume changes never retain old authority.
            payload = self._authorized_resume_payload()
            binding = await self.adapter.bind_resume(page, payload,
                applicant_id=self.state.mock_applicant_id or self.jobs.mock_workflow_settings().get("applicant_id"),
                resume_id=self.state.mock_resume_id)
            self.state.mock_resume_id, self.state.mock_applicant_id = binding.resume_id, binding.applicant_id
            self._update(DeliveryTaskStatus.RUNNING, "已同步并绑定授权主简历，正在核对发送条件", checkpoint="resume_bound")
            self._authorized_resume_payload()
            observation = await self.adapter.inspect(page)
            if not observation.application_submitted and not observation.resume_ready:
                raise RuntimeError("授权主简历已同步，但发送按钮尚不可用；请核对招聘方回复和页面状态")
            submit_started = True
            self._update(
                DeliveryTaskStatus.RUNNING,
                "即将执行已授权的简历提交动作",
                url=page.url,
                checkpoint="submit_started",
            )
            result = await self.adapter.submit(page, binding=binding, before_click=self._authorized_resume_payload)
            if result.confirmed:
                if result.await_reply:
                    detail = "模拟网站已确认直接投递；后台将持续监测招聘方回复"
                    self.jobs.mark_delivery_completed(job_id, detail)
                    self._update(DeliveryTaskStatus.WAITING_FOR_REPLY, detail, url=page.url, submitted=True, checkpoint="submission_confirmed")
                    return
                detail = "模拟网站已确认简历发送" + ("（检测到重复投递）" if result.duplicate else "")
                self.jobs.mark_delivery_completed(job_id, detail)
                self._update(DeliveryTaskStatus.COMPLETED, detail, url=page.url, submitted=True, checkpoint="submission_confirmed")
                return
            self.jobs.mark_outcome_indeterminate(job_id, result.detail)
            self._update(DeliveryTaskStatus.OUTCOME_INDETERMINATE, result.detail, url=page.url, checkpoint="submission_unconfirmed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            detail = str(exc)[:500] if isinstance(exc, (ValueError, RuntimeError)) else (
                f"浏览器操作失败（{type(exc).__name__}）；请核对模拟站与运行记录，不会自动重试")
            if submit_started:
                self.jobs.mark_outcome_indeterminate(job_id, detail)
                self._update(DeliveryTaskStatus.OUTCOME_INDETERMINATE, detail, checkpoint="submission_unconfirmed")
            elif self.state.submitted:
                self._update(DeliveryTaskStatus.COMPLETED, f"已确认的投递保留；回复监测停止：{detail}", checkpoint="reply_monitor_stopped")
            else:
                # Nothing was clicked, so the resume was certainly not sent and a
                # retry cannot duplicate a submission.
                self._retry_or_fail(job_id, detail)
        finally:
            lock.release()
