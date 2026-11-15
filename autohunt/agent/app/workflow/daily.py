"""Durable daily work, sharing the ordinary run ledger and browser safeguards.

The schedule cursor is advanced only after an idempotent run has been persisted.
One pending occurrence represents a missed interval; external effects have their
own checkpoints and are reconciled before any restart may send again.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta, timezone

from ..core.models import BrowserMode, BrowserSearchRequest, BrowserTaskStatus
from ..memory.resume_workspace import resume_document_digest
from ..runtime.context import RunContext, current_run, diagnostic_scope
from ..runtime.store import TERMINAL
from ..runtime.resilience import guarded_operation

BEIJING = timezone(timedelta(hours=8))


class DailyWorkflow:
    def __init__(self, jobs, runs, browser, delivery, candidate, resume, *, clock=None):
        self.jobs, self.runs, self.browser, self.delivery = jobs, runs, browser, delivery
        self.candidate, self.resume_workspace = candidate, resume
        self.clock = clock or (lambda: datetime.now(UTC))
        self.worker = None
        self.active_id = None
        self.lock = asyncio.Lock()
        self.stopping = False

    def records(self, *, active=False):
        return [r for r in self.runs.list("default", active=active, limit=None) if r["kind"] == "daily"]

    def info(self):
        rows = self.records()
        latest = rows[0] if rows else None
        return {"daily_pending": any(r["status"] not in TERMINAL for r in rows),
                "daily_run_id": latest["id"] if latest else None,
                "daily_status": latest["status"] if latest else None,
                "daily_trigger": latest["payload"]["trigger"] if latest else None}

    def owns_job(self, job_id):
        # The polling synchronizer must not mint a second submission capability
        # for work already governed by a daily run's frozen policy.
        return any(str(job_id) in (r.get("checkpoint") or {}).get("jobs", {}) for r in self.records())

    async def recover(self):
        self.stopping = False
        for run in self.records(active=True):
            if run["cancel_requested"]:
                await self.cancel(run["id"])
                continue
            cp = run["checkpoint"] or {}
            cp.pop("next_check_at", None)
            self.runs.update(run["id"], status="queued", checkpoint=cp)
            self.runs.event(run["id"], "daily_recovered", {"detail": "服务恢复；接续只读步骤并核对外部回执，未重放发送"})

    async def shutdown(self):
        self.stopping = True
        if self.worker and not self.worker.done():
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)

    async def resume(self, run_id):
        run = self.runs.get(run_id)
        if run["status"] in TERMINAL or run["cancel_requested"]:
            raise ValueError("该每日任务已结束，不能重放；请查看回执")
        if self.active_id == run_id and self.worker and not self.worker.done():
            await self.browser.resume()
        else:
            cp = run["checkpoint"] or {}
            cp.pop("next_check_at", None)
            self.save(run_id, cp, status="queued")
            await self.tick()
        return self.runs.get(run_id)

    async def cancel(self, run_id):
        run = self.runs.get(run_id)
        if run["kind"] != "daily" or run["status"] in TERMINAL:
            return run
        self.runs.update(run_id, cancel_requested=1)
        if self.active_id == run_id and self.worker and not self.worker.done():
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        run = self.runs.get(run_id)
        if (run["checkpoint"] or {}).get("browser_id") == self.browser.current().id:
            await self.browser.stop()
        uncertain = any(j.get("phase") in {"contact_started", "submit_started", "uncertain"}
                        for j in (run["checkpoint"] or {}).get("jobs", {}).values())
        self.finish(run_id, "outcome_indeterminate" if uncertain else "cancelled", "用户停止每日任务；已发送内容保留")
        return self.runs.get(run_id)

    def enqueue(self, request_id: str):
        if not self.jobs.mock_workflow_settings()["monitor_enabled"]:
            raise ValueError("请先开启模拟站会话监测并保存设置")
        # Repeated HTTP requests with the same id return the same durable run.
        run, _ = self.runs.create("default", "daily-test:" + request_id, "daily", {"trigger": "manual"})
        return run

    def materialize_due(self):
        settings = self.jobs.mock_workflow_settings()
        if not settings["daily_enabled"]:
            return
        now = self.clock().astimezone(BEIJING)
        hour, minute = map(int, settings["daily_time"].split(":"))
        cursor = settings.get("daily_next_due_at")
        if cursor is None:
            # Old installations have no cursor; never backfill before opt-in.
            day = (datetime.fromisoformat(settings["last_run_date"]).date() + timedelta(days=1)
                   if settings.get("last_run_date") else now.date())
            due = datetime.combine(day, datetime.min.time(), BEIJING).replace(hour=hour, minute=minute)
            if not settings.get("last_run_date") and due <= now:
                due += timedelta(days=1)
            self.jobs.advance_daily_schedule(None, due.astimezone(UTC).isoformat())
            return
        due = datetime.fromisoformat(cursor).astimezone(BEIJING)
        if now < due:
            return
        latest = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if latest > now:
            latest -= timedelta(days=1)
        missed = max(0, (latest.date() - due.date()).days)
        scheduled = latest.astimezone(UTC).isoformat()
        key = "daily:" + scheduled
        if not any(r["request_key"] == key for r in self.records()):
            self.runs.create("default", key, "daily",
                             {"trigger": "catch_up" if missed or now - latest > timedelta(minutes=5) else "scheduled",
                              "scheduled_for": scheduled, "missed_from": cursor, "coalesced_occurrences": missed + 1})
        self.jobs.advance_daily_schedule(cursor, (latest + timedelta(days=1)).astimezone(UTC).isoformat(),
                                         run_date=latest.date().isoformat())

    async def tick(self):
        async with self.lock:
            if self.stopping:
                return False
            self.materialize_due()
            if self.worker and not self.worker.done():
                return False
            if any(s.worker and not s.worker.done() for s in (self.browser, self.delivery)):
                return False
            now = self.clock().timestamp()
            ready = [r for r in self.records(active=True)
                     if (r["checkpoint"] or {}).get("next_check_at", 0) <= now]
            if not ready:
                return False
            run = min(ready, key=lambda r: (r["checkpoint"] or {}).get("next_check_at", 0))
            self.active_id = run["id"]
            self.worker = asyncio.create_task(self.execute(run["id"]), name="daily-" + run["id"])
            return True

    def save(self, run_id, cp, **fields):
        return self.runs.update(run_id, checkpoint=cp, **fields)

    def finish(self, run_id, status, detail):
        run = self.runs.get(run_id)
        if run["status"] in TERMINAL:
            return
        result = {"message": detail, "jobs": {k: v.get("phase") for k, v in (run["checkpoint"] or {}).get("jobs", {}).items()}}
        # Idempotent conversation key covers a crash between summary and finish.
        self.jobs.add_conversation_message("assistant", detail, conversation_id="default", run_id="daily:" + run_id)
        self.runs.event(run_id, "workflow_summary", {"message": detail})
        self.runs.update(run_id, status=status, result=result, error=detail if status not in {"completed", "cancelled"} else None)
        self.runs.event(run_id, "status", {"status": status, "detail": detail})

    def snapshot(self):
        settings = self.jobs.mock_workflow_settings()
        state = self.candidate.read()
        roles = (state.preferences.target_roles.values if state.preferences.target_roles.source == "user_confirmed"
                 else state.profile.role_suggestions if state.profile.resume_id else state.preferences.search_keywords.values)
        queries = [str(x).strip() for x in roles if str(x).strip()][:3]
        if not queries or not state.profile.resume_id:
            raise ValueError("请配置目标岗位并启用主简历，再执行每日任务")
        doc = self.resume_workspace.read_document()
        return {"request": BrowserSearchRequest(source="mock", query=queries[0], queries=queries,
                    locations=state.preferences.locations.values[:3], browser_mode=BrowserMode.CDP, max_jobs=30).model_dump(mode="json"),
                "binding": {"resume_document_id": doc.id, "resume_version": doc.version, "resume_digest": resume_document_digest(doc)},
                "auto_submit_epoch": settings["auto_submit_enabled_at"] if settings["auto_submit"] else None,
                "applicant_id": settings.get("applicant_id"), "min_score": settings["min_score"], "max_contacts": settings["max_contacts"]}

    async def execute(self, run_id):
        run = self.runs.get(run_id)
        cp = run["checkpoint"] or {}
        token = current_run.set(RunContext(run_id, asyncio.current_task(),
            lambda kind, data: self.runs.event(run_id, kind, data), lambda value: None,
            session_id="default", diagnose=lambda kind, data: self.runs.event(run_id, kind, data)))
        watcher = asyncio.create_task(self.observe(run_id))
        try:
            if run["cancel_requested"]:
                self.finish(run_id, "cancelled", "每日任务已取消")
                return
            if "snapshot" not in cp:
                cp = {"phase": "daily", "snapshot": self.snapshot(), "browser_id": uuid.uuid4().hex,
                      "jobs": {}, "attempts": 0, "daily_progress": {"id": run_id, "revision": 1, "steps": []}}
            cp["attempts"] += 1
            self.save(run_id, cp, status="running", error=None, error_type=None)
            if not cp.get("search_complete"):
                await self.search(run_id, cp)
            if "selected" not in cp:
                report = self.jobs.search_report(cp["browser_id"])
                candidates = [j for j in report["jobs"] if j["score"] >= cp["snapshot"]["min_score"]
                              and j["stage"] == "review" and not self.owns_job(j["id"])]
                cp["selected"] = [j["id"] for j in candidates[:cp["snapshot"]["max_contacts"]]]
                cp["jobs"] = {str(i): {"phase": "pending"} for i in cp["selected"]}
                self.save(run_id, cp)
            from .daily_delivery import deliver
            # Own the existing browser service while delivery uses its session,
            # so pause/resume, single-browser exclusion and stop remain shared.
            self.browser.current_id = cp["browser_id"]
            self.browser.worker = asyncio.current_task()
            def progress():
                view = self.browser.current()
                return (view.status, view.checkpoint, tuple(j["phase"] for j in cp["jobs"].values())), view.status in {"paused", "waiting_for_user"}
            await guarded_operation(deliver(self, run_id, cp), progress, limits=self.browser.watch_limits)
            cp["failures"] = 0
            phases = [j["phase"] for j in cp["jobs"].values()]
            if "waiting_reply" in phases:
                cp["next_check_at"] = self.clock().timestamp() + 30
                self.save(run_id, cp, status="waiting_reply")
                self.runs.event(run_id, "status", {"status": "waiting_reply", "detail": "已沟通，等待招聘方回复；尚未投递的岗位将继续检查"})
            else:
                status = "outcome_indeterminate" if "uncertain" in phases else "failed" if "failed" in phases else "completed"
                self.finish(run_id, status, f"每日模拟站任务：搜索完成，处理 {len(phases)} 个岗位，"
                    f"投递 {phases.count('submitted')} 个，待人工确认 {phases.count('withheld')} 个，"
                    f"失败 {phases.count('failed')} 个，结果待核验 {phases.count('uncertain')} 个。")
        except asyncio.CancelledError:
            # Shutdown keeps the checkpoint for restart; user cancellation is
            # persisted by cancel() and never turned into a runnable retry.
            if not self.runs.get(run_id)["cancel_requested"]:
                self.save(run_id, cp, status="queued")
            raise
        except Exception as exc:
            cp["attempts"] = cp.get("attempts", 0) or 1
            cp["failures"] = cp.get("failures", 0) + 1
            detail = f"{type(exc).__name__}: {str(exc)[:250]}"
            self.runs.event(run_id, "daily_error", {"detail": detail, "attempt": cp["attempts"]})
            if "snapshot" in cp and cp["failures"] < 3:
                cp["next_check_at"] = self.clock().timestamp() + 30
                self.save(run_id, cp, status="queued", error=detail, error_type=type(exc).__name__)
            else:
                self.save(run_id, cp)
                self.finish(run_id, "failed", ("每日任务恢复预算用尽：" if "snapshot" in cp else "每日任务配置不完整：") + detail)
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            if self.browser.worker is asyncio.current_task():
                self.browser.worker = None
            current_run.reset(token)

    async def observe(self, run_id):
        previous = None
        while True:
            run = self.runs.get(run_id)
            ident = (run["checkpoint"] or {}).get("browser_id")
            try:
                view = self.jobs.get_task(ident) if ident else None
            except KeyError:
                view = None
            if view:
                signature = (view.status, view.checkpoint, view.detail)
                if signature != previous:
                    self.runs.event(run_id, "browser", {"task": view.model_dump(mode="json")})
                    if view.status in {"waiting_for_user", "paused"}:
                        self.runs.update(run_id, status="waiting_user")
                    elif run["status"] == "waiting_user" and view.status in {"running", "starting"}:
                        self.runs.update(run_id, status="running")
                    previous = signature
            await asyncio.sleep(1)

    async def search(self, run_id, cp):
        request = BrowserSearchRequest.model_validate(cp["snapshot"]["request"])
        ident = cp["browser_id"]
        execution = uuid.uuid4().hex
        scope = diagnostic_scope.set({"plan_id": run_id, "revision": 1, "step_id": "search", "execution_id": execution})
        started = time.monotonic()
        step = {"id": "search", "tool": "daily_search", "status": "running", "execution_id": execution}
        cp["daily_progress"]["steps"] = [step]
        self.save(run_id, cp)
        event = {"tool": "daily_search", "plan_id": run_id, "revision": 1, "step_id": "search", "execution_id": execution}
        self.runs.event(run_id, "tool_started", event)
        try:
            try:
                view = self.jobs.get_task(ident)
            except KeyError:
                view = await self.browser.start(request, task_id=ident)
            else:
                if view.status != BrowserTaskStatus.COMPLETED:
                    if not self.jobs.search_recovery(ident)["request"]:
                        self.jobs.save_search_request(ident, request.model_dump(mode="json"))
                    view = await self.browser.continue_search(ident, request)
            worker = self.browser.worker
            if worker and not worker.done():
                while not worker.done():
                    view = self.jobs.get_task(ident)
                    self.save(run_id, cp, status="waiting_user" if view.status in {"waiting_for_user", "paused"} else "running")
                    await asyncio.wait({worker}, timeout=1)
                await worker
            view = self.jobs.get_task(ident)
            self.runs.event(run_id, "browser", {"task": view.model_dump(mode="json")})
            if view.status != BrowserTaskStatus.COMPLETED:
                raise RuntimeError(view.detail)
            cp["search_complete"] = True
            # Capture the logged-in account before any communication starts.
            cp["snapshot"]["applicant_id"] = cp["snapshot"]["applicant_id"] or self.jobs.mock_workflow_settings().get("applicant_id")
            step["status"] = "completed"
            self.save(run_id, cp, status="running", error=None, error_type=None)
            self.runs.event(run_id, "tool_finished", {**event,
                            "status": "ok", "duration_ms": round((time.monotonic() - started) * 1000), "attempts": cp["attempts"]})
        except BaseException as exc:
            step["status"] = "interrupted" if isinstance(exc, asyncio.CancelledError) else "failed"
            self.save(run_id, cp)
            self.runs.event(run_id, "tool_finished", {**event,
                "status": step["status"], "detail": str(exc)[:250],
                "duration_ms": round((time.monotonic() - started) * 1000)})
            if self.browser.worker and not self.browser.worker.done():
                await self.browser.stop()
            raise
        finally:
            diagnostic_scope.reset(scope)
