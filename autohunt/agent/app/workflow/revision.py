"""Versioned task updates/additions preserving durable execution receipts."""
from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..security.action_grants import action_binding
from .contracts import PlannedStep, Goal, EVIDENCE_TOOLS, MAX_PLAN_STEPS
from .validation import normalize_encoding, preflight, dependency_ancestors, resolved_slots, PlanValidationError, SLOT_LABELS
from .graph import bind_evidence, wait_for_scope


class StepUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step_id: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    user_requested: bool | None = None
    depends_on: list[str] | None = Field(default=None, max_length=MAX_PLAN_STEPS)


class RevisionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: str
    expected_revision: int = Field(ge=0)
    pause: bool | None = None
    updates: list[StepUpdate] = Field(default_factory=list, max_length=MAX_PLAN_STEPS)
    add_steps: list[PlannedStep] = Field(default_factory=list, max_length=MAX_PLAN_STEPS)
    remove_steps: list[str] = Field(default_factory=list, max_length=32)
    invalidated_steps: list[str] = Field(default_factory=list, max_length=8)
    goals: list[Goal] = Field(default_factory=list, max_length=MAX_PLAN_STEPS)
    questions: list[str] | None = Field(default=None, max_length=8)
    # Observation only, never execution authority. The planner overwrites this
    # after independently reviewing changed conditions.
    scope_review: dict | None = None


def _repairable_predicate_fields(step: dict) -> set[str]:
    """Only an explicitly corrected, never-executed technical defect is editable.

    Merely continuing a task does not call this revision path or provide the
    replacement fields. Contradictory/absent effect evidence remains blocked.
    """
    failure = step.get("failure") or {}
    if (step.get("status") != "blocked" or failure.get("code") != "predicate_repair_exhausted"
            or failure.get("effect_started") is not False or failure.get("uncertain") is not False
            or step.get("withdrawn") or step.get("execution_phase") in {"started", "committed"}):
        return set()
    fields = set(step.get("unresolved_arguments") or {}) | {
        issue.get("field") for issue in step.get("technical_issues", [])}
    return fields if fields and fields <= {"filters", "result_groups"} else set()


def edited_plan(plan: dict, edit: RevisionDraft, registry, *, stamp: str, session_id: str, message: str) -> dict:
    from .planning import effective_arguments
    if plan.get("session_id", session_id) != session_id:
        raise ValueError("原任务不属于当前会话")
    if edit.plan_id != plan["id"] or edit.expected_revision != plan.get("revision", 0):
        raise ValueError("计划已变化，请重新读取当前计划再修改")
    if plan.get("resume_stamp") != stamp:
        raise PlanValidationError("resume_binding_changed", "任务绑定的简历已变化，请核对新简历后重新绑定任务范围", category="clarify")
    if plan.get("status") in {"cancelled", "outcome_indeterminate"} or (
            plan.get("status") in {"finished", "completed"} and not edit.add_steps and not edit.invalidated_steps):
        raise ValueError("原计划已结束或结果不确定，不能修改并重放")
    result = copy.deepcopy(plan)
    steps = {step["id"]: step for step in result["steps"]}
    invalidated = set(edit.invalidated_steps) & steps.keys()
    revision_questions = []
    unknown = (set(edit.invalidated_steps) | set(edit.remove_steps) | {item.step_id for item in edit.updates}) - steps.keys()
    if unknown:
        revision_questions.append("部分指定步骤不存在，未对这些步骤操作，请确认对象：" + "、".join(sorted(unknown)) + "。")
    replacements = {}
    # Preserve old receipts. Only recomputable evidence/analysis gets a fresh node;
    # external sends/searches and accepted resume changes cannot be replayed here.
    for old_id in dict.fromkeys(edit.invalidated_steps):
        if old_id not in invalidated:
            continue
        old = steps[old_id]
        if old["status"] != "completed" or old["tool"] not in {
                "job_results_read", "job_stats_read", "job_results_refilter", "interview_preparation_generate"}:
            raise ValueError("只能刷新已完成的证据或分析；发送与结果不确定的动作需要核验")
        new_id = f"r{plan.get('revision', 0) + 1}_{old_id}"[:38]
        while new_id in steps:
            new_id = new_id[:37] + "_"
            if len(new_id) == 38 and new_id in steps:
                raise ValueError("刷新步骤ID冲突，请拆分修订")
        replacement = copy.deepcopy(old)
        replacement.update(id=new_id, status="pending", result=None, task_id=None, refresh_of=old_id)
        for key in ("retained_result", "failure", "approval_id", "detail", "proposal_id", "invalidated_by"):
            replacement.pop(key, None)
        old.update(invalidated_by=new_id, retained_result=True)
        replacements[old_id] = new_id
        result["steps"].append(replacement)
        steps[new_id] = replacement
    for step in result["steps"]:
        if step["status"] in {"pending", "waiting_approval", "waiting_input"}:
            step["depends_on"] = [replacements.get(key, key) for key in step["depends_on"]]
            if step.get("evidence_step") in replacements:
                step["evidence_step"] = replacements[step["evidence_step"]]
    removed = set(edit.remove_steps)
    # Auto-filter nonexistent step IDs instead of failing (T-25).
    removed = {sid for sid in removed if sid in steps}
    for step_id in removed:
        step = steps[step_id]
        if step["status"] not in {"pending", "waiting_approval", "waiting_input"}:
            raise ValueError("只能取消尚未执行的步骤；正在发送的操作请使用停止任务入口")
        step.update(status="skipped", withdrawn=True, detail="已按用户要求取消此后续动作",
                    failure={"code": "user_withdrawn", "effect_started": False, "uncertain": False})
    seen, combined = set(), {}
    for update in edit.updates:
        if update.step_id not in steps:
            continue
        if update.step_id not in combined:
            combined[update.step_id] = update.model_copy(deep=True)
            continue
        previous = combined[update.step_id]
        if (any(key in previous.arguments and previous.arguments[key] != value for key, value in update.arguments.items())
                or previous.depends_on is not None and update.depends_on is not None and previous.depends_on != update.depends_on
                or previous.user_requested is not None and update.user_requested is not None and previous.user_requested != update.user_requested):
            raise PlanValidationError("conflicting_revision", "同一步骤包含相互冲突的修改，请确认最终条件", category="clarify")
        previous.arguments.update(update.arguments)
        if update.depends_on is not None:
            previous.depends_on = update.depends_on
        if update.user_requested is not None:
            previous.user_requested = update.user_requested
    for update in combined.values():
        seen.add(update.step_id)
        step = steps[update.step_id]
        active_search = step["tool"] == "job_search_start" and step["status"] == "waiting_external" and not step["arguments"].get("dry_run")
        predicate_fields = _repairable_predicate_fields(step)
        if predicate_fields:
            if not predicate_fields <= update.arguments.keys():
                raise PlanValidationError("predicate_correction_missing", "请明确修正原失败步骤的全部条件字段；其他参数修改不会恢复该步骤", category="clarify")
            if any(update.arguments[field] is None or field == "result_groups" and update.arguments[field] == []
                   for field in predicate_fields):
                raise PlanValidationError("predicate_correction_removed", "移除原失败条件不能作为结构修复；请按新的完整范围另行确认执行", category="clarify")
        if (step["status"] not in {"pending", "waiting_approval", "waiting_input"} and not active_search
                and not predicate_fields
                or step["tool"] in result.get("revoked_tools", [])):
            raise ValueError("只能修改尚未执行的步骤；已执行、已撤回和结果不确定的步骤不会重放")
        spec = registry._tools[step["tool"]]
        arguments = effective_arguments(registry, step["tool"], normalize_encoding(
            {**step.get("staged_arguments", step["arguments"]), **update.arguments}, spec.parameters))
        # Explicit null clears optional nonnullable arguments, rather than inventing a default.
        for key, value in list(arguments.items()):
            if value is None and key not in spec.parameters.get("required", []) and "null" not in spec.parameters.get("properties", {}).get(key, {}).get("type", []):
                arguments.pop(key)
        try:
            registry.validate_arguments(spec.parameters, arguments, path=f"steps.{step['id']}.{step['tool']}.arguments")
        except ValueError as exc:
            raise PlanValidationError("invalid_argument_schema", str(exc), tool=step["tool"], field="arguments") from exc
        if arguments.get("filters") is not None:
            from ..core.job_filters import validate_filter
            arguments["filters"] = validate_filter(arguments["filters"]).model_dump(mode="json")
        if predicate_fields and arguments.get("result_groups") is not None:
            from ..core.result_groups import normalize_groups
            arguments["result_groups"] = normalize_groups(arguments["result_groups"])
        if predicate_fields:
            step.setdefault("attempt_history", []).append({"status": step["status"],
                "arguments": copy.deepcopy(step["arguments"]), "failure": copy.deepcopy(step["failure"]),
                "unresolved_arguments": copy.deepcopy(step.get("unresolved_arguments", {})),
                "reason": "predicate_corrected"})
        if active_search:
            if update.depends_on is not None:
                raise ValueError("正在执行的搜索不能改写已生效依赖；请追加后续步骤")
            step["staged_arguments"] = arguments
            result["paused"] = True  # Hold descendants until the old worker is joined.
        else:
            step["arguments"] = arguments
            if update.depends_on is not None:
                step["depends_on"] = list(dict.fromkeys(update.depends_on))
                step.pop("evidence_step", None)
        if update.user_requested is not None:
            step["user_requested"] = update.user_requested
        if step["tool"] == "mock_delivery_start" and "scope_mode" in update.arguments:
            step.pop("selection_inherits_scope", None)
        if step["status"] in {"waiting_approval", "waiting_input"} or predicate_fields:
            step.update(status="pending")
            step.pop("approval_id", None)
            step.pop("wait_reason", None)
            step.pop("detail", None)
            step.pop("failure", None)
            if predicate_fields:
                obsolete = set(step.pop("technical_questions", [])) | {
                    "请补充" + SLOT_LABELS.get(field, field) + "。" for field in predicate_fields}
                step["questions"] = [question for question in step.get("questions", []) if question not in obsolete]
                step["technical_issues"] = [issue for issue in step.get("technical_issues", [])
                                            if issue.get("field") not in predicate_fields]
            unresolved = step.get("unresolved_arguments", {})
            for key in update.arguments:
                unresolved.pop(key, None)
            filled = resolved_slots(step, {}) - set(unresolved)
            step["missing_slots"] = [slot for slot in step.get("missing_slots", []) if slot not in filled]
            if not predicate_fields and not step["missing_slots"] and not unresolved:
                step["questions"] = []
        step["authority_binding"] = action_binding(step["tool"], arguments, session_id=session_id,
            requested=step.get("user_requested", (step.get("authority_binding") or {}).get("user_requested", False)))
    added_ids = []
    for addition in edit.add_steps:
        step = addition.model_dump()
        if step["id"] in steps:
            old = steps[step["id"]]
            if old["tool"] == step["tool"] and old["arguments"] == step["arguments"]:
                continue  # A reference to an existing action is never a replay.
            if any(step["id"] in other.depends_on for other in edit.add_steps if other.id != step["id"]) or any(
                    step["id"] in (change.depends_on or []) for change in edit.updates):
                raise PlanValidationError("ambiguous_dependency", "撞名节点有依赖引用，需要重新生成唯一ID与对应关系")
            # Keep historical identities untouched. Rename only a fresh node;
            # existing dependency references continue to mean the old node.
            stem = f"r{plan.get('revision', 0) + 1}_{step['id']}"[:35]
            key, counter = stem, 1
            while key in steps:
                key = f"{stem}_{counter}"[:40]; counter += 1
            step["id"] = key
        if step["tool"] not in registry._tools or step["tool"] in {"task_continue", "context_action_propose", "context_question_ask"}:
            raise ValueError("新增工具不存在或不能嵌套执行")
        if step["tool"] in result.get("revoked_tools", []):
            raise ValueError("该动作已撤回，新增步骤不能恢复原授权")
        spec = registry._tools[step["tool"]]
        step["arguments"] = effective_arguments(registry, step["tool"], normalize_encoding(step["arguments"], spec.parameters))
        try:
            registry.validate_arguments(spec.parameters, step["arguments"], path=f"steps.{step['id']}.{step['tool']}.arguments")
        except ValueError as exc:
            raise PlanValidationError("invalid_argument_schema", str(exc), tool=step["tool"], field="arguments") from exc
        if step["arguments"].get("filters") is not None:
            from ..core.job_filters import validate_filter
            step["arguments"]["filters"] = validate_filter(step["arguments"]["filters"]).model_dump(mode="json")
        step.update(status="pending", effect=spec.effect, result=None, task_id=None,
                    command_quote=step["command_quote"] or message)
        step["authority_binding"] = action_binding(step["tool"], step["arguments"], session_id=session_id,
                                                   requested=step["user_requested"])
        result["steps"].append(step)
        steps[step["id"]] = step
        added_ids.append(step["id"])
    condition_updates = [step for step in result["steps"] if step["id"] in seen
                         and step["tool"] == "search_state_update" and step["status"] == "pending"]
    condition_updates.extend(step for step in result["steps"] if step["tool"] == "search_state_update"
                             and step["id"] in added_ids)
    from .graph import depend_on
    from .target_selection import bind_target_dependencies
    bind_target_dependencies(result["steps"])
    removals = [step for step in result["steps"] if step["tool"] == "job_remove_selection" and step["id"] in added_ids]
    for delivery in [step for step in result["steps"] if step["tool"] == "mock_delivery_start"
                     and step["id"] in added_ids]:
        for removal in removals:
            depend_on(result["steps"], delivery, removal)
    fresh_actions = [step for step in result["steps"] if step["tool"] in EVIDENCE_TOOLS and step["status"] == "pending"
                     and (step["id"] in added_ids or step["id"] in seen or step.get("refresh_of"))]
    if condition_updates and fresh_actions:
        for producer in result["steps"]:
            if producer["status"] == "pending" and producer["tool"] in {"job_search_start", "job_results_refilter"}:
                producer["depends_on"] = list(dict.fromkeys([*producer["depends_on"], *[step["id"] for step in condition_updates]]))
        key = f"refresh_r{plan.get('revision', 0) + 1}"
        while key in steps:
            key += "_"
        producers = [step for step in result["steps"] if step["tool"] in {"job_search_start", "job_results_refilter"}]
        if len(producers) > 1:
            latest = [step for step in producers if not any(step["id"] in dependency_ancestors(other["id"], result["steps"])
                       for other in producers if other["id"] != step["id"])]
        else:
            latest = producers
        refresh = {"id": key, "tool": "job_results_refilter", "arguments": {}, "status": "pending", "effect": "write",
            "depends_on": [step["id"] for step in condition_updates] + [step["id"] for step in latest],
            "command_quote": message, "scope_quote": "", "missing_slots": [], "user_requested": True,
            "result": None, "task_id": None}
        if len(latest) > 1:
            wait_for_scope(refresh, "请明确刷新哪一轮搜索结果：" + "、".join(item["id"] for item in latest) + "。")
        if len(latest) == 1 and latest[0].get("task_id"):
            refresh["arguments"]["task_id"] = latest[0]["task_id"]
        result["steps"].append(refresh)
        steps[key] = refresh
        for step in fresh_actions:
            step["depends_on"].append(key)
            step["evidence_step"] = key
    if len(result["steps"]) > MAX_PLAN_STEPS:
        raise ValueError("任务累计步骤超过32，请分为独立任务")
    for step in result["steps"]:
        if step["id"] not in set(added_ids) | seen and not step.get("refresh_of"):
            continue
        if step["status"] != "pending" or step["tool"] not in EVIDENCE_TOOLS:
            continue
        producers = [item for item in result["steps"] if item["tool"] in {"job_search_start", "job_results_refilter"}]
        from .result_sources import result_references
        from ..runtime.context import active_run
        known_context = active_run()
        known_sources = {item["task_id"] for item in result_references(known_context.dialogue if known_context else {}, plan)}
        bind_evidence(result["steps"], step, producers, known_sources=known_sources)
        if step["tool"] == "mock_delivery_start":
            from ..runtime.context import active_run
            from .delivery_scope import materialize_delivery_scope, remember_selection_scope
            from .result_sources import ResultSourceClarification
            remember_selection_scope(step, step["arguments"])
            context = active_run()
            remembered = (context.dialogue or {}).get("delivery_scope") if context else None
            ancestors = dependency_ancestors(step["id"], result["steps"])
            sources = [item for item in producers if item["id"] in ancestors]
            try:
                step["arguments"] = materialize_delivery_scope(step["arguments"], remembered,
                    session_id=session_id, stamp=stamp,
                    task_id=next((item.get("task_id") for item in reversed(sources) if item.get("task_id")), None),
                    new_search=any(item["tool"] == "job_search_start" and item["status"] != "completed" for item in sources))
            except ResultSourceClarification as exc:
                wait_for_scope(step, str(exc), field="scope_mode", value=step["arguments"].get("scope_mode", "inherit"))
            if remembered or "scope_mode" in step["arguments"]:
                step["arguments"]["scope_mode"] = "new"
            step["authority_binding"] = action_binding(step["tool"], step["arguments"], session_id=session_id,
                requested=step.get("user_requested", False))
    result["goals"] = [*result.get("goals", []), *[goal.model_dump() for goal in edit.goals]]
    preflight(result["steps"], result["goals"], [plan["original"], message], set(registry._tools))
    paused = result.get("paused", False) if edit.pause is None else edit.pause
    if any(step.get("staged_arguments") is not None for step in result["steps"]):
        paused = True
    original_scoped = {question for step in plan["steps"] for question in step.get("questions", [])}
    unscoped = [question for question in plan.get("questions", []) if question not in original_scoped]
    if edit.questions is not None:
        unscoped = edit.questions
    waits = [step for step in result["steps"] if step["status"] == "waiting_input" or step.get("missing_slots") or step.get("questions")]
    result["questions"] = list(dict.fromkeys([*unscoped, *revision_questions, *[question for step in waits for question in step.get("questions", [])]]))
    result.update(revision=plan.get("revision", 0) + 1, paused=paused, status="paused" if paused else "running")
    result["answers"] = [*result.get("answers", []), message][-12:]
    result.setdefault("revision_history", []).append({"revision": result["revision"], "message": message,
        "updated_steps": sorted(seen), "added_steps": added_ids,
        "removed_steps": sorted(removed), "paused": paused})
    result["revision_history"][-1]["invalidated_steps"] = sorted(invalidated)
    from .scope_review import apply_review
    apply_review(result,edit.scope_review)
    return result
