from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class RunContext:
    run_id: str
    owner: asyncio.Task
    emit: Callable[[str, dict], None]
    save: Callable[[dict], None]
    restored: dict | None = None
    session_id: str = "default"
    search_state: dict | None = None
    persist_search: Callable[[dict], dict] | None = None
    routing_context: str = ""
    dialogue: dict | None = None
    mutate_dialogue: Callable[[dict], dict] | None = None
    retrieve_history: Callable[[str, int], list[dict]] | None = None
    request_records: Callable[[], list[dict]] | None = None
    resume_task: Callable | None = None
    enforce_task_scope: bool = False
    user_message: str = ""
    # Only the validated plan runner may install a single-step authority. Model
    # tool arguments and historical messages cannot populate this field.
    action_authority: dict | None = None


current_run: ContextVar[RunContext | None] = ContextVar("autohunt_run", default=None)


def active_run() -> RunContext | None:
    context = current_run.get()
    # Browser/scoring workers inherit contextvars, but own separate lifecycles.
    return context if context and context.owner is asyncio.current_task() else None


def emit(kind: str, **data: Any) -> None:
    context = active_run()
    if context:
        context.emit(kind, data)


def checkpoint(data: dict) -> None:
    context = active_run()
    if context:
        context.save(data)


def save_search_state(value: dict, *, user_edit: bool | dict = False) -> dict:
    context = active_run()
    if not context:
        return value
    context.search_state = context.persist_search(value) if context.persist_search else value
    emit("search_state", conditions=context.search_state)
    dialogue_change("slots", conditions=context.search_state)
    if user_edit is not False:
        from ..workflow.requests import amended_request
        fields = set(user_edit) if isinstance(user_edit, dict) else None
        if fields is not None and "use_resume_profile" in fields:
            fields.update({"queries", "query_source", "resume_stamp"})
        if fields is not None and "queries" in fields:
            fields.update({"query_source", "resume_stamp"})
        amended = amended_request(context.dialogue or {}, context.search_state, context.user_message, context.run_id, fields)
        if amended:
            dialogue_change("request_state", request=amended.model_dump())
    return context.search_state


def dialogue_change(op: str, **data) -> dict:
    from .dialogue import apply_operation, compact_state
    context = active_run()
    if context is None:
        return {}
    operation = {"op": op, "source": context.run_id, **data}
    context.dialogue = (context.mutate_dialogue(operation) if context.mutate_dialogue else
                        apply_operation(context.dialogue, operation))
    emit("dialogue_state", state=compact_state(context.dialogue))
    return context.dialogue


def scoped_browser(browser):
    """Global browser serialization does not imply permission to operate another session's task."""
    context = active_run()
    view = browser.current()
    if context and context.enforce_task_scope:
        owned = (context.dialogue or {}).get("tasks", {}).get("browser_id")
        if view.id and view.id != owned:
            raise ValueError("当前浏览器任务不属于本会话；请切换到发起任务的会话")
    return view


def browser_search_available(browser) -> bool:
    probe = getattr(browser, "can_start_search", None)
    if callable(probe):
        return probe()
    # Test doubles / alternative providers without a live-worker probe.
    return browser.current().status in {"idle", "completed", "failed", "stopped"}


def browser_availability(browser) -> dict:
    """Shared availability is readable; another session's task details are not."""
    context = active_run()
    view = browser.current()
    owned_id = (context.dialogue or {}).get("tasks", {}).get("browser_id") if context else None
    owned = not (context and context.enforce_task_scope and view.id and view.id != owned_id)
    available = browser_search_available(browser)
    if owned:
        result = view.model_dump(mode="json")
    else:
        result = {"id": None, "status": "idle" if available else str(view.status),
                  "detail": ("共享浏览器空闲，可以启动本会话搜索；旧任务详情仅在所属会话可见" if available else
                             "共享浏览器有其他会话的任务正在执行、等待人工接管或清理；请在所属会话处理")}
    if available and view.status not in {"idle", "completed", "failed", "stopped"}:
        result.update(status="idle", stale_task_record=True,
                      detail="旧任务没有存活的执行器；启动新搜索时将安全结束旧记录")
    return {**result, "task_owned_by_session": owned, "can_start_search": available,
            "shared_browser_busy": not available}
