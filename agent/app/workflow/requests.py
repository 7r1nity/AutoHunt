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
    origin_run_id: str = ""
    original: str = Field(min_length=1, max_length=12000)
    status: str = "planning"
    resume_stamp: str = ""
    conditions: dict = Field(default_factory=dict)
    amendments: list[dict] = Field(default_factory=list, max_length=12)
    revoked_tools: list[str] = Field(default_factory=list, max_length=12)
    condition_overrides: dict = Field(default_factory=dict)
    execution_started: bool = False
    failure: str = ""
    updated_at: float = Field(default_factory=time.time)


CONTINUE = re.compile(
    r"^(?:(?:请|帮我|麻烦你|我要你|我希望你|你)\s*)?"
    r"(?:继续执行|重新执行|重新尝试|再试一次|再试一下|继续|恢复|接着(?:执行)?|重试)(?:一下)?"
    r"(?:(?:我)?(?:第(?:一|1)(?:次|个|条)|最初|最开始|上(?:一|个|次)?(?:文|面|条)?|刚才|之前|原来|这个|该|当前|前面)"
    r"(?:(?:发送|发|提出|说)(?:给你)?(?:过)?)?)?(?:的)?"
    r"(?:任务|请求|操作|执行|搜索|检索)?(?:吧)?[。！!\s]*$")


def is_continuation(message: str) -> bool:
    return bool(CONTINUE.fullmatch(message.strip()))


def action_request(message: str) -> bool:
    """Coarse task focus only, never grants execution authority."""
    from ..core.request_summary import is_request_summary, is_search_summary
    if is_continuation(message) or is_request_summary(message) or is_search_summary(message) or not re.search(r"[\w\u4e00-\u9fff]", message):
        return False  # References and repair signals are not new task definitions.
    from .planning import command_grounded, MUTATION_CUES
    clauses = re.split(r"[，,。；;\n]|然后|并且|同时|接着", message)
    return any(command_grounded(tool, clause.strip(), [message])
               for tool in MUTATION_CUES if tool not in {"browser_task_resume", "browser_task_pause", "browser_task_stop", "search_state_update"}
               for clause in clauses)


def request_selector(message: str) -> str:
    return "first" if is_continuation(message) and re.search(r"第(?:一|1)(?:次|个|条)|最初|最开始", message) else "recent"


def capture_request(saved: dict, message: str, run_id: str, stamp: str, conditions: dict, *, retry: bool = False) -> TaskRequest:
    """All execution paths use the same immutable user-task anchor.

    Retrying the same never-executed task keeps its identity and amendments.
    A new command, including re-search, is a new task, not old write authority.
    """
    old = saved.get("task_request") or {}
    if old.get("original") == message and (retry or old.get("run_id") == run_id) and not old.get("execution_started") and old.get("status") not in {"finished", "cancelled", "outcome_indeterminate"}:
        return TaskRequest.model_validate({**old, "run_id": run_id, "status": "planning", "failure": "",
            "resume_stamp": old.get("resume_stamp") or stamp})
    from ..core.search_contract import explicit_search_constraints
    try:
        explicit = explicit_search_constraints(message)
    except ValueError:
        explicit = {}  # Conflict remains for the planner/executor to reject.
    return TaskRequest(original=message, run_id=run_id, origin_run_id=run_id,
        resume_stamp=stamp, conditions={**conditions, **explicit})


def amended_request(saved: dict, conditions: dict, message: str, source: str, fields: set[str] | None = None) -> TaskRequest | None:
    """Only an observed, validated session edit updates requirements; no authority."""
    from ..core.search_language import parse_search_message
    root = saved.get("task_request") or {}
    if not root or not parse_search_message(root.get("original", "")).authorized:
        return None
    changed = {key: value for key, value in conditions.items()
        if key in {"queries", "locations", "result_limit", "salary_min", "max_jobs", "query_source", "resume_stamp"}
        and (fields is None or key in fields)
        and root.get("conditions", {}).get(key) != value}
    if not changed:
        return None
    amendments = root.get("amendments", [])
    if message and source != root.get("run_id"):
        amendments = [*amendments, {"message": message, "source": source, "fields": changed}][-12:]
    return TaskRequest.model_validate({**root, "conditions": {**root.get("conditions", {}), **changed},
        "condition_overrides": {**root.get("condition_overrides", {}), **changed}, "amendments": amendments})


def withdrawn_request(saved: dict, message: str) -> TaskRequest | None:
    from .planning import MUTATION_CUES, command_grounded, revoked
    root = saved.get("task_request") or {}
    if not root:
        return None
    clauses = re.split(r"[，,。；;\n]|然后|并且|同时|接着", root.get("original", ""))
    withdrawn = {tool for tool in MUTATION_CUES if revoked(tool, [root["original"], message])
        and any(command_grounded(tool, clause.strip(), [root["original"]]) for clause in clauses)}
    new = withdrawn - set(root.get("revoked_tools", []))
    return TaskRequest.model_validate({**root, "revoked_tools": sorted(set(root.get("revoked_tools", [])) | new)}) if new else None


def proven_idle_resume(events: list[dict]) -> bool:
    """Narrow legacy no-op proof, never use a model's success/failure prose.

    The old runtime marked a resume attempt as a mutation even when the service
    returned id=None/status=idle. Any other or unpaired effect remains uncertain.
    """
    effects = [event for event in events if event.get("kind") == "tool_started" and event.get("effect") != "read"]
    finished = [event for event in events if event.get("kind") == "tool_finished" and event.get("tool") == "browser_task_resume"]
    return bool(effects and len(effects) == len(finished) and
        all(event.get("tool") == "browser_task_resume" for event in effects) and
        all(event.get("status") == "ok" and isinstance(event.get("result"), dict) and
            event["result"].get("id") is None and event["result"].get("status") == "idle" for event in finished))


def canonical_request(saved: dict, history: list[dict], records: list[dict] | None = None, *, selector="recent") -> TaskRequest | None:
    """Select observed user task facts, not an assistant's interpretation of a pronoun.

    Runtime records are session scoped and bounded independently of model history.
    They provide execution evidence; no historical authorization/token is imported.
    """
    current = saved.get("task_request") or {}
    if selector != "first" and action_request(current.get("original", "")):
        return TaskRequest.model_validate(current)
    from datetime import datetime
    from ..runtime.dialogue import REJECT
    from ..core.search_language import parse_search_message
    candidates = []
    epoch = saved.get("history_epoch", 0)
    if selector == "first" and records and len(records) >= 500 and datetime.fromisoformat(records[0]["created_at"]).timestamp() > epoch:
        return None  # The first record of a bounded window is not necessarily the first task.
    for item in records or []:  # chronological order from RunStore, not model output
        created = datetime.fromisoformat(item["created_at"]).timestamp()
        if created < epoch:
            continue
        text = item.get("message", "").strip()
        if (REJECT.fullmatch(text) or text in {"退出当前任务", "不办了", "重置上下文", "清空会话状态", "重新开始"}
                or parse_search_message(text).action == "stop"):
            for candidate in candidates:
                if candidate.status in {"failed", "timed_out", "clarifying", "planning", "interrupted"}:
                    candidate.status = "cancelled"
            if text in {"重置上下文", "清空会话状态", "重新开始"}:
                candidates = []
            continue
        if not action_request(text):
            # A later continuation may have begun effects. Do not replan the
            # old never-executed request merely because its own run had no writes.
            if candidates and is_continuation(text) and item.get("execution_started"):
                target = candidates[0] if request_selector(text) == "first" else candidates[-1]
                target.execution_started = True
                target.status = "finished" if item["status"] == "completed" else item["status"]
            continue
        reply = item.get("reply", "")
        status = item["status"]
        no_execution = not item.get("execution_started")
        if no_execution and re.match(r"^(?:没有执行任务[:：]|任务规划暂不可用)", reply):
            status = "timed_out" if "规划超时" in reply else "failed"
        elif no_execution and status == "completed" and reply.startswith("已记录完整请求，尚未执行"):
            status = "clarifying"
        elif status == "completed":
            status = "finished"
        from ..core.search_contract import explicit_search_constraints
        try:
            conditions = explicit_search_constraints(text)
        except ValueError:
            conditions = {}  # Conflicts are revalidated before execution.
        candidates.append(TaskRequest(id="request:"+item["id"], run_id=item["id"], origin_run_id=item["id"],
            original=text, status=status, conditions=conditions, execution_started=not no_execution,
            resume_stamp=item.get("resume_stamp") or "", updated_at=created))
    if candidates:
        return candidates[0] if selector == "first" else candidates[-1]
    if records is not None:
        return None  # A reset/empty authoritative store must not revive raw chat history.
    # Local/legacy callers without a run store can recover only an explicitly
    # paired failure, never a guessed action from raw assistant/JD text.
    return failed_history_request(history)


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
    if record and record.status in {"finished", "completed", "cancelled", "outcome_indeterminate"}:
        return Continuation("clarify", "所指原任务已经结束、取消或结果待核验；不会恢复另一个任务或重放历史操作。请明确新的请求，或先核对执行记录。")
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
