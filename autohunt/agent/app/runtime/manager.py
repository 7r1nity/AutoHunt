from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from typing import AsyncIterator

from .context import RunContext, current_run
from .search_state import prepare_search_state
from .store import RunStore, TERMINAL
from .control import progress_requested, stop_scope
from ..core.models import BrowserSearchRequest
from ..security.risk import broad_delivery_scope
from .resilience import WatchLimits, wait_expired


# Remind once after five minutes; waiting is not a failed execution.
_APPROVAL_REMINDER_SECONDS = 300


def _bound_action_snapshot(plan: dict, step: dict) -> dict:
    """Use the same complete binding when issuing and settling approval."""
    from ..workflow.execution import RESUME_BOUND_TOOLS
    producer = next((item for item in plan["steps"] if item["id"] == step.get("evidence_step")), {})
    snapshot = {"plan_id": plan["id"], "tool": step["tool"], "arguments": copy.deepcopy(step["arguments"]),
                "resume_stamp": plan["resume_stamp"] if step["tool"] in RESUME_BOUND_TOOLS else None,
                "evidence_task_id": producer.get("task_id")}
    if step.get("selection_inherits_scope"):
        snapshot["selection_inherits_scope"] = True
    return snapshot


def _approval_detail(tool: str, arguments: dict) -> str:
    """Generate a human-readable confirmation prompt from action arguments (T-13/T-19).

    This text appears directly in the approval card so the user knows what they
    are being asked to confirm without opening the raw arguments JSON.
    """
    if tool == "job_remove_selection":
        return f"请确认移除范围：{arguments.get('selector', '尚未指定')}。仅移出申请看板并停止选中岗位未完成的投递；不会删除JD或撤回已发送内容。"
    if tool == "mock_delivery_start":
        if broad_delivery_scope(tool, arguments):
            return ("本次投递未设置分数门槛、筛选条件或指定岗位，将按当前已搜索岗位批量入队。"
                    "请确认此范围。\n\n确认后加入自动队列：先沟通，招聘方回复后发送已授权简历；"
                    "可随时停止，在任务记录中查看进度。")
        parts = []
        count = arguments.get("limit", arguments.get("expected_count"))
        if isinstance(count, int) and count > 0:
            parts.append(f"最多投递 {count} 个符合条件职位")
        else:
            parts.append("投递职位")
        min_score = arguments.get("min_score")
        if isinstance(min_score, int):
            parts.append(f"匹配分 ≥ {min_score}")
        salary_min = arguments.get("salary_min")
        if isinstance(salary_min, int) and salary_min > 0:
            parts.append(f"月薪下限 ≥ {salary_min:,} 元")
        filters = arguments.get("filters")
        if filters:
            try:
                from ..core.job_filters import filter_summary
                summary = filter_summary(filters)
                if summary and summary != "未设置语义限制":
                    parts.append(f"筛选：{summary}")
            except Exception:
                pass
        selector = arguments.get("selector")
        if selector:
            parts.append("指定对象：" + json.dumps(selector, ensure_ascii=False))
        detail = "；".join(parts) + "。请确认此投递范围。"
        return detail + "\n\n确认后加入自动队列：先沟通，招聘方回复后发送已授权简历；可随时停止，在任务记录中查看进度。"
    if tool == "job_search_start" and arguments.get("dry_run"):
        queries = "、".join(arguments.get("queries") or [])
        return f"模拟站试运行搜索「{queries}」，可能实际发起沟通并发送已授权简历，请核对范围。"
    return "请核对本动作范围后确认；已有步骤和独立分支保留。"


class AgentRuntime:
    """Durable runs, per-session ordering, bounded concurrency and one deadline."""
    def __init__(self, store: RunStore, agent, jobs, browser, resume_workspace,
                 *, queue_timeout=120, chat_timeout=55, plan_timeout=105):
        self.store, self.agent, self.jobs = store, agent, jobs
        self.browser, self.resume_workspace = browser, resume_workspace
        self.tasks: dict[str, asyncio.Task] = {}
        self.cancellations: dict[str, asyncio.Task] = {}
        self.sessions: dict[str, asyncio.Lock] = {}
        self.slots = asyncio.Semaphore(2)
        self.queue_timeout, self.chat_timeout, self.plan_timeout = queue_timeout, chat_timeout, plan_timeout
        self.delivery = None
        self.daily = None
        self.stop_jobs: dict[str, asyncio.Task] = {}
        self.stop_contexts: dict[str, dict] = {}
        self.stop_lock = asyncio.Lock()
        self.watch_limits = WatchLimits.configured()
        # HR waiters release model/browser slots. Their SQLite/encryption work
        # must also release the event loop, without creating 120 worker threads.
        self._watch_io_slots = asyncio.Semaphore(4)
        self._control_io_slots = asyncio.Semaphore(2)

    async def _background_io(self, function, *args, control=False, **kwargs):
        """Bound local DB work; drain an in-flight write before cancellation.

        Cancelling to_thread alone does not stop its thread. Waiting for that
        call to finish keeps stop/shutdown from returning before a late write.
        No browser/model/sending operation belongs in this lane.
        """
        async with self._control_io_slots if control else self._watch_io_slots:
            work = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
            try:
                return await asyncio.shield(work)
            except asyncio.CancelledError:
                while not work.done():
                    try:
                        await asyncio.shield(work)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not work.cancelled():
                    work.exception()  # Consume a DB error without hiding cancellation.
                raise

    async def start(self):
        self.sessions.clear()
        self.slots = asyncio.Semaphore(2)
        await self._background_io(self.recover_search_summaries)
        # A persisted control request must never restart as a normal model task.
        # Settle pending stops before admitting other recovered foreground work.
        for control in await self._background_io(self.store.list, active=True, limit=None):
            if ((control.get("payload") or {}).get("control_kind") == "stop"
                    or control["kind"] == "chat" and stop_scope((control.get("payload") or {}).get("message", ""))):
                self.store.update(control["id"], checkpoint={"phase": "control_result", "control_kind": "stop"})
                await self._stop_command(control, stop_scope(control["payload"].get("message", "")) or "session")
        # Re-enqueue only requests which never began. Side effects are not replayed.
        for run in reversed(await self._background_io(self.store.list, active=True, limit=None)):
            if ((run.get("payload") or {}).get("control_kind") == "progress"
                    or run["kind"] == "chat" and progress_requested((run.get("payload") or {}).get("message", ""))):
                try:
                    await self._background_io(self._progress_answer, run, control=True)
                except Exception:
                    # Reporting is read-only. A failed recovery query must not
                    # prevent unrelated work or the application from starting.
                    pass
                continue
            if run["kind"] == "learning_plan":
                self.finish(run["id"], "cancelled", "该功能已移除；历史记录保留，不再执行")
                continue
            from .retired_features import retire_growth_steps
            checkpoint, retired = retire_growth_steps(run.get("checkpoint"))
            if retired:
                self.store.update(run["id"], checkpoint=checkpoint)
                self.store.event(run["id"], "feature_retired", {"step_ids": retired, "detail": "已取消移除功能的未完成步骤；其他执行记录保留"})
                run = self.store.get(run["id"])
            if run["kind"] == "daily":
                continue  # Owned and recovered by DailyWorkflow, using this ledger.
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
            if run["status"] not in TERMINAL and not (run["checkpoint"] or {}).get("browser_id") and not (run["checkpoint"] or {}).get("delivery_id") and (run["checkpoint"] or {}).get("phase") != "action_plan" and run["status"] != "waiting_confirmation":
                self.finish(run_id, "outcome_indeterminate" if run["mutation_started"] else "interrupted", "服务停止，检查点已保存")
            task.cancel()
        await asyncio.gather(*(task for _, task in tasks), return_exceptions=True)
        await asyncio.gather(*list(self.cancellations.values()), return_exceptions=True)
        await asyncio.gather(*list(self.stop_jobs.values()), return_exceptions=True)
        self.tasks.clear()

    def schedule(self, run_id: str, *, watch_only=False):
        previous = self.tasks.get(run_id)
        if previous and not previous.done():
            return
        task = asyncio.create_task(self.execute(run_id, watch_only=watch_only), name=f"agent-run-{run_id}")
        self.tasks[run_id] = task
        task.add_done_callback(lambda done: self.tasks.pop(run_id, None) if self.tasks.get(run_id) is done else None)

    def submit(self, session_id: str, request_key: str, kind: str, payload: dict, parent_id=None) -> dict:
        scope = stop_scope(payload.get("message", "")) if kind == "chat" else None
        progress = not scope and kind == "chat" and progress_requested(payload.get("message", ""))
        if not scope and not progress and any(not task.done() for key, task in self.stop_jobs.items()
                             if key in {"*", session_id}):
            raise ValueError("当前正在停止任务，请等待停止结果后再发起新任务")
        if kind not in {"chat", "job_analysis"}:
            raise ValueError("不支持的任务类型")
        if scope or progress:
            payload = {**payload, "control_kind": "stop" if scope else "progress"}
        run, created = self.store.create(session_id, request_key, kind, payload, parent_id, control=bool(scope or progress))
        if created:
            if scope:
                self.store.update(run["id"], checkpoint={"phase": "control_result", "control_kind": "stop"})
                task = asyncio.create_task(self._stop_command(run, scope), name=f"stop-command-{run['id']}")
                self.tasks[run["id"]] = task
                task.add_done_callback(lambda done: self.tasks.pop(run["id"], None))
            elif progress:
                task = asyncio.create_task(self._progress_answer_async(run), name=f"progress-command-{run['id']}")
                self.tasks[run["id"]] = task
                task.add_done_callback(lambda done: self.tasks.pop(run["id"], None)
                    if self.tasks.get(run["id"]) is done else None)
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
            await self.stop_tasks(None if scope == "all" else run["session_id"], exclude_run=run["id"],
                                  conversation_id=run["session_id"])
            result = await asyncio.shield(self.stop_jobs[key])
            response = {**result, "message": result.get("result_message", result["message"]), "lane": "application"}
            self.store.update(run["id"], result=response)
            self.store.event(run["id"], "response", {"result": response})
            self.finish(run["id"], "completed" if result["status"] == "stopped" else "failed", result["message"])
        except Exception as exc:
            self.finish(run["id"], "failed", f"停止未能确认：{type(exc).__name__}；请核对任务状态")

    def task_snapshot(self, session_id: str | None, *, run_ids: list[str] | None = None) -> dict:
        from .task_results import read_task_snapshot
        return read_task_snapshot(self.store, self.jobs, session_id, run_ids=run_ids)

    def _progress_answer(self, run: dict) -> dict:
        """Read-only control lane; never takes the planning/session execution lock."""
        from .task_results import render_task_snapshot
        self.store.update(run["id"], status="running", checkpoint={"phase": "control_result", "control_kind": "progress"})
        self.jobs.add_conversation_message("user", run["payload"]["message"],
            conversation_id=run["session_id"], run_id=run["id"])
        try:
            snapshot = self.task_snapshot(run["session_id"])
            message_id = "task-progress:" + run["id"]
            response = {"message": render_task_snapshot(snapshot), "snapshot": snapshot,
                        "snapshot_id": snapshot["snapshot_id"], "message_id": message_id,
                        "run_id": run["id"], "conversation_id": run["session_id"], "lane": "application"}
            self.jobs.add_conversation_message("assistant", response["message"], response=response,
                conversation_id=run["session_id"], run_id=message_id)
            self.store.event(run["id"], "execution_path", {"mode": "local_rule", "reason": "task_results_control"})
            self.store.event(run["id"], "response", {"result": response})
            self.finish(run["id"], "completed", response["message"], result=response)
            return response
        except Exception as exc:
            self.finish(run["id"], "failed", f"当前结果读取失败：{type(exc).__name__}；原任务继续执行，可重试只读查询")
            raise

    def progress_result(self, session_id: str, message: str = "当前结果", *, request_id: str | None = None) -> dict:
        import uuid
        run, created = self.store.create(session_id, "progress:" + (request_id or uuid.uuid4().hex), "chat",
            {"message": message, "control_kind": "progress"}, control=True)
        if not created:
            if run.get("result"):
                return run["result"]
            raise ValueError("同一结果查询尚在处理，请稍后重试")
        return self._progress_answer(run)

    async def _progress_answer_async(self, run: dict) -> dict | None:
        try:
            return await self._background_io(self._progress_answer, run, control=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            # _progress_answer already persists the failure. SSE consumers read
            # that record; do not leave an unobserved task exception behind.
            return None

    async def progress_result_async(self, session_id: str, message: str = "当前结果", *, request_id: str | None = None) -> dict:
        """Use from async callers; synchronous HTTP handlers use progress_result."""
        return await self._background_io(self.progress_result, session_id, message, request_id=request_id, control=True)

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

    def delivery_owner(self, task_id: str) -> str | None:
        """An active tracked authorization, not a manually opened site conversation."""
        owners = {run["session_id"] for run in self.store.list(active=True, limit=None)
                  if run["kind"] == "delivery" and not run["cancel_requested"]
                  and (run["checkpoint"] or {}).get("delivery_id") == task_id}
        return next(iter(owners)) if len(owners) == 1 else None

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
        if kind == "effect_phase":
            run = self.store.get(run_id)
            saved = copy.deepcopy(run["checkpoint"] or {})
            plan = saved.get("plan") or {}
            node = next((step for step in plan.get("steps", []) if step["id"] == data.get("step_id")), None)
            if (plan.get("id") == data.get("plan_id") and node and node.get("status") == "claimed"
                    and node.get("execution_id") == data.get("execution_id")):
                node["execution_phase"] = data["phase"]
                self.store.save_plan(run_id, plan)
            if data["phase"] in {"started", "committed"}:
                self.store.update(run_id, mutation_started=1)
        if kind == "tool_started" and data.get("effect") in {"write", "external"}:
            self.store.update(run_id, mutation_started=1)
        if kind == "tool_finished" and data.get("tool") == "job_search_start" and data.get("status") == "ok":
            browser_id = (data.get("result") or {}).get("id")
            if browser_id:
                self.store.update(run_id, checkpoint={**(self.store.get(run_id)["checkpoint"] or {}), "browser_id": browser_id})
        if kind == "tool_finished" and data.get("tool") == "mock_delivery_start" and data.get("status") == "ok":
            binding_id = (data.get("result") or {}).get("binding_task_id")
            if binding_id:
                self.store.update(run_id, checkpoint={**(self.store.get(run_id)["checkpoint"] or {}), "binding_browser_id": binding_id})
        if kind == "plan_state":
            saved = self.store.get(run_id)["checkpoint"] or {}
            previous_plan = saved.get("plan") or {}
            if previous_plan.get("id") == data["plan"]["id"] and previous_plan.get("revision", 0) > data["plan"].get("revision", 0):
                self.store.event(run_id, "stale_plan_write_ignored", {"revision": data["plan"].get("revision", 0)})
                return
            if not self.store.save_plan(run_id, data["plan"]):
                return
        if kind == "plan_response_selection":
            saved = self.store.get(run_id)["checkpoint"] or {}
            self.store.update(run_id, checkpoint={**saved, "response_selection": data})
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
            if (current["checkpoint"] or {}).get("phase") == "action_plan":
                checkpoint.pop("phase", None)
                checkpoint.pop("plan", None)
            self.store.update(run_id, checkpoint={**(current["checkpoint"] or {}), **checkpoint})

    def action_snapshot(self, run_id: str, tool: str, arguments: dict) -> tuple[str, dict, bool]:
        from .context import active_run
        context = active_run()
        run = self.store.get(run_id)
        metadata = context.action_authority or {} if context else {}
        plan = (run["checkpoint"] or {}).get("plan") or {}
        step_id = metadata.get("step_id")
        if not step_id:
            # Tool-loop callers use the same durable runner after confirmation.
            # A model cannot authorize its own speculative external call.
            step_id = "action"
            if not plan:
                from ..memory.resume_identity import resume_stamp
                stamp = resume_stamp(self.resume_workspace.read_document(), self.agent.candidate_state)
                plan = {"id": run_id, "session_id": run["session_id"], "original": context.user_message if context else "",
                    "answers": [], "questions": [], "reply": "", "turn": 0, "expires_at": time.time() + 1800,
                    "revision": 0, "resume_stamp": stamp, "status": "running", "goals": [],
                    "steps": [{"id": step_id, "tool": tool, "arguments": copy.deepcopy(arguments), "depends_on": [],
                        "command_quote": context.user_message if context else "", "scope_quote": "", "missing_slots": [],
                        "status": "pending", "effect": self.agent.tool_registry._tools[tool].effect, "user_requested": False}]}
                self.store.update(run_id, checkpoint={**(run["checkpoint"] or {}), "phase": "action_plan", "plan": plan})
        step = next((step for step in plan.get("steps", []) if step["id"] == step_id), None)
        if not step or step["tool"] != tool or step["arguments"] != arguments or step.get("withdrawn") or tool in plan.get("revoked_tools", []):
            raise ValueError("动作与任务执行记录不一致或已撤回")
        snapshot = _bound_action_snapshot(plan, step)
        return step_id, snapshot, bool(step.get("user_requested", (step.get("authority_binding") or {}).get("user_requested", False)))

    def authorize_action(self, run_id: str, tool: str, arguments: dict, risk: str, always: bool) -> dict:
        run = self.store.get(run_id)
        step_id, snapshot, requested = self.action_snapshot(run_id, tool, arguments)
        scope_reset = None
        if tool == "mock_delivery_start":
            from ..security.scope_changes import delivery_scope_change
            # A pending human-only decision stays human-only across retries,
            # restarts or a later change of the dialogue's current-scope pointer.
            for saved in self.store.approvals(run_id):
                recorded = saved["snapshot"]
                if (saved["step_id"] == step_id and recorded.get("scope_reset")
                        and {k: v for k, v in recorded.items() if k != "scope_reset"} == snapshot):
                    scope_reset = recorded["scope_reset"]
                    break
            dialogue = self.store.dialogue_state(run["session_id"])
            previous_scope = dialogue.get("delivery_scope")
            source_id = arguments.get("source_task_id") or snapshot["evidence_task_id"]
            # A fresh bounded request for this plan/current round is a new grant,
            # not an expansion of an unrelated historical batch. Prove identity
            # from server receipts, never from scope_mode or model flags alone.
            current_round = False
            source_reader = getattr(self.jobs, "search_source", None)
            if (requested and callable(source_reader) and type(arguments.get("limit")) is int
                    and 1 <= arguments["limit"] <= 200 and source_id
                    and previous_scope and previous_scope.get("task_id")
                    and previous_scope["task_id"] != source_id
                    and (not snapshot["evidence_task_id"] or snapshot["evidence_task_id"] == source_id)):
                from ..memory.resume_identity import resume_stamp
                from ..workflow.validation import dependency_ancestors
                current_stamp = resume_stamp(self.resume_workspace.read_document(), self.agent.candidate_state)
                source = source_reader(source_id)
                plan = (run["checkpoint"] or {}).get("plan") or {}
                ancestors = dependency_ancestors(step_id, plan.get("steps", []))
                produced_here = any(item["id"] in ancestors and item.get("task_id") == source_id
                    and item.get("tool") in {"job_search_start", "job_results_refilter"}
                    and item.get("status") == "completed" for item in plan.get("steps", []))
                current_round = bool(source and source.get("status") == "completed"
                    and source.get("session_id") == run["session_id"]
                    and source.get("resume_id") == self.agent.candidate_state.read().profile.resume_id
                    and snapshot["resume_stamp"] == current_stamp
                    and (produced_here or dialogue.get("tasks", {}).get("browser_id") == source_id))
            if not current_round:
                scope_reset = scope_reset or delivery_scope_change(arguments, previous_scope,
                    session_id=run["session_id"], stamp=snapshot["resume_stamp"], task_id=snapshot["evidence_task_id"],
                    selection_inherits_scope=bool(snapshot.get("selection_inherits_scope")))
            if arguments.get("allow_outside_search") and not scope_reset:
                scope_reset = {"previous_scope_id": None, "previous_filters": None, "previous_min_score": 0,
                    "previous_task_id": snapshot["evidence_task_id"], "exception_requested": True,
                    "reasons": ["指定岗位申请业务筛选条件例外；仅对本次选中岗位和当前简历有效"]}
            if scope_reset:
                snapshot["scope_reset"] = copy.deepcopy(scope_reset)
        record = self.store.ensure_approval(run_id, run["session_id"], step_id, snapshot,
                                           requested=requested and risk != "UNKNOWN" and not always
                                           and not broad_delivery_scope(tool, arguments) and not scope_reset)
        detail = _approval_detail(tool, arguments)
        if scope_reset:
            from ..core.job_filters import filter_summary
            try:
                old_summary = filter_summary(scope_reset["previous_filters"]) if scope_reset.get("previous_filters") else "无额外语义限制"
            except (TypeError, ValueError, KeyError):
                old_summary = "原筛选结构需要人工核对"
            detail = ("投递范围变化，需要人工确认：" + "；".join(scope_reset["reasons"]) + "。\n"
                + ("业务筛选例外不会变更搜索条件，也不会跳过账号、简历版本和重复发送检查。\n" if scope_reset.get("exception_requested") else
                   f"上一批：匹配分≥{scope_reset['previous_min_score']}；{old_summary}。\n")
                + ("本次未设置额外语义筛选。\n" if not arguments.get("filters") else "") + detail)
        if record["status"] == "pending":
            # Persist wait BEFORE returning to the caller, including tool-loop callers.
            current = self.store.get(run_id)
            plan = copy.deepcopy(current["checkpoint"]["plan"])
            step = next(step for step in plan["steps"] if step["id"] == step_id)
            # T-13/T-19: Generate a human-readable summary from the step arguments
            # so the approval card immediately shows what the user is being asked
            # to confirm, without them needing to open the raw arguments JSON.
            step.update(status="waiting_approval", approval_id=record["id"], detail=detail)
            self.store.update(run_id, checkpoint={**current["checkpoint"], "plan": plan}, status="waiting_confirmation")
            if not self.store.has_event(run_id, "approval:" + record["id"]):
                self.store.event(run_id, "approval:" + record["id"], {"approval_id": record["id"]})
                self.store.event(run_id, "action_approval", {"approval_id": record["id"], "step_id": step_id,
                    "tool": tool, "risk": risk, "status": "pending", "detail": detail})
        return {key: record[key] for key in ("id", "status", "source")} | {
            "scope": snapshot, "detail": detail,
            **({"code": "delivery_scope_change_confirmation_required"} if scope_reset else {})}

    async def decide_approval(self, run_id: str, approval_id: str, session_id: str, approve: bool) -> dict:
        lock = self.sessions.setdefault(session_id, asyncio.Lock())
        async with lock:
            return self._decide_approval(run_id, approval_id, session_id, approve)

    async def choose_job_target(self, run_id: str, decision) -> dict:
        """Choose recorded IDs under the session lock; no model/tools run here."""
        session_id = decision.conversation_id
        async with self.sessions.setdefault(session_id, asyncio.Lock()):
            run = self.store.get(run_id)
            plan = copy.deepcopy((run["checkpoint"] or {}).get("plan") or {})
            step = next((s for s in plan.get("steps", []) if s["id"] == decision.step_id), None)
            choice = step.get("target_choice") if step else None
            if run["session_id"] != session_id or not choice or choice["id"] != decision.choice_id:
                raise ValueError("岗位选择不属于此任务和会话，请刷新候选列表")
            ids = list(dict.fromkeys(decision.job_ids))
            if choice.get("status") == "selected":
                if choice.get("selected_ids") == ids:
                    return run
                raise ValueError("这次选择已提交，不能替换已开始执行的目标")
            if (run["cancel_requested"] or run["status"] in TERMINAL or plan.get("status") == "cancelled" or step.get("withdrawn") or step["status"] != "waiting_input"
                    or plan.get("revision", 0) != decision.expected_revision):
                raise ValueError("任务已停止、修订或不再等待此选择，请刷新")
            from ..memory.resume_identity import resume_stamp
            if plan["resume_stamp"] != resume_stamp(self.resume_workspace.read_document(), self.agent.candidate_state):
                raise ValueError("简历已变化，请核对新的目标范围")
            options = {item["id"]: item for item in choice["options"]}
            if not ids or not set(ids) <= options.keys() or choice.get("selection_mode", "single") == "single" and len(ids) != 1:
                raise ValueError("请仅从这次候选列表选择允许数量的岗位")
            selected = [options[ident] for ident in ids]
            for item in selected:
                job = self.jobs.get_job(item["id"])
                if (job.get("removed_at") or self.jobs.review_fingerprint(job) != item["content_hash"]
                        or item.get("score") is not None and job.get("score") != item["score"]):
                    raise ValueError("候选岗位已变化，请刷新后选择；未继续执行")
            answered = set(step.get("questions", []))
            if step["tool"] == "job_target_resolve":
                scope_items = choice.get("scope_items") or choice["options"]
                selection = {"snapshot_id": choice.get("source_snapshot_id") or choice["snapshot_id"],
                    "items": selected, "scope_items": scope_items, "scope": choice.get("scope", "displayed"),
                    "selector": step["arguments"]["selector"], "resolution": "user_choice"}
                if selection["scope"] == "board" and {row.id for row in self.jobs.list_jobs(limit=201)} != {item["id"] for item in scope_items}:
                    raise ValueError("看板范围已变化，请刷新后选择；没有移除岗位")
                step.update(status="completed", result={"selection": selection, "selected_ids": ids,
                    "message": "已选定目标岗位；后续动作仍按原计划执行。"})
            elif step["tool"] in {"mock_delivery_start", "job_remove_selection"}:
                if len(ids) != 1:
                    raise ValueError("此旧动作只支持选择一个目标，请重新说明多选范围")
                ordinal = next(i for i,item in enumerate(choice["options"], 1) if item["id"] == ids[0])
                step["arguments"] = {**step["arguments"], "selector": f"第{ordinal}个", "snapshot_id": choice["snapshot_id"]}
                from ..security.action_grants import action_binding
                step["authority_binding"] = action_binding(step["tool"], step["arguments"], session_id=session_id, requested=step.get("user_requested", False))
                step["status"] = "pending"
            else:
                raise ValueError("此步骤不支持岗位选择")
            choice.update(status="selected", selected_ids=ids)
            step.update(questions=[], missing_slots=[], detail="已点击选定目标")
            step.pop("failure", None)
            plan["questions"] = [q for q in plan.get("questions", []) if q not in answered]
            plan["revision"] = decision.expected_revision + 1
            plan["status"] = "clarifying" if plan["questions"] else "running"
            if not self.store.save_plan(run_id, plan, expected_revision=decision.expected_revision):
                raise ValueError("任务已变化，请刷新")
            self.store.update(run_id, status="waiting_user" if plan.get("paused") else "running")
            self.store.event(run_id, "job_target_choice", {"choice_id": choice["id"], "step_id": step["id"],
                             "status": "selected", "job_ids": ids, "revision": plan["revision"]})
            self.store.invalidate_approvals(run_id, [step["id"]])
            if not plan.get("paused"):
                self.schedule(run_id, watch_only=True)
            return self.store.get(run_id)

    def _decide_approval(self, run_id: str, approval_id: str, session_id: str, approve: bool) -> dict:
        from ..memory.resume_identity import resume_stamp
        run = self.store.get(run_id)
        record = self.store.approval(approval_id)
        if record["run_id"] != run_id or record["session_id"] != session_id or run["session_id"] != session_id:
            raise ValueError("确认记录不属于此任务和会话")
        plan = (run["checkpoint"] or {}).get("plan") or {}
        step = next((step for step in plan.get("steps", []) if step["id"] == record["step_id"]), None)
        if not step or step.get("approval_id") != approval_id:
            raise ValueError("动作已修订，请核对当前待确认范围")
        snapshot = _bound_action_snapshot(plan, step)
        if record["snapshot"].get("scope_reset"):
            snapshot["scope_reset"] = copy.deepcopy(record["snapshot"]["scope_reset"])
        if step.get("withdrawn") or step["tool"] in plan.get("revoked_tools", []):
            raise ValueError("动作已撤回")
        if snapshot["resume_stamp"] is not None and snapshot["resume_stamp"] != resume_stamp(self.resume_workspace.read_document(), self.agent.candidate_state):
            self.store.invalidate_approvals(run_id, [step["id"]])
            raise ValueError("简历已变化，请核对新的简历及动作范围")
        settled = self.store.settle_approval(approval_id, run_id=run_id, session_id=session_id, approve=approve, snapshot=snapshot)
        self.store.event(run_id, "action_approval", {"approval_id": approval_id, "status": settled["status"],
            "step_id": step["id"], "detail": "范围已确认，接续此步骤" if approve else "已取消此动作"})
        # Watcher observes the persisted decision; the API never executes a tool.
        return self.store.get(run_id)

    async def resolve_approval(self, session_id: str, message: str):
        from ..core.models import ChatResponse, AgentLane
        text = message.strip().rstrip("。！!")
        accepted = {"确认", "同意", "可以", "好", "好的", "行", "执行", "确认执行", "确认投递", "就这样", "照这个执行", "按这个执行"}
        rejected = {"拒绝", "不同意", "取消", "不执行", "不用了"}
        if text not in accepted | rejected:
            return None
        records = [record for record in self.store.approvals(session_id=session_id, pending=True)
                   if self.store.get(record["run_id"])["status"] not in TERMINAL]
        if not records:
            return None
        other_confirmations = any((run.get("result") or {}).get("resume_proposal") or
            any(step["status"] == "waiting_confirmation" for step in ((run.get("checkpoint") or {}).get("plan") or {}).get("steps", []))
            for run in self.store.list(session_id, active=True, limit=None))
        if len(records) != 1 or other_confirmations and text != "确认投递":
            return ChatResponse(message="有多个动作等待确认，请在任务记录中选择对应动作；没有扩大任何范围。", lane=AgentLane.APPLICATION)
        record = records[0]
        # Called under this session's foreground lock, so do not acquire it again.
        try:
            self._decide_approval(record["run_id"], record["id"], session_id, text in accepted)
        except ValueError as exc:
            return ChatResponse(message=str(exc), lane=AgentLane.APPLICATION)
        return ChatResponse(message="已确认此动作范围，将从原步骤继续。" if text in accepted else "已取消此动作，其他分支继续。", lane=AgentLane.APPLICATION)

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
        if watch_only:
            try:
                await self.watch(run_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._background_io(self.finish, run_id, "failed", "恢复任务状态时发生错误，请查看执行记录")
            return
        run = self.store.get(run_id)
        if run["kind"] == "learning_plan":
            self.finish(run_id, "cancelled", "该功能已移除；历史记录保留，不再执行")
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
                                         resume_task=lambda message: self.continue_task(run_id, message),
                                         plan_reader=lambda: self.active_plan(run["session_id"]),
                                         plan_lookup=lambda plan_id: self.plan_for_revision(run["session_id"], plan_id),
                                         revise_task=lambda revision, message: self.revise_plan(run_id, revision, message),
                                         cancellation_requested=lambda: bool(self.store.get(run_id)["cancel_requested"]),
                                         authorize_action=lambda *args: self.authorize_action(run_id, *args),
                                         resolve_approval=lambda message: self.resolve_approval(run["session_id"], message),
                                         task_results_read=lambda: self.task_snapshot(run["session_id"]),
                                         diagnose=lambda kind, data: self.store.event(run_id, kind, data),
                                         enforce_task_scope=True)
                    token = current_run.set(context)
                    started = time.monotonic()
                    try:
                        from ..workflow.planning import PLANNING_TIMEOUT, shortcut_safe
                        from ..workflow.requests import is_continuation
                        resuming_request = bool(is_continuation(run["payload"].get("message", "")) and
                            (context.dialogue or {}).get("task_request"))
                        budget = (
                            self.plan_timeout + PLANNING_TIMEOUT if resuming_request or ((context.dialogue or {}).get("instruction_request") or {}).get("status") == "clarifying"
                            else max(self.chat_timeout, PLANNING_TIMEOUT + 20) if not shortcut_safe(run["payload"].get("message", ""))
                            else self.chat_timeout)
                        if run["kind"] == "chat":
                            from ..workflow.presentation import TIMEOUT as RESPONSE_TIMEOUT
                            budget += RESPONSE_TIMEOUT + 2
                        async with asyncio.timeout(budget):
                            if run["kind"] == "job_analysis":
                                from ..jobs.analysis import analyze_job
                                from ..core.models import JobAnalysisRequest
                                result = analyze_job(JobAnalysisRequest.model_validate(run["payload"])).model_dump(mode="json")
                            else:
                                worker = copy.copy(self.agent)
                                from .semantic_memory import SemanticMemory
                                memory = SemanticMemory(self.store, getattr(self.agent, "llm", None),
                                    record=lambda kind, data: self.store.event(run_id, kind, data))
                                epoch = (context.dialogue or {}).get("history_epoch", 0)
                                context.recall_memory = lambda query, limit: memory.recall(run["session_id"], query,
                                    epoch=(context.dialogue or {}).get("history_epoch", 0), limit=limit)
                                context.memory_source_read = lambda source_id: self.store.memory_source(run["session_id"], source_id,
                                    epoch=(context.dialogue or {}).get("history_epoch", 0))
                                try:
                                    await memory.compact(run["session_id"], epoch=epoch)
                                    context.historical_memory = await memory.recall(run["session_id"], run["payload"]["message"], epoch=epoch)
                                except asyncio.CancelledError:
                                    raise
                                except Exception as exc:
                                    self.store.event(run_id, "memory_unavailable", {"error_type": type(exc).__name__})
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
                                        source=snapshot.get("source", "mock"), filters=snapshot.get("filters"),
                                        query=response.action_query, queries=response.action_queries or [],
                                        locations=snapshot["locations"], dry_run=response.action_dry_run,
                                        result_limit=snapshot["result_limit"], salary_min=snapshot["salary_min"],
                                        result_groups=snapshot.get("result_groups"),
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
                            selection = (self.store.get(run_id)["checkpoint"] or {}).get("response_selection", {})
                            self.store.event(run_id, "response", {"result": result, **selection,
                                "elapsed_ms": round((time.monotonic()-started)*1000)})
                            for snapshot_id in selection.get("displayed_snapshot_ids", []):
                                self.store.mutate_dialogue(run["session_id"], {"op": "display", "snapshot_id": snapshot_id})
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
        run = await self._background_io(self.store.get, run_id)
        if run["status"] in TERMINAL:
            return
        if run["kind"] == "learning_plan":
            await self._background_io(self.finish, run_id, "cancelled", "该功能已移除；历史记录保留，不再执行")
            return
        if (run["checkpoint"] or {}).get("phase") == "action_plan":
            await self.watch_plan(run_id)
            return
        if (run["checkpoint"] or {}).get("delivery_id"):
            await self.watch_delivery(run_id, task_id=run["checkpoint"]["delivery_id"])
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
        from ..workflow.execution import advance, DONE, persist, completion_steps
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
        from ..workflow.recovery import reconcile_receipts
        reconcile_receipts(plan, self.store.all_events(run_id))
        searches = [step for step in plan["steps"] if step["tool"] == "job_search_start"]
        canonical_id = (run["checkpoint"] or {}).get("browser_id")
        if (len(searches) == 1 and canonical_id and searches[0].get("task_id") != canonical_id
                and "[REDACTED_" in str(searches[0].get("task_id", ""))):
            # Legacy checkpoints may have redacted a task reference. Recover only
            # from this run's trusted tool-finished ID, never a guessed/latest task.
            self.jobs.get_task(canonical_id)
            searches[0]["task_id"] = canonical_id
            self.store.update(run_id, checkpoint={**run["checkpoint"], "plan": plan})
            self.store.event(run_id, "checkpoint_reference_repaired", {"task_id": canonical_id})
        previous = {}
        while True:
            run = self.store.get(run_id)
            if run["cancel_requested"]:
                return
            saved_plan = (run["checkpoint"] or {}).get("plan") or {}
            if saved_plan.get("revision", 0) > plan.get("revision", 0):
                plan = copy.deepcopy(saved_plan)
            if plan.get("paused"):
                self.store.update(run_id, status="waiting_user")
                await asyncio.sleep(1)
                continue
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
                    if browser.status in {"waiting_for_user", "paused"}:
                        detail = "职位搜索等待人工处理：" + browser.detail
                        if step.get("detail") != detail or step.get("external_wait_status") != str(browser.status):
                            step.update(detail=detail, external_wait_status=str(browser.status))
                            changed = True
                    elif step.pop("external_wait_status", None):
                        changed = True
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
                elif step["status"] == "waiting_approval":
                    record = self.store.approval(step["approval_id"])
                    if record["status"] == "pending":
                        approval_started = record.get("created_at", 0)
                        marker = "approval_reminder:" + record["id"]
                        if (approval_started and wait_expired(approval_started, _APPROVAL_REMINDER_SECONDS)
                                and not self.store.has_event(run_id, marker)):
                            self.store.event(run_id, marker, {"step_id": step["id"], "approval_id": record["id"],
                                "detail": "仍在等待确认；任务记录保留，尚未发送。"})
                        self.store.update(run_id, status="waiting_confirmation")
                        waiting = True
                    elif record["status"] == "approved":
                        step.update(status="pending")
                        changed = True
                    else:
                        step.update(status="skipped", withdrawn=True, detail="此动作已拒绝或范围已变化",
                            failure={"code": "user_withdrawn", "effect_started": False, "uncertain": False})
                        changed = True
                elif step["status"] == "waiting_input":
                    waiting = True
                elif step["status"] == "waiting_binding":
                    coordinator = getattr(self.agent.tool_registry, "mock_workflow", None)
                    if coordinator and callable(getattr(coordinator, "settings", None)) and coordinator.settings().get("applicant_id"):
                        step.update(status="pending")
                        changed = True
                    elif step.get("wait_reason") == "waiting_resource":
                        if self.browser.can_start_search():
                            step.update(status="pending")
                            changed = True
                        else:
                            waiting = True
                    elif step.get("task_id"):
                        browser = self.observe_browser(step["task_id"])
                        if browser.status in {"completed", "failed", "stopped"}:
                            question = "登录/账号绑定尚未确认，请核对模拟站求职者账号后更新或继续本次投递范围。"
                            step.update(status="waiting_input", wait_reason="account_binding", detail=question, questions=[question])
                            plan["questions"] = list(dict.fromkeys([*plan.get("questions", []), question]))
                            changed = True
                        else:
                            waiting = True
                    else:
                        step.update(status="waiting_input", wait_reason="account_binding", detail="缺少可观察的账号绑定任务，请确认求职者登录")
                        changed = True
            ready = any(step["status"] == "pending" and all(
                next(item for item in plan["steps"] if item["id"] == dep)["status"] in DONE
                for dep in step["depends_on"]) for step in plan["steps"])
            if changed or not waiting or ready:
                lock = self.sessions.setdefault(run["session_id"], asyncio.Lock())
                async with lock:
                    latest = (self.store.get(run_id)["checkpoint"] or {}).get("plan") or {}
                    if latest.get("revision", 0) > plan.get("revision", 0):
                        plan = copy.deepcopy(latest)
                    if plan.get("paused"):
                        continue
                    async with self.slots:
                        context = RunContext(run_id, asyncio.current_task(),
                            lambda kind, data: self.emit(run_id, kind, data), lambda value: self.save(run_id, value),
                            session_id=run["session_id"], search_state=self.search_state(run["session_id"]),
                            persist_search=lambda value: self.store.save_search_state(run["session_id"], value),
                            dialogue=self.store.dialogue_state(run["session_id"]),
                            mutate_dialogue=lambda op: self.store.mutate_dialogue(run["session_id"], op),
                            retrieve_history=lambda query, limit: self.jobs.search_conversation_history(run["session_id"], query, limit=limit),
                            request_records=lambda: self.store.request_records(run["session_id"]),
                            plan_reader=lambda: (self.store.get(run_id)["checkpoint"] or {}).get("plan"),
                            diagnose=lambda kind, data: self.store.event(run_id, kind, data),
                            enforce_task_scope=True, user_message=plan["original"],
                            cancellation_requested=lambda: bool(self.store.get(run_id)["cancel_requested"]))
                        context.authorize_action = lambda *args: self.authorize_action(run_id, *args)
                        context.task_results_read = lambda: self.task_snapshot(run["session_id"])
                        token = current_run.set(context)
                        try:
                            self.store.update(run_id, status="running")
                            persist(plan)
                            await advance(plan, self.agent.tool_registry)
                        finally:
                            current_run.reset(token)
            # A result report closes only the search step. Publish newly
            # observed waits/failures even while another branch is unfinished,
            # rather than leaving their reason only in the task inspector.
            if not self.store.get(run_id)["cancel_requested"]:
                if any(step["status"] in {"waiting_approval", "waiting_confirmation"} for step in plan["steps"]):
                    self.store.update(run_id, status="waiting_confirmation")
                elif any(step["status"] == "waiting_external" and step.get("external_wait_status") for step in plan["steps"]):
                    self.store.update(run_id, status="waiting_user")
                lock = self.sessions.setdefault(run["session_id"], asyncio.Lock())
                async with lock:
                    latest = (self.store.get(run_id)["checkpoint"] or {}).get("plan") or {}
                    if latest.get("revision", 0) > plan.get("revision", 0):
                        plan = copy.deepcopy(latest)
                        continue
                    if plan.get("questions") or not all(step["status"] in DONE for step in plan["steps"]):
                        selected = [step for step in completion_steps(plan, self.store.all_events(run_id))
                            if step["status"] in {"waiting_input", "waiting_approval", "waiting_confirmation",
                                "waiting_binding", "failed", "blocked", "outcome_indeterminate", "skipped"}
                            or step["status"] == "waiting_external" and step.get("external_wait_status")]
                        if selected:
                            await self.publish_plan_answer(run_id, plan, selected, intermediate=True)
            if all(step["status"] in DONE for step in plan["steps"]):
                if plan.get("questions"):
                    self.store.update(run_id, status="waiting_user")
                    return
                if not self._has_final_plan_answer(run_id):
                    lock = self.sessions.setdefault(run["session_id"], asyncio.Lock())
                    async with lock:
                        latest = (self.store.get(run_id)["checkpoint"] or {}).get("plan") or {}
                        if latest.get("revision", 0) > plan.get("revision", 0):
                            plan = copy.deepcopy(latest)
                            continue
                        selected = completion_steps(plan, self.store.all_events(run_id))
                        if selected:
                            await self.publish_plan_answer(run_id, plan, selected)
                if self.store.get(run_id)["cancel_requested"]:
                    return
                uncertain = any(step["status"] == "outcome_indeterminate" for step in plan["steps"])
                failed = any(step["status"] in {"failed", "blocked", "skipped"}
                             and (step.get("failure") or {}).get("code") != "user_withdrawn" for step in plan["steps"])
                self.finish(run_id, "outcome_indeterminate" if uncertain else "failed" if failed else "completed",
                    "所有步骤已结束；部分步骤未成功，请查看逐步记录" if uncertain or failed else "完整请求执行结束")
                return
            if any(step["status"] == "waiting_input" for step in plan["steps"]) and not any(
                    step["status"] in {"waiting_external", "waiting_binding", "waiting_approval", "waiting_confirmation"}
                    for step in plan["steps"]) and not ready:
                self.store.update(run_id, status="waiting_user")
                return  # Input waits do not occupy a polling worker or LLM slot.
            await asyncio.sleep(1)

    def _has_final_plan_answer(self, run_id: str) -> bool:
        return any(event["kind"] == "workflow_summary" and not event["data"].get("intermediate")
                   for event in self.store.all_events(run_id))

    async def publish_plan_answer(self, run_id: str, plan: dict, steps: list[dict], *, intermediate=False):
        """Persist presentation before publishing; recovery never reruns tools."""
        from ..workflow.presentation import compose
        from ..workflow.execution import step_receipt
        run = self.store.get(run_id)
        receipts = [step_receipt(step) for step in steps]
        identity = {"id": plan["id"], "receipts": receipts, "intermediate": True} if intermediate else {
            "id": plan["id"], "revision": plan.get("revision", 0), "original": plan.get("original"),
            "answers": plan.get("answers"), "goals": plan.get("goals"), "steps": steps}
        fingerprint = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        message_id = f"plan-progress:{run_id}:{fingerprint}" if intermediate else f"plan:{run_id}"
        events = self.store.all_events(run_id)
        if any(event["kind"] == "workflow_summary" and event["data"].get("message_id") == message_id for event in events):
            return
        prepared = next((event["data"] for event in reversed(self.store.all_events(run_id))
            if event["kind"] == "plan_answer_prepared" and event["data"].get("fingerprint") == fingerprint), None)
        if prepared is None:
            started = time.monotonic()
            self.store.event(run_id, "response_synthesis", {"status": "started", "plan_id": plan["id"]})
            if intermediate:
                # Waiting/failure receipts need no semantic synthesis and must
                # not queue behind a model slot just to tell the user why.
                answer = await compose(plan, steps, None)
            else:
                async with self.slots:
                    # Background presentation runs outside the action executor.
                    # Give diagnostics an identity, not execution authority.
                    from .context import diagnostic_scope
                    diagnostic = RunContext(run_id, asyncio.current_task(), lambda *_: None, lambda *_: None,
                        session_id=run["session_id"], diagnose=lambda kind, data: self.store.event(run_id, kind, data))
                    run_token = current_run.set(diagnostic)
                    scope_token = diagnostic_scope.set({"plan_id": plan["id"], "revision": plan.get("revision"),
                        "step_id": None, "execution_id": None})
                    try:
                        answer = await compose(plan, steps, getattr(self.agent.tool_registry, "response_llm", None))
                    finally:
                        diagnostic_scope.reset(scope_token)
                        current_run.reset(run_token)
            prepared = {"fingerprint": fingerprint, "message": answer.text, "snapshot_ids": answer.snapshot_ids,
                "mode": answer.mode, "reason": answer.reason, "elapsed_ms": round((time.monotonic()-started)*1000)}
            self.store.event(run_id, "plan_answer_prepared", prepared)
            self.store.event(run_id, "response_synthesis", {"status": "finished", "plan_id": plan["id"],
                **{key: prepared[key] for key in ("mode", "reason", "elapsed_ms")}})
        if self.store.get(run_id)["cancel_requested"]:
            return
        latest = (self.store.get(run_id)["checkpoint"] or {}).get("plan") or {}
        if latest and (latest.get("id") != plan["id"] or latest.get("revision", 0) != plan.get("revision", 0)
                or any(step_receipt(next((current for current in latest.get("steps", []) if current["id"] == step["id"]), {})) != receipt
                       for step, receipt in zip(steps, receipts))):
            return  # A correction/stop during synthesis cannot publish an old wait.
        message = prepared["message"]
        if message and (intermediate or not self._has_final_plan_answer(run_id)):
            result = {"message_id": message_id, "message": message}
            self.jobs.add_conversation_message("assistant", message, response=result,
                conversation_id=run["session_id"], run_id=message_id)
            self.store.event(run_id, "workflow_summary", {"message": message, "statistics_changed": True, "plans_changed": True,
                "plan_id": plan["id"], "message_id": message_id, "result": result, "intermediate": intermediate,
                "presented_step_receipts": receipts})
            for snapshot_id in prepared["snapshot_ids"]:
                self.store.mutate_dialogue(run["session_id"], {"op": "display", "snapshot_id": snapshot_id})

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
        from ..memory.resume_identity import resume_stamp
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
                    if action["kind"] != "delivery":
                        self.store.event(run_id, "workflow_step", {"action": action["kind"], "status": "skipped", "code": "feature_removed"})
                        continue
                    name = "自动投递"
                    try:
                        self.store.event(run_id, "workflow_step", {"action": action["kind"], "status": "started", "scope": action["scope"]})
                        if action["kind"] == "delivery":
                            coordinator = self.agent.tool_registry.mock_workflow
                            if coordinator is None:
                                raise ValueError("模拟站自动投递执行器未配置；不支持真实网站投递")
                            result = await asyncio.wait_for(coordinator.queue_deliveries(
                                min_score=action["scope"]["min_score"], session_id=run["session_id"], task_id=browser.id), timeout=15)
                            lines.append(result["message"])
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

    async def watch_delivery(self, run_id: str, *, task_id: str | None = None):
        if task_id is None:
            run = await self._background_io(self.store.get, run_id)
            task_id = run["checkpoint"]["delivery_id"]
        previous = None
        while True:
            view = await self._background_io(self._read_delivery_observation, run_id, task_id)
            if view is None:
                return
            # Liveness checks and any in-memory worker state stay on the loop.
            view = self.observe_delivery(task_id, view=view)
            key = (str(view.status), view.checkpoint, view.recruiter_reply, view.submitted, view.detail, view.takeover_reason)
            if key != previous:
                previous = key
                saved = await self._background_io(self._save_delivery_observation, run_id, task_id, view)
                if saved is None:
                    return
                view = saved
            if view.status in {"completed", "failed", "outcome_indeterminate", "cancelled"}:
                return
            await asyncio.sleep(1)

    def _read_delivery_observation(self, run_id: str, task_id: str):
        run = self.store.get(run_id)
        if (run["status"] in TERMINAL | {"cancelling"} or run["cancel_requested"]
                or (run.get("checkpoint") or {}).get("delivery_id") != task_id):
            return None
        return self.jobs.get_delivery_run(task_id)

    def _save_delivery_observation(self, run_id: str, task_id: str, view):
        # The background admission itself yields. Publish the latest durable
        # receipt, rather than settling a stale terminal/working snapshot.
        view = self.jobs.get_delivery_run(task_id)
        status = "waiting_user" if view.status == "waiting_for_user" else "waiting_reply" if view.status == "waiting_for_reply" else "running"
        data = {"task": view.model_dump(mode="json"), "status": status, "detail": view.detail}
        if not self.store.record_delivery_observation(run_id, task_id, data):
            return None
        if view.status in {"completed", "failed", "outcome_indeterminate", "cancelled"}:
            # A single bound queue lookup replaces scanning the entire batch for
            # each completed child. Older JobStore adapters retain their fallback.
            lookup = getattr(self.jobs, "_stored_delivery_queue_item", None)
            batch = lookup(view.job_id) if callable(lookup) else next(
                (item for item in self.jobs.mock_delivery_queue(active_only=False) if item["run_id"] == task_id), None)
            if batch and batch.get("run_id") == task_id and view.submitted and not self.store.has_event(run_id, "response"):
                message = f"模拟站自动投递 · 职位 #{view.job_id}：已核实简历投递完成。"
                self.store.event(run_id, "response", {"result": {"message": message, "delivery": view.model_dump(mode="json")}})
            self.finish(run_id, str(view.status), view.detail, result={"delivery": view.model_dump(mode="json")})
        return view

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

    def observe_delivery(self, task_id: str, *, view=None):
        view = view if view is not None else self.jobs.get_delivery_run(task_id)
        code = None
        alive = self.delivery and self.delivery.current().id == task_id and self.delivery.worker and not self.delivery.worker.done()
        if view.status == "waiting_for_user" and wait_expired(view.updated_at, self.watch_limits.human):
            # A live worker has a cooperative watchdog which releases its lease.
            if not alive:
                code = "human_timeout"
        elif view.status in {"starting", "running"}:
            if not alive:
                code = "worker_lost"
        if code:
            view = self.jobs.record_delivery_worker_loss(view, code)
            if self.delivery and self.delivery.current().id == task_id and (not self.delivery.worker or self.delivery.worker.done()):
                self.delivery.state = view
        return view

    def recover_search_summaries(self) -> None:
        """Backfill confirmed search reports only; never reopen a plan or authority."""
        for run in self.store.list():
            if run["kind"] == "daily":
                continue
            browser_id = (run["checkpoint"] or {}).get("browser_id")
            if not browser_id or self.store.has_event(run["id"], "browser_summary"):
                continue
            try:
                browser = self.jobs.get_task(browser_id)
            except KeyError:
                continue
            if browser.status == "completed":
                self.browser_summary(run["id"], browser, restore_display=False)

    def browser_summary(self, run_id: str, browser, *, restore_display: bool = True):
        run = self.store.get(run_id)
        if any(event["kind"] in {"browser_summary", "report_reference"} and event["data"].get("task_id") == browser.id for event in self.store.all_events(run_id)):
            return
        from ..jobs.search_reporting import render_search_report
        report_rendered = True
        try:
            report = self.jobs.search_report(browser.id)
            message = render_search_report(browser, report)
        except (KeyError, ValueError):
            report_rendered = False
            message = "搜索已结束；部分详情尚未读取，可在看板核对已保存结果。"
            report = {"jobs": []}
        self.jobs.add_conversation_message("assistant", message, conversation_id=run["session_id"], run_id=f"summary:{run_id}:{browser.id}")
        if restore_display and report.get("jobs"):
            self.store.mutate_dialogue(run["session_id"], {"op": "snapshot", "source": run_id,
                "topic": "applications", "kind": "job", "version": browser.id,
                "items": [{key: item[key] for key in ("id", "title", "company", "stage", "content_hash")}
                          for item in report["jobs"]], "displayed": True})
        self.store.event(run_id, "browser_summary", {"task_id": browser.id, "message": message, "discovered": browser.discovered,
                                                    "stored": browser.stored, "duplicates": browser.duplicates,
                                                    "report": report, "report_rendered": report_rendered, "statistics_changed": True})

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
        key = session_id if session_id is not None else "*"
        task = self.stop_jobs.get(key)
        if task is None:
            return {"status": "idle", "message": "尚无停止请求"}
        if not task.done():
            result = {"status": "stopping", "message": "正在停止执行器并核验队列"}
            context = self.stop_contexts.get(key, {})
            return self._with_stop_results(result, session_id, context, publish=False)
        if task.cancelled() or task.exception():
            return self._with_stop_results({"status": "failed", "message": "停止清理未确认，请核对运行记录"},
                                           session_id, self.stop_contexts.get(key, {}), publish=False)
        return task.result()

    async def stop_tasks(self, session_id: str | None, *, exclude_run: str | None = None,
                         conversation_id: str | None = None, message: str | None = None,
                         request_id: str | None = None) -> dict:
        key = session_id if session_id is not None else "*"
        owner = conversation_id or session_id or "default"
        # Validate even an empty session: no accidental fallback to another chat.
        self.store.dialogue_state(owner)
        if message:
            import uuid
            self.jobs.add_conversation_message("user", message, conversation_id=owner,
                run_id="task-stop-request:" + (request_id or uuid.uuid4().hex))
        task = self.stop_jobs.get(key)
        active = [run for run in self.store.list(session_id, active=True, limit=None)
                  if run["id"] != exclude_run and not (run.get("payload") or {}).get("control_kind")
                  and not (run["kind"] == "chat" and stop_scope((run.get("payload") or {}).get("message", "")))]
        queue = self.jobs.mock_delivery_queue(session_id=session_id) if hasattr(self.jobs, "mock_delivery_queue") else []
        if task is not None and task.done() and not task.cancelled() and not task.exception():
            previous = task.result()
            context = self.stop_contexts.get(key, {})
            if (previous.get("status") == "stopped" and not active and not queue
                    and context.get("conversation_id") == owner
                    and context.get("work_version") == self._stop_work_version(session_id)):
                # No new work does not imply no new evidence (e.g. a late
                # receipt). Re-read the same scope before reusing the response.
                try:
                    fresh = self.task_snapshot(session_id, run_ids=context.get("run_ids") or None)
                    if fresh["snapshot_id"] == previous.get("snapshot_id"):
                        return previous
                except Exception:
                    pass
        if task is None or task.done():
            self.stop_contexts[key] = {"conversation_id": owner, "run_ids": [run["id"] for run in active]}
            task = asyncio.create_task(self._stop_scope(session_id, exclude_run), name=f"stop-scope-{key}")
            self.stop_jobs[key] = task
        # Disconnects do not cancel cleanup. Clients poll until it is verified.
        await asyncio.wait({task}, timeout=1)
        return self.stop_status(session_id)

    def _stop_work_version(self, session_id: str | None) -> str:
        records = [(run["id"], run["status"], run.get("updated_at"))
                   for run in self.store.list(session_id, limit=None)
                   if not (run.get("payload") or {}).get("control_kind")
                   and (run.get("checkpoint") or {}).get("phase") != "control_result"
                   and not (run["kind"] == "chat" and stop_scope((run.get("payload") or {}).get("message", "")))]
        return hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()

    def _with_stop_results(self, result: dict, session_id: str | None, context: dict, *, publish: bool) -> dict:
        """Stopping and stopped views use the same saved evidence, never browser writes."""
        from .task_results import render_task_snapshot
        owner = context.get("conversation_id") or session_id or "default"
        try:
            snapshot = self.task_snapshot(session_id, run_ids=context.get("run_ids") or None)
            heading = "停止后的当前结果" if result["status"] == "stopped" else "停止尚未确认，以下为已保存结果"
            text = result["message"] + "\n\n" + render_task_snapshot(snapshot, heading=heading)
            identity = {"scope": session_id, "conversation_id": owner, "run_ids": snapshot["run_ids"], "counts": snapshot["counts"],
                        "jobs": snapshot["jobs"], "status": result["status"]}
            digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:32]
            message_id = "task-stop:" + digest if publish else None
            response = {**result, "result_message": text, "snapshot": snapshot,
                        "snapshot_id": snapshot["snapshot_id"], "message_id": message_id,
                        "conversation_id": owner}
        except Exception as exc:
            # A reporting error must not undo confirmed cancellation, or fabricate zero successes.
            response = {**result, "result_message": result["message"] +
                        f"\n当前结果暂不可读取（{type(exc).__name__}）；记录保留，可重试只读查询。",
                        "snapshot": None, "snapshot_id": None, "message_id": None, "conversation_id": owner}
        if publish:
            key = response.get("message_id") or "task-stop-unavailable:" + hashlib.sha256(
                json.dumps({"scope": session_id, "conversation_id": owner, "runs": context.get("run_ids", [])}, sort_keys=True).encode()).hexdigest()[:32]
            response["message_id"] = key
            self.jobs.add_conversation_message("assistant", response["result_message"],
                response={**response, "message": response["result_message"]}, conversation_id=owner, run_id=key)
        return response

    async def _stop_scope(self, session_id: str | None, exclude_run: str | None) -> dict:
        async with self.stop_lock:
            failures, stopped, uncertain = [], [], []
            coordinator = getattr(self.agent.tool_registry, "mock_workflow", None)
            runs = [run for run in self.store.list(session_id, active=True) if run["id"] != exclude_run
                    and not (run.get("payload") or {}).get("control_kind")
                    and not (run["kind"] == "chat" and stop_scope(run["payload"].get("message", "")))]
            key = session_id if session_id is not None else "*"
            context = self.stop_contexts.setdefault(key, {"conversation_id": session_id or "default"})
            context["run_ids"] = [run["id"] for run in runs]
            # Persist cancellation before yielding; no dependent plan step may proceed.
            for run in runs:
                self.store.update(run["id"], cancel_requested=1, status="cancelling")
                self.store.event(run["id"], "status", {"status": "cancelling", "detail": "停止当前会话及其后台队列"})
                if run["id"] in self.tasks:
                    self.tasks[run["id"]].cancel()
            if coordinator:
                # Daily cancellation normally publishes its own result. Scope stop
                # publishes one aggregate message instead of one reply per child.
                if self.daily:
                    for run in runs:
                        if run["kind"] == "daily":
                            try:
                                async with asyncio.timeout(10):
                                    await self.daily.cancel(run["id"], publish_result=False)
                            except Exception as exc:
                                failures.append(f"daily:{run['id']}:{type(exc).__name__}")
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
                        await self._stop_bound_tasks(run["id"], publish_results=False)
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
                bound = any((run.get("checkpoint") or {}).get(f"{kind}_id") == view.id or
                            kind == "browser" and (run.get("checkpoint") or {}).get("binding_browser_id") == view.id
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
            result = {"status": status, "message": message, "stopped_runs": len(stopped),
                      "cancelled_queue": queue.get("cancelled_queue", 0), "failed_runs": failures,
                      "uncertain_outcomes": uncertain}
            context["work_version"] = self._stop_work_version(session_id)
            return self._with_stop_results(result, session_id, context, publish=True)

    async def _stop_bound_tasks(self, run_id: str, *, publish_results: bool = True):
        if self.daily and self.store.get(run_id)["kind"] == "daily":
            await self.daily.cancel(run_id, publish_result=publish_results)
            return
        self.store.invalidate_approvals(run_id)
        task = self.tasks.get(run_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        current = self.store.get(run_id)
        browser_id = (current["checkpoint"] or {}).get("browser_id")
        binding_id = (current["checkpoint"] or {}).get("binding_browser_id")
        if binding_id and self.browser.current().id == binding_id:
            browser_id = binding_id
        if browser_id and self.browser.current().id == browser_id:
            view = await self.browser.stop()
            if publish_results:
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
                    unknown = step.get("effect") in {"write", "external"} and step.get("execution_phase") != "preparing"
                    uncertain |= unknown
                    step.update(status="outcome_indeterminate" if unknown else "skipped", detail="执行期间中断，结果需核对" if unknown else "已中断")
                elif step["status"] in {"pending", "waiting_external", "waiting_binding", "waiting_input", "waiting_confirmation", "waiting_approval"}:
                    step.update(status="skipped", withdrawn=True, failure={"code": "user_withdrawn", "effect_started": False, "uncertain": False},
                        detail="用户已停止，后续步骤不再执行；已产生的结果/提案保留")
            self.store.update(run_id, checkpoint=checkpoint)
            active = self.store.dialogue_state(current["session_id"]).get("instruction_request") or {}
            if active.get("id") == plan["id"]:
                self.store.mutate_dialogue(current["session_id"], {"op": "instruction", "request": {**active,
                    "status": "cancelled", "expires_at": 0, "steps": plan["steps"]}})
        else:
            uncertain = bool(current["mutation_started"] and not current["result"] and not browser_id and not delivery_id)
        self.finish(run_id, "outcome_indeterminate" if uncertain else "cancelled",
            "任务已停止；中断时正在执行的写入需核对，未自动重试" if uncertain else "任务已停止，已完成的步骤保留")

    def active_plan(self, session_id: str) -> dict | None:
        """Full durable plan, not a truncated chat or an old failed edit run."""
        candidates = [(run, (run.get("checkpoint") or {}).get("plan"))
                      for run in self.store.list(session_id, active=True, limit=None)]
        candidates = [(run, plan) for run, plan in candidates if plan and not run["cancel_requested"]
                      and plan.get("status") not in {"finished", "completed", "cancelled", "outcome_indeterminate"}]
        logical = {plan["id"] for _, plan in candidates}
        if len(logical) != 1:
            return None
        return copy.deepcopy(candidates[0][1])

    async def revise_plan(self, run_id: str, revision: dict, message: str):
        """Session-locked edits; terminal receipts are retained in child additions."""
        from .context import active_run, dialogue_change
        from ..core.models import ChatResponse, AgentLane
        from ..memory.resume_identity import resume_stamp
        from ..workflow.revision import RevisionDraft, edited_plan
        from ..workflow.requests import TaskRequest
        session_id = self.store.get(run_id)["session_id"]
        target = self.plan_for_revision(session_id, revision.get("plan_id"))
        if target is None:
            raise ValueError("没有唯一可修改的原任务")
        edit = RevisionDraft.model_validate(revision)
        stamp = resume_stamp(self.resume_workspace.read_document(), self.agent.candidate_state)
        updated = edited_plan(target, edit, self.agent.tool_registry, stamp=stamp, session_id=session_id, message=message)
        paused = updated["paused"]
        sources = [run for run in self.store.list(session_id, limit=None)
                   if ((run.get("checkpoint") or {}).get("plan") or {}).get("id") == target["id"]]
        owner = next((run for run in sources if run["status"] not in TERMINAL), None)
        existing_owner = owner is not None
        if owner is None:
            # Search may finish while the model is preparing an addition. Keep
            # that run immutable and continue only the new steps in this turn.
            source = sources[0]
            owner = self.store.get(run_id)
            for step in updated["steps"]:
                if step["status"] == "completed":
                    step["retained_result"] = True
            self.store.update(run_id, parent_id=source["id"])
            for event in self.store.all_events(source["id"]):
                if event["kind"] in {"browser_summary", "report_reference"}:
                    self.store.event(run_id, "report_reference", {"task_id": event["data"].get("task_id"),
                        "report_rendered": event["data"].get("report_rendered", True), "source_run_id": source["id"]})
        if not self.store.save_plan(owner["id"], updated,
                expected_revision=target.get("revision", 0) if existing_owner else None):
            raise ValueError("任务已结束，请重新读取执行记录")
        self.store.update(owner["id"], status="waiting_user" if paused else "running")
        changed_ids = [step["id"] for step in updated["steps"] if step.get("withdrawn") or
            any(old["id"] == step["id"] and (old["arguments"] != step["arguments"] or
                old.get("evidence_step") != step.get("evidence_step")) for old in target["steps"])]
        if changed_ids:
            self.store.invalidate_approvals(owner["id"], changed_ids)
        # A compact instruction is memory; the complete plan above remains the checkpoint.
        dialogue_change("instruction", request=updated)
        dialogue_change("task", kind="plan_id", id=updated["id"], status=updated["status"], bind=True)
        root = (active_run().dialogue or {}).get("task_request") or {}
        anchor = TaskRequest(original=updated["original"], run_id=owner["id"],
            origin_run_id=root.get("origin_run_id") or owner["id"], interpretation="model", status=updated["status"],
            resume_stamp=stamp, conditions=updated.get("verified_conditions", {}), execution_started=True,
            amendments=[{"message": item, "source": run_id, "fields": {}} for item in updated.get("answers", [])][-12:])
        dialogue_change("request_state", request=anchor.model_dump())
        self.store.event(owner["id"], "plan_revision", {"revision": updated["revision"], "source_run_id": run_id,
                         "updated_steps": [item.step_id for item in edit.updates],
                         "added_steps": updated["revision_history"][-1]["added_steps"],
                         "removed_steps": edit.remove_steps, "paused": paused})
        browser_id = (owner["checkpoint"] or {}).get("browser_id")
        if paused and browser_id and self.browser.current().id == browser_id and self.browser.current().status in {"starting", "running"}:
            await self.browser.pause()
        if existing_owner and owner["status"] == "waiting_user" and not paused:
            self.schedule(owner["id"], watch_only=True)
        details = []
        for item in edit.updates:
            step = next(step for step in updated["steps"] if step["id"] == item.step_id)
            args = step["arguments"]
            if step.get("staged_arguments") is not None:
                details.append("新搜索条件已暂存；继续时会安全结束旧只读搜索并按新条件启动，旧请求与已有结果保留")
            else:
                from ..core.replies import tool_label
                details.append(tool_label(step["tool"]) + "的未执行参数已更新")
        for item in edit.add_steps:
            from ..core.replies import tool_label
            details.append("已追加" + tool_label(item.tool) + "，将在其前置步骤完成后执行")
        for step_id in edit.remove_steps:
            from ..core.replies import tool_label
            step = next(item for item in updated["steps"] if item["id"] == step_id)
            details.append("已取消后续" + tool_label(step["tool"]))
        return ChatResponse(message=("已暂停原任务的新动作；已有进度保留。" if paused else "原任务已更新。") +
            ("\n\n" + "\n".join(details) if details else "") +
            "\n\n已有结果及任务依赖保留。" + ("可说「继续任务」。" if paused else ""), lane=AgentLane.APPLICATION)

    def plan_for_revision(self, session_id: str, plan_id: str | None) -> dict | None:
        if not plan_id:
            return self.active_plan(session_id)
        for run in self.store.list(session_id, limit=None):
            plan = (run.get("checkpoint") or {}).get("plan") or {}
            if plan.get("id") == plan_id and not run["cancel_requested"]:
                return copy.deepcopy(plan)
        return None

    async def continue_task(self, run_id: str, message: str):
        """Resolve a user continuation against durable plans before model routing."""
        from .context import active_run, dialogue_change
        from ..core.models import ChatResponse, AgentLane
        from ..workflow.recovery import recover_plan
        from ..workflow.execution import begin
        from ..workflow.requests import request_selector, TaskRequest
        from ..memory.resume_identity import resume_stamp
        context = active_run()
        run = self.store.get(run_id)
        # Recovery uses execution records, not the UI's recent-500 history window.
        records = [item for item in self.store.list(run["session_id"], limit=None) if item["id"] != run_id]
        root = (context.dialogue or {}).get("task_request") or {}
        explicit = (run["checkpoint"] or {}).get("resume_source_id")
        source_id = explicit or root.get("run_id") or root.get("origin_run_id")
        candidates = [item for item in records if (item.get("checkpoint") or {}).get("plan")]
        if request_selector(message) == "first":
            epoch = (context.dialogue or {}).get("history_epoch", 0)
            from datetime import datetime
            candidates = [item for item in reversed(candidates)
                          if datetime.fromisoformat(item["created_at"]).timestamp() >= epoch]
            source_id = candidates[0]["id"] if candidates else source_id
        source = next((item for item in records if item["id"] == source_id), None)
        if source is None:
            if not candidates:
                return None  # No executed plan: the original no-effect planner path handles it.
            logical_tasks = {item["checkpoint"]["plan"]["id"] for item in candidates}
            if len(logical_tasks) != 1:
                return ChatResponse(message="有多个执行计划，无法唯一确定接续对象；请在运行记录中选择任务。", lane=AgentLane.APPLICATION)
            source = candidates[0]
        if source["session_id"] != run["session_id"]:
            return ChatResponse(message="未恢复其他会话的任务。", lane=AgentLane.APPLICATION)
        logical = ((source.get("checkpoint") or {}).get("plan") or {}).get("id")
        # Pick the latest descendant, not an older failed attempt that could replay a
        # step already completed by a previous explicit continuation.
        descendants = {source["id"]}
        for _ in records:
            descendants.update(item["id"] for item in records if item.get("parent_id") in descendants
                and ((item.get("checkpoint") or {}).get("plan") or {}).get("id") == logical)
        source = next(item for item in records if item["id"] in descendants)
        self.store.event(run_id, "recovery_resolution", {"source_run_id": source["id"], "plan_id": logical})
        worker = self.tasks.get(source["id"])
        if source["status"] not in TERMINAL and worker and not worker.done():
            saved = source["checkpoint"] or {}
            active_plan = saved.get("plan") or {}
            if active_plan.get("paused"):
                stamp = resume_stamp(self.resume_workspace.read_document(), self.agent.candidate_state)
                if active_plan.get("resume_stamp") != stamp:
                    return ChatResponse(message="简历已变化，请确认新的执行范围；原任务仍暂停。", lane=AgentLane.APPLICATION)
                from ..workflow.lifecycle import activate_staged_searches
                await activate_staged_searches(active_plan, self.browser)
                active_plan.update(paused=False, status="running", revision=active_plan.get("revision", 0) + 1,
                                   expires_at=time.time() + 1800)
                self.store.update(source["id"], checkpoint={**saved, "plan": active_plan}, status="running")
                dialogue_change("instruction", request=active_plan)
                dialogue_change("task", kind="plan_id", id=active_plan["id"], status="running", bind=True)
            browser_id = (source["checkpoint"] or {}).get("browser_id")
            if browser_id and self.browser.current().id == browser_id and self.browser.current().status in {"paused", "waiting_for_user"}:
                await self.browser.resume()
                return ChatResponse(message="已继续原任务的浏览器执行器，正在核验登录和页面；没有重新规划或重复执行。", lane=AgentLane.APPLICATION)
            if any(step["status"] == "waiting_approval" for step in active_plan.get("steps", [])):
                return ChatResponse(message="原任务有动作等待范围确认，请在运行记录中确认或修改；接续不会重复执行已完成步骤。", lane=AgentLane.APPLICATION)
            return ChatResponse(message="原任务仍在执行或等待招聘方回复；没有启动重复任务。", lane=AgentLane.APPLICATION)
        stamp = resume_stamp(self.resume_workspace.read_document(), self.agent.candidate_state)
        decision = recover_plan(source, session_id=run["session_id"], stamp=stamp,
            events=self.store.all_events(source["id"]),
            revoked_tools=root.get("revoked_tools", []) if root.get("original") == ((source.get("checkpoint") or {}).get("plan") or {}).get("original") else ())
        self.store.event(run_id, "recovery_decision", {"action": decision.action, "source_run_id": source["id"], "detail": decision.message})
        if decision.action == "no_plan":
            return None
        if decision.action != "resume":
            return ChatResponse(message=decision.message, lane=AgentLane.APPLICATION)
        plan = decision.plan
        from ..workflow.lifecycle import activate_staged_searches
        await activate_staged_searches(plan, self.browser)
        for step in plan["steps"]:
            if step.get("approval_id"):
                old = self.store.approval(step["approval_id"])
                if old["run_id"] != run_id and old["status"] in {"pending", "approved"}:
                    transferred = self.store.ensure_approval(run_id, run["session_id"], step["id"], old["snapshot"],
                        requested=old["status"] == "approved")
                    step["approval_id"] = transferred["id"]
                    self.store.invalidate_approvals(source["id"], [step["id"]])
        # Uncertain external searches can be attached by their durable task ID. Dead
        # search workers are restarted only by the explicitly authorized search step.
        for step in plan["steps"]:
            if step["tool"] == "job_search_start" and step["status"] == "waiting_external":
                task = self.jobs.get_task(step["task_id"])
                if task.status == "completed":
                    step.update(status="completed", retained_result=True)
                elif self.browser.can_start_search():
                    step.update(status="pending", resume_search_task_id=step["task_id"])
        self.store.update(run_id, parent_id=source["id"], checkpoint={"phase": "action_plan", "plan": plan,
            "recovery_source_id": source["id"]})
        for event in self.store.all_events(source["id"]):
            if event["kind"] in {"browser_summary", "report_reference"}:
                self.store.event(run_id, "report_reference", {"task_id": event["data"].get("task_id"),
                    "report_rendered": event["data"].get("report_rendered", True), "source_run_id": source["id"]})
        # Install an immutable task anchor for the new attempt without parsing chat
        # history, importing expired tokens or changing the original parameters.
        anchor = TaskRequest(original=plan["original"], run_id=run_id,
            origin_run_id=root.get("origin_run_id") or source["id"], interpretation="model",
            status="running", resume_stamp=stamp, revoked_tools=plan.get("revoked_tools", []),
            conditions=plan.get("verified_conditions", {}))
        dialogue_change("request_state", request=anchor.model_dump())
        response = await begin(plan, self.agent.tool_registry)
        response.message = decision.message + "\n\n" + response.message
        return response

    async def resume(self, run_id: str) -> dict:
        if self.daily and self.store.get(run_id)["kind"] == "daily":
            return await self.daily.resume(run_id)
        run = self.store.get(run_id)
        if (run["checkpoint"] or {}).get("plan"):
            import uuid
            child = self.submit(run["session_id"], f"continue:{run_id}:{uuid.uuid4().hex}", "chat",
                {"message": "继续任务"}, parent_id=run_id)
            self.store.update(child["id"], checkpoint={"resume_source_id": run_id})
            return self.store.get(child["id"])
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
            events = self.store.public_events(run_id, cursor)
            for event in events:
                cursor = event["seq"]
                yield f"id: {cursor}\nevent: {event['kind']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            if self.store.get(run_id)["status"] in TERMINAL and not events:
                return
            if not events:
                yield ": heartbeat\n\n"
            await asyncio.sleep(0.4)
