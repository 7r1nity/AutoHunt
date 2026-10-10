"""Bounded DAG execution; the model never decides whether a step completed."""
from __future__ import annotations

import asyncio
import copy
import time

from ..core.agent_tools import ToolLoopState
from ..core.models import AgentLane, ChatResponse, ToolEvent
from ..growth.requests import resume_stamp
from ..runtime.context import active_run, dialogue_change, emit
from ..core.replies import tool_reply, tool_label, public_reply
from ..runtime.resilience import classify_failure

DONE = {"completed", "failed", "skipped", "blocked", "outcome_indeterminate"}


def persist(plan: dict) -> None:
    emit("plan_state", plan=plan)
    previous = (active_run().dialogue or {}).get("instruction_request") or {}
    if previous.get("id") != plan["id"] and previous.get("status") == "clarifying" and plan["status"] != "clarifying":
        return  # A background completion must not erase a newer foreground question.
    if previous.get("id") == plan["id"]:
        plan["revoked_tools"] = sorted(set(plan.get("revoked_tools", [])) | set(previous.get("revoked_tools", [])))
    dialogue_change("instruction", request={"revoked_tools": plan.get("revoked_tools", []), "condition_overrides": plan.get("condition_overrides", {}), **{key: plan[key] for key in
        ("id", "original", "answers", "questions", "resume_stamp", "turn", "expires_at", "status", "goals")},
        "understanding": plan.get("understanding"),
        "steps": [{key: step.get(key) for key in ("id", "tool", "arguments", "depends_on", "missing_slots", "status", "authority_binding")}
                  for step in plan["steps"]]})


def render_step(step: dict) -> str:
    result = step.get("result") if step["status"] in {"completed", "waiting_external", "waiting_confirmation"} else step.get("detail")
    return public_reply(tool_reply(step["tool"], step["status"], result))


def completion_lines(plan: dict, events: list[dict]) -> list[str]:
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
    lines = []
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
        lines.append(render_step(step))
    return lines


async def advance(plan: dict, registry) -> list[dict]:
    """Claim writes before execution, continue independent branches only.

    Callers serialize a session and refresh context before resuming. A restored
    claim is indeterminate, never an instruction to repeat a tool call.
    """
    context = active_run()
    observed = []
    steps = {step["id"]: step for step in plan["steps"]}
    for step in plan["steps"]:
        if step["status"] == "claimed":
            step.update(status="outcome_indeterminate", detail="步骤执行期间中断，请核对结果；不会重放")
            observed.append(step)
    while True:
        changed = False
        for step in plan["steps"]:
            if step["status"] != "pending":
                continue
            deps = [steps[key] for key in step["depends_on"]]
            if any(dep["status"] in DONE - {"completed"} for dep in deps):
                step.update(status="skipped", detail="前置步骤未成功，依赖动作未执行")
                observed.append(step); changed = True
                continue
            if not all(dep["status"] == "completed" for dep in deps):
                continue
            current_stamp = resume_stamp(registry.resume_workspace.read_document(), registry.candidate_state)
            active_plan = (context.dialogue or {}).get("tasks", {}).get("plan_id")
            current_instruction = (context.dialogue or {}).get("instruction_request") or {}
            withdrawn = set(plan.get("revoked_tools", [])) | (set(current_instruction.get("revoked_tools", [])) if current_instruction.get("id") == plan["id"] else set())
            if step["tool"] in withdrawn:
                step.update(status="blocked", detail="用户已撤回此动作，记忆回顾或继续任务不能恢复授权",
                    failure={"code": "authority_revoked", "effect_started": False, "uncertain": False})
                observed.append(step); changed = True
                continue
            if step["effect"] != "read" and (plan["resume_stamp"] != current_stamp or plan["expires_at"] < time.time() or active_plan != plan["id"]):
                step.update(status="blocked", detail="简历变更、原计划取消/替代或授权过期，请重新授权此动作")
                step["failure"] = {"code": "authority_stale", "effect_started": False, "uncertain": False}
                observed.append(step); changed = True
                continue
            task_id = steps.get(step.get("evidence_step"), {}).get("task_id")
            authority = {"tool": step["tool"], "arguments": copy.deepcopy(step["arguments"]),
                "command": step["command_quote"], "sources": [plan["original"], *plan["answers"]],
                "resume_stamp": plan["resume_stamp"], "expires_at": plan["expires_at"],
                "candidate": registry.candidate_state, "resumes": registry.resume_workspace,
                "evidence_task_id": task_id, "plan_id": plan["id"], "step_id": step["id"]}
            authority["binding"] = step.get("authority_binding")
            authority["resume_search_task_id"] = step.get("resume_search_task_id")
            step.update(status="claimed")
            persist(plan)  # Durable claim BEFORE the executor or any write.
            emit("plan_step", step_id=step["id"], tool=step["tool"], status="claimed", depends_on=step["depends_on"])
            previous_message, previous_authority = context.user_message, context.action_authority
            context.user_message, context.action_authority = step["command_quote"], authority
            try:
                # A plan over 20 JDs needs the full analysis budget; a 100s cap cut
                # it off mid-write and reported "结果未确认" instead of a plan.
                timeout = 180 if step["tool"] == "learning_plan_generate" else 20
                async with asyncio.timeout(timeout):
                    outcome = await registry.execute(step["tool"], step["arguments"],
                        user_message=step["command_quote"], state=ToolLoopState())
                if outcome.status == "ok":
                    step.update(status="completed", result=outcome.content)
                    if step["tool"] == "job_search_start":
                        step.update(status="waiting_external", task_id=outcome.content["id"], wait_started_at=time.time())
                    if step["tool"] in {"job_results_read", "resume_document_read"} and outcome.content.get("snapshot_id"):
                        dialogue_change("display", snapshot_id=outcome.content["snapshot_id"])
                elif outcome.resume_proposal is not None:
                    step.update(status="waiting_confirmation", result={"proposal": outcome.resume_proposal.model_dump(mode="json")},
                        proposal_id=outcome.resume_proposal.id)
                    step["wait_started_at"] = time.time()
                else:
                    step.update(status="outcome_indeterminate" if outcome.status == "outcome_indeterminate" else "blocked",
                        detail=str(outcome.content)[:1000], failure=outcome.failure)
                    if outcome.status == "error" and (outcome.failure or {}).get("effect_started") is False:
                        step["status"] = "failed"
            except asyncio.CancelledError:
                # Leave claim durable so restart cannot replay an uncertain write.
                raise
            except TimeoutError:
                step["failure"] = classify_failure(TimeoutError(), effect=step["effect"], started=True).metadata()
                step.update(status="outcome_indeterminate" if step["effect"] in {"write", "external"} else "failed",
                    detail="步骤超时；结果未确认，不自动重试")
            except Exception as exc:
                step["failure"] = classify_failure(exc, effect=step["effect"], started=True).metadata()
                step.update(status="outcome_indeterminate" if step["effect"] in {"write", "external"} else "failed",
                    detail=f"步骤异常：{type(exc).__name__}；请查看执行记录，不自动重试")
            finally:
                context.user_message, context.action_authority = previous_message, previous_authority
            observed.append(step); changed = True
            emit("plan_step", step_id=step["id"], tool=step["tool"], status=step["status"], detail=step.get("detail", ""), failure=step.get("failure"))
            persist(plan)
        if not changed:
            break
    plan["status"] = "running" if any(step["status"] not in DONE for step in plan["steps"]) else "finished"
    persist(plan)
    return observed


async def begin(plan: dict, registry) -> ChatResponse:
    if plan["questions"]:
        persist(plan)
        return ChatResponse(message="已记录完整请求，尚未执行。请补充：\n\n" + "\n".join(
            f"{index}. {question}" for index, question in enumerate(plan["questions"], 1)), lane=AgentLane.CANDIDATE)
    if any(step["effect"] != "read" for step in plan["steps"]):
        dialogue_change("task", kind="plan_id", id=plan["id"], status="running", bind=True)
    observed = await advance(plan, registry)
    proposal = next((step.get("result", {}).get("proposal") for step in observed
        if step["status"] == "waiting_confirmation" and isinstance(step.get("result"), dict)), None)
    pending = [step["tool"] for step in plan["steps"] if step["status"] == "pending"]
    text = "\n\n".join(render_step(step) for step in observed) or (
        "正在观察原任务的未完成步骤，没有重放已完成动作。" if plan.get("recovery_count") else
        plan["reply"] or "没有识别到可执行目标，请说明希望完成的操作。")
    if pending:
        text += "\n\n待前置步骤完成后：" + "、".join(tool_label(tool) for tool in pending) + "。不会把启动、入队或提案视为最终完成。"
    # This selects content, but is not proof that a response was delivered.
    emit("plan_response_selection", plan_id=plan["id"],
         completed_step_ids=[step["id"] for step in observed if step["status"] == "completed"])
    return ChatResponse(message=text, lane=AgentLane.APPLICATION,
        action="resume_proposal" if proposal else None, resume_proposal=proposal,
        tool_events=[ToolEvent(tool=step["tool"], status=step["status"], detail=step.get("detail", "")[:300]) for step in observed])
