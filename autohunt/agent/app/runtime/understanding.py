"""Turn roles are data-flow decisions, never tool authorization.

Use the shared action grammar rather than a second keyword extractor. Unknown
semantics still pass through the existing rule/small/main model hierarchy.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class TurnRole:
    kind: str
    new_task: bool = False


def turn_role(message: str, *, patch: dict | None = None) -> TurnRole:
    from ..core.request_summary import is_request_summary, is_search_summary
    from ..core.search_language import parse_search_message
    from ..workflow.requests import action_request, is_continuation
    from .dialogue import CONFIRM, REJECT
    text = message.strip()
    if is_request_summary(text):
        return TurnRole("recall")
    if is_search_summary(text):
        return TurnRole("question")
    if not re.search(r"[\w\u4e00-\u9fff]", text):
        return TurnRole("repair")
    if text in {"重置上下文", "清空会话状态", "重新开始"}:
        return TurnRole("reset")
    if REJECT.fullmatch(text) or text in {"退出当前任务", "不办了"}:
        return TurnRole("reject")
    if is_continuation(text):
        return TurnRole("reference")
    if CONFIRM.fullmatch(text):
        return TurnRole("confirm")
    search = parse_search_message(text)
    if search.action in {"stop", "pause", "resume"}:
        return TurnRole("control")
    if action_request(text):
        return TurnRole("command", True)
    if patch:
        return TurnRole("update")
    from ..core.task_semantics import understand
    meaning = understand(text)
    if search.action == "results" or any(item.kind == "question" for item in meaning.actions):
        return TurnRole("question")
    return TurnRole("unresolved")


def may_answer_pending(message: str, pending: dict, *, rule_match=None) -> bool:
    """Only unresolved answers/amendments may enter an old clarification.

    A new explicit action or known read query must not inherit the old task's
    original/authority. Open semantic answers are checked by the planner.
    """
    role = turn_role(message)
    if role.kind not in {"unresolved", "update", "confirm"}:
        return False
    if rule_match and rule_match.intent not in {"none", "search_conditions"}:
        return False
    return bool(pending.get("questions") or any(step.get("missing_slots") for step in pending.get("steps", [])))
