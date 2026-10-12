"""Task recovery is a decision over durable steps, never a new model plan."""
from __future__ import annotations

import copy
import time
from dataclasses import dataclass

from ..security.action_grants import action_binding


@dataclass
class RecoveryDecision:
    action: str
    message: str
    plan: dict | None = None


def reconcile_receipts(plan: dict, events: list[dict]) -> None:
    """Repair only a claim paired with an exact durable successful tool receipt."""
    for step in plan["steps"]:
        if step["status"] not in {"claimed", "outcome_indeterminate"}:
            continue
        ordered = sorted(events, key=lambda event: event.get("seq", 0))
        starts = [event for event in ordered if event["kind"] == "tool_started"
                  and event["data"].get("plan_id") == plan["id"]
                  and event["data"].get("step_id") == step["id"]
                  and event["data"].get("tool") == step["tool"]
                  and event["data"].get("arguments") == step["arguments"]]
        if not starts:
            continue
        receipts = [event for event in ordered if event["kind"] == "tool_finished"
                    and event.get("seq", 0) > starts[-1].get("seq", 0)
                    and event["data"].get("plan_id") == plan["id"]
                    and event["data"].get("step_id") == step["id"]
                    and event["data"].get("tool") == step["tool"]]
        if not receipts or receipts[-1]["data"].get("status") != "ok":
            continue
        result = copy.deepcopy(receipts[-1]["data"].get("result"))
        if not isinstance(result, dict):
            continue
        step.update(status="completed", result=result, retained_result=True)
        if step["tool"] == "job_search_start" and result.get("id"):
            step.update(status="waiting_external", task_id=result["id"])
        if step["tool"] == "resume_propose_patch" and isinstance(result.get("proposal"), dict):
            step.update(status="waiting_confirmation", proposal_id=result["proposal"]["id"])


def recover_plan(run: dict, *, session_id: str, stamp: str,
                 revoked_tools=(), now: float | None = None, events=()) -> RecoveryDecision:
    """An explicit continuation renews only the original, unchanged action scope.

    A terminal run remains immutable. Its child attempt carries the same logical
    plan and completed receipts. Uncertain writes are never reset to pending.
    """
    if run["session_id"] != session_id:
        return RecoveryDecision("clarify", "任务不属于当前会话，未恢复其他会话的操作。")
    plan = copy.deepcopy((run.get("checkpoint") or {}).get("plan"))
    if not plan:
        return RecoveryDecision("no_plan", "原请求还没有执行计划，需要按原请求重新规划。")
    reconcile_receipts(plan, list(events))
    if run.get("cancel_requested") or plan.get("status") == "cancelled":
        return RecoveryDecision("clarify", "原任务已由你取消；不会恢复撤回的操作。请明确新的执行范围。")
    if plan.get("resume_stamp") != stamp:
        return RecoveryDecision("clarify", "原任务绑定的简历已变化，请确认按新简历执行的范围；旧结果和授权未复用。")
    if plan.get("questions"):
        return RecoveryDecision("clarify", "原任务仍有条件待补充：" + "；".join(plan["questions"]))
    if int(plan.get("recovery_count", 0)) >= 5:
        return RecoveryDecision("clarify", "本任务已接续五次仍未完成，请先查看失败原因并修正条件；未继续循环执行。")
    revoked = set(plan.get("revoked_tools", [])) | set(revoked_tools)
    retry, verify = [], []
    for step in plan["steps"]:
        status, effect = step["status"], step.get("effect", "unknown")
        if status == "completed":
            step["retained_result"] = True
            continue
        if step["tool"] in revoked or step.get("withdrawn"):
            step.update(status="blocked", detail="用户已撤回此动作，续接不恢复授权")
            continue
        if step["tool"] == "job_search_start" and step.get("task_id") and step.get("arguments", {}).get("dry_run"):
            verify.append(step["id"])
            continue  # A dead dry-run executor may have already communicated.
        if status in {"waiting_external", "waiting_confirmation", "waiting_approval", "pending"}:
            retry.append(step["id"])
            continue
        failure = step.get("failure") or {}
        safe = effect == "read" or (
            failure.get("effect_started") is False and not failure.get("uncertain"))
        if step["tool"] == "job_search_start" and step.get("task_id"):
            safe = True
            step["resume_search_task_id"] = step["task_id"]
        if status == "skipped" and step.get("depends_on"):
            safe = True  # The runner skipped it before claiming any action.
        if status in {"claimed", "outcome_indeterminate"} and effect != "read" and not step.get("resume_search_task_id"):
            safe = False
        if not safe:
            verify.append(step["id"])
            continue
        if failure.get("code") in {"invalid_arguments", "unknown_tool", "authority_revoked", "arguments_changed"}:
            verify.append(step["id"])
            continue
        step.setdefault("attempt_history", []).append({"status": status,
            "detail": step.get("detail", ""), "failure": failure})
        step.update(status="pending", result=None)
        for key in ("failure", "detail", "wait_started_at"):
            step.pop(key, None)
        retry.append(step["id"])
    # A skipped descendant of an uncertain/withdrawn ancestor is not runnable.
    runnable = set(retry)
    for _ in plan["steps"]:
        for step in plan["steps"]:
            if step["id"] in runnable and any(
                dep["id"] in step.get("depends_on", []) and dep["status"] != "completed"
                and dep["id"] not in runnable for dep in plan["steps"]):
                runnable.remove(step["id"])
                if step["status"] == "pending":
                    step.update(status="skipped", detail="前置步骤待核验或已撤回，未恢复依赖动作")
    if not runnable:
        return RecoveryDecision("verify" if verify else "completed",
            "存在结果不确定或条件未修复的步骤，请先核验；没有重放操作。" if verify else
            "原任务没有可继续的未完成步骤；已完成结果保留，没有重复执行。")
    plan.update(status="ready", paused=False, expires_at=(time.time() if now is None else now) + 1800,
                recovery_count=int(plan.get("recovery_count", 0)) + 1,
                revoked_tools=sorted(revoked), recovery_pending=sorted(runnable))
    for step in plan["steps"]:
        if step["id"] in runnable:
            if "[REDACTED_" in str(step.get("arguments", {})):
                return RecoveryDecision("clarify", "旧任务的业务参数已被改写，无法可靠接续，请重新确认原条件。")
            binding = step.get("authority_binding") or {}
            step["authority_binding"] = action_binding(step["tool"], step["arguments"],
                session_id=session_id, requested=bool(binding.get("user_requested", step.get("user_requested", False))))
    return RecoveryDecision("resume", "已按执行记录接续原任务；已完成步骤不重放，结果不确定的动作仍待核验。", plan)
