"""Receipt and wait transitions shared by foreground execution and recovery."""
from __future__ import annotations

import copy
import time

DONE = {"completed", "failed", "skipped", "blocked", "outcome_indeterminate"}
EDITABLE = {"pending", "waiting_approval", "waiting_input"}


def input_waits(plan: dict) -> None:
    """Scope missing inputs to nodes; never stop independent safe reads.

    Node-scoped questions pause only their branches. Legacy unscoped ambiguity
    permits independent reads, not speculative local/external mutations.
    """
    scoped = any(step.get("missing_slots") or step.get("questions") or step["status"] == "waiting_input"
                 for step in plan["steps"])
    for step in plan["steps"]:
        if step["status"] != "pending":
            continue
        questions = step.get("questions", [])
        if step.get("missing_slots") or questions:
            step.update(status="waiting_input", wait_reason="missing_slots",
                detail="；".join(questions) or "请补充：" + "、".join(step["missing_slots"]))
        elif plan.get("questions") and not scoped and step.get("effect") != "read":
            step.update(status="waiting_input", wait_reason="unscoped_question",
                detail="；".join(plan["questions"]), questions=list(plan["questions"]))


def successful_receipt(step: dict, content: dict) -> None:
    step.update(status="completed", result=copy.deepcopy(content))
    if step["tool"] == "job_search_start":
        step.update(status="waiting_external", task_id=content["id"], wait_started_at=time.time())
    elif step["tool"] == "job_results_refilter":
        step["task_id"] = content["task_id"]
    elif step["tool"] == "mock_delivery_start" and content.get("binding") in {"searching", "waiting_resource"}:
        step.update(status="waiting_binding", task_id=content.get("binding_task_id"),
            wait_started_at=time.time(), wait_reason=content["binding"], detail="正在等待求职者账号绑定，尚未完成本批投递入队")


async def activate_staged_searches(plan: dict, browser) -> None:
    """JOIN the old read-only worker BEFORE changing its effective parameters.

    Old task/request/results stay in the provider store and in attempt history.
    A new task ID is created by the ordinary search tool, with all normal gates.
    """
    for step in plan["steps"]:
        if "staged_arguments" not in step:
            continue
        if step["tool"] != "job_search_start" or step["arguments"].get("dry_run"):
            raise ValueError("不能自动替换包含投递副作用的执行器")
        view = browser.current()
        if view.id == step.get("task_id"):
            await browser.stop()
        elif not browser.can_start_search():
            raise ValueError("共享浏览器有另一任务，未替换原搜索")
        step.setdefault("attempt_history", []).append({"status": step["status"],
            "arguments": copy.deepcopy(step["arguments"]), "task_id": step.get("task_id"), "reason": "parameters_revised"})
        step.update(arguments=step.pop("staged_arguments"), status="pending", result=None, task_id=None)
        for key in ("resume_search_task_id", "failure", "detail", "wait_started_at", "approval_id"):
            step.pop(key, None)
