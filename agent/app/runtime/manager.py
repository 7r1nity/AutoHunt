from __future__ import annotations

import asyncio
import copy
import json
import time
from typing import AsyncIterator

from .context import RunContext, current_run
from .search_state import prepare_search_state
from .store import RunStore, TERMINAL
from .control import stop_scope
from ..core.models import BrowserSearchRequest
from .resilience import WatchLimits, wait_expired


class AgentRuntime:
    """Durable runs, per-session ordering, bounded concurrency and one deadline."""
    def __init__(self, store: RunStore, agent, jobs, browser, plans, resume_workspace,
                 *, queue_timeout=120, chat_timeout=55, plan_timeout=105):
        self.store, self.agent, self.jobs = store, agent, jobs
        self.browser, self.plans, self.resume_workspace = browser, plans, resume_workspace
        self.tasks: dict[str, asyncio.Task] = {}
        self.cancellations: dict[str, asyncio.Task] = {}
        self.sessions: dict[str, asyncio.Lock] = {}
        self.slots = asyncio.Semaphore(2)
        self.queue_timeout, self.chat_timeout, self.plan_timeout = queue_timeout, chat_timeout, plan_timeout
        self.delivery = None
        self.stop_jobs: dict[str, asyncio.Task] = {}
        self.stop_lock = asyncio.Lock()
        self.watch_limits = WatchLimits.configured()

    async def start(self):
        self.sessions.clear()
        self.slots = asyncio.Semaphore(2)
        # Re-enqueue only requests which never began. Side effects are not replayed.
        for run in reversed(self.store.list(active=True)):
            if run["cancel_requested"]:
                self.finish(run["id"], "outcome_indeterminate" if run["mutation_started"] else "cancelled", "取消请求已恢复")
            elif run["status"] == "queued":
                self.schedule(run["id"])
            elif run["result"] is not None or (run["checkpoint"] or {}).get("phase") == "action_plan" or (run["checkpoint"] or {}).get("browser_id") or (run["checkpoint"] or {}).get("delivery_id") or run["status"] == "waiting_confirmation":
                if run["kind"] == "chat" and run["result"]:
                    self.jobs.add_conversation_message("assistant", run["result"].get("message", ""), response=run["result"],
                                                       conversation_id=run["session_id"], run_id=run["id"])
                self.schedule(run["id"], watch_only=True)
            else:
                self.finish(run["id"], "outcome_indeterminate" if run["mutation_started"] else "interrupted",
                            "服务重启；已保存步骤，请核对后恢复")

    async def shutdown(self):
        # Persist before cancelling workers; distinguish service loss from user stop.
        tasks = list(self.tasks.items())
        for run_id, task in tasks:
            run = self.store.get(run_id)
            if run["status"] not in TERMINAL and not (run["checkpoint"] or {}).get("browser_id") and not (run["checkpoint"] or {}).get("delivery_id") and run["status"] != "waiting_confirmation":
                self.finish(run_id, "outcome_indeterminate" if run["mutation_started"] else "interrupted", "服务停止，检查点已保存")
            task.cancel()
        await asyncio.gather(*(task for _, task in tasks), return_exceptions=True)
        await asyncio.gather(*list(self.cancellations.values()), return_exceptions=True)
        await asyncio.gather(*list(self.stop_jobs.values()), return_exceptions=True)
        self.tasks.clear()

    def schedule(self, run_id: str, *, watch_only=False):
        task = asyncio.create_task(self.execute(run_id, watch_only=watch_only), name=f"agent-run-{run_id}")
        self.tasks[run_id] = task
        task.add_done_callback(lambda done: self.tasks.pop(run_id, None))

    def submit(self, session_id: str, request_key: str, kind: str, payload: dict, parent_id=None) -> dict:
        scope = stop_scope(payload.get("message", "")) if kind == "chat" else None
        if not scope and any(not task.done() for key, task in self.stop_jobs.items()
                             if key in {"*", session_id}):
            raise ValueError("当前正在停止任务，请等待停止结果后再发起新任务")
        if kind not in {"chat", "learning_plan", "job_analysis"}:
            raise ValueError("不支持的任务类型")
        run, created = self.store.create(session_id, request_key, kind, payload, parent_id, control=bool(scope))
        if created:
            if scope:
                task = asyncio.create_task(self._stop_command(run, scope), name=f"stop-command-{run['id']}")
                self.tasks[run["id"]] = task
                task.add_done_callback(lambda done: self.tasks.pop(run["id"], None))
            else:
                self.schedule(run["id"])
        return run

    async def _stop_command(self, run: dict, scope: str):
        """Control requests bypass the session/model queue, including busy sessions."""
        self.store.update(run["id"], status="running")
        self.jobs.add_conversation_message("user", run["payload"]["message"],
            conversation_id=run["session_id"], run_id=run["id"])
        try:
            key = "*" if scope == "all" else run["session_id"]
            await self.stop_tasks(None if scope == "all" else run["session_id"], exclude_run=run["id"])
            result = await asyncio.shield(self.stop_jobs[key])
            self.store.update(run["id"], result={"message": result["message"], "lane": "application"})
            self.jobs.add_conversation_message("assistant", result["message"],
                conversation_id=run["session_id"], run_id=run["id"])
            self.store.event(run["id"], "response", {"result": {"message": result["message"], "lane": "application"}})
            self.finish(run["id"], "completed" if result["status"] == "stopped" else "failed", result["message"])
        except Exception as exc:
            self.finish(run["id"], "failed", f"停止未能确认：{type(exc).__name__}；请核对任务状态")

    def track_delivery(self, task_id: str, session_id: str = "default") -> dict:
        run, created = self.store.create(session_id, f"delivery:{task_id}", "delivery", {})
        if created:
            self.store.update(run["id"], status="running", mutation_started=1, checkpoint={"delivery_id": task_id})
            self.store.mutate_dialogue(session_id, {"op": "task", "source": run["id"], "kind": "delivery_id", "id": task_id, "status": "running", "bind": True})
            self.schedule(run["id"], watch_only=True)
        return self.store.get(run["id"])

    def track_browser(self, task_id: str, session_id: str = "default") -> dict:
        run, created = self.store.create(session_id, f"browser:{task_id}", "browser", {})
        if created:
            self.store.update(run["id"], status="running", mutation_started=1, checkpoint={"browser_id": task_id})
            self.store.mutate_dialogue(session_id, {"op": "task", "source": run["id"], "kind": "browser_id", "id": task_id, "status": "running", "bind": True})
            self.schedule(run["id"], watch_only=True)
        return self.store.get(run["id"])

    def finish(self, run_id: str, status: str, detail: str, **fields):
        previous = self.store.get(run_id)
        if previous["status"] in TERMINAL:
            return
        self.store.update(run_id, status=status, error=detail if status != "completed" else None, **fields)
        request = self.store.dialogue_state(previous["session_id"]).get("task_request")
        if request and request.get("run_id") == run_id:
            # Chat response delivery and task execution are different lifecycles.
            # A successfully returned planning-error reply is not a finished task.
            completed_status = (request.get("status") if request.get("status") in {"clarifying", "failed", "timed_out", "cancelled", "outcome_indeterminate"}
                else "finished")
            self.store.mutate_dialogue(previous["session_id"], {"op": "request_state", "source": run_id,
                "expected_run": run_id,
                "request": {**request, "status": completed_status if status == "completed" else status,
                    "failure": request.get("failure", "") if status == "completed" and completed_status != "finished" else "" if status == "completed" else detail[:300]}})
        self.store.event(run_id, "status", {"status": status, "detail": detail})

    def emit(self, run_id: str, kind: str, data: dict):
        if self.store.get(run_id)["status"] in TERMINAL:
            return
        if kind == "tool_started" and data.get("effect") in {"write", "external"}:
            self.store.update(run_id, mutation_started=1)
        if kind == "tool_finished" and data.get("tool") == "job_search_start" and data.get("status") == "ok":
            browser_id = (data.get("result") or {}).get("id")
            if browser_id:
                self.store.update(run_id, checkpoint={**(self.store.get(run_id)["checkpoint"] or {}), "browser_id": browser_id})
        if kind == "plan_state":
            saved = self.store.get(run_id)["checkpoint"] or {}
            self.store.update(run_id, checkpoint={**saved, "phase": "action_plan", "plan": data["plan"]})
        if kind == "plan_failed":
            saved = self.store.get(run_id)["checkpoint"] or {}
            self.store.update(run_id, checkpoint={**saved, "planning_failure": data})
        if kind == "workflow_bound":
            saved = self.store.get(run_id)["checkpoint"] or {}
            if saved.get("browser_id") == data.get("browser_id"):
                self.store.update(run_id, checkpoint={**saved, "workflow": data["workflow"]})
        self.store.event(run_id, kind, data)

    def save(self, run_id: str, checkpoint: dict):
        current = self.store.get(run_id)
        # A durable external-task binding must never be overwritten by an LLM step.
        if not (current["checkpoint"] or {}).get("browser_id"):
            checkpoint = copy.deepcopy(checkpoint)
            for message in checkpoint.get("messages", []):
                message.pop("reasoning_content", None)
            self.store.update(run_id, checkpoint=checkpoint)

    async def wait_response(self, run_id: str) -> dict:
        while True:
            run = self.store.get(run_id)
            if run["result"] is not None:
                return run["result"]
            if run["status"] in TERMINAL:
                if run.get("error_type") == "ValueError":
                    raise ValueError(run["error"])
                if run.get("error_type") == "TimeoutError":
                    raise TimeoutError(run["error"])
                raise RuntimeError(run["error"] or "任务没有返回结果")
            await asyncio.sleep(0.1)

    def search_state(self, session_id: str) -> dict:
        candidate = self.agent.candidate_state.read() if hasattr(self.agent, "candidate_state") else None
        workspace = getattr(self.agent, "resume_workspace", None)
        version = workspace.read_document().version if workspace else None
        saved = self.store.search_state(session_id)
        return prepare_search_state(saved, candidate, version)

    async def execute(self, run_id: str, *, watch_only=False):
        run = self.store.get(run_id)
        if watch_only:
            try:
                await self.watch(run_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.finish(run_id, "failed", "恢复任务状态时发生错误，请查看执行记录")
            return
        lock = self.sessions.setdefault(run["session_id"], asyncio.Lock())
        try:
            # Queue time is separately bounded; processing budget starts at admission.
            async with asyncio.timeout(self.queue_timeout):
                await lock.acquire()
            try:
                await asyncio.wait_for(self.slots.acquire(), timeout=self.queue_timeout)
                try:
                    self.store.update(run_id, status="running")
                    self.store.event(run_id, "status", {"status": "running", "detail": "正在处理请求"})
                    context = RunContext(run_id, asyncio.current_task(),
                                         lambda kind, data: self.emit(run_id, kind, data),
                                         lambda value: self.save(run_id, value),
                                         (run["checkpoint"] or {}).get("resume_checkpoint"),
                                         session_id=run["session_id"],
                                         search_state=self.store.save_search_state(run["session_id"], self.search_state(run["session_id"])),
                                         persist_search=lambda value: self.store.save_search_state(run["session_id"], value),
                                         dialogue=self.store.dialogue_state(run["session_id"]),
                                         mutate_dialogue=lambda operation: self.store.mutate_dialogue(run["session_id"], operation),
                                         retrieve_history=lambda query, limit: self.jobs.search_conversation_history(run["session_id"], query, limit=limit),
                                         request_records=lambda: self.store.request_records(run["session_id"]),
                                         enforce_task_scope=True)
                    token = current_run.set(context)
                    started = time.monotonic()
                    try:
                        from ..growth.plans import is_explicit_plan_request
                        from ..core.action_language import plan_scope_answer
                        from ..workflow.planning import PLANNING_TIMEOUT, shortcut_safe
                        from ..workflow.requests import is_continuation
                        resuming_request = bool(is_continuation(run["payload"].get("message", "")) and
                            (context.dialogue or {}).get("task_request"))
                        plan_followup = bool((context.dialogue or {}).get("learning_request")
                            and plan_scope_answer(run["payload"].get("message", "")))
                        budget = self.plan_timeout if run["kind"] == "learning_plan" else (
                            self.plan_timeout + PLANNING_TIMEOUT if is_explicit_plan_request(run["payload"].get("message", ""))
                            or plan_followup or resuming_request or ((context.dialogue or {}).get("instruction_request") or {}).get("status") == "clarifying"
                            else max(self.chat_timeout, PLANNING_TIMEOUT + 20) if not shortcut_safe(run["payload"].get("message", ""))
                            else self.chat_timeout)
                        async with asyncio.timeout(budget):
                            if run["kind"] == "learning_plan":
                                self.store.event(run_id, "progress", {"detail": "正在核对 JD 证据并生成计划"})
                                result = await self.plans.generate(run["payload"].get("focus", "")[:400])
                            elif run["kind"] == "job_analysis":
                                from ..jobs.analysis import analyze_job
                                from ..core.models import JobAnalysisRequest
                                result = analyze_job(JobAnalysisRequest.model_validate(run["payload"])).model_dump(mode="json")
                            else:
                                worker = copy.copy(self.agent)
                                history = self.jobs.conversation_history(conversation_id=run["session_id"], limit=100)
                                worker.restore_history(history)
                                if not run["parent_id"]:
                                    self.jobs.add_conversation_message("user", run["payload"]["message"], conversation_id=run["session_id"], run_id=run_id)
                                response = await worker.handle(run["payload"]["message"])
                                # Resolve the old UI-only search confirmation within this run.
                                if response.action == "confirm_search":
                                    from .search_state import merge_search_state
                                    candidate = worker.candidate_state.read() if hasattr(worker, "candidate_state") else None
                                    workspace = getattr(worker, "resume_workspace", None)
                                    snapshot = merge_search_state(context.search_state, {
                                        "queries": response.action_queries or [response.action_query],
                                        "locations": response.action_locations or context.search_state["locations"],
                                    }, candidate, workspace.read_document().version if workspace else None)
                                    context.search_state = self.store.save_search_state(run["session_id"], snapshot)
                                    self.store.update(run_id, mutation_started=1)
                                    browser = await self.browser.start(BrowserSearchRequest(
                                        query=response.action_query, queries=response.action_queries or [],
                                        locations=snapshot["locations"], dry_run=response.action_dry_run,
                                        result_limit=snapshot["result_limit"], salary_min=snapshot["salary_min"],
                                        conversation_id=run["session_id"], search_state_revision=context.search_state["revision"],
                                        max_jobs=snapshot["max_jobs"] or min(200, max(30, (snapshot["result_limit"] or 0)*5)),
                                    ))
                                    self.store.update(run_id, mutation_started=1, checkpoint={"browser_id": browser.id})
                                    from .context import dialogue_change
                                    dialogue_change("task", kind="browser_id", id=browser.id, status=str(browser.status), bind=True)
                                    response.action = None
                                result = response.model_dump(mode="json")
                            self.store.update(run_id, result=result)
                            if run["kind"] == "chat":
                                self.jobs.add_conversation_message("assistant", result["message"], response=result, conversation_id=run["session_id"], run_id=run_id)
                            self.store.event(run_id, "response", {"result": result, "elapsed_ms": round((time.monotonic()-started)*1000)})
                            if run["kind"] == "chat":
                                planning_failure = (self.store.get(run_id)["checkpoint"] or {}).get("planning_failure")
                                if planning_failure:
                                    self.finish(run_id, "timed_out" if planning_failure["code"] == "planning_timeout" else "failed",
                                        planning_failure["detail"], error_type="TimeoutError" if planning_failure["code"] == "planning_timeout" else "ValueError")
                                    return
                                uncertain = any(event.get("status") == "outcome_indeterminate" for event in result.get("tool_events", []))
                                controller_error = any(event.get("tool") == "tool_call_controller" and event.get("status") == "error" for event in result.get("tool_events", []))
                                if uncertain or controller_error:
                                    status = "outcome_indeterminate" if uncertain or self.store.get(run_id)["mutation_started"] else "failed"
                                    self.finish(run_id, status, "工具控制器中断，已保存已执行的步骤")
                                    return
                    finally:
                        current_run.reset(token)
                finally:
                    self.slots.release()
            finally:
                lock.release()
            # Human/browser waits do not consume an LLM slot or block later chat turns.
            await self.watch(run_id)
        except asyncio.CancelledError:
            # The cancellation coordinator owns finalization AFTER child/remote
            # cleanup. Never advertise a terminal state while cleanup is running.
            raise
        except TimeoutError:
            current = self.store.get(run_id)
            self.finish(run_id, "outcome_indeterminate" if current["mutation_started"] else "timed_out",
                        "任务超过时间预算；已有执行记录保留，未自动重试", error_type="TimeoutError")
        except Exception as exc:
            current = self.store.get(run_id)
            # A bare KeyError hides which key was missing, which makes the failure
            # impossible to diagnose from the run record. Keep the key (or the
            # first traceback frame) in the detail text.
            if isinstance(exc, KeyError):
                detail = f"任务执行失败：缺少必需字段 {exc!r}；请查看步骤记录"
            elif isinstance(exc, (ValueError, RuntimeError)):
                detail = str(exc)
            else:
                detail = f"任务执行失败：{type(exc).__name__}：{exc}；请查看步骤记录"
            self.finish(run_id, "outcome_indeterminate" if current["mutation_started"] and not current["result"] else "failed",
                        detail, error_type=type(exc).__name__)

    async def watch(self, run_id: str):
        run = self.store.get(run_id)
        if run["status"] in TERMINAL:
            return
        if (run["checkpoint"] or {}).get("phase") == "action_plan":
            await self.watch_plan(run_id)
            return
        if (run["checkpoint"] or {}).get("delivery_id"):
            await self.watch_delivery(run_id)
            return
        proposal = (run["result"] or {}).get("resume_proposal")
        if proposal:
            self.store.update(run_id, status="waiting_confirmation")
            self.store.event(run_id, "confirmation", {"proposal_id": proposal["id"], "detail": "请审核简历修改提案"})
            while True:
                decision = self.resume_workspace.read_proposal(proposal["id"])
                if decision.status != "pending":
                    self.finish(run_id, "completed", "修改提案已处理")
                    return
                if wait_expired(decision.created_at, self.watch_limits.confirmation):
                    self.store.event(run_id, "watchdog_expired", {"code": "confirmation_timeout", "proposal_id": decision.id})
                    self.finish(run_id, "timed_out", "简历提案确认等待已过期；未自动接受，提案仍保留供核对")
                    return
                await asyncio.sleep(1)
        browser_id = (run["checkpoint"] or {}).get("browser_id")
        if not browser_id:
            self.finish(run_id, "completed", "任务完成")
            return
        previous = None
        while True:
            browser = self.observe_browser(browser_id)
            snapshot = browser.model_dump(mode="json")
            key = (str(browser.status), browser.checkpoint, browser.stored, browser.discovered)
            if key != previous:
                previous = key
                status = "waiting_user" if browser.status in {"waiting_for_user", "paused"} else "running"
                self.store.update(run_id, status=status)
                self.store.event(run_id, "browser", {"task": snapshot, "status": status})
                self.store.mutate_dialogue(run["session_id"], {"op": "task", "source": run_id,
                    "kind": "browser_id", "id": browser_id, "status": str(browser.status)})
            if browser.status in {"completed", "failed", "stopped"}:
                status = {"completed": "completed", "failed": "failed", "stopped": "cancelled"}[browser.status]
                self.browser_summary(run_id, browser)
                await self.complete_workflow(run_id, browser)
                self.finish(run_id, status, browser.detail)
                return
            await asyncio.sleep(1)

    async def watch_plan(self, run_id: str):
        """Observe asynchronous dependencies without holding model/session slots."""
        from ..workflow.execution import advance, DONE, persist, render_step
        run = self.store.get(run_id)
        # A missing plan checkpoint must not raise a bare KeyError that hides the
        # reason. The clarifying questions survive in the dialogue state, so the
        # request is still reported instead of silently failing.
        plan = copy.deepcopy((run["checkpoint"] or {}).get("plan"))
        if not plan:
            pending = (self.store.dialogue_state(run["session_id"]).get("instruction_request") or {})
            questions = pending.get("questions") or []
            detail = ("已记录完整请求，尚未执行。请补充：\n\n" + "\n".join(
                f"{index}. {question}" for index, question in enumerate(questions, 1))
                if questions else "任务计划记录缺失；原始请求仍保留，请重新确认要执行的动作")
            self.store.event(run_id, "watchdog_expired", {"code": "missing_plan_checkpoint", "detail": detail})
            self.finish(run_id, "completed" if questions else "failed", detail)
            return
        if plan["questions"]:
            self.finish(run_id, "completed", "参数尚待补充，没有执行动作")
            return
        previous = {}
        while True:
            run = self.store.get(run_id)
            if run["cancel_requested"]:
                return
            changed = any(step["status"] == "claimed" for step in plan["steps"])
            waiting = False
            for step in plan["steps"]:
                if step["status"] == "waiting_external":
                    # Only job_search_start records a task_id. A step that reported a
                    # wait without an external task can never be observed; fail it
                    # explicitly instead of raising a bare KeyError that kills the run.
                    if not step.get("task_id"):
                        step.update(status="failed",
                                    detail="该步骤没有可观察的外部任务标识，无法确认结果；不会自动重试")
                        self.store.event(run_id, "watchdog_expired",
                                         {"code": "missing_external_task", "step_id": step["id"]})
                        changed = True
                        continue
                    browser = self.observe_browser(step["task_id"])
                    if previous.get(step["id"]) != str(browser.status):
                        previous[step["id"]] = str(browser.status)
                        status = "waiting_user" if browser.status in {"waiting_for_user", "paused"} else "running"
                        self.store.update(run_id, status=status)
                        self.store.event(run_id, "browser", {"task": browser.model_dump(mode="json"), "status": status})
                    if browser.status in {"completed", "failed", "stopped"}:
                        step.update(status="completed" if browser.status == "completed" else "failed", detail=browser.detail)
                        self.browser_summary(run_id, browser)
                        changed = True
                    else:
                        waiting = True
                elif step["status"] == "waiting_confirmation":
                    proposal = self.resume_workspace.read_proposal(step["proposal_id"])
                    if proposal.status == "pending":
                        if wait_expired(proposal.created_at, self.watch_limits.confirmation):
                            step.update(status="blocked", detail="简历提案确认等待已过期，未自动接受或执行依赖动作")
                            self.store.event(run_id, "watchdog_expired", {"code": "confirmation_timeout", "step_id": step["id"]})
                            changed = True
                        else:
                            self.store.update(run_id, status="waiting_confirmation")
                            waiting = True
                    else:
                        step.update(status="completed" if proposal.status in {"approved", "partially_approved"} else "blocked",
                            detail="简历提案已处理；不重新执行提案")
                        changed = True
            if changed or not waiting:
                lock = self.sessions.setdefault(run["session_id"], asyncio.Lock())
                async with lock:
                    async with self.slots:
                        context = RunContext(run_id, asyncio.current_task(),
                            lambda kind, data: self.emit(run_id, kind, data), lambda value: self.save(run_id, value),
                            session_id=run["session_id"], search_state=self.search_state(run["session_id"]),
                            persist_search=lambda value: self.store.save_search_state(run["session_id"], value),
                            dialogue=self.store.dialogue_state(run["session_id"]),
                            mutate_dialogue=lambda op: self.store.mutate_dialogue(run["session_id"], op),
                            retrieve_history=lambda query, limit: self.jobs.search_conversation_history(run["session_id"], query, limit=limit),
                            request_records=lambda: self.store.request_records(run["session_id"]),
                            enforce_task_scope=True, user_message=plan["original"])
                        token = current_run.set(context)
                        try:
                            self.store.update(run_id, status="running")
                            persist(plan)
                            await advance(plan, self.agent.tool_registry)
                        finally:
                            current_run.reset(token)
            if all(step["status"] in DONE for step in plan["steps"]):
                if not self.store.has_event(run_id, "workflow_summary") and any(step.get("task_id") or step.get("proposal_id") for step in plan["steps"]):
                    lines = [render_step(step) for step in plan["steps"] if step["tool"] != "job_search_start" or step["status"] != "completed"]
                    message = "任务编排结果：\n\n" + ("\n\n".join(lines) or "搜索已完成，暂无其他待执行动作。")
                    self.jobs.add_conversation_message("assistant", message, conversation_id=run["session_id"], run_id=f"plan:{run_id}")
                    self.store.event(run_id, "workflow_summary", {"message": message, "statistics_changed": True, "plans_changed": True})
                uncertain = any(step["status"] == "outcome_indeterminate" for step in plan["steps"])
                failed = any(step["status"] in {"failed", "blocked", "skipped"} for step in plan["steps"])
                self.finish(run_id, "outcome_indeterminate" if uncertain else "failed" if failed else "completed",
                    "所有步骤已结束；部分步骤未成功，请查看逐步记录" if uncertain or failed else "完整请求执行结束")
                return
            await asyncio.sleep(1)

    async def complete_workflow(self, run_id: str, browser):
        """Post-search actions are finite, resume/task bound, claimed before effects.

        A crash during this section never replays delivery or plan generation.
        Search itself is already completed and will not be restarted here.
        """
        run = self.store.get(run_id)
        saved = run["checkpoint"] or {}
        workflow = saved.get("workflow")
        if not workflow or saved.get("workflow_finished"):
            return
        from ..growth.requests import resume_stamp
        lock = self.sessions.setdefault(run["session_id"], asyncio.Lock())
        async with lock:
            current = self.store.dialogue_state(run["session_id"]).get("workflow_request") or {}
            stamp = resume_stamp(self.resume_workspace.read_document(), self.agent.candidate_state)
            reason = None
            if saved.get("workflow_claimed"):
                reason = "服务在后续动作执行期间中断；请核对队列和计划，未自动重放"
            elif run["cancel_requested"] or browser.status != "completed":
                reason = "搜索停止或失败，未执行后续动作"
            elif workflow["resume_stamp"] != stamp:
                reason = "主简历已变更，后续授权不再适用"
            elif workflow["expires_at"] < time.time():
                reason = "后续动作授权已过期，请核对搜索结果后重新下达指令"
            elif current.get("id") != workflow["id"] or current.get("status") != "searching":
                reason = "会话中的原工作流已取消或被新请求替代"
            if reason:
                lines = [reason]
            else:
                self.store.update(run_id, checkpoint={**saved, "workflow_claimed": True})
                self.store.event(run_id, "progress", {"detail": "搜索已完成，正在处理本次结果的后续动作"})
                lines = []
                for action in workflow["actions"]:
                    if self.store.get(run_id)["cancel_requested"]:
                        break
                    # Resume can change from the UI independently of chat ordering.
                    if resume_stamp(self.resume_workspace.read_document(), self.agent.candidate_state) != stamp:
                        lines.append("主简历已变更，剩余动作未执行")
                        break
                    name = "自动投递" if action["kind"] == "delivery" else "成长计划"
                    try:
                        self.store.event(run_id, "workflow_step", {"action": action["kind"], "status": "started", "scope": action["scope"]})
                        if action["kind"] == "delivery":
                            coordinator = self.agent.tool_registry.mock_workflow
                            if coordinator is None:
                                raise ValueError("模拟站自动投递执行器未配置；不支持真实网站投递")
                            result = await asyncio.wait_for(coordinator.queue_deliveries(
                                min_score=action["scope"]["min_score"], session_id=run["session_id"], task_id=browser.id), timeout=15)
                            lines.append(result["message"])
                        else:
                            if self.plans is None:
                                raise ValueError("成长计划执行器未配置")
                            async with asyncio.timeout(self.plan_timeout):
                                async with self.slots:
                                    result = await self.plans.generate(action["instruction"], task_id=browser.id, **action["scope"])
                            lines.append("劣势分析：" + result["summary"] + f"\n已保存成长计划「{result['title']}」，依据本次 {result['jobs_analyzed']} 份 JD。")
                        self.store.event(run_id, "workflow_step", {"action": action["kind"], "status": "completed"})
                    except (ValueError, RuntimeError, TimeoutError) as exc:
                        lines.append(f"{name}未完成：{str(exc) or '超时'}；不会自动重试或扩大范围。")
                        self.store.event(run_id, "workflow_step", {"action": action["kind"], "status": "failed", "detail": str(exc)})
                    except Exception as exc:
                        lines.append(f"{name}执行未确认（{type(exc).__name__}），请核对执行记录；不会自动重试。")
                        self.store.event(run_id, "workflow_step", {"action": action["kind"], "status": "outcome_indeterminate", "error_type": type(exc).__name__})
            if not lines:
                lines = ["本次搜索没有待执行的后续动作。"]
            message = "本次工作流结果：\n\n" + "\n\n".join(lines)
            self.jobs.add_conversation_message("assistant", message, conversation_id=run["session_id"], run_id=f"workflow:{run_id}")
            self.store.event(run_id, "workflow_summary", {"message": message, "statistics_changed": True, "plans_changed": True})
            self.store.update(run_id, checkpoint={**(self.store.get(run_id)["checkpoint"] or {}), "workflow_finished": True})
            if current.get("id") == workflow["id"]:
                self.store.mutate_dialogue(run["session_id"], {"op": "workflow", "request": {**current, "status": "finished"}})

    async def watch_delivery(self, run_id: str):
        run = self.store.get(run_id)
        task_id = run["checkpoint"]["delivery_id"]
        previous = None
        while True:
            view = self.observe_delivery(task_id)
            key = (str(view.status), view.checkpoint, view.recruiter_reply, view.submitted)
            if key != previous:
                previous = key
                status = "waiting_user" if view.status == "waiting_for_user" else "waiting_reply" if view.status == "waiting_for_reply" else "running"
                self.store.update(run_id, status=status)
                self.store.event(run_id, "delivery", {"task": view.model_dump(mode="json"), "status": status, "detail": view.detail})
            if view.status in {"completed", "failed", "outcome_indeterminate", "cancelled"}:
                batch = next((item for item in self.jobs.mock_delivery_queue(active_only=False) if item["run_id"] == task_id), None)
                if batch and view.submitted and not self.store.has_event(run_id, "response"):
                    message = f"模拟站自动投递 · 职位 #{view.job_id}：已核实简历投递完成。"
                    self.store.event(run_id, "response", {"result": {"message": message, "delivery": view.model_dump(mode="json")}})
                self.finish(run_id, str(view.status), view.detail, result={"delivery": view.model_dump(mode="json")})
                return
            await asyncio.sleep(1)

    def observe_browser(self, task_id: str):
        """A dead worker is not a live browser lease; never replay it on restart."""
        from ..core.models import BrowserTaskStatus
        view = self.jobs.get_task(task_id)
        alive = self.browser.current().id == task_id and self.browser.worker and not self.browser.worker.done()
        if view.status not in {"completed", "failed", "stopped"} and not alive:
            detail = "浏览器执行器已中断，原任务已停止；部分结果保留，不自动重新搜索"
            self.jobs.event(task_id, "watchdog_expired", detail, reason_code="worker_lost")
            view = self.jobs.update_task(task_id, status=BrowserTaskStatus.FAILED, detail=detail, checkpoint="worker_lost")
        return view

    def observe_delivery(self, task_id: str):
        from ..core.models import DeliveryTaskStatus
        view = self.jobs.get_delivery_run(task_id)
        code = None
        alive = self.delivery and self.delivery.current().id == task_id and self.delivery.worker and not self.delivery.worker.done()
        if view.status == "waiting_for_reply" and not alive and wait_expired(view.started_at, self.watch_limits.reply):
            code = "reply_timeout"
        elif view.status == "waiting_for_user" and wait_expired(view.updated_at, self.watch_limits.human):
            # A live worker has a cooperative watchdog which releases its lease.
            if not alive:
                code = "human_timeout"
        elif view.status in {"starting", "running"}:
            if not alive:
                code = "worker_lost"
        if code:
            uncertain = view.checkpoint in {"submit_started", "submission_unconfirmed"} and not view.submitted
            detail = "任务等待已过期或执行器中断；不自动重放，已确认的投递保留，请核对执行记录"
            if uncertain:
                self.jobs.mark_outcome_indeterminate(view.job_id, detail)
            elif not view.submitted:
                self.jobs.mark_delivery_failed(view.job_id, detail)
            status = DeliveryTaskStatus.OUTCOME_INDETERMINATE if uncertain else DeliveryTaskStatus.COMPLETED if view.submitted else DeliveryTaskStatus.FAILED
            view = self.jobs.update_delivery_run(task_id, status=status, detail=detail, checkpoint=code)
            if self.delivery and self.delivery.current().id == task_id and (not self.delivery.worker or self.delivery.worker.done()):
                self.delivery.state = view
        return view

    def browser_summary(self, run_id: str, browser):
        run = self.store.get(run_id)
        if any(event["kind"] == "browser_summary" and event["data"].get("task_id") == browser.id for event in self.store.events(run_id)):
            return
        from ..jobs.search_reporting import render_search_report
        try:
            report = self.jobs.search_report(browser.id)
            message = render_search_report(browser, report)
        except (KeyError, ValueError):
            message = "搜索已结束；部分详情尚未读取，可在看板核对已保存结果。"
            report = {"jobs": []}
        self.jobs.add_conversation_message("assistant", message, conversation_id=run["session_id"], run_id=f"summary:{run_id}:{browser.id}")
        if report.get("jobs"):
            self.store.mutate_dialogue(run["session_id"], {"op": "snapshot", "source": run_id,
                "topic": "applications", "kind": "job", "version": browser.id,
                "items": [{key: item[key] for key in ("id", "title", "company", "stage", "content_hash")}
                          for item in report["jobs"]], "displayed": True})
        self.store.event(run_id, "browser_summary", {"task_id": browser.id, "message": message, "discovered": browser.discovered,
                                                    "stored": browser.stored, "duplicates": browser.duplicates,
                                                    "report": report, "statistics_changed": True})

    async def cancel(self, run_id: str) -> dict:
        run = self.store.get(run_id)
        if run["status"] in TERMINAL:
            return run
        cleanup = self.cancellations.get(run_id)
        if cleanup is None:
            self.store.update(run_id, cancel_requested=1, status="cancelling")
            self.store.event(run_id, "status", {"status": "cancelling", "detail": "正在停止任务，等待执行器安全清理"})
            cleanup = asyncio.create_task(self._cancel_cleanup(run_id), name=f"cancel-run-{run_id}")
            self.cancellations[run_id] = cleanup
            cleanup.add_done_callback(lambda done: self.cancellations.pop(run_id, None))
        # Return an acknowledgement promptly. Cleanup is durable/observed and is
        # not cancelled by HTTP disconnect or the Next.js proxy's 15s deadline.
        await asyncio.wait({cleanup}, timeout=1)
        return self.store.get(run_id)

    async def _cancel_cleanup(self, run_id: str):
        try:
            await self._stop_bound_tasks(run_id)
        except Exception as exc:
            current = self.store.get(run_id)
            self.finish(run_id, "outcome_indeterminate" if current["mutation_started"] else "failed",
                f"停止清理未能确认：{type(exc).__name__}；请核对运行记录，不要重复提交")

    async def stop_all(self, *, reason: str = "用户要求停止全部任务") -> dict:
        await self.stop_tasks(None)
        return await asyncio.shield(self.stop_jobs["*"])

    def stop_status(self, session_id: str | None) -> dict:
        task = self.stop_jobs.get(session_id if session_id is not None else "*")
        if task is None:
            return {"status": "idle", "message": "尚无停止请求"}
        if not task.done():
            return {"status": "stopping", "message": "正在停止执行器并核验队列"}
        if task.cancelled() or task.exception():
            return {"status": "failed", "message": "停止清理未确认，请核对运行记录"}
        return task.result()

    async def stop_tasks(self, session_id: str | None, *, exclude_run: str | None = None) -> dict:
        key = session_id if session_id is not None else "*"
        task = self.stop_jobs.get(key)
        if task is None or task.done():
            task = asyncio.create_task(self._stop_scope(session_id, exclude_run), name=f"stop-scope-{key}")
            self.stop_jobs[key] = task
        # Disconnects do not cancel cleanup. Clients poll until it is verified.
        await asyncio.wait({task}, timeout=1)
        return self.stop_status(session_id)

    async def _stop_scope(self, session_id: str | None, exclude_run: str | None) -> dict:
        async with self.stop_lock:
            failures, stopped, uncertain = [], [], []
            coordinator = getattr(self.agent.tool_registry, "mock_workflow", None)
            runs = [run for run in self.store.list(session_id, active=True) if run["id"] != exclude_run
                    and not (run["kind"] == "chat" and stop_scope(run["payload"].get("message", "")))]
            # Persist cancellation before yielding; no dependent plan step may proceed.
            for run in runs:
                self.store.update(run["id"], cancel_requested=1, status="cancelling")
                self.store.event(run["id"], "status", {"status": "cancelling", "detail": "停止当前会话及其后台队列"})
                if run["id"] in self.tasks:
                    self.tasks[run["id"]].cancel()
            if coordinator:
                if session_id in {None, "default"}:
                    try:
                        settings = coordinator.settings()
                        self.jobs.update_mock_workflow_settings(**{key: settings[key] for key in
                            ("monitor_enabled", "daily_time", "min_score", "max_contacts")},
                            auto_submit=False, daily_enabled=False)
                    except Exception as exc:
                        failures.append(f"automation:{type(exc).__name__}")
                try:
                    async with asyncio.timeout(10):
                        queue = await coordinator.cancel_delivery_queue(session_id)
                except Exception as exc:
                    queue = {"cancelled_queue": 0}
                    failures.append(f"queue:{type(exc).__name__}")
            else:
                queue = {"cancelled_queue": 0}
            for run in runs:
                try:
                    async with asyncio.timeout(10):
                        await self._stop_bound_tasks(run["id"])
                    stopped.append(run["id"])
                except Exception as exc:
                    failures.append(f"run:{run['id']}:{type(exc).__name__}")
                    self.finish(run["id"], "outcome_indeterminate" if run["mutation_started"] else "failed", "停止清理未确认，请核验")
            # Orphan executors are included only when their ownership is provable.
            owned = self.store.dialogue_state(session_id).get("tasks", {}) if session_id else {}
            for kind, service in (("browser", self.browser), ("delivery", self.delivery)):
                if not service:
                    continue
                view = service.current()
                bound = any((run.get("checkpoint") or {}).get(f"{kind}_id") == view.id
                            for run in self.store.list(session_id))
                daily_owned = kind == "browser" and session_id == "default" and coordinator and coordinator.settings().get("last_run_id") == view.id
                if session_id is None or bound or owned.get(f"{kind}_id") == view.id or daily_owned:
                    try:
                        async with asyncio.timeout(10):
                            stopped_view = await service.stop()
                            if stopped_view.status == "outcome_indeterminate" and stopped_view.id:
                                uncertain.append(stopped_view.id)
                    except Exception as exc:
                        failures.append(f"{kind}:{type(exc).__name__}")
            if coordinator and self.jobs.mock_delivery_queue(session_id=session_id):
                failures.append("queue:remaining")
            remaining = [run["id"] for run in self.store.list(session_id, active=True)
                         if run["id"] in {item["id"] for item in runs}]
            if remaining:
                failures.append("runs:remaining")
            status = "failed" if failures else "stopped"
            message = ("部分任务停止未确认，请核对运行记录；不会自动重试。" if failures else
                       "已停止" + ("全部会话" if session_id is None else "当前会话") + "的搜索、沟通、投递及待执行队列；已发送内容和历史保留。")
            if uncertain:
                message += "发送结果不确定的记录仍需人工核验，未自动重投。"
            return {"status": status, "message": message, "stopped_runs": len(stopped),
                    "cancelled_queue": queue.get("cancelled_queue", 0), "failed_runs": failures,
                    "uncertain_outcomes": uncertain}

    async def _stop_bound_tasks(self, run_id: str):
        task = self.tasks.get(run_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        current = self.store.get(run_id)
        browser_id = (current["checkpoint"] or {}).get("browser_id")
        if browser_id and self.browser.current().id == browser_id:
            view = await self.browser.stop()
            self.browser_summary(run_id, view)
        delivery_id = (current["checkpoint"] or {}).get("delivery_id")
        if delivery_id and self.delivery and self.delivery.current().id == delivery_id:
            view = await self.delivery.stop()
            self.finish(run_id, "outcome_indeterminate" if view.status == "outcome_indeterminate" else "cancelled", view.detail)
        current = self.store.get(run_id)
        checkpoint = copy.deepcopy(current["checkpoint"] or {})
        plan = checkpoint.get("plan")
        uncertain = False
        if plan:
            plan.update(status="cancelled", expires_at=0)
            for step in plan["steps"]:
                if step["status"] == "claimed":
                    unknown = step.get("effect") in {"write", "external"}
                    uncertain |= unknown
                    step.update(status="outcome_indeterminate" if unknown else "skipped", detail="执行期间中断，结果需核对" if unknown else "已中断")
                elif step["status"] in {"pending", "waiting_external", "waiting_confirmation"}:
                    step.update(status="skipped", detail="用户已停止，后续步骤不再执行；已产生的结果/提案保留")
            self.store.update(run_id, checkpoint=checkpoint)
            active = self.store.dialogue_state(current["session_id"]).get("instruction_request") or {}
            if active.get("id") == plan["id"]:
                self.store.mutate_dialogue(current["session_id"], {"op": "instruction", "request": {**active,
                    "status": "cancelled", "expires_at": 0, "steps": plan["steps"]}})
        else:
            uncertain = bool(current["mutation_started"] and not current["result"] and not browser_id and not delivery_id)
        self.finish(run_id, "outcome_indeterminate" if uncertain else "cancelled",
            "任务已停止；中断时正在执行的写入需核对，未自动重试" if uncertain else "任务已停止，已完成的步骤保留")

    async def resume(self, run_id: str) -> dict:
        run = self.store.get(run_id)
        browser_id = (run["checkpoint"] or {}).get("browser_id")
        delivery_id = (run["checkpoint"] or {}).get("delivery_id")
        if run["status"] in {"waiting_user", "waiting_reply"} and delivery_id and self.delivery:
            if self.delivery.current().id != delivery_id:
                raise ValueError("投递任务已经变更，请核对原任务")
            await self.delivery.resume()
            return self.store.update(run_id, status="running")
        if run["status"] == "waiting_user" and browser_id:
            if self.browser.current().id != browser_id:
                raise ValueError("浏览器任务已经变更，请重新搜索")
            await self.browser.resume()
            return self.store.update(run_id, status="running")
        if (run["status"] not in {"interrupted", "timed_out", "failed"} or run["mutation_started"]
                or run["kind"] == "chat" and (run["checkpoint"] or {}).get("phase") != "tool_loop"):
            raise ValueError("该任务不能重放；请核对已执行步骤，或在原提案中确认")
        child = self.submit(run["session_id"], f"resume:{run_id}", run["kind"], run["payload"], parent_id=run_id)
        if child["status"] == "queued":
            self.store.update(child["id"], checkpoint={"resume_checkpoint": run["checkpoint"]})
        return self.store.get(child["id"])

    async def stream(self, run_id: str, after: int = 0) -> AsyncIterator[str]:
        self.store.get(run_id)
        cursor = after
        while True:
            events = self.store.events(run_id, cursor)
            for event in events:
                cursor = event["seq"]
                yield f"id: {cursor}\nevent: {event['kind']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            if self.store.get(run_id)["status"] in TERMINAL and not events:
                return
            if not events:
                yield ": heartbeat\n\n"
            await asyncio.sleep(0.4)
