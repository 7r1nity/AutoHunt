"""Risk grading and confirmation policy: the authorization layer.

Two questions used to be answered by one mechanism, and the confusion is what made the
validator brittle. They are separate:

* **Interpretation** — what does the user want, and which parameters? The model's job.
  No regex can promise that a generated plan is faithful to the user's words, and trying
  to caused one rejection per paraphrase.
* **Authorization** — may this action happen without asking? This module's job. It reads
  the action that is about to run, never the user's phrasing.

The graded object is therefore the *action*, not the sentence:

    LOW      read-only: search, read a JD, read stats
    MEDIUM   reversible write, or a scoped external action inside a stated boundary
    HIGH     irreversible or bulk external effect: submitting résumés
    UNKNOWN  not classified yet

`UNKNOWN` is deliberately excluded from the ordering, mirroring OpenHands' design
(``_RISK_ORDER`` omits it so comparisons raise). A new tool therefore cannot be waved
through by accident: it grades as UNKNOWN until someone classifies it, and the default
policy asks about UNKNOWN. Forgetting to classify turns into one extra question rather
than a silent execution.

A user's own explicit instruction is an authorization for the action it names, so it
satisfies the *question*, not the *boundary*: criteria such as "85分以上" define the set
the instruction covers, and anything outside that set is not authorized at all. See
``within_authorised_scope``.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class SecurityRisk(StrEnum):
    UNKNOWN = "UNKNOWN"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


#: Concrete levels only. UNKNOWN is absent on purpose: comparing it to a threshold is a
#: programming error, not a low risk, and must not silently sort as "below HIGH".
_RISK_ORDER = {SecurityRisk.LOW: 1, SecurityRisk.MEDIUM: 2, SecurityRisk.HIGH: 3}

#: Irreversible or bulk effects. Submitting a résumé cannot be taken back.
_IRREVERSIBLE_TOOLS = frozenset({"mock_delivery_start"})
#: Read-only tools never need confirmation; this is classification, not leniency.
_READ_EFFECT = "read"


class RiskNotComparable(ValueError):
    """Raised when a comparison involves UNKNOWN, which has no order by design."""


def risk_rank(risk: SecurityRisk) -> int:
    try:
        return _RISK_ORDER[risk]
    except KeyError as exc:
        raise RiskNotComparable(
            f"{risk} 没有风险序：不确定的风险必须由策略显式处理，不能当作低风险") from exc


def at_least(risk: SecurityRisk, threshold: SecurityRisk) -> bool:
    """Whether ``risk`` is at or above ``threshold``. UNKNOWN is never "at least"."""
    if risk is SecurityRisk.UNKNOWN or threshold is SecurityRisk.UNKNOWN:
        raise RiskNotComparable("UNKNOWN 不能参与风险比较")
    return risk_rank(risk) >= risk_rank(threshold)


@dataclass(frozen=True)
class ActionRisk:
    risk: SecurityRisk
    tool: str
    reason: str
    #: Which stated criterion, if any, the action falls under. Empty when the action is
    #: not covered by an explicit instruction.
    authorised_by: str = ""


def grade_action(tool: str, arguments: dict[str, Any] | None = None, *,
                 effect: str | None = None, batch_size: int | None = None,
                 explicit_instruction: str = "") -> ActionRisk:
    """Grade one action. Unknown tools grade UNKNOWN rather than being assumed safe."""
    arguments = arguments or {}
    if effect == _READ_EFFECT:
        return ActionRisk(SecurityRisk.LOW, tool, "只读操作，无外部副作用",
                          authorised_by=explicit_instruction)
    if tool in _IRREVERSIBLE_TOOLS:
        size = batch_size if batch_size is not None else arguments.get("expected_count")
        if isinstance(size, int) and size > 0:
            reason = f"对外投递且不可撤销；本批 {size} 个职位"
        else:
            reason = "对外投递且不可撤销，影响范围未知"
        return ActionRisk(SecurityRisk.HIGH, tool, reason, authorised_by=explicit_instruction)
    if effect == "write":
        return ActionRisk(SecurityRisk.MEDIUM, tool, "可回退的写入", authorised_by=explicit_instruction)
    if effect == "external":
        return ActionRisk(SecurityRisk.HIGH, tool, "对外副作用", authorised_by=explicit_instruction)
    return ActionRisk(SecurityRisk.UNKNOWN, tool, "该工具尚未分级；必须有意识地分类，不得默认放行")


# --------------------------------------------------------------------------- policy

class ConfirmationPolicy(ABC):
    """Whether an action of a given risk needs the user's answer before running."""

    @abstractmethod
    def should_confirm(self, risk: SecurityRisk) -> bool: ...

    def describe(self) -> str:
        return type(self).__name__


@dataclass(frozen=True)
class AlwaysConfirm(ConfirmationPolicy):
    def should_confirm(self, risk: SecurityRisk) -> bool:
        return True


@dataclass(frozen=True)
class NeverConfirm(ConfirmationPolicy):
    def should_confirm(self, risk: SecurityRisk) -> bool:
        return False


@dataclass(frozen=True)
class ConfirmRisky(ConfirmationPolicy):
    """Confirm at or above the threshold, and always confirm an unclassified action.

    ``confirm_unknown`` is the fail-safe: not knowing how dangerous something is is a
    reason to ask, not a reason to proceed.
    """

    threshold: SecurityRisk = SecurityRisk.MEDIUM
    confirm_unknown: bool = True

    def should_confirm(self, risk: SecurityRisk) -> bool:
        if risk is SecurityRisk.UNKNOWN:
            return self.confirm_unknown
        return at_least(risk, self.threshold)

    def describe(self) -> str:
        return f"ConfirmRisky(threshold={self.threshold}, confirm_unknown={self.confirm_unknown})"


@dataclass(frozen=True)
class ScopedInstruction:
    """A user instruction that authorises the actions matching its own criteria.

    The instruction answers "may I?" for the set its criteria define. It does not widen
    the set: "85分以上直接投递" authorises jobs at or above 85, and says nothing about a
    job at 82. That asymmetry is what keeps auto-delivery honest — the boundary is the
    user's own condition, not the agent's judgement of what looked close enough.
    """

    tool: str
    quote: str
    #: Minimum match score the instruction covers, when it states one.
    min_score: int | None = None
    #: Maximum number of jobs the instruction covers, when it states one.
    limit: int | None = None
    #: Criteria that must hold for a candidate to be in scope, keyed by name.
    requirements: dict[str, Any] = field(default_factory=dict)

    def covers(self, *, score: int | None = None, salary_min: int | None = None,
               count: int | None = None) -> tuple[bool, str]:
        """Whether one candidate falls inside the instruction's set.

        Returns ``(in_scope, reason)``. An unknown value is **not** in scope: treating
        "salary not stated" as satisfying "salary >= 15k" would silently drop the user's
        own condition.
        """
        if self.min_score is not None:
            if score is None:
                return False, f"未评分，无法确认是否达到「{self.min_score} 分以上」"
            if score < self.min_score:
                return False, f"匹配分 {score} 低于要求的 {self.min_score}"
        floor = self.requirements.get("salary_min")
        if isinstance(floor, int):
            if salary_min is None:
                return False, "JD 未标注薪资，无法确认是否满足薪资条件"
            if salary_min < floor:
                return False, f"薪资 {salary_min} 低于要求的 {floor}"
        if self.limit is not None and count is not None and count > self.limit:
            return False, f"已超出该指令覆盖的 {self.limit} 个职位"
        return True, f"符合「{self.quote}」的范围"
