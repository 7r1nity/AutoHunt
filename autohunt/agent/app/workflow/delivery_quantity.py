"""Preserve literal delivery counts, without interpreting job criteria or authority.

The model still selects actions and semantic conditions. This syntax projection
only constrains an already requested delivery action; it cannot add one, approve
one, derive a count from salary/score, or read a count from historical job prose.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..core.search_contract import numeric_value
from ..core.task_semantics import understand
from ..runtime.job_references import NUMBER


@dataclass(frozen=True)
class DeliveryQuantity:
    limit: int
    prefix: bool = False


def delivery_quantity(message: str, quote: str = "") -> DeliveryQuantity | None:
    meanings = understand(message).commands("mock_delivery_start")
    if quote and quote not in message:
        return None
    if quote:
        start = message.index(quote)
        meanings = tuple(action for action in meanings
                         if start <= action.trigger_start < start + len(quote))
        if len(meanings) != 1:
            return None  # A whole multi-action request is not a local clause.
    rows = []
    for action in meanings:
        tail = message[action.trigger_end:action.end]
        match = re.match(rf"\s*(?:最多|至多|不超过)?\s*(前)?\s*({NUMBER})\s*(?:个|份|条)", tail)
        if not match:
            continue  # 第20个 is an identity, never a quantity of twenty.
        if re.search(rf"(?:{NUMBER})\s*(?:个|份|条)", tail[match.end():]):
            continue  # Alternative quantities belong to the model/clarification.
        count = numeric_value(match[2])
        if type(count) is not int or count <= 0:
            continue
        rows.append(DeliveryQuantity(count, bool(match[1])))
    unique = set(rows)
    return next(iter(unique)) if len(unique) == 1 else None


def normalize_delivery_quantities(draft, message: str, *, live_plan=None):
    """Repair a lost count in execute/add/update envelopes using the same syntax."""
    result = draft.model_copy(deep=True)
    revision = result.revision or {}
    existing = {step["id"]: step for step in (live_plan or {}).get("steps", [])}
    deliveries = [step for step in result.steps if step.tool == "mock_delivery_start"]
    additions = [step for step in revision.get("add_steps", []) if step.get("tool") == "mock_delivery_start"]
    updates = [step for step in revision.get("updates", [])
               if existing.get(step.get("step_id"), {}).get("tool") == "mock_delivery_start"]
    commands = understand(message).commands("mock_delivery_start")
    if len(deliveries) + len(additions) + len(updates) > 1 and len(commands) == 1 and delivery_quantity(message):
        from .validation import PlanValidationError
        raise PlanValidationError("delivery_quantity_duplicated",
            "用户只给出一个投递总量，请合并为一个投递步骤，条件分支用筛选树表达，不能为每个步骤复制此数量。",
            category="repair", tool="mock_delivery_start")
    multiple = (len(deliveries) + len(additions) + len(updates) > 1
                or len(commands) > 1)

    def apply(arguments, *, requested, quote=""):
        if multiple and not quote:
            return
        quantity = delivery_quantity(message, quote if multiple else "")
        if not requested or quantity is None:
            return
        if quantity.prefix and arguments.get("selector"):
            from ..runtime.job_references import job_reference
            selector = arguments["selector"]
            reference = job_reference(selector) if isinstance(selector, str) else None
            if not reference or not reference[0].startswith("前") or reference[1]:
                from .validation import PlanValidationError
                raise PlanValidationError("delivery_quantity_selector_conflict",
                    "批量数量与岗位选择器不一致；请保留岗位限定到filters后使用纯前N个选择器，不要删除限定或覆盖单岗对象。",
                    category="repair", tool="mock_delivery_start")
        arguments["limit"] = quantity.limit
        if quantity.prefix:
            # Prefix delivery means a bounded current-round batch. A single
            # ordinal remains strict and uses its original target resolver.
            arguments["selector"] = f"前{quantity.limit}个"

    for step in deliveries:
        # Ambiguous multiple delivery clauses are left to the model unless its
        # step carries a literal, narrower command clause from this user turn.
        apply(step.arguments, requested=step.user_requested, quote=step.command_quote)
    if result.revision:
        for step in additions:
            apply(step.setdefault("arguments", {}), requested=step.get("user_requested", False),
                  quote=step.get("command_quote", ""))
        for update in updates:
            original = existing.get(update.get("step_id"), {})
            if original.get("tool") == "mock_delivery_start":
                apply(update.setdefault("arguments", {}), requested=update.get("user_requested", original.get("user_requested", False)),
                      quote=update.get("command_quote", ""))
    return result
