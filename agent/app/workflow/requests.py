"""Task references are resolved before routing; history is not a checkpoint.

A failed, never-executed request can be planned again on an explicit retry.
An observed write or external action can never be replayed by this resolver.
"""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field


class TaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    run_id: str = ""
    original: str = Field(min_length=1, max_length=12000)
    status: str = "planning"
    resume_stamp: str = ""
    conditions: dict = Field(default_factory=dict)
    execution_started: bool = False
    failure: str = ""
    updated_at: float = Field(default_factory=time.time)


CONTINUE = re.compile(r"^(?:(?:请|帮我)\s*)?(?:继续|恢复|接着|重试|重新尝试|再试一次|再试一下|重新执行|继续执行)(?:一下)?(?:上(?:一|个|次)?(?:文|面|条)?|刚才|之前|原来|这个|该|当前)?(?:的)?(?:任务|请求|操作|执行|搜索|检索)?[。！!\s]*$")


def is_continuation(message: str) -> bool:
    return bool(CONTINUE.fullmatch(message.strip()))


def action_request(message: str) -> bool:
    """Coarse task focus only, never grants execution authority."""
    from .planning import command_grounded, MUTATION_CUES
    clauses = re.split(r"[，,。；;\n]|然后|并且|同时|接着", message)
    return any(command_grounded(tool, clause.strip(), [message])
               for tool in MUTATION_CUES for clause in clauses)


def failed_history_request(history: list[dict]) -> TaskRequest | None:
    """Legacy migration: only pair a user request with an explicit no-execution result.

    Never infer a task from JD/resume/tool data or from a model's success claim.
    A later actionable user command supersedes the earlier failed request.
    """
    from ..runtime.dialogue import REJECT
    from ..core.search_language import parse_search_message
    last, return_candidate = None, None
    for item in history:
        role, text = item.get("role"), str(item.get("content", ""))
        if role == "user" and (REJECT.fullmatch(text.strip()) or text.strip() in {"退出当前任务", "不办了", "重置上下文", "清空会话状态", "重新开始"}
                               or parse_search_message(text).action == "stop"):
            last, return_candidate = None, None
            continue
        if role == "user" and not is_continuation(text) and action_request(text):
            last = text
            return_candidate = None
        elif role == "assistant" and last and re.match(r"^(?:没有执行任务[:：]|任务规划暂不可用)", text):
            return_candidate = TaskRequest(original=last, status="failed", failure="历史中的规划失败，未执行")
            last = None
        elif role == "assistant" and last:
            return_candidate = None
            last = None
    return return_candidate if last is None else None


def employment_dropped(previous: dict | None, answers: list[str]) -> bool:
    if any(re.search(r"(?:去掉|忽略|取消|不限制).*(?:招聘类型|实习|校招|这个条件|此条件)", answer) for answer in answers):
        return True
    from ..runtime.dialogue import CONFIRM
    questions = previous.get("questions", []) if previous else []
    return bool(answers and len(questions) == 1 and "是否去掉招聘类型" in questions[0] and CONFIRM.fullmatch(answers[-1].strip()))


@dataclass(frozen=True)
class Continuation:
    action: str
    message: str = ""
    original: str = ""


def resolve_continuation(saved: dict, browser, stamp: str, *, now: float | None = None) -> Continuation:
    now = time.time() if now is None else now
    record = TaskRequest.model_validate(saved["task_request"]) if saved.get("task_request") else None
    view = browser.current() if browser is not None else None
    owned = bool(view and view.id and saved.get("tasks", {}).get("browser_id") == view.id)
    from ..runtime.context import browser_search_available
    live = bool(owned and not browser_search_available(browser))
    if record and record.status == "clarifying":
        questions = (saved.get("instruction_request") or {}).get("questions", [])
        return Continuation("clarify", "上文任务还有条件待确认，尚未执行。" + ("请补充：" + "；".join(questions) if questions else "请回答上一轮的追问。"))
    # An unresolved new request and an older live browser task are different referents.
    if record and record.status in {"planning", "failed", "timed_out", "interrupted"} and not record.execution_started:
        if live:
            return Continuation("clarify", "上文请求尚未执行，但本会话还有浏览器任务在运行。请明确继续原请求还是处理浏览器任务。")
        if record.resume_stamp and record.resume_stamp != stamp:
            return Continuation("clarify", "原请求保留，但当前简历已变更。请确认是否按新简历重新执行原请求；尚未启动任何操作。")
        if now-record.updated_at > 86400:
            return Continuation("clarify", "原请求已经较久未处理，请重新确认完整条件；不会自动重放旧任务。")
        return Continuation("replan", original=record.original)
    if live and str(view.status) in {"paused", "waiting_for_user"}:
        return Continuation("browser")
    if live:
        return Continuation("running", "本会话的浏览器任务仍在运行，没有启动重复搜索。")
    if record and record.execution_started:
        return Continuation("clarify", "原任务已经开始执行，但当前没有可恢复的浏览器执行器。请先核对已完成步骤；如需新一轮搜索，请明确说重新搜索，不会重放投递或写入。")
    return Continuation("clarify", "当前没有可确定的未完成任务。请说明要继续哪项请求，或明确开始新的搜索；没有执行浏览器操作。")
