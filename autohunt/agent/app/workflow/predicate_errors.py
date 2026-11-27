"""Keep model predicate defects separate from missing user business information."""
from __future__ import annotations

import copy

from .graph import wait_for_scope
from .validation import SLOT_LABELS


_PATH_FIELDS = frozenset({"filters", "result_groups", "children", "salary", "op", "text", "label", "quote",
    "mode", "unit", "minimum", "maximum", "range_mode", "applies_to", "origin", "count"})


def quarantine_predicate(step: dict, field: str, value, error: ValueError) -> None:
    """Preserve the invalid value and give bounded repair exact error locations.

    These diagnostics describe model output, never user instructions. Pydantic
    input/ctx and arbitrary property names are not copied into the feedback.
    """
    question = "系统尚未完成筛选条件结构整理，相关分支尚未执行。"
    wait_for_scope(step, question, field=field, value=copy.deepcopy(value))
    step.setdefault("technical_questions", []).append(question)
    errors = error.errors(include_input=False, include_context=False, include_url=False) if hasattr(error, "errors") else [
        {"loc": (), "type": "value_error", "msg": str(error)}]
    for issue in errors[:8]:
        path = [part if isinstance(part, int) or part in _PATH_FIELDS else "unknown_field"
                for part in issue.get("loc", ())]
        step.setdefault("technical_issues", []).append({"field": field, "path": [field, *path],
            "code": issue.get("type", "value_error"), "reason": str(issue.get("msg", "条件结构无效"))[:300]})


def finalize_predicate_failures(plan: dict) -> dict:
    """Exhausted format repair is a scoped system failure, not a user question."""
    obsolete = set()
    for step in plan["steps"]:
        if not step.get("technical_issues"):
            continue
        fields = {issue["field"] for issue in step["technical_issues"]}
        questions = set(step.get("technical_questions", [])) | {
            "请补充" + SLOT_LABELS.get(field, field) + "。" for field in fields}
        obsolete.update(questions)
        step["missing_slots"] = [field for field in step.get("missing_slots", []) if field not in fields]
        step["questions"] = [question for question in step.get("questions", []) if question not in questions]
        step.update(status="blocked", detail="系统未能完成条件结构整理，相关操作未执行；原请求和条件已保留。",
            failure={"code": "predicate_repair_exhausted", "effect_started": False, "uncertain": False})
    remaining = {question for step in plan["steps"] for question in step.get("questions", [])}
    plan["questions"] = [question for question in plan.get("questions", [])
                         if question not in obsolete or question in remaining]
    validation = plan.get("validation", {})
    validation["issues"] = [issue for issue in validation.get("issues", [])
        if issue.get("detail") not in obsolete or issue.get("detail") in remaining]
    plan["status"] = "paused" if plan.get("paused") else "clarifying" if plan["questions"] else "ready"
    plan["reply"] = "部分步骤的条件结构解析失败，相关操作未执行；原请求和条件已保留。"
    return plan
