"""Bounded DAG execution; the model never decides whether a step completed."""
from __future__ import annotations

import asyncio
import copy
import time

from ..core.agent_tools import ToolLoopState
from ..core.models import AgentLane, ChatResponse, ToolEvent
from ..growth.requests import resume_stamp
from ..runtime.context import active_run, dialogue_change, emit
from ..runtime.dialogue import bounded_data
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
        "steps": [{key: step.get(key) for key in ("id", "tool", "arguments", "depends_on", "missing_slots", "status")}
                  for step in plan["steps"]]})


def render_step(step: dict) -> str:
    result, tool = step.get("result"), step["tool"]
    if step["status"] not in {"completed", "waiting_external", "waiting_confirmation"}:
        return f"{tool}：{step.get('detail') or step['status']}"
    if not isinstance(result, dict):
        return f"{tool}：{str(result)[:800]}"
    if tool == "search_state_read":
        from ..core.request_summary import search_summary
        return search_summary(result)
    if tool == "job_search_start":
        args = result.get("search_conditions", {})
        return ("已启动搜索：" + " · ".join(args.get("queries") or [])
            + ("；城市：" + "、".join(args["locations"]) if args.get("locations") else "")
            + (f"；目标 {args['result_limit']} 个" if args.get("result_limit") else "")
            + (f"；最低月薪 {args['salary_min']} 元" if args.get("salary_min") else "")
            + "。登录/验证码需人工接管；后续依赖步骤等待搜索完成。")
    if tool == "learning_plan_generate":
        return f"已保存成长计划《{result.get('title', '成长计划')}》，依据 {result.get('jobs_analyzed', 0)} 份 JD。\n劣势分析：{result.get('summary', '')}"
    if tool == "resume_propose_patch":
        return "已生成简历修改提案，尚未写入；请核对并批准。" if result.get("proposal") else "没有生成可验证的简历修改提案。"
    if tool == "job_results_read":
        jobs = result.get("jobs") or []
        return "职位简表（序号以此为准）：\n\n" + ("\n".join(
            f"{index}. {job.get('role', job.get('title', ''))} · {job.get('company', '')} · {job.get('location', '')} · {job.get('salary', '未注明')} · 匹配 {job.get('score', '未评分')}"
            for index, job in enumerate(jobs, 1)) or "暂无符合条件的已保存职位。")
    if isinstance(result.get("message"), str):
        return result["message"]
    if tool == "job_review_selection":
        return f"已通过 {len(result.get('reviewed_ids', []))} 个职位的本地审核，未自动投递。"
    return f"{tool}：\n{bounded_data(result, budget=1800)}"


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
                step.update(status="blocked", detail="用户已撤回此动作，记忆回顾或继续任务不能恢复授权")
                observed.append(step); changed = True
                continue
            if step["effect"] != "read" and (plan["resume_stamp"] != current_stamp or plan["expires_at"] < time.time() or active_plan != plan["id"]):
                step.update(status="blocked", detail="简历变更、原计划取消/替代或授权过期，请重新授权此动作")
                observed.append(step); changed = True
                continue
            task_id = steps.get(step.get("evidence_step"), {}).get("task_id")
            authority = {"tool": step["tool"], "arguments": copy.deepcopy(step["arguments"]),
                "command": step["command_quote"], "sources": [plan["original"], *plan["answers"]],
                "resume_stamp": plan["resume_stamp"], "expires_at": plan["expires_at"],
                "candidate": registry.candidate_state, "resumes": registry.resume_workspace,
                "evidence_task_id": task_id, "plan_id": plan["id"], "step_id": step["id"]}
            step.update(status="claimed")
            persist(plan)  # Durable claim BEFORE the executor or any write.
            emit("plan_step", step_id=step["id"], tool=step["tool"], status="claimed", depends_on=step["depends_on"])
            previous_message, previous_authority = context.user_message, context.action_authority
            context.user_message, context.action_authority = step["command_quote"], authority
            try:
                timeout = 100 if step["tool"] == "learning_plan_generate" else 20
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
    text = "\n\n".join(render_step(step) for step in observed) or plan["reply"] or "没有识别到可执行目标，请说明希望完成的操作。"
    if pending:
        text += "\n\n待前置步骤完成后：" + "、".join(pending) + "。不会把启动、入队或提案视为最终完成。"
    return ChatResponse(message=text, lane=AgentLane.APPLICATION,
        action="resume_proposal" if proposal else None, resume_proposal=proposal,
        tool_events=[ToolEvent(tool=step["tool"], status=step["status"], detail=step.get("detail", "")[:300]) for step in observed])
