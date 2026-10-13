"""Bind result operations to trusted search receipts, never a chat/plan ID.

No natural-language intent parsing. Known references come from producer receipts
or session snapshots; the store validates completion, session and resume ownership.
Unknown model IDs can be repaired only when that trusted context is unambiguous.
"""
from __future__ import annotations

from ..runtime.context import emit
from ..runtime.resilience import PreflightRefused
from .validation import dependency_ancestors


class ResultSourceClarification(PreflightRefused):
    """No write attempted; ask for an evidence source instead of poisoning a task."""


def result_references(dialogue, plan=None):
    rows = []
    for step in (plan or {}).get("steps", []):
        if step.get("tool") in {"job_search_start", "job_results_refilter"} and step.get("task_id"):
            rows.append(dict(task_id=step["task_id"], origin="receipt", step_id=step["id"], status=step.get("status")))
    if (dialogue or {}).get("tasks", {}).get("browser_id"):
        rows.append(dict(task_id=dialogue["tasks"]["browser_id"], origin="session"))
    for snapshot in (dialogue or {}).get("snapshots", []):
        if snapshot.get("kind") == "job" and snapshot.get("displayed") and snapshot.get("version"):
            rows.append(dict(task_id=snapshot["version"], origin="snapshot", snapshot_id=snapshot["id"]))
    return rows


def resolve_search_source(store, context, *, resume_id, explicit=None):
    authority = context.action_authority or {}
    plan = context.plan_reader() if context.plan_reader else None
    bound = authority.get("evidence_task_id")
    if not bound and plan and authority.get("step_id"):
        ancestors = dependency_ancestors(authority["step_id"], plan.get("steps", []))
        producers = [s for s in plan.get("steps", []) if s["id"] in ancestors
                     and s.get("tool") in {"job_search_start", "job_results_refilter"} and s.get("task_id")]
        nearest = [s for s in producers if not any(s["id"] in dependency_ancestors(other["id"], plan["steps"])
                   for other in producers if other is not s)]
        ids = set(s["task_id"] for s in nearest)
        if len(ids) > 1:
            if explicit in ids:
                bound = explicit
            else:
                raise ResultSourceClarification("此步骤依赖多轮搜索，请先明确使用哪一轮结果；没有修改岗位。")
        else:
            bound = next(iter(ids), None)

    def read(ident):
        return store.search_source(ident) if ident else None

    def validate(source):
        if source["session_id"] != context.session_id:
            raise ResultSourceClarification("这轮搜索不属于当前会话，请选择本会话的结果；没有修改岗位。")
        if source["resume_id"] != resume_id:
            raise ResultSourceClarification("这轮搜索绑定的简历与当前简历不同，请核对结果来源；没有修改岗位。")
        if source["status"] != "completed":
            raise ResultSourceClarification("这轮搜索尚未完成，请等待完成或明确另一轮已完成的结果；没有修改岗位。")
        return source["task_id"]

    selected, prescribed = read(explicit), read(bound)
    if prescribed:
        ident = validate(prescribed)
        if selected and validate(selected) != ident:
            raise ResultSourceClarification("指定搜索与前置步骤结果不同，请确认要使用哪一轮；没有修改岗位。")
    elif bound:
        raise ResultSourceClarification("前置步骤没有有效搜索回执，请核对执行记录；没有修改岗位。")
    elif selected:
        return validate(selected)  # Never repair a valid foreign/incomplete search into another one.
    else:
        references = result_references(context.dialogue, plan)
        current = read((context.dialogue or {}).get("tasks", {}).get("browser_id"))
        if current and not explicit:
            ident = validate(current)
        else:
            known = {row["task_id"]: read(row["task_id"]) for row in references}
            eligible = [row for row in known.values() if row and row["status"] == "completed"
                        and row["session_id"] == context.session_id and row["resume_id"] == resume_id]
            if len(eligible) != 1:
                raise ResultSourceClarification("无法唯一确定本会话、当前简历的已完成搜索，请明确结果来源；没有修改岗位。")
            ident = eligible[0]["task_id"]
    if explicit and explicit != ident:
        emit("result_source_normalized", supplied_id=explicit, task_id=ident, reason="unknown_id_unique_trusted_source")
    return ident
