from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import httpx

from ..browser.automation import BrowserAutomationService
from ..core.models import BrowserMode, BrowserSearchRequest, BrowserTaskStatus, JobAnalysisRequest
from ..core.models import DeliveryTaskStatus
from ..delivery.automation import DeliveryAutomationService
from ..jobs.analysis import analyze_job
from ..jobs.store import JobStore
from ..memory.candidate_state import CandidateStateStore
from ..memory.resume_workspace import ResumeWorkspaceStore, resume_document_digest
from ..runtime.resilience import PreflightRefused, safe_retry, wait_expired
from .delivery_rules import build_rules
from .rules import RuleEngine


MOCK_ORIGIN = "http://127.0.0.1:8080"
BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")
TERMINAL = {BrowserTaskStatus.COMPLETED, BrowserTaskStatus.FAILED, BrowserTaskStatus.STOPPED}


def _utc_naive(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        return datetime.min


class MockWorkflowCoordinator:
    """Synchronize manual simulator conversations and run opt-in daily searches.

    Read-side polling uses the local simulator API without changing unread state.
    All writes to its application flow still go through the browser adapter and
    a job-bound, one-use delivery preview. No real recruiting site is supported.
    """

    def __init__(self, jobs: JobStore, browser: BrowserAutomationService,
                 delivery: DeliveryAutomationService, candidate: CandidateStateStore,
                 resume: ResumeWorkspaceStore, *, poll_seconds: float = 30) -> None:
        self.jobs = jobs
        self.browser = browser
        self.delivery = delivery
        self.candidate = candidate
        self.resume = resume
        self.poll_seconds = poll_seconds
        self.worker: asyncio.Task | None = None
        self.sync_lock = asyncio.Lock()
        self.queue_lock = asyncio.Lock()
        self.wakeup = asyncio.Event()
        self.on_delivery_started = None
        self.delivery_owner = None
        self.stopping_sessions: set[str] = set()
        self.operations: dict[asyncio.Task, str] = {}
        self.operation_jobs: dict[asyncio.Task, int | None] = {}
        self.poll_failures: dict[str, tuple[int, float]] = {}
        #: Observation -> rule matching -> action -> recorded decision.
        self.engine = RuleEngine(build_rules(self))

    def settings(self) -> dict:
        return self.jobs.mock_workflow_settings()

    async def start(self) -> None:
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self._loop(), name="mock-workflow-monitor")

    async def stop(self) -> None:
        if self.worker and not self.worker.done():
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        self.worker = None

    async def _loop(self) -> None:
        while True:
            settings = self.settings()
            # One observation, then the rules decide. Conditions read facts only, so a
            # cycle can be replayed from the recorded observation, and the reason a
            # cycle did nothing is visible instead of inferred from a branch order.
            try:
                observation = await self.observe(settings)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.jobs.record_mock_workflow_error(f"采集状态失败：{type(exc).__name__}：{exc}")
                observation = None
            if observation is not None:
                busy = bool(self.delivery.worker and not self.delivery.worker.done())
                decision = await self.engine.step(observation, busy=busy)
                if decision and decision.rule:
                    self.jobs.record_automation_decision(
                        rule=decision.rule, result=decision.result,
                        considered=decision.considered, facts=observation.as_dict())
            try:
                self._report_finished_daily_run()
            except Exception as exc:
                self.jobs.record_mock_workflow_error(f"{type(exc).__name__}: {exc}")
            try:
                pending = self.jobs.mock_delivery_queue() or self.jobs.delivery_runs_waiting_reply()
                await asyncio.wait_for(self.wakeup.wait(), timeout=2 if pending else self.poll_seconds)
            except TimeoutError:
                pass
            self.wakeup.clear()

    async def observe(self, settings: dict | None = None):
        """Capture board and site facts for one cycle."""
        from .observations import observe as collect
        settings = self.settings() if settings is None else settings
        observation = await collect(self.jobs, settings)
        facts = dict(observation.facts)
        facts["delivery_idle"] = not (self.delivery.worker and not self.delivery.worker.done())
        facts["browser_idle"] = not (self.browser.worker and not self.browser.worker.done())
        return type(observation)(at=observation.at, facts=facts)

    async def _ensure_applicant_bound(self, session_id: str) -> str:
        """Ensure a mock-site applicant is bound, opening the browser if that is what it takes.

        Binding is a side effect of running a search: the browser opens, the user logs in
        if required, and the logged-in identity is read from the page. Delivery used to
        refuse outright here, which is why no browser ever appeared for a delivery-only
        turn — the pre-flight aborted before any browser work could begin. The refusal was
        accurate but unhelpful: it asked the user to do something the agent can initiate.

        Returns "bound" when an account is already known, or "searching" when a search was
        launched (or is already running) and binding will follow from its login check.
        """
        if self.settings().get("applicant_id"):
            return "bound"
        running = self.browser.current().status in {
            BrowserTaskStatus.STARTING, BrowserTaskStatus.RUNNING, BrowserTaskStatus.WAITING_FOR_USER}
        if running:
            return "searching"
        if self.browser.can_start_search():
            conditions = self._saved_search_request(session_id)
            if conditions is not None:
                await self.browser.start(conditions)
                return "searching"
        raise PreflightRefused("请先在模拟站登录求职者账号并完成一次搜索，以绑定账号")

    def _saved_search_request(self, session_id: str):
        """The session's saved search state as a browser request, when it is usable."""
        state = getattr(self, "search_state_reader", None)
        saved = state(session_id) if callable(state) else None
        if not isinstance(saved, dict) or not saved.get("queries"):
            return None
        query = str(saved["queries"][0] or "").strip()
        if not query:
            return None
        from ..core.models import BrowserSearchRequest
        try:
            return BrowserSearchRequest(
                conversation_id="default", query=query,
                result_limit=saved.get("result_limit"), salary_min=saved.get("salary_min"),
                locations=saved.get("locations") or None, filters=saved.get("filters"))
        except Exception:  # noqa: BLE001 - malformed saved state must not break delivery
            return None

    async def queue_deliveries(self, *, min_score: int, session_id: str, task_id: str | None = None) -> dict:
        if session_id in self.stopping_sessions or "*" in self.stopping_sessions:
            raise PreflightRefused("当前正在停止任务，未加入投递队列")
        """One explicit bounded batch, not a persistent rule for unseen future jobs."""
        from ..growth.requests import resume_stamp
        if not 0 <= min_score <= 100:
            raise PreflightRefused("匹配分数必须在 0–100 之间")
        profile = self.candidate.read().profile
        if not profile.resume_id:
            raise PreflightRefused("请先上传并启用主简历；没有简历不能投递")
        if await self._ensure_applicant_bound(session_id) == "searching":
            return {"queued": 0, "binding": "searching",
                    "message": "尚未绑定求职者账号，已为你启动一次搜索以完成登录与绑定："
                               "请在浏览器中完成登录（如需要），登录成功后账号会自动绑定，"
                               "随后再发起本次投递即可。"}
        document = self.resume.read_document()
        evidence = self.jobs.jobs_for_resume_analysis(profile.resume_id, limit=200, min_score=min_score, task_id=task_id)
        eligible = [self.jobs.get_job(item["id"]) for item in evidence
                    if item["stage"] in {"review", "applying"}
                    and item.get("filter_status", "pass") == "pass"
                    and self.delivery.supports(item["source_url"])
                    and self.jobs.latest_delivery_status_for_job(item["id"]) is None]
        binding = {"resume_document_id": document.id, "resume_version": document.version,
                   "resume_digest": resume_document_digest(document)}
        ids = self.jobs.enqueue_mock_deliveries(eligible, session_id=session_id,
            resume_stamp=resume_stamp(document, self.candidate), binding=binding, min_score=min_score)
        self.wakeup.set()
        return {"queued": len(ids), "job_ids": ids, "min_score": min_score,
                "message": f"已将 {len(ids)} 个匹配分 ≥{min_score} 的模拟站职位加入本次自动投递队列（不是已投递）。"
                "先沟通，收到招聘方回复且投递按钮解锁后才发送已授权简历；验证需人工接管，失败或不确定不会重试。"
                "仅处理当前简历的已搜索职位，不启用每日任务，也不自动添加未来职位。可说「停止自动投递」。"}

    async def _operation(self, session_id: str, factory, *, job_id: int | None = None, queued: bool = True):
        if session_id in self.stopping_sessions or "*" in self.stopping_sessions:
            return None
        if job_id is not None and queued:
            item = next((row for row in self.jobs.mock_delivery_queue(active_only=False)
                         if row["job_id"] == job_id), None)
            if not item or item["status"] == "cancelled":
                return None
        task = asyncio.create_task(factory(), name=f"mock-delivery-operation-{session_id}")
        self.operations[task] = session_id
        self.operation_jobs[task] = job_id
        try:
            return await task
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
            return None
        finally:
            self.operations.pop(task, None)
            self.operation_jobs.pop(task, None)

    async def cancel_delivery_queue(self, session_id: str | None) -> dict:
        key = session_id if session_id is not None else "*"
        self.stopping_sessions.add(key)
        try:
            # Revoke before yielding: stale observations and restart cannot replay.
            items = [row for row in self.jobs.mock_delivery_queue(session_id=session_id, active_only=False)
                     if row["status"] in {"queued", "dispatched", "blocked"}]
            for item in items:
                self.jobs.update_mock_delivery(item["job_id"], "cancelled", "用户停止自动投递；已发送内容保留")
            operations = [task for task, owner in self.operations.items()
                          if session_id is None or owner == session_id]
            operation_jobs = {self.operation_jobs.get(task) for task in operations}
            for task in operations:
                task.cancel()
            if operations:
                await asyncio.gather(*operations, return_exceptions=True)
            current = self.delivery.current()
            if (any((item["run_id"] and item["run_id"] == current.id)
                    or (not item["run_id"] and item["job_id"] == current.job_id) for item in items)
                    or current.job_id is not None and current.job_id in operation_jobs):
                await self.delivery.stop()
            for item in items:
                if item["run_id"]:
                    run = self.jobs.get_delivery_run(item["run_id"])
                    if run.status not in {DeliveryTaskStatus.COMPLETED, DeliveryTaskStatus.FAILED,
                                          DeliveryTaskStatus.OUTCOME_INDETERMINATE, DeliveryTaskStatus.CANCELLED} and not run.submitted:
                        self.jobs.update_delivery_run(run.id, status=DeliveryTaskStatus.CANCELLED, detail="用户停止本会话自动投递，已发送沟通保留")
            return {"cancelled_queue": len(items), "message": f"已停止 {len(items)} 项自动投递；已经发送的消息和简历保留。"}
        finally:
            self.stopping_sessions.discard(key)

    async def recover_failed_starts(self) -> dict:
        """Re-run starts that failed before any click, within a bounded budget.

        A transient browser error used to park a job at "需要重新授权" even when its
        recruiter had already asked for the resume, so the user had to approve the
        same job again. The run is resumed in place rather than re-dispatched: its
        binding and one-use authorisation were never consumed, and re-dispatching
        would mint a second preview and a second run for the same job.
        """
        recovered, skipped = 0, 0
        for run in self.jobs.delivery_runs_failed_before_submit():
            if ((self.delivery.worker and not self.delivery.worker.done())
                    or (self.browser.worker and not self.browser.worker.done())):
                break
            if int(run.get("attempts") or 0) >= self.delivery.retry_limit:
                skipped += 1
                continue
            if not str(run.get("recruiter_reply") or "").strip():
                skipped += 1          # nothing to send yet; the reply check owns it
                continue
            item = next((entry for entry in self.jobs.mock_delivery_queue(active_only=False)
                         if entry["run_id"] == run["id"]), None)
            if item is None or item["status"] == "cancelled":
                skipped += 1          # not a batch job; a human owns its authorisation
                continue
            # reopen the run so the reply check can pick it up again
            reopened = self.jobs.update_delivery_run(
                run["id"], status=DeliveryTaskStatus.WAITING_FOR_REPLY,
                checkpoint="waiting_for_reply", submitted=0,
                detail="上次提交未能开始（未发出任何内容）；招聘方已回复，正在自动重试，无需重新授权")
            if self.delivery.current().id == run["id"]:
                self.delivery.state = reopened
            try:
                view = await self._operation(item["session_id"],
                    lambda: self.delivery.resume_authorized_run(run["id"], **item["binding"]), job_id=item["job_id"])
                if view is None:
                    continue
                self.jobs.update_mock_delivery(run["job_id"], "dispatched", "已回复，正在自动重试投递", run_id=run["id"])
                self.jobs.mark_job_retrying_delivery(run["job_id"])
                recovered += 1
            except Exception as exc:  # noqa: BLE001 - one job must not stop the rest
                self.jobs.record_mock_workflow_error(
                    f"job={run['job_id']} 自动重试启动失败：{type(exc).__name__}：{exc}")
                skipped += 1
            worker = self.delivery.worker
            if worker is not None:
                await asyncio.gather(worker, return_exceptions=True)
            break
        return {"recovered": recovered, "skipped": skipped}

    async def check_reply_queue(self) -> dict:
        """Reply monitoring that does not hold the delivery worker.

        Dispatch releases the worker as soon as the conversation is open, so the
        rest of the batch proceeds while each opened job is checked here on a
        timer. A job only consumes the worker again when there is something to do:
        a verified reply whose send button is unlocked.
        """
        from ..delivery.automation import wait_expired
        checked = submitted = 0
        for run in self.jobs.delivery_runs_waiting_reply():
            if ((self.delivery.worker and not self.delivery.worker.done())
                    or (self.browser.worker and not self.browser.worker.done())):
                break  # a dispatch or another reply check holds the slot
            item = next((entry for entry in self.jobs.mock_delivery_queue(active_only=False)
                         if entry["run_id"] == run["id"]), None)
            if item is not None and item["status"] not in {"queued", "dispatched"}:
                continue
            # Single-job UI authorizations also release the browser while waiting.
            # A site greeting alone is NOT authority: require an active tracked run
            # plus the original preview/resume binding; never create a new preview.
            owner = item["session_id"] if item else self.delivery_owner(run["id"]) if self.delivery_owner else None
            binding = item["binding"] if item else {key: run.get(key) for key in
                ("resume_document_id", "resume_version", "resume_digest")}
            if (not owner or not run.get("preview_id") or run.get("action") != "submit_resume"
                    or any(value is None for value in binding.values())):
                continue
            if wait_expired(run["started_at"], self.delivery.watch_limits.reply):
                continue  # expiry is owned by the run's own watchdog
            try:
                view = await self._operation(owner,
                    lambda: self.delivery.resume_authorized_run(run["id"], **binding),
                    job_id=run["job_id"], queued=item is not None)
                if view is None:
                    continue
                checked += 1
                if view.submitted:
                    submitted += 1
                    if item:
                        self._finish_queue_item(item, "completed", "已核实简历投递完成")
            except Exception as exc:  # noqa: BLE001 - one job must not stop the rest
                self.jobs.record_mock_workflow_error(f"check_reply job={run['job_id']}: {type(exc).__name__}：{exc}")
            # Wait for the one-shot check to finish before touching the next job.
            worker = self.delivery.worker
            if worker is not None:
                await asyncio.gather(worker, return_exceptions=True)
        return {"checked": checked, "submitted": submitted}

    async def process_delivery_queue(self) -> None:
        from ..growth.requests import resume_stamp
        async with self.queue_lock:
            if (self.delivery.worker and not self.delivery.worker.done()) or (self.browser.worker and not self.browser.worker.done()):
                return
            stamp = resume_stamp(self.resume.read_document(), self.candidate)
            for item in self.jobs.mock_delivery_queue():
                job = self.jobs.get_job(item["job_id"])
                if (item["resume_stamp"] != stamp or job["score"] < item["min_score"]
                        or job.get("filter_status", "pass") != "pass"
                        or self.jobs.review_fingerprint(job) != item["job_hash"]
                        or not self.delivery.supports(job["source_url"])):
                    self._finish_queue_item(item, "stale", "简历、职位或分数已变化，旧授权失效，请重新核对")
                    continue
                if item["status"] == "queued":
                    if job["stage"] not in {"review", "applying"} or self.jobs.latest_delivery_status_for_job(job["id"]) is not None:
                        self._finish_queue_item(item, "blocked", "职位已处理或已有投递记录，不能重复投递")
                        continue
                    if not self.jobs.claim_mock_delivery(job["id"]):
                        continue
                    try:
                        preview = self.jobs.create_delivery_preview(job["id"], resume_label="候选人主简历", **item["binding"])
                        result = await self._operation(item["session_id"],
                            lambda: self.delivery.authorize_and_start(preview.id, monitor_reply=False, **item["binding"]), job_id=job["id"])
                        if result is None:
                            return
                        self.jobs.update_mock_delivery(job["id"], "dispatched", "已启动浏览器沟通/投递", run_id=result[3].id)
                        if self.on_delivery_started:
                            self.on_delivery_started(result[3].id, item["session_id"])
                    except Exception as exc:
                        self._finish_queue_item(item, "blocked", f"启动失败，不会自动重试：{exc}")
                    return
                if not item["run_id"]:
                    self._finish_queue_item(item, "blocked", "启动结果没有可靠记录，禁止自动重试")
                    continue
                run = self.jobs.get_delivery_run(item["run_id"])
                if run.submitted or run.status == DeliveryTaskStatus.COMPLETED:
                    if run.status == DeliveryTaskStatus.WAITING_FOR_REPLY:
                        self.jobs.update_delivery_run(run.id, status=DeliveryTaskStatus.COMPLETED, detail="本次自动投递已核实完成")
                    self._finish_queue_item(item, "completed", "已核实简历投递完成")
                    continue
                if run.status == DeliveryTaskStatus.WAITING_FOR_REPLY:
                    if wait_expired(run.started_at, self.delivery.watch_limits.reply):
                        expired = self.jobs.update_delivery_run(run.id, status=DeliveryTaskStatus.FAILED,
                            checkpoint="reply_timeout", detail="回复等待过期，未自动重试投递")
                        if self.delivery.current().id == run.id:
                            self.delivery.state = expired
                        self._finish_queue_item(item, "blocked", "回复等待过期，请重新核对授权")
                        continue
                    self.jobs.update_mock_delivery(job["id"], "dispatched", "等待回复；正在检查", run_id=run.id)
                    await self._operation(item["session_id"],
                        lambda: self.delivery.resume_authorized_run(run.id, **item["binding"]), job_id=job["id"])
                    return
                if run.status == DeliveryTaskStatus.WAITING_FOR_USER:
                    if wait_expired(run.updated_at, self.delivery.watch_limits.human) and (not self.delivery.worker or self.delivery.worker.done()):
                        expired = self.jobs.update_delivery_run(run.id, status=DeliveryTaskStatus.FAILED,
                            checkpoint="human_timeout", detail="人工接管等待过期，未绕过验证")
                        if self.delivery.current().id == run.id:
                            self.delivery.state = expired
                        self._finish_queue_item(item, "blocked", "人工接管等待过期，请重新核对")
                        continue
                    return  # Dedicated UI takeover, never bypass verification.
                self._finish_queue_item(item, "blocked", f"任务状态 {run.status}，需人工核验，不会重试")

    def _finish_queue_item(self, item: dict, status: str, detail: str) -> None:
        current = next((row for row in self.jobs.mock_delivery_queue(active_only=False)
                        if row["job_id"] == item["job_id"]), None)
        if current and current["status"] == "cancelled":
            return
        self.jobs.update_mock_delivery(item["job_id"], status, detail)
        if status in {"stale", "blocked"} and item["run_id"]:
            run = self.jobs.get_delivery_run(item["run_id"])
            if run.status == DeliveryTaskStatus.WAITING_FOR_REPLY and not run.submitted:
                self.jobs.update_delivery_run(run.id, status=DeliveryTaskStatus.CANCELLED, detail=detail)
        self.jobs.add_conversation_message("assistant", f"模拟站自动投递 · 职位 #{item['job_id']}：{detail}。",
                                           conversation_id=item["session_id"])

    async def sync_once(self) -> dict[str, int]:
        async with self.sync_lock:
            settings = self.settings()
            applicant_id = settings["applicant_id"]
            if not applicant_id:
                raise RuntimeError("请先在模拟站登录求职者账号并完成一次搜索，以绑定会话")
            headers = {"X-User-Id": str(applicant_id), "X-User-Role": "applicant"}
            counters = {"conversations": 0, "imported": 0, "awaiting_reply": 0, "replied": 0, "submitted": 0, "delivery_started": 0}
            async with httpx.AsyncClient(base_url=MOCK_ORIGIN, timeout=8, trust_env=False) as client:
                conversations = await self._get(client, "/api/messages/conversations", headers)
                applications = await self._get(client, "/api/applications/mine", headers)
                submitted_job_ids = {int(item["job_id"]) for item in applications if item.get("job_id")}
                for conversation in conversations:
                    external_id = conversation.get("job_id")
                    employer_id = conversation.get("user_id")
                    if not isinstance(external_id, int) or not isinstance(employer_id, int):
                        continue
                    messages = await self._get(
                        client, "/api/messages/peek", headers,
                        params={"job_id": external_id, "with_user_id": employer_id, "limit": 100},
                    )
                    outbound = [message for message in messages if message.get("sender_id") == applicant_id
                                and message.get("sender_role") == "applicant"]
                    if not outbound:
                        continue
                    counters["conversations"] += 1
                    job_id = self.jobs.job_id_for_source_url(f"{MOCK_ORIGIN}/job.html?id={external_id}")
                    if job_id is None:
                        remote_job = await self._get(client, f"/api/jobs/{external_id}", headers)
                        job_id, inserted = self._import_contacted_job(external_id, remote_job)
                        counters["imported"] += int(inserted)
                    job = self.jobs.get_job(job_id)
                    if job["stage"] in {"interview", "outcome_indeterminate", "rejected"}:
                        continue
                    first_outbound_time = min(message.get("created_at", "") for message in outbound)
                    replies = [message for message in messages if message.get("sender_id") == employer_id
                               and message.get("sender_role") == "employer"
                               and message.get("created_at", "") >= first_outbound_time
                               and str(message.get("content") or "").strip()]
                    reply = replies[-1]["content"] if replies else None
                    if external_id in submitted_job_ids:
                        if reply and reply != job.get("recruiter_reply"):
                            self.jobs.mark_recruiter_reply(job_id, reply)
                        if job["stage"] != "waiting":
                            self.jobs.mark_delivery_completed(job_id, "模拟站已存在投递记录；已同步到看板")
                        counters["submitted"] += 1
                        continue
                    delivery_status = self.jobs.latest_delivery_status_for_job(job_id)
                    if delivery_status in {"failed", "outcome_indeterminate"}:
                        # A failed/ambiguous browser action needs fresh human review and
                        # must not re-enter the auto-submit path. The recruiter's reply is
                        # still a fact from the site, so it is recorded: otherwise the
                        # conversation shows nothing to the applicant and the message is
                        # silently dropped.
                        if reply and reply != job.get("recruiter_reply"):
                            self.jobs.mark_recruiter_reply(job_id, reply)
                            counters["replied"] += 1
                        continue
                    if job["stage"] == "review":
                        self.jobs.mark_delivery_waiting_reply(
                            job_id, "求职者已在模拟站手动发起沟通，等待招聘方回复", mode="人工沟通·等待回复",
                        )
                    if reply:
                        if reply != job.get("recruiter_reply"):
                            self.jobs.mark_recruiter_reply(job_id, reply)
                        counters["replied"] += 1
                        authorized_after_reply = bool(
                            settings["auto_submit"] and job["score"] >= settings["min_score"] and settings["auto_submit_enabled_at"]
                            and _utc_naive(replies[-1].get("created_at")) >= _utc_naive(settings["auto_submit_enabled_at"])
                        )
                        if authorized_after_reply and await self._start_authorized_delivery(job_id):
                            counters["delivery_started"] += 1
                    else:
                        counters["awaiting_reply"] += 1
            return counters

    @staticmethod
    async def _get(client: httpx.AsyncClient, path: str, headers: dict, params: dict | None = None):
        async def read():
            response = await client.get(path, headers=headers, params=params)
            response.raise_for_status()
            return response.json()
        return await safe_retry(read, retry_safe=True)

    def _import_contacted_job(self, external_id: int, remote: dict) -> tuple[int, bool]:
        description = "\n".join([
            str(remote.get("description") or ""),
            *[str(item) for item in remote.get("requirements") or []],
        ]).strip() or "模拟网站职位详情未注明"
        state = self.candidate.read()
        analysis = analyze_job(JobAnalysisRequest(
            title=str(remote.get("title") or "模拟职位"),
            company=str(remote.get("company_name") or "模拟招聘方"),
            description=description,
            candidate_skills=state.profile.skills,
            target_roles=state.preferences.target_roles.values,
        )) if len(description) >= 30 else None
        return self.jobs.upsert_job({
            "source": "MockJobSite",
            "source_url": f"{MOCK_ORIGIN}/job.html?id={external_id}",
            "external_id": str(external_id),
            "title": str(remote.get("title") or "模拟职位"),
            "company": str(remote.get("company_name") or "模拟招聘方"),
            "location": str(remote.get("city") or "未注明"),
            "salary": f"{remote.get('salary_min', '?')}-{remote.get('salary_max', '?')}K",
            "description": description,
            "tags": [str(value) for value in remote.get("skills") or []],
            "score": analysis.score if analysis else 0,
            "decision": "REVIEW",
            "reasoning": "已在模拟站手动发起沟通；" + (analysis.reasoning if analysis else "职位说明过短，需人工核对"),
            "stage": "review",
        })

    async def _start_authorized_delivery(self, job_id: int) -> bool:
        result = await self._operation("default", lambda: self._start_monitor_delivery(job_id), job_id=job_id, queued=False)
        return bool(result)

    async def _start_monitor_delivery(self, job_id: int) -> bool:
        if not self.settings()["auto_submit"]:
            return False
        if self.jobs.latest_delivery_status_for_job(job_id) is not None:
            return False
        if self.delivery.worker and not self.delivery.worker.done():
            return False
        if self.browser.worker and not self.browser.worker.done():
            return False
        if not self.candidate.read().profile.resume_id:
            self.jobs.record_mock_workflow_error("未上传主简历；不会自动投递")
            return False
        document = self.resume.read_document()
        binding = {
            "resume_document_id": document.id,
            "resume_version": document.version,
            "resume_digest": resume_document_digest(document),
        }
        preview = self.jobs.create_delivery_preview(
            job_id, resume_label=document.source_filename or "候选人主简历", **binding,
        )
        result = await self.delivery.authorize_and_start(preview.id, **binding)
        if self.on_delivery_started:
            self.on_delivery_started(result[3].id, "default")
        self.jobs.event(f"job:{job_id}", "mock_monitor_delivery_authorized", preview.payload_digest)
        return True

    async def run_daily_if_due(self) -> bool:
        return bool(await self._operation("default", self._run_daily_if_due))

    async def _run_daily_if_due(self) -> bool:
        settings = self.settings()
        if not settings["daily_enabled"]:
            return False
        now = datetime.now(BEIJING)
        day = now.date().isoformat()
        if now.strftime("%H:%M") < settings["daily_time"] or settings["last_run_date"] == day:
            return False
        if self.browser.worker and not self.browser.worker.done():
            return False
        if self.delivery.worker and not self.delivery.worker.done():
            return False
        state = self.candidate.read()
        roles = (state.preferences.target_roles.values if state.preferences.target_roles.source == "user_confirmed"
                 else state.profile.role_suggestions if state.profile.resume_id else state.preferences.search_keywords.values)
        queries = [str(role).strip() for role in roles if str(role).strip()][:3]
        if not queries:
            self.jobs.record_mock_workflow_error("未配置目标岗位或有效简历；每日搜索未启动")
            return False
        if not self.jobs.claim_daily_mock_run(day):
            return False
        try:
            task = await self.browser.start(BrowserSearchRequest(
                source="mock", query=queries[0], queries=queries,
                locations=state.preferences.locations.values[:3],
                browser_mode=BrowserMode.CDP, max_jobs=30,
                dry_run=True, dry_run_min_score=settings["min_score"],
                dry_run_max_contacts=settings["max_contacts"],
                dry_run_simulate_reply=False,
            ))
            self.jobs.record_daily_mock_run(task.id)
            return True
        except Exception:
            self.jobs.release_daily_mock_run(day)
            raise

    def _report_finished_daily_run(self) -> None:
        settings = self.settings()
        task_id = settings["last_run_id"]
        if not task_id or task_id == settings["last_reported_run_id"]:
            return
        task = self.jobs.get_task(task_id)
        if task.status not in TERMINAL:
            return
        self.jobs.add_conversation_message(
            "assistant",
            f"每日模拟站任务：{task.status.value}。发现 {task.discovered} 个职位，"
            f"新增 {task.stored} 个，发起沟通 {task.dry_run_contacted} 个，"
            f"已投递 {task.dry_run_submitted} 个，等待招聘方回复 {task.dry_run_waiting_reply} 个。"
            + (f"\n{task.dry_run_report}" if task.dry_run_report else ""),
        )
        self.jobs.record_daily_mock_report(task_id)
