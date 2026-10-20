"""Conservative structural proof of a batch retaining earlier constraints.

No user-text matching or model-owned approval flags. An unprovable replacement
asks a human; it is not rejected or guessed semantically equivalent.
"""
from __future__ import annotations

from functools import lru_cache

from ..core.job_filters import validate_filter


def _hard_tree(node):
    if node.op == "condition":
        if node.mode != "require":
            return None
        if node.salary:
            s = node.salary
            return ("salary", s.unit, s.minimum, s.maximum, s.range_mode, s.applies_to)
        return ("semantic", node.text.strip())
    children = tuple(item for c in node.children if (item := _hard_tree(c)) is not None)
    if not children:
        return None
    if len(children) == 1 and node.op != "not":
        return children[0]
    return (node.op, children)


@lru_cache(maxsize=2048)
def _implies(new, old):
    if old is None or new == old:
        return True
    if new is None:
        return False
    if old[0] == "all":
        return all(_implies(new, c) for c in old[1])
    if new[0] == "any":
        return all(_implies(c, old) for c in new[1])
    if new[0] == "all":
        return any(_implies(c, old) for c in new[1])
    if old[0] == "any":
        return any(_implies(new, c) for c in old[1])
    if new[0] == old[0] == "salary" and (new[1], new[4], new[5]) == (old[1], old[4], old[5]):
        return ((old[2] is None or new[2] is not None and new[2] >= old[2])
                and (old[3] is None or new[3] is not None and new[3] <= old[3]))
    return False


def delivery_scope_change(arguments, previous, *, session_id, stamp, task_id=None):
    """Return a server-owned reason to confirm, or None for a proved subset."""
    if not previous or arguments.get("selector") or arguments.get("target_step_id"):
        return None  # Explicit job selection has its own bounded-target contract.
    reasons = []
    if previous.get("session_id") != session_id or previous.get("resume_stamp") != stamp:
        reasons.append("会话或简历绑定不同")
    source = arguments.get("source_task_id") or task_id
    if previous.get("task_id") and previous["task_id"] != source:
        reasons.append("搜索来源变化或未明确")
    if arguments.get("min_score", 0) < previous.get("min_score", 0):
        reasons.append("匹配分数门槛降低或缺失")
    try:
        old = _hard_tree(validate_filter(previous["filters"])) if previous.get("filters") else None
        new = _hard_tree(validate_filter(arguments["filters"])) if arguments.get("filters") else None
        if not _implies(new, old):
            reasons.append("原有筛选条件被移除、替换或无法证明保留")
    except (TypeError, ValueError, KeyError):
        reasons.append("无法可靠比较新旧筛选范围")
    if not reasons:
        return None
    return {"previous_scope_id": previous.get("id"), "previous_filters": previous.get("filters"),
            "previous_min_score": previous.get("min_score", 0), "previous_task_id": previous.get("task_id"),
            "reasons": reasons}
