"""One action decision at the tool seam; no natural-language re-authorization."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .risk import ConfirmRisky, ConfirmationPolicy, SecurityRisk, broad_delivery_scope, grade_action


class Decision(StrEnum):
    ALLOW = "ALLOW"
    WARN = "WARN"
    REPAIR = "REPAIR"
    REPLAN = "REPLAN"
    CLARIFY = "CLARIFY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    DENY = "DENY"


CONTROL_TOOLS = frozenset({"browser_task_pause", "browser_task_stop", "mock_delivery_cancel"})
LOCAL_TOOLS = frozenset({"task_continue", "context_action_propose", "context_question_ask",
    "job_review_selection", "job_remove_selection", "search_state_update", "job_results_refilter", "preference_update",
    "resume_propose_patch", "interview_preparation_generate"})


@dataclass(frozen=True)
class ActionDecision:
    decision: Decision
    code: str
    risk: SecurityRisk
    detail: str
    approval: dict | None = None


class ActionPolicy:
    def __init__(self, confirmation: ConfirmationPolicy | None = None):
        self.confirmation = confirmation or ConfirmRisky(threshold=SecurityRisk.HIGH)

    def evaluate(self, tool: str, arguments: dict, effect: str) -> ActionDecision:
        if tool in CONTROL_TOOLS:
            return ActionDecision(Decision.ALLOW, "control_action", SecurityRisk.LOW, "立即处理停止或暂停")
        if tool == "browser_task_resume":
            risk = SecurityRisk.LOW  # Existing browser owner/verification checks still apply.
        elif tool in LOCAL_TOOLS:
            # Default HIGH threshold allows these without extra confirmation.
            # Still honor an explicitly configured AlwaysConfirm/MEDIUM policy.
            risk = SecurityRisk.MEDIUM
        elif effect == "read" or tool in {"job_search_start", "mock_delivery_start"}:
            risk = grade_action(tool, arguments, effect=effect).risk
        else:
            risk = SecurityRisk.UNKNOWN
        broad_delivery = broad_delivery_scope(tool, arguments)
        confirm = broad_delivery or risk == SecurityRisk.UNKNOWN or self.confirmation.should_confirm(risk)
        code = ("delivery_scope_confirmation_required" if broad_delivery else
                "unknown_risk" if risk == SecurityRisk.UNKNOWN else
                "confirmation_required" if confirm else "within_policy")
        detail = ("本次投递未设置分数门槛、筛选条件或指定岗位，范围较宽；请核对后确认。"
                  if broad_delivery else "请核对并确认此动作的范围" if confirm else "符合当前执行策略")
        return ActionDecision(Decision.REQUIRE_APPROVAL if confirm else Decision.ALLOW, code, risk, detail)
