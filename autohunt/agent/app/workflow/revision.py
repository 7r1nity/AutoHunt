"""Versioned task updates/additions preserving durable execution receipts."""
from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..security.action_grants import action_binding
from .contracts import PlannedStep, Goal, EVIDENCE_TOOLS
from .validation import normalize_encoding, preflight, dependency_ancestors


class StepUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step_id: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class RevisionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: str
    expected_revision: int = Field(ge=0)
    pause: bool | None = None
    updates: list[StepUpdate] = Field(default_factory=list, max_length=8)
    add_steps: list[PlannedStep] = Field(default_factory=list, max_length=8)
    remove_steps: list[str] = Field(default_factory=list, max_length=32)
    goals: list[Goal] = Field(default_factory=list, max_length=12)


def edited_plan(plan: dict, edit: RevisionDraft, registry, *, stamp: str, session_id: str, message: str) -> dict:
    if plan.get("session_id", session_id) != session_id:
        raise ValueError("原任务不属于当前会话")
    if edit.plan_id != plan["id"] or edit.expected_revision != plan.get("revision", 0):
        raise ValueError("计划已变化，请重新读取当前计划再修改")
    if plan.get("resume_stamp") != stamp:
        raise ValueError("任务绑定的简历已变化，请确认新的任务范围")
    if plan.get("status") in {"cancelled", "outcome_indeterminate"} or (
            plan.get("status") in {"finished", "completed"} and not edit.add_steps):
        raise ValueError("原计划已结束或结果不确定，不能修改并重放")
    result = copy.deepcopy(plan)
    steps = {step["id"]: step for step in result["steps"]}
    removed = set(edit.remove_steps)
    if len(removed) != len(edit.remove_steps) or not removed <= steps.keys():
        raise ValueError("待取消步骤不存在或重复")
    for step_id in removed:
        step = steps[step_id]
        if step["status"] != "pending":
            raise ValueError("只能取消尚未执行的步骤；正在发送的操作请使用停止任务入口")
        step.update(status="skipped", withdrawn=True, detail="已按用户要求取消此后续动作",
                    failure={"code": "user_withdrawn", "effect_started": False, "uncertain": False})
    seen = set()
    for update in edit.updates:
        if update.step_id in seen or update.step_id not in steps:
            raise ValueError("修改步骤不存在或重复")
        seen.add(update.step_id)
        step = steps[update.step_id]
        if step["status"] != "pending" or step["tool"] in result.get("revoked_tools", []):
            raise ValueError("只能修改尚未执行的步骤；已执行、已撤回和结果不确定的步骤不会重放")
        spec = registry._tools[step["tool"]]
        arguments = normalize_encoding({**step["arguments"], **update.arguments}, spec.parameters)
        # Explicit null clears optional nonnullable arguments, rather than inventing a default.
        for key, value in list(arguments.items()):
            if value is None and key not in spec.parameters.get("required", []) and "null" not in spec.parameters.get("properties", {}).get(key, {}).get("type", []):
                arguments.pop(key)
        registry.validate_arguments(spec.parameters, arguments)
        if arguments.get("filters") is not None:
            from ..core.job_filters import validate_filter
            arguments["filters"] = validate_filter(arguments["filters"]).model_dump(mode="json")
        if step["tool"] == "learning_plan_generate" and arguments.get("min_score", 0) > arguments.get("max_score", 100):
            raise ValueError("分析分数下界不能超过上界")
        step["arguments"] = arguments
        step["authority_binding"] = action_binding(step["tool"], arguments, session_id=session_id,
            requested=step.get("user_requested", (step.get("authority_binding") or {}).get("user_requested", False)))
    for addition in edit.add_steps:
        step = addition.model_dump()
        if step["id"] in steps:
            old = steps[step["id"]]
            if old["tool"] == step["tool"] and old["arguments"] == step["arguments"]:
                continue  # A reference to an existing action is never a replay.
            raise ValueError("新增步骤ID与已有步骤冲突，请修正规划")
        if step["tool"] not in registry._tools or step["tool"] in {"task_continue", "context_action_propose", "context_question_ask"}:
            raise ValueError("新增工具不存在或不能嵌套执行")
        if step["tool"] in result.get("revoked_tools", []):
            raise ValueError("该动作已撤回，新增步骤不能恢复原授权")
        spec = registry._tools[step["tool"]]
        step["arguments"] = normalize_encoding(step["arguments"], spec.parameters)
        registry.validate_arguments(spec.parameters, step["arguments"])
        if step["arguments"].get("filters") is not None:
            from ..core.job_filters import validate_filter
            step["arguments"]["filters"] = validate_filter(step["arguments"]["filters"]).model_dump(mode="json")
        if step["tool"] == "mock_delivery_start" and not step["user_requested"]:
            raise ValueError("新增投递范围尚待确认，请核对要投递的岗位")
        step.update(status="pending", effect=spec.effect, result=None, task_id=None,
                    command_quote=step["command_quote"] or message)
        step["authority_binding"] = action_binding(step["tool"], step["arguments"], session_id=session_id,
                                                   requested=step["user_requested"])
        result["steps"].append(step)
        steps[step["id"]] = step
    if len(result["steps"]) > 32:
        raise ValueError("任务累计步骤超过32，请分为独立任务")
    for addition in edit.add_steps:
        step = steps[addition.id]
        if step["status"] != "pending" or step["tool"] not in EVIDENCE_TOOLS:
            continue
        producers = [item for item in result["steps"] if item["tool"] in {"job_search_start", "job_results_refilter"}]
        bound = [item["id"] for item in producers if item["id"] in dependency_ancestors(step["id"], result["steps"])]
        if not bound and len(producers) == 1:
            step["depends_on"].append(producers[0]["id"])
            bound = [producers[0]["id"]]
        # Resolve sequential search/refilter producers to their newest evidence.
        bound = [key for key in bound if not any(key in dependency_ancestors(other, result["steps"]) for other in bound if other != key)]
        if producers and len(bound) != 1:
            raise ValueError("新增动作需要明确对应哪一轮搜索结果")
        if bound:
            step["evidence_step"] = bound[0]
    result["goals"] = [*result.get("goals", []), *[goal.model_dump() for goal in edit.goals]]
    preflight(result["steps"], result["goals"], [plan["original"], message], set(registry._tools))
    paused = plan.get("paused", False) if edit.pause is None else edit.pause
    result.update(revision=plan.get("revision", 0) + 1, paused=paused, status="paused" if paused else "running")
    result["answers"] = [*result.get("answers", []), message][-12:]
    result.setdefault("revision_history", []).append({"revision": result["revision"], "message": message,
        "updated_steps": sorted(seen), "added_steps": [step.id for step in edit.add_steps],
        "removed_steps": sorted(removed), "paused": paused})
    return result
