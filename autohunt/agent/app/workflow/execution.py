"""Bounded DAG execution; the model never decides whether a step completed."""
from __future__ import annotations

import asyncio
import copy
import time
import uuid

from ..core.agent_tools import ToolLoopState
from ..core.models import AgentLane, ChatResponse, ToolEvent
from ..growth.requests import resume_stamp
from ..runtime.context import active_run, dialogue_change, emit
from ..core.replies import tool_reply, tool_label, public_reply
from ..runtime.resilience import classify_failure
from .contracts import EXECUTION_BATCH_SIZE
from .lifecycle import DONE, input_waits, successful_receipt

RESUME_BOUND_TOOLS = {"job_search_start", "job_results_refilter", "mock_delivery_start",
                      "learning_plan_generate", "resume_propose_patch", "job_review_selection"}


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
        "steps": [{key: step.get(key) for key in ("id", "tool", "arguments", "staged_arguments", "depends_on", "missing_slots", "questions", "status", "authority_binding")}
                  for step in plan["steps"]]})


def render_step(step: dict) -> str:
    result = step.get("result") if step["status"] in {"completed", "waiting_external", "waiting_confirmation"} else step.get("detail")
    return public_reply(tool_reply(step["tool"], step["status"], result))


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
    selected = []
    for step in plan["steps"]:
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
    observed = []
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
            authority = {"tool": step["tool"], "arguments": copy.deepcopy(step["arguments"]),
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
                step.update(status="blocked", detail=str(exc),
                    failure={"code": "action_state_conflict", "effect_started": False, "uncertain": False})
                observed.append(step); changed = True
                context.user_message, context.action_authority = previous_message, previous_authority
                persist(plan)
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
            claimed += 1
            persist(plan)  # Durable claim BEFORE the executor or any write.
            emit("plan_step", step_id=step["id"], tool=step["tool"], status="claimed", depends_on=step["depends_on"])
            try:
                # A plan over 20 JDs needs the full analysis budget; a 100s cap cut
                # it off mid-write and reported "结果未确认" instead of a plan.
                timeout = 180 if step["tool"] in {"learning_plan_generate", "job_results_refilter"} or (
                    step["tool"] == "mock_delivery_start" and step["arguments"].get("filters")) else 20
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
                step["failure"] = classify_failure(TimeoutError(), effect=step["effect"], started=started).metadata()
                step.update(status="outcome_indeterminate" if step["failure"]["uncertain"] else "failed",
                    detail="步骤超时；结果未确认，不自动重试")
            except Exception as exc:
                started = context.effect_state["phase"] != "preparing"
                step["failure"] = classify_failure(exc, effect=step["effect"], started=started).metadata()
                step.update(status="outcome_indeterminate" if step["failure"]["uncertain"] else "failed",
                    detail=f"步骤异常：{type(exc).__name__}；请查看执行记录，不自动重试")
            finally:
                step["execution_phase"] = context.effect_state["phase"]
                context.effect_state = previous_effect_state
                context.user_message, context.action_authority = previous_message, previous_authority
            observed.append(step); changed = True
            emit("plan_step", step_id=step["id"], tool=step["tool"], status=step["status"], detail=step.get("detail", ""), failure=step.get("failure"))
            persist(plan)
            if claimed >= EXECUTION_BATCH_SIZE:
                plan["batch_progress"] = {"claimed": claimed, "remaining": sum(s["status"] == "pending" for s in plan["steps"])}
                plan["status"] = "running"
                persist(plan)
                return observed
        if not changed:
            break
    plan["status"] = "clarifying" if plan.get("questions") else "running" if any(step["status"] not in DONE for step in plan["steps"]) else "finished"
    persist(plan)
    return observed


async def begin(plan: dict, registry) -> ChatResponse:
    persist(plan, activate=True)
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
        text += "\n\n待补充的分支：\n" + "\n".join(f"{index}. {question}" for index, question in enumerate(plan["questions"], 1))
    # This selects content, but is not proof that a response was delivered.
    emit("plan_response_selection", plan_id=plan["id"],
         displayed_snapshot_ids=presentation.snapshot_ids,
         completed_step_ids=[step["id"] for step in observed if step["status"] == "completed"])
    return ChatResponse(message=text, lane=AgentLane.APPLICATION,
        action="resume_proposal" if proposal else None, resume_proposal=proposal,
        tool_events=[ToolEvent(tool=step["tool"], status=step["status"], detail=step.get("detail", "")[:300]) for step in observed])
