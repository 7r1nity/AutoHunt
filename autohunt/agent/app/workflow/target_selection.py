"""Read-only target anchors and deterministic dependent set operations."""
from __future__ import annotations

from ..core.job_targets import TargetClarification
from .graph import depend_on
from .validation import dependency_ancestors, PlanValidationError


def bind_target_dependencies(steps):
    by_id = {step["id"]: step for step in steps}
    for consumer in steps:
        for field in ("target_step_id", "exclude_target_step_id"):
            reference = consumer.get("arguments", {}).get(field)
            if not reference:
                continue
            producer = by_id.get(reference)
            if not producer or producer["tool"] != "job_target_resolve":
                raise PlanValidationError("invalid_target_reference", "目标引用必须指向本计划的岗位定位步骤", tool=consumer["tool"], field=field)
            depend_on(steps, consumer, producer)


def prepared_selection(context, jobs, step_id):
    authority = context.action_authority or {}
    plan = context.plan_reader() if context.plan_reader else None
    if not plan or plan.get("id") != authority.get("plan_id") or plan.get("session_id", context.session_id) != context.session_id:
        raise TargetClarification("目标引用不属于本次执行计划，请重新选定岗位。")
    producer = next((s for s in plan["steps"] if s["id"] == step_id), None)
    if (not producer or producer["tool"] != "job_target_resolve" or producer["status"] != "completed"
            or producer.get("withdrawn") or step_id not in dependency_ancestors(authority.get("step_id"), plan["steps"])):
        raise TargetClarification("请先完成本计划的岗位选择；没有移除或投递。")
    selection = (producer.get("result") or {}).get("selection")
    if not selection or not selection.get("items"):
        raise TargetClarification("定位步骤没有可用的选中岗位，请重新选择。")
    for item in selection["items"]:
        job = jobs.get_job(item["id"])
        if (job.get("removed_at") or jobs.review_fingerprint(job) != item["content_hash"]
                or item.get("score") is not None and job.get("score") != item["score"]):
            raise TargetClarification("选中岗位已经移除或内容变化，请重新选择；没有继续执行。")
    return selection


def excluded_selection(selection, jobs):
    scope = selection.get("scope_items", [])
    if selection.get("scope") == "board":
        current = {row.id for row in jobs.list_jobs(limit=201)}
        if current != {item["id"] for item in scope}:
            raise TargetClarification("确认期间看板范围发生变化，请重新查看后选择；没有扩大移除范围。")
    kept = {item["id"] for item in selection["items"]}
    others = []
    for item in scope:
        if item["id"] in kept:
            continue
        job = jobs.get_job(item["id"])
        if job.get("removed_at") or jobs.review_fingerprint(job) != item["content_hash"]:
            raise TargetClarification("待移除岗位证据变化，请重新查看后选择；没有移除任何对象。")
        others.append(item)
    return {**selection, "items": others, "resolution": "prepared_complement", "selector": "除选中岗位外的其余岗位"}
