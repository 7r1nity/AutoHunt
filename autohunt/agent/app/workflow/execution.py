"""Bounded DAG execution; the model never decides whether a step completed."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
import uuid

from ..core.agent_tools import ToolLoopState
from ..core.models import AgentLane, ChatResponse, ToolEvent
from ..memory.resume_identity import resume_stamp
from ..runtime.context import active_run, dialogue_change, emit
from ..core.replies import tool_reply, tool_label, public_reply
from ..runtime.resilience import classify_failure
from .contracts import EXECUTION_BATCH_SIZE
from .lifecycle import DONE, input_waits, successful_receipt

RESUME_BOUND_TOOLS = {"job_search_start", "job_results_refilter", "mock_delivery_start",
                      "interview_preparation_generate", "resume_propose_patch", "job_review_selection"}


def persist(plan: dict, *, activate: bool = False) -> None:
    previous = (active_run().dialogue or {}).get("instruction_request") or {}
    if previous.get("id") == plan["id"] and previous.get("revision", 0) > plan.get("revision", 0):
        return  # A stale watcher may not overwrite a foreground edit.
    emit("plan_state", plan=plan)
    if not activate and previous.get("id") and previous.get("id") != plan["id"]:
        return  # Only foreground begin may replace another logical plan.
    if previous.get("id") == plan["id"]:
        plan["revoked_tools"] = sorted(set(plan.get("revoked_tools", [])) | set(previous.get("revoked_tools", [])))
    dialogue_change("instruction", activate=activate, request={"revoked_tools": plan.get("revoked_tools", []), "condition_overrides": plan.get("condition_overrides", {}), **{key: plan[key] for key in
        ("id", "original", "answers", "questions", "resume_stamp", "turn", "expires_at", "status", "goals")},
        "understanding": plan.get("understanding"),
        "revision": plan.get("revision", 0), "paused": plan.get("paused", False),
        "steps": [{key: step.get(key) for key in ("id", "tool", "arguments", "unresolved_arguments", "technical_issues", "failure", "detail", "staged_arguments", "depends_on", "missing_slots", "questions", "status", "authority_binding")}
                  for step in plan["steps"]]})


def render_step(step: dict) -> str:
    result = step.get("result") if step["status"] in {"completed", "waiting_external", "waiting_confirmation"} else step.get("detail")
    return public_reply(tool_reply(step["tool"], step["status"], result))


def step_receipt(step: dict) -> str:
    """Identity of an observed outcome, not text similarity or plan revision."""
    evidence = {key: step.get(key) for key in ("id", "tool", "execution_id", "status", "arguments", "detail", "result", "failure")}
    return hashlib.sha256(json.dumps(evidence, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def completion_steps(plan: dict, events: list[dict]) -> list[dict]:
    """Do not republish a search report already delivered by its task watcher.

    Deduplicate by this run's durable task identity, not by text or tool name.
    Keep failed reads, other searches and explicit detail/list reads visible.
    """
    published = {event["data"].get("task_id") for event in events
                 if event["kind"] in {"browser_summary", "report_reference"}
                 and event["data"].get("report_rendered", True)} - {None, ""}
    rendered = {step_id for event in events if event["kind"] == "response"
                and event["data"].get("plan_id") == plan["id"]
                for step_id in event["data"].get("completed_step_ids", [])}
    receipts = {receipt for event in events if event["kind"] in {"response", "workflow_summary"}
                and event["data"].get("plan_id") == plan["id"]
                for receipt in event["data"].get("presented_step_receipts", [])}
    historical_steps = []
    for event in events:
        data = event["data"]
        if event["kind"] == "plan_state" and data.get("plan", {}).get("id") == plan["id"]:
            historical_steps = data["plan"]["steps"]
        if event["kind"] != "response" or data.get("plan_id") != plan["id"] or "presented_step_receipts" in data:
            continue
        for outcome in (data.get("result") or {}).get("tool_events", []):
            candidates = [s for s in historical_steps if s["tool"] == outcome.get("tool")
                          and s["status"] == outcome.get("status")
                          and str(s.get("detail") or "")[:300] == outcome.get("detail")]
            if len(candidates) == 1:
                receipts.add(step_receipt(candidates[0]))
    selected = []
    for step in plan["steps"]:
        if step_receipt(step) in receipts:
            continue
        if step["status"] == "completed" and (step.get("retained_result") or step["id"] in rendered):
            continue
        if step["status"] == "completed":
            if step["tool"] == "job_search_start" and step.get("task_id") in published:
                continue
            result = step.get("result") or {}
            if (step["tool"] == "job_results_read" and isinstance(result, dict)
                    and isinstance(result.get("search_report"), dict)
                    and isinstance(result.get("search_task"), dict)
                    and result["search_task"].get("id") in published):
                continue
        selected.append(step)
    return selected


def completion_lines(plan: dict, events: list[dict]) -> list[str]:
    """Compatibility renderer; production completion uses model synthesis."""
    return [render_step(step) for step in completion_steps(plan, events)]


async def advance(plan: dict, registry) -> list[dict]:
    """Claim writes before execution, continue independent branches only.

    Callers serialize a session and refresh context before resuming. A restored
    claim is indeterminate, never an instruction to repeat a tool call.
    """
    context = active_run()
    if plan.get("paused"):
        return []
    input_waits(plan)
    persist(plan)  # Make the exact action snapshot authoritative before decisions.
    observed = [step for step in plan["steps"] if step["status"] == "blocked"
                and (step.get("failure") or {}).get("code") == "predicate_repair_exhausted"]
    steps = {step["id"]: step for step in plan["steps"]}
    for step in plan["steps"]:
        if step["status"] == "claimed":
            preparing = step.get("execution_phase") == "preparing"
            uncertain = not preparing and step.get("effect") in {"write", "external"}
            step.update(status="outcome_indeterminate" if uncertain else "failed",
                detail="步骤准备期间中断，尚未产生效果，可按记录接续" if preparing else "步骤执行期间中断，请核对结果；不会重放",
                failure={"code": "worker_lost", "effect_started": not preparing, "uncertain": uncertain})
            observed.append(step)
    claimed = 0
    while True:
        changed = False
        for step in plan["steps"]:
            if step["status"] != "pending":
                continue
            deps = [steps[key] for key in step["depends_on"]]
            if any(dep["status"] in DONE - {"completed"} for dep in deps):
                step.update(status="skipped", detail="前置步骤未成功，依赖动作未执行")
                if any(dep.get("withdrawn") or (dep.get("failure") or {}).get("code") == "user_withdrawn" for dep in deps):
                    step.update(withdrawn=True, failure={"code": "user_withdrawn", "effect_started": False, "uncertain": False},
                        detail="前置分支已按用户要求取消，此后续动作也已取消")
                observed.append(step); changed = True
                continue
            if not all(dep["status"] == "completed" for dep in deps):
                continue
            current_stamp = resume_stamp(registry.resume_workspace.read_document(), registry.candidate_state)
            current_instruction = (context.dialogue or {}).get("instruction_request") or {}
            withdrawn = set(plan.get("revoked_tools", [])) | (set(current_instruction.get("revoked_tools", [])) if current_instruction.get("id") == plan["id"] else set())
            if step["tool"] in withdrawn or step.get("withdrawn"):
                step.update(status="blocked", detail="用户已撤回此动作，记忆回顾或继续任务不能恢复授权",
                    failure={"code": "authority_revoked", "effect_started": False, "uncertain": False})
                observed.append(step); changed = True
                continue
            cancelled = plan.get("status") == "cancelled" or (context.cancellation_requested and context.cancellation_requested())
            if cancelled or plan.get("session_id", context.session_id) != context.session_id:
                step.update(status="blocked", detail="此任务已取消或不属于当前会话，未执行后续动作",
                            failure={"code": "task_cancelled_or_foreign", "effect_started": False, "uncertain": False})
                observed.append(step); changed = True
                continue
            if step["tool"] in RESUME_BOUND_TOOLS and plan["resume_stamp"] != current_stamp:
                step.update(status="blocked", detail="任务绑定的简历已变化，请确认新的简历范围")
                step["failure"] = {"code": "authority_stale", "effect_started": False, "uncertain": False}
                observed.append(step); changed = True
                continue
            task_id = steps.get(step.get("evidence_step"), {}).get("task_id")
            if hasattr(registry, "_effective_arguments"):
                try:
                    prepared = registry._effective_arguments(step["tool"], step["arguments"])
                except ValueError as exc:
                    step.update(status="blocked", detail="内部筛选条件需要修复：" + str(exc),
                        failure={"code": "global_filter_projection", "effect_started": False, "uncertain": False})
                    observed.append(step); changed = True
                    persist(plan)
                    continue
                if prepared != step["arguments"]:
                    from ..security.action_grants import action_binding
                    step["arguments"] = prepared
                    step["authority_binding"] = action_binding(step["tool"], prepared,
                        session_id=context.session_id,
                        requested=step.get("user_requested", (step.get("authority_binding") or {}).get("user_requested", False)))
                    persist(plan)
            prefix = (registry._delivery_prefix(step["arguments"].get("selector"))
                      if step["tool"] == "mock_delivery_start" and hasattr(registry, "_delivery_prefix") else None)
            if step["tool"] == "mock_delivery_start" and (step["arguments"].get("limit") is not None or prefix is not None):
                # Freeze the producer receipt and target list BEFORE approval;
                # resolving the latest list during execution would rebind intent.
                prepared = copy.deepcopy(step["arguments"])
                if task_id and not prepared.get("source_task_id"):
                    prepared["source_task_id"] = task_id
                try:
                    if hasattr(registry, "_delivery_arguments"):
                        prepared = registry._delivery_arguments(prepared)
                except ValueError as exc:
                    step.update(status="waiting_input", wait_reason="result_source", detail=str(exc),
                        questions=[str(exc)], failure={"code": "delivery_source_unresolved", "effect_started": False,
                                                     "uncertain": False})
                    plan["questions"] = list(dict.fromkeys([*plan.get("questions", []), str(exc)]))
                    observed.append(step); changed = True
                    persist(plan)
                    continue
                if prepared != step["arguments"]:
                    from ..security.action_grants import action_binding
                    step["arguments"] = prepared
                    step["authority_binding"] = action_binding(step["tool"], prepared,
                        session_id=context.session_id,
                        requested=step.get("user_requested", (step.get("authority_binding") or {}).get("user_requested", False)))
                    persist(plan)
            authority = {"tool": step["tool"], "arguments": copy.deepcopy(step["arguments"]),
                "revision": plan.get("revision"),
                "command": step["command_quote"], "sources": [plan["original"], *plan["answers"]],
                "resume_stamp": plan["resume_stamp"], "expires_at": plan["expires_at"],
                "candidate": registry.candidate_state, "resumes": registry.resume_workspace,
                "evidence_task_id": task_id, "plan_id": plan["id"], "step_id": step["id"]}
            authority["requested"] = step.get("user_requested", (step.get("authority_binding") or {}).get("user_requested", False))
            authority["binding"] = step.get("authority_binding")
            authority["resume_search_task_id"] = step.get("resume_search_task_id")
            previous_message, previous_authority = context.user_message, context.action_authority
            context.user_message, context.action_authority = step["command_quote"], authority
            # Decisions precede claims. Waiting for consent is not an uncertain write.
            try:
                decision = registry.authorize(step["tool"], step["arguments"]) if hasattr(registry, "authorize") else None
            except ValueError as exc:
                invalid_arguments = False
                spec = getattr(registry, "_tools", {}).get(step["tool"])
                if spec:
                    try:
                        registry.validate_arguments(spec.parameters, step["arguments"])
                    except ValueError:
                        invalid_arguments = True
                step.update(status="blocked", detail=str(exc),
                    failure={"code": "invalid_arguments" if invalid_arguments else "action_state_conflict", "effect_started": False, "uncertain": False})
                observed.append(step); changed = True
                context.user_message, context.action_authority = previous_message, previous_authority
                persist(plan)
                from .failure_recovery import recover_failed_step
                await recover_failed_step(plan, step, registry, persist)
                continue
            if decision and decision.decision != "ALLOW":
                emit("action_decision", tool=step["tool"], step_id=step["id"], plan_id=plan["id"],
                     decision=decision.decision.value, code=decision.code, risk=decision.risk.value)
                step.update(status="waiting_approval" if decision.decision == "REQUIRE_APPROVAL" and decision.approval else "blocked",
                    detail=decision.detail, approval_id=(decision.approval or {}).get("id"),
                    failure={"code": decision.code, "effect_started": False, "uncertain": False})
                observed.append(step); changed = True
                context.user_message, context.action_authority = previous_message, previous_authority
                persist(plan)
                continue
            step.update(status="claimed", execution_id=uuid.uuid4().hex, execution_phase="unknown")
            authority["execution_id"] = step["execution_id"]
            previous_effect_state = context.effect_state
            context.effect_state = {"phase": "unknown"}
            from ..runtime.context import diagnostic_scope
            claimed += 1
            persist(plan)  # Durable claim BEFORE the executor or any write.
            emit("plan_step", step_id=step["id"], tool=step["tool"], status="claimed", depends_on=step["depends_on"])
            scope_token = diagnostic_scope.set({"plan_id": plan["id"], "revision": plan.get("revision"),
                "step_id": step["id"], "execution_id": step["execution_id"]})
            try:
                # Refiltering may inspect several persisted JDs; reserve its bounded analysis budget.
                timeout = 90 if step["tool"] == "interview_preparation_generate" else 180 if step["tool"] == "job_results_refilter" or (
                    step["tool"] == "mock_delivery_start" and step["arguments"].get("filters")) else (
                    45 if step["tool"] in {"mock_delivery_start", "job_remove_selection", "job_target_resolve"} and step["arguments"].get("selector") else 20)
                async with asyncio.timeout(timeout):
                    outcome = await registry.execute(step["tool"], step["arguments"],
                        user_message=step["command_quote"], state=ToolLoopState())
                if outcome.status == "ok":
                    successful_receipt(step, outcome.content)
                elif outcome.resume_proposal is not None:
                    step.update(status="waiting_confirmation", result={"proposal": outcome.resume_proposal.model_dump(mode="json")},
                        proposal_id=outcome.resume_proposal.id)
                    step["wait_started_at"] = time.time()
                elif outcome.status == "awaiting_approval" and outcome.approval:
                    step.update(status="waiting_approval", approval_id=outcome.approval["id"], detail=str(outcome.content))
                elif outcome.status == "awaiting_user":
                    step.update(status="waiting_input", wait_reason="business_input", detail=str(outcome.content),
                        questions=[str(outcome.content)], failure=outcome.failure)
                    if outcome.target_choice:
                        step["target_choice"] = outcome.target_choice
                    plan["questions"] = list(dict.fromkeys([*plan.get("questions", []), str(outcome.content)]))
                else:
                    step.update(status="outcome_indeterminate" if outcome.status == "outcome_indeterminate" else "blocked",
                        detail=str(outcome.content)[:1000], failure=outcome.failure)
                    if outcome.status == "error" and (outcome.failure or {}).get("effect_started") is False:
                        step["status"] = "failed"
            except asyncio.CancelledError:
                # Leave claim durable so restart cannot replay an uncertain write.
                step["execution_phase"] = context.effect_state["phase"]
                persist(plan)
                raise
            except TimeoutError:
                started = context.effect_state["phase"] != "preparing"
                # Read-only tools have no external side effects: a timeout cannot
                # produce an uncertain outcome. Mark them failed (retryable) instead
                # of outcome_indeterminate so the user does not need to manually verify.
                if step.get("effect") == "read":
                    step["failure"] = classify_failure(TimeoutError(), effect="read", started=started).metadata()
                    step.update(status="failed",
                        detail="读取步骤超时；无外部副作用，可以直接说「继续任务」重试")
                else:
                    step["failure"] = classify_failure(TimeoutError(), effect=step["effect"], started=started).metadata()
                    step.update(status="outcome_indeterminate" if step["failure"]["uncertain"] else "failed",
                        detail="步骤超时；结果未确认，不自动重试")
            except Exception as exc:
                started = context.effect_state["phase"] != "preparing"
                step["failure"] = classify_failure(exc, effect=step["effect"], started=started).metadata()
                step.update(status="outcome_indeterminate" if step["failure"]["uncertain"] else "failed",
                    detail=f"步骤异常：{type(exc).__name__}；请查看执行记录，不自动重试")
            finally:
                diagnostic_scope.reset(scope_token)
                step["execution_phase"] = context.effect_state["phase"]
                context.effect_state = previous_effect_state
                context.user_message, context.action_authority = previous_message, previous_authority
            observed.append(step); changed = True
            emit("plan_step", step_id=step["id"], tool=step["tool"], status=step["status"], detail=step.get("detail", ""), failure=step.get("failure"))
            persist(plan)
            if step["status"] in {"failed", "blocked", "outcome_indeterminate"}:
                from .failure_recovery import recover_failed_step
                await recover_failed_step(plan, step, registry, persist)
            if claimed >= EXECUTION_BATCH_SIZE:
                plan["batch_progress"] = {"claimed": claimed, "remaining": sum(s["status"] == "pending" for s in plan["steps"])}
                plan["status"] = "running"
                persist(plan)
                return list({step["id"]: step for step in observed}.values())
        if not changed:
            break
    plan["status"] = "clarifying" if plan.get("questions") else "running" if any(step["status"] not in DONE for step in plan["steps"]) else "finished"
    persist(plan)
    return list({step["id"]: step for step in observed}.values())


async def begin(plan: dict, registry) -> ChatResponse:
    persist(plan, activate=True)
    if plan.get("paused"):
        # Describe the persisted control state, not the model's free-form reply.
        # No synthesis or tool loop may accidentally turn a saved plan into work.
        text = "任务已保存并暂停，本轮没有执行任何步骤。可说「继续任务」后再执行。"
        if plan.get("questions"):
            text += "\n\n待补充：" + "；".join(plan["questions"])
        return ChatResponse(message=text, lane=AgentLane.APPLICATION)
    observed = await advance(plan, registry)
    proposal = next((step.get("result", {}).get("proposal") for step in observed
        if step["status"] == "waiting_confirmation" and isinstance(step.get("result"), dict)), None)
    pending = [step["tool"] for step in plan["steps"] if step["status"] == "pending"]
    from .presentation import compose
    response_started = time.monotonic()
    emit("response_synthesis", status="started", plan_id=plan["id"])
    presentation = await compose(plan, observed, getattr(registry, "response_llm", None))
    emit("response_synthesis", status="finished", plan_id=plan["id"], mode=presentation.mode, reason=presentation.reason,
         elapsed_ms=round((time.monotonic() - response_started) * 1000))
    text = presentation.text or (
        "正在观察原任务的未完成步骤，没有重放已完成动作。" if plan.get("recovery_count") else
        plan["reply"] or "没有识别到可执行目标，请说明希望完成的操作。")
    if pending:
        text += "\n\n待前置步骤完成后：" + "、".join(tool_label(tool) for tool in pending) + "。不会把启动、入队或提案视为最终完成。"
    if plan.get("questions"):
        card_questions = {q for step in plan["steps"]
                          if step.get("status") == "waiting_input" and step.get("target_choice", {}).get("status") == "pending"
                          for q in step.get("questions", [])}
        unanswered = [q for q in plan["questions"] if q not in text and q not in card_questions]
        if unanswered:
            text += "\n\n待补充的分支：\n" + "\n".join(f"{index}. {question}" for index, question in enumerate(unanswered, 1))
    # This selects content, but is not proof that a response was delivered.
    emit("plan_response_selection", plan_id=plan["id"],
         displayed_snapshot_ids=presentation.snapshot_ids,
        presented_step_receipts=[step_receipt(step) for step in observed],
         completed_step_ids=[step["id"] for step in observed if step["status"] == "completed"])
    return ChatResponse(message=text, lane=AgentLane.APPLICATION,
        action="resume_proposal" if proposal else None, resume_proposal=proposal,
        tool_events=[ToolEvent(tool=step["tool"], status=step["status"], detail=step.get("detail", "")[:300]) for step in observed])
