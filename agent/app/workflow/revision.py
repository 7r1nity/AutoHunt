"""Scoped, versioned edits of a durable plan; no new actions or replay."""
from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..security.action_grants import action_binding


class StepUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step_id: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class RevisionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: str
    expected_revision: int = Field(ge=0)
    pause: bool = True
    updates: list[StepUpdate] = Field(default_factory=list, max_length=8)


def edited_plan(plan: dict, edit: RevisionDraft, registry, *, stamp: str, session_id: str, message: str) -> dict:
    if edit.plan_id != plan["id"] or edit.expected_revision != plan.get("revision", 0):
        raise ValueError("计划已变化，请重新读取当前计划再修改")
    if plan.get("resume_stamp") != stamp:
        raise ValueError("任务绑定的简历已变化，请确认新的任务范围")
    if plan.get("status") in {"finished", "completed", "cancelled", "outcome_indeterminate"}:
        raise ValueError("原计划已结束或结果不确定，不能修改并重放")
    result = copy.deepcopy(plan)
    steps = {step["id"]: step for step in result["steps"]}
    seen = set()
    for update in edit.updates:
        if update.step_id in seen or update.step_id not in steps:
            raise ValueError("修改步骤不存在或重复")
        seen.add(update.step_id)
        step = steps[update.step_id]
        if step["status"] != "pending" or step["tool"] in result.get("revoked_tools", []):
            raise ValueError("只能修改尚未执行的步骤；已执行、已撤回和结果不确定的步骤不会重放")
        spec = registry._tools[step["tool"]]
        arguments = {**step["arguments"], **update.arguments}
        # Explicit null clears optional nonnullable arguments, rather than inventing a default.
        for key, value in list(arguments.items()):
            if value is None and key not in spec.parameters.get("required", []) and "null" not in spec.parameters.get("properties", {}).get(key, {}).get("type", []):
                arguments.pop(key)
        registry.validate_arguments(spec.parameters, arguments)
        if step["tool"] in {"job_search_start", "search_state_update"} and arguments.get("filters") is not None:
            from ..core.job_filters import validate_filter
            arguments["filters"] = validate_filter(arguments["filters"]).model_dump(mode="json")
        if step["tool"] == "learning_plan_generate" and arguments.get("min_score", 0) > arguments.get("max_score", 100):
            raise ValueError("分析分数下界不能超过上界")
        step["arguments"] = arguments
        step["authority_binding"] = action_binding(step["tool"], arguments, session_id=session_id,
            requested=step.get("user_requested", (step.get("authority_binding") or {}).get("user_requested", False)))
    result.update(revision=plan.get("revision", 0) + 1, paused=edit.pause, status="paused" if edit.pause else "running")
    result["answers"] = [*result.get("answers", []), message][-12:]
    result.setdefault("revision_history", []).append({"revision": result["revision"], "message": message,
        "updated_steps": sorted(seen), "paused": edit.pause})
    return result
