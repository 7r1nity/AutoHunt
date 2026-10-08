"""Session DST. Only validated user edits and observed tool results become state.

Extractive traces are navigation aids, never instructions or durable profile facts.
External submission authority remains in the delivery preview/one-use token layer.
"""
from __future__ import annotations

import copy
import json
import re
import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..core.security import redact

Topic = Literal["search", "applications", "resume", "learning", "general"]
ACTION_TOOLS = {"job_search_start", "learning_plan_generate", "resume_propose_patch"}
CONFIRM = re.compile(r"^(?:行吧[，, ]*)?(?:好|好的|行|行吧|可以|确认|同意|就这样|照你说的办|那就照你说的办|按这个方案|开始吧)[。！! ]*$")
REJECT = re.compile(r"^(?:不|不要|算了|取消|不执行|不同意|先不做|先不要)[。！! ]*$")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Frame(Strict):
    slots: dict[str, Any] = Field(default_factory=dict)
    missing_slots: list[str] = Field(default_factory=list, max_length=12)
    last_question: str = Field(default="", max_length=1000)
    question_source: str = Field(default="", max_length=128)
    question_turn: int = 0
    question_expires_at: float = 0
    slot_sources: dict[str, dict] = Field(default_factory=dict)


class Snapshot(Strict):
    id: str
    topic: Topic
    kind: Literal["job", "resume_entry", "plan"]
    source: str
    turn: int
    expires_at: float
    version: str = ""
    displayed: bool = True
    items: list[dict[str, Any]] = Field(max_length=200)


class Pending(Strict):
    id: str
    topic: Topic
    tool: str
    arguments: dict[str, Any]
    description: str = Field(max_length=500)
    source: str
    turn: int
    expires_at: float
    search_revision: int
    resume_stamp: str
    status: Literal["pending", "claimed", "consumed", "rejected", "expired", "stale"] = "pending"


class JobSelection(Strict):
    job_id: int
    snapshot_id: str
    selector: str
    title: str
    company: str


class DialogueState(Strict):
    schema_version: Literal[1] = 1
    revision: int = 0
    turn: int = 0
    last_activity: float = 0
    history_epoch: float = 0
    active_intent: Topic = "general"
    frames: dict[str, Frame] = Field(default_factory=dict)
    snapshots: list[Snapshot] = Field(default_factory=list, max_length=8)
    pending: list[Pending] = Field(default_factory=list, max_length=8)
    tasks: dict[str, str] = Field(default_factory=dict)
    trace: list[dict[str, Any]] = Field(default_factory=list, max_length=32)
    summary: list[dict[str, Any]] = Field(default_factory=list, max_length=24)
    clarification: dict[str, Any] | None = None
    selected_job: JobSelection | None = None
    learning_request: dict[str, Any] | None = None


def topic_for(message: str, previous: str = "general") -> str:
    # This is a coarse focus hint, NOT an intent router or authorization check.
    if CONFIRM.fullmatch(message.strip()) or REJECT.fullmatch(message.strip()):
        return previous
    if message.strip().strip("。!?！？") in {"看看我的情况", "看看情况", "现在呢", "怎么样", "这样是否合理", "进展怎么样", "这个怎么样"}:
        return previous
    if re.search(r"简历|项目经历|教育经历|实习经历", message):
        return "resume"
    if re.search(r"学习|成长计划|行动方案", message):
        return "learning"
    if re.search(r"审核|投递|沟通|回复|看板|前[一二三四五六七八九十\d]+个|第[一二三四五六七八九十\d]+个", message):
        return "applications"
    if re.search(r"搜索|找工作|帮我找|职位|岗位|薪资|月薪|城市|数量|关键词|换成|改成", message):
        return "search"
    if re.fullmatch(r"(?:那就|还是|再|也|换|改|那个|它|这些).{0,24}", message.strip()):
        return previous
    return "general"


def trace(state: DialogueState, kind: str, source: str, **data):
    item = {"turn": state.turn, "kind": kind, "source": source, **redact(data)}
    state.trace.append(item)
    if len(state.trace) > 32:
        old = state.trace.pop(0)
        # Compress observed operations and user decisions, never model claims.
        if old["kind"] in {"tool", "condition_update", "rejection", "confirmation", "topic"}:
            state.summary = [*state.summary, old][-24:]


def apply_operation(saved: dict | None, operation: dict, *, clock: float | None = None) -> dict:
    state = DialogueState.model_validate(saved or {})
    now = time.time() if clock is None else clock
    op = operation["op"]
    source = str(operation.get("source", ""))[:128]
    for item in state.pending:
        if item.status == "pending" and item.expires_at < now:
            item.status = "expired"
    if op == "begin":
        # Idle time is not an instruction to forget conditions or displayed
        # results. Only per-action authority/questions expire independently.
        state.last_activity = now
        state.turn += 1
        for frame in state.frames.values():
            if frame.question_expires_at and (frame.question_expires_at < now or state.turn-frame.question_turn > 4):
                frame.last_question, frame.missing_slots = "", []
        message = str(operation["message"])
        next_topic = topic_for(message, state.active_intent)
        if state.learning_request and state.active_intent == "learning":
            from ..core.action_language import plan_scope_answer
            try:
                if plan_scope_answer(message):
                    next_topic = "learning"
            except ValueError:
                next_topic = "learning"  # Invalid slot values are clarified, never a reset.
        if state.learning_request and next_topic != "learning":
            state.learning_request = None
        frame = state.frames.get(state.active_intent)
        if frame and frame.missing_slots and re.fullmatch(r"[\d一二三四五六七八九十]+(?:个|份|条)?", message.strip()):
            next_topic = state.active_intent
        if frame and frame.missing_slots == ["queries"]:
            from ..core.search_contract import safe_role_keyword
            if safe_role_keyword(message.strip()):
                next_topic = state.active_intent
        if next_topic != state.active_intent:
            trace(state, "topic", source, previous=state.active_intent, current=next_topic)
        state.active_intent = next_topic
        # A clarification click is bound to its original question, not last user text.
        if not message.startswith("【澄清】选择："):
            state.clarification = None
        trace(state, "request", source, text=message[:600])
    elif op == "slots":
        conditions = copy.deepcopy(operation["conditions"])
        previous = state.frames.get("search", Frame())
        sources = {key: previous.slot_sources.get(key, {}) if previous.slots.get(key) == value else
            {"source": source, "turn": state.turn, "origin": conditions.get("query_source", "resume") if key == "queries" else "validated_search_state"}
            for key, value in conditions.items()}
        if previous.slots == conditions:
            return state.model_dump(mode="json")
        state.frames["search"] = Frame(slots=conditions,
            missing_slots=[] if conditions.get("queries") else ["queries"],
            last_question="请说明目标岗位，或上传简历以提供方向。" if not conditions.get("queries") else "",
            slot_sources=sources)
        if previous.slots != conditions:
            trace(state, "condition_update", source, slots=conditions)
    elif op == "snapshot":
        items = copy.deepcopy(operation["items"])
        if any("id" not in item for item in items):
            raise ValueError("引用对象必须有稳定 ID")
        snapshot = Snapshot(id=uuid.uuid4().hex, topic=operation["topic"], kind=operation["kind"],
            source=source, turn=state.turn, expires_at=now + 1800,
            version=str(operation.get("version", "")), items=items[:200], displayed=operation.get("displayed", True))
        state.snapshots = [*state.snapshots, snapshot][-8:]
        state.active_intent = snapshot.topic
        if snapshot.kind == "job" and snapshot.displayed:
            state.selected_job = None
    elif op == "display":
        item = next((item for item in state.snapshots if item.id == operation["snapshot_id"]), None)
        if item is None:
            raise ValueError("展示快照不存在")
        item.displayed = True
        item.turn = state.turn
        if item.kind == "job":
            state.selected_job = None
    elif op == "select_job":
        snapshot = next((item for item in state.snapshots if item.id == operation["snapshot_id"] and item.kind == "job" and item.displayed), None)
        item = next((item for item in snapshot.items if item["id"] == operation["job_id"]), None) if snapshot else None
        if item is None:
            raise ValueError("职位不属于已展示列表，不能建立焦点")
        state.selected_job = JobSelection(job_id=item["id"], snapshot_id=snapshot.id,
            selector=str(operation["selector"]), title=item["title"], company=item.get("company", ""))
        state.active_intent = "applications"
    elif op == "learning_request":
        state.learning_request = {"message": str(operation["message"])[:600],
            "resume_stamp": operation["resume_stamp"], "scope": operation.get("scope", {}),
            "source": source, "turn": state.turn, "expires_at": now + 600}
        state.active_intent = "learning"
    elif op == "learning_consume":
        state.learning_request = None
        frame = state.frames.get("learning")
        if frame:
            frame.last_question, frame.missing_slots = "", []
    elif op == "question":
        frame = state.frames.setdefault(operation["topic"], Frame())
        frame.last_question = str(operation["question"])[:1000]
        frame.question_source = source
        frame.question_turn, frame.question_expires_at = state.turn, now + 600
        frame.missing_slots = operation.get("missing_slots", [])[:12]
        state.active_intent = operation["topic"]
    elif op == "clarify":
        state.clarification = {"message": str(operation["message"])[:2000],
            "options": operation["options"], "source": source, "expires_at": now + 600, "turn": state.turn}
    elif op == "clarify_consume":
        state.clarification = None
    elif op == "propose":
        if operation["tool"] not in ACTION_TOOLS:
            raise ValueError("此动作不能通过泛化确认授权；投递和主简历写入需要专用审核")
        if len(json.dumps(operation["arguments"], ensure_ascii=False)) > 2500:
            raise ValueError("待确认参数超过预算")
        for item in state.pending:
            if item.status == "pending" and item.topic == operation["topic"]:
                item.status = "stale"
        conditions = operation.get("conditions", {})
        item = Pending(id=uuid.uuid4().hex, topic=operation["topic"], tool=operation["tool"],
            arguments=operation["arguments"], description=operation["description"], source=source,
            turn=state.turn, expires_at=now + 600, search_revision=conditions.get("revision", 0),
            resume_stamp=conditions.get("resume_stamp", ""))
        state.pending = [*state.pending, item][-8:]
        state.active_intent = item.topic
        trace(state, "proposal", source, action_id=item.id, tool=item.tool)
    elif op == "decision":
        item = next((item for item in state.pending if item.id == operation["action_id"]), None)
        if item is None or item.status != "pending":
            raise ValueError("待办不存在、已处理或已过期；不能重复执行")
        if item.topic != state.active_intent or state.turn - item.turn > 4:
            raise ValueError("当前话题已经变化，请明确指出需要执行的动作")
        conditions = operation.get("conditions", {})
        if operation["decision"] == "claim" and (item.search_revision != conditions.get("revision", 0)
                or item.resume_stamp != conditions.get("resume_stamp", "")):
            raise ValueError("简历或搜索条件已变化，请重新核对动作")
        item.status = "claimed" if operation["decision"] == "claim" else "rejected"
        frame = state.frames.get(item.topic)
        if frame:
            frame.missing_slots, frame.last_question = [], ""
        trace(state, "confirmation" if item.status == "claimed" else "rejection", source,
              action_id=item.id, tool=item.tool)
    elif op == "settle":
        item = next((item for item in state.pending if item.id == operation["action_id"]), None)
        if item is None or item.status != "claimed":
            raise ValueError("动作未被本次确认领取")
        item.status = "consumed"  # Failure/uncertainty also consumes authority; never auto replay.
        trace(state, "decision_result", source, action_id=item.id, status=operation["status"])
    elif op == "tool":
        tool = operation["tool"]
        trace(state, "tool", source, tool=tool, status=operation["status"],
              detail=str(operation.get("detail", ""))[:500])
        if operation["status"] == "ok" and tool == "job_search_start" and operation.get("browser_id"):
            state.tasks["browser_id"] = operation["browser_id"]
    elif op == "task":
        key = operation["kind"]
        if operation.get("bind") or key not in state.tasks or state.tasks[key] == operation["id"]:
            state.tasks[key] = operation["id"]
        trace(state, "task", source, task_id=operation["id"], status=operation.get("status", "unknown"))
    elif op == "reset":
        # Clear dialogue scope only, not resume, preference, board, raw history or external jobs.
        refusals = [item for item in state.trace if item["kind"] == "rejection"][-3:]
        state = DialogueState(revision=state.revision, turn=state.turn, tasks=state.tasks,
                              last_activity=now, history_epoch=now, summary=refusals)
        trace(state, "reset", source)
    else:
        raise ValueError("未知上下文更新操作")
    state.revision += 1
    return state.model_dump(mode="json")


def resolve_reference(saved: dict, *, kind: str, selector: str, snapshot_id: str | None = None,
                      clock: float | None = None, read_only: bool = False) -> dict:
    state = DialogueState.model_validate(saved)
    now = time.time() if clock is None else clock
    snapshots = [item for item in state.snapshots if item.kind == kind and (read_only or item.expires_at >= now)
        and item.displayed
        and (read_only or state.turn - item.turn <= 6) and (read_only or item.topic == state.active_intent)
        and (snapshot_id is None or item.id == snapshot_id)]
    if not snapshots:
        raise ValueError("没有当前话题中有效的已展示对象，请先查看列表或明确对象")
    snapshot = snapshots[-1]
    if read_only and kind == "job" and state.selected_job and snapshot_id is None:
        # Browsing a single item keeps its source list as the ordinal anchor.
        # Explicitly displaying a new list clears selected_job in apply_operation.
        snapshot = next((item for item in snapshots if item.id == state.selected_job.snapshot_id), snapshot)
    if read_only and kind == "job" and selector in {"它", "那个", "这个"} and state.selected_job:
        focus = state.selected_job
        selected_snapshot = next((item for item in snapshots if item.id == focus.snapshot_id), None)
        selected_item = next((item for item in selected_snapshot.items if item["id"] == focus.job_id), None) if selected_snapshot else None
        if selected_item is None:
            raise ValueError("此前选中的职位列表不可用，请说明职位名称或序号")
        return {"snapshot_id": selected_snapshot.id, "source": selected_snapshot.source,
                "version": selected_snapshot.version, "kind": kind, "items": [selected_item]}
    numerals = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    ordinal = re.fullmatch(r"(前|第)([一二三四五六七八九十百\d]+)(?:个|条|份)?", selector.strip())
    if ordinal:
        number_text = ordinal[2]
        if number_text.isdigit():
            number = int(number_text)
        elif "百" in number_text:
            hundred, rest = number_text.split("百", 1)
            number = numerals.get(hundred, 0) * 100
            if "十" in rest:
                tens, ones = rest.split("十", 1)
                number += numerals.get(tens, 1 if not tens else 0) * 10 + numerals.get(ones, 0)
            else:
                number += numerals.get(rest, 0)
        elif "十" in number_text:
            tens, ones = number_text.split("十", 1)
            number = numerals.get(tens, 1 if not tens else 0) * 10 + numerals.get(ones, 0)
        else:
            number = numerals.get(number_text, 0)
        if not 1 <= number <= len(snapshot.items):
            raise ValueError("序号超过实际展示范围，不能扩展到未展示对象")
        selected = snapshot.items[:number] if ordinal[1] == "前" else [snapshot.items[number - 1]]
    elif selector in {"这些", "这几个", "全部已展示"}:
        selected = snapshot.items
    elif selector in {"它", "那个", "这个"}:
        if len(snapshot.items) != 1:
            raise ValueError("指代可能对应多个对象，请说明序号或名称")
        selected = snapshot.items
    else:
        selected = [item for item in snapshot.items if selector == str(item["id"])
            or selector == item.get("title") or selector == item.get("company")]
        if len(selected) != 1:
            raise ValueError("名称不存在或有多个同名对象，请说明序号")
    return {"snapshot_id": snapshot.id, "source": snapshot.source, "version": snapshot.version,
            "kind": kind, "items": selected}


def pending_action(saved: dict, *, clock: float | None = None) -> dict:
    state = DialogueState.model_validate(saved)
    now = time.time() if clock is None else clock
    items = [item for item in state.pending if item.status == "pending" and item.expires_at >= now
        and item.topic == state.active_intent and state.turn - item.turn <= 4]
    if len(items) != 1:
        raise ValueError("当前没有唯一、有效的待确认动作，请明确要执行的操作")
    return items[0].model_dump(mode="json")


def slot_answer_patch(saved: dict, message: str, *, clock: float | None = None) -> dict | None:
    """Resolve a fully specified short answer only against one actual pending question."""
    state = DialogueState.model_validate(saved or {})
    frame = state.frames.get("search") if state.active_intent == "search" else None
    if not frame or not frame.last_question:
        return None
    now = time.time() if clock is None else clock
    if frame.question_expires_at and (frame.question_expires_at < now or state.turn-frame.question_turn > 4):
        return None
    if frame.missing_slots == ["queries"]:
        from ..core.search_contract import safe_role_keyword
        answer = message.strip().strip("。！! ")
        queries = [item.strip() for item in re.split(r"[、,，;/]", answer)]
        if 1 <= len(queries) <= 5 and all(safe_role_keyword(item) for item in queries):
            return {"queries": queries}
    if frame.missing_slots == ["result_limit"]:
        answer = re.fullmatch(r"(\d{1,3})(?:个|份|条)?[。!！ ]*", message.strip())
        if answer and 1 <= int(answer[1]) <= 100:
            return {"result_limit": int(answer[1])}
    if frame.missing_slots == ["salary_min"]:
        answer = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kK千万]|元)[。!！ ]*", message.strip())
        if answer:
            value = int(float(answer[1]) * {"k": 1000, "千": 1000, "万": 10000}.get(answer[2].lower(), 1))
            if 0 <= value <= 10_000_000:
                return {"salary_min": value}
    return None


def compact_state(saved: dict) -> dict:
    state = DialogueState.model_validate(saved or {})
    return {"active_intent": state.active_intent, "turn": state.turn, "revision": state.revision,
        "frames": {key: frame.model_dump() for key, frame in state.frames.items()},
        "pending": [item.model_dump() for item in state.pending if item.status == "pending"
                    and item.expires_at >= time.time() and state.turn - item.turn <= 4][-3:],
        "focus": [{"id": item.id, "kind": item.kind, "source": item.source, "version": item.version,
                   "count": len(item.items), "items": item.items[:10],
                   "read_only": item.expires_at < time.time() or state.turn - item.turn > 6}
                  for item in state.snapshots[-2:] if item.displayed and item.topic == state.active_intent],
        "tasks": state.tasks, "selected_job": state.selected_job.model_dump() if state.selected_job else None,
        "learning_request": state.learning_request,
        "trace": [{**item, "recency_weight": round(0.8 ** (state.turn-item["turn"]), 3)}
                  for item in state.trace[-6:]], "summary": state.summary[-8:]}


def bounded_data(value: Any, *, budget: int = 8000) -> str:
    """Valid JSON projections, never mid-JSON truncation; retain IDs/status and omission notices."""
    def shrink(item, string_limit, list_limit):
        if isinstance(item, dict):
            return {key: shrink(val, string_limit, list_limit) for key, val in item.items()}
        if isinstance(item, list):
            result = [shrink(val, string_limit, list_limit) for val in item[:list_limit]]
            if len(item) > list_limit:
                result.append({"omitted_items": len(item) - list_limit})
            return result
        if isinstance(item, str) and len(item) > string_limit:
            return item[:string_limit] + "…[内容省略，可用工具查询]"
        return item
    def sanitize(item, key=""):
        if isinstance(item, dict):
            return {name: sanitize(content, name) for name, content in item.items()}
        if isinstance(item, list):
            return [sanitize(content) for content in item]
        if key in {"id", "source", "browser_id", "delivery_id", "action_id", "snapshot_id", "run_id", "resume_stamp", "content_hash"} and isinstance(item, str) and re.fullmatch(r"[A-Za-z0-9_:-]{1,128}", item):
            return item
        return redact(item)
    for string_limit, list_limit in ((1500, 30), (600, 12), (240, 6), (80, 3)):
        text = json.dumps(shrink(sanitize(value), string_limit, list_limit), ensure_ascii=False, default=str)
        if len(text) <= budget:
            return text
    envelope = {key: value[key] for key in ("status", "tool") if isinstance(value, dict) and key in value}
    return json.dumps({**envelope, "omitted": True, "reason": "内容超过预算，请缩小查询范围"}, ensure_ascii=False)


def assemble_context(sections: list[tuple[str, Any, int]]) -> str:
    # Labels and JSON preserve data boundaries. Documents/memory/history have no tool authority.
    return "\n\n".join(f"# {label}\n{bounded_data(data, budget=budget)}" for label, data, budget in sections)


def compact_model_messages(messages: list[dict], *, budget=40000) -> list[dict]:
    """Preserve native assistant/tool groups and current request; bound old text/observations.

    No model-generated summary and no mutation of original checkpoints/call arguments.
    A budget is in characters (provider-independent), not a promise of exact token counts.
    """
    output = copy.deepcopy(messages)
    def size():
        return len(json.dumps(output, ensure_ascii=False, default=str))
    if size() <= budget:
        return output
    for item in output:
        if item.get("role") == "tool":
            try:
                data = json.loads(item["content"])
            except (ValueError, TypeError):
                data = {"content": item.get("content", "")}
            item["content"] = bounded_data(data, budget=1500)
    # Drop oldest plain history as complete user/assistant pairs. Never orphan tool observations.
    while size() > budget and len(output) > 1:
        if output[0].get("role") != "user" or output[0].get("content", "").startswith("<context_data"):
            break
        if len(output) >= 2 and output[1].get("role") == "assistant" and not output[1].get("tool_calls"):
            del output[:2]
        elif output[1].get("role") == "user":
            del output[:1]
        else:
            break
    if size() > budget:
        raise ValueError("模型上下文超过安全预算；请缩小查询范围，原请求与检查点保留")
    return output
