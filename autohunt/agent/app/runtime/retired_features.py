"""Retire unfinished legacy steps without replaying effects or deleting history."""
from __future__ import annotations

import copy


def retire_growth_steps(checkpoint: dict | None) -> tuple[dict, list[str]]:
    value = copy.deepcopy(checkpoint or {})
    plan = value.get("plan") or {}
    steps = plan.get("steps", [])
    # Remove only questions whose recorded owner is being retired. Unbound
    # global questions and questions also owned by a live branch stay intact.
    retired_questions = {question for step in steps
                         if step.get("tool") == "learning_plan_generate" and step.get("status") != "completed"
                         for question in step.get("questions") or [] if isinstance(question, str)}
    retained_questions = {question for step in steps
                          if step.get("tool") != "learning_plan_generate"
                          for question in step.get("questions") or [] if isinstance(question, str)}
    retired = []
    for step in steps:
        if step.get("tool") != "learning_plan_generate" or step.get("status") == "completed":
            continue
        changed = False
        if step.get("status") != "skipped" or (step.get("failure") or {}).get("code") != "feature_removed":
            step.update(status="skipped", detail="该功能已移除，不再执行；其他步骤与历史结果保留",
                        failure={"code": "feature_removed", "effect_started": False, "uncertain": False})
            changed = True
        for field in ("questions", "missing_slots"):
            if step.get(field):
                step[field] = []
                changed = True
        if changed:
            retired.append(step["id"])
    if retired_questions and plan.get("questions"):
        plan["questions"] = [question for question in plan["questions"]
                             if question not in retired_questions or question in retained_questions]
    return value, retired
