"""Project current operational state through the global filter policy.

Historical messages, model traces, successful receipts and uncertain effects
remain immutable. Migration never searches, evaluates an unseen JD or sends.
"""
from __future__ import annotations

import copy

from ..core.filter_policy import FilterPolicyRepair, project_arguments, project_keywords


def project_conditions(value: dict | None, *, repairable=False) -> dict:
    original = copy.deepcopy(value or {})
    try:
        result = project_arguments(original)
    except FilterPolicyRepair:
        if not repairable:
            raise
        # Preserve the unresolved text for semantic repair. Reads must not keep
        # an otherwise normal new command from reaching the planner. Execution
        # uses the strict shared projection again before a tool is authorised.
        result = copy.deepcopy(original)
        for key in ("filters", "result_groups", "queries", "excluded_keywords", "query"):
            if key not in original:
                continue
            try:
                result.update(project_arguments({key: original[key]}))
            except FilterPolicyRepair:
                pass
    for key in ("target_roles", "search_keywords", "work_modes"):
        if isinstance(result.get(key), list):
            try:
                result[key] = project_keywords(result[key])
            except FilterPolicyRepair:
                if not repairable:
                    raise
    return result


def _unstarted(step, revoked):
    if step.get("withdrawn") or step.get("tool") in revoked:
        return False
    if step.get("status") not in {"pending", "waiting_input", "waiting_approval", "waiting_binding"}:
        return False
    return not step.get("execution_id") or step.get("execution_phase") in {None, "", "preparing"}


def project_pending_plan(value: dict, *, session_id: str, pause=True):
    """Return a projected copy and changed IDs; never reset an effect's outcome."""
    from ..security.action_grants import action_binding
    result, changed = copy.deepcopy(value), []
    revoked = set(result.get("revoked_tools") or [])
    removed_questions = set()
    for step in result.get("steps") or []:
        if not _unstarted(step, revoked):
            continue
        previous = step.get("arguments") or {}
        arguments = project_conditions(previous)
        if arguments == previous:
            continue
        changed.append(step["id"])
        step["arguments"] = arguments
        changed_fields = {key for key in set(previous) | set(arguments) if previous.get(key) != arguments.get(key)}
        step["missing_slots"] = [slot for slot in step.get("missing_slots") or [] if slot not in changed_fields]
        if not step["missing_slots"]:
            removed_questions.update(step.get("questions") or [])
            step["questions"] = []
        step["authority_binding"] = action_binding(step["tool"], arguments, session_id=session_id,
            requested=step.get("user_requested", (step.get("authority_binding") or {}).get("user_requested", False)))
        for field in ("approval_id", "scope_reset", "failure", "wait_reason", "wait_started_at"):
            step.pop(field, None)
        step["status"] = "pending"
        step["detail"] = "全局筛选策略已更新，已清除禁用维度；此步骤尚未重新执行。"
    if changed:
        result["questions"] = [item for item in result.get("questions") or [] if item not in removed_questions]
        for field in ("verified_conditions", "condition_overrides"):
            if isinstance(result.get(field), dict):
                result[field] = project_conditions(result[field])
        if pause:
            result["revision"] = int(result.get("revision") or 0) + 1
            result.update(paused=True, status="ready", pause_reason="global_filter_policy_updated")
    return result, changed


def project_dialogue(value: dict | None, *, session_id="default", repairable=False) -> dict:
    """Only mutable constraints are projected; source text and facts are retained."""
    result = copy.deepcopy(value or {})
    for frame in (result.get("frames") or {}).values():
        if isinstance(frame.get("slots"), dict):
            frame["slots"] = project_conditions(frame["slots"], repairable=repairable)
    for key in ("delivery_scope", "workflow_request"):
        if isinstance(result.get(key), dict):
            result[key] = project_conditions(result[key], repairable=repairable)
    for key in ("task_request", "workflow_request", "instruction_request"):
        request = result.get(key)
        if not isinstance(request, dict):
            continue
        for field in ("conditions", "verified_conditions", "condition_overrides"):
            if isinstance(request.get(field), dict):
                request[field] = project_conditions(request[field], repairable=repairable)
    request = result.get("instruction_request")
    if isinstance(request, dict) and request.get("steps"):
        try:
            result["instruction_request"], _ = project_pending_plan(request, session_id=session_id, pause=False)
        except FilterPolicyRepair:
            if not repairable:
                raise
    for pending in result.get("pending") or []:
        if pending.get("status") == "pending":
            arguments = project_conditions(pending.get("arguments"), repairable=repairable)
            if arguments != pending.get("arguments"):
                pending.update(arguments=arguments, status="stale")
    return result


def migrate_filter_policy(store, candidate_store=None) -> dict:
    """Explicit startup migration. Returns current sources for separate rechecking.

    New candidate admission is deliberately outside this function: removing a
    condition is not evidence for any remaining salary/location/benefit rule.
    """
    from .store import TERMINAL, now
    result = {"sessions_updated": [], "plans_updated": [], "steps_updated": 0,
              "refilter_sources": [], "candidate_preferences_updated": False, "issues": []}
    if candidate_store is not None and hasattr(candidate_store, "migrate_filter_policy"):
        try:
            result["candidate_preferences_updated"] = candidate_store.migrate_filter_policy()
        except ValueError:
            result["issues"].append({"scope": "preferences", "error": "混合条件需内部重新解释，原记录保留"})
    with store.lock, store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        for row in db.execute("SELECT session_id,conditions FROM session_search_state").fetchall():
            try:
                original = store._load(row["conditions"])
                projected = project_conditions(original)
                if projected != original:
                    projected["revision"] = int(original.get("revision", 0)) + 1
                    db.execute("UPDATE session_search_state SET conditions=?,updated_at=? WHERE session_id=?",
                               (store._encode(projected), now(), row["session_id"]))
                    result["sessions_updated"].append(row["session_id"])
            except ValueError:
                result["issues"].append({"scope": "search:" + row["session_id"], "error": "混合条件需内部重新解释，原记录保留"})
        for row in db.execute("SELECT session_id,state FROM session_dialogue_state").fetchall():
            original = store._load(row["state"])
            source = (original.get("tasks") or {}).get("browser_id")
            if source:
                result["refilter_sources"].append({"session_id": row["session_id"], "task_id": source})
            try:
                projected = project_dialogue(original, session_id=row["session_id"])
                if projected != original:
                    projected["revision"] = int(original.get("revision", 0)) + 1
                    db.execute("UPDATE session_dialogue_state SET state=?,updated_at=? WHERE session_id=?",
                               (store._encode(projected), now(), row["session_id"]))
                    result["sessions_updated"].append(row["session_id"])
            except ValueError:
                result["issues"].append({"scope": "dialogue:" + row["session_id"], "error": "混合条件需内部重新解释，原记录保留"})
        for row in db.execute("SELECT id,session_id,status,checkpoint FROM agent_runs").fetchall():
            if row["status"] in TERMINAL or not row["checkpoint"]:
                continue
            checkpoint = store._load(row["checkpoint"])
            if not checkpoint.get("plan"):
                continue
            try:
                plan, changed = project_pending_plan(checkpoint["plan"], session_id=row["session_id"])
                if not changed:
                    continue
                checkpoint["plan"] = plan
                db.execute("UPDATE agent_runs SET checkpoint=?,status='waiting_user',updated_at=? WHERE id=?",
                           (store._encode(checkpoint), now(), row["id"]))
                marks = ",".join("?" for _ in changed)
                db.execute("UPDATE action_approvals SET status='invalidated',updated_at=? WHERE run_id=? "
                           "AND status IN ('pending','approved') AND step_id IN (" + marks + ")",
                           (now(), row["id"], *changed))
                db.execute("INSERT INTO agent_run_events(run_id,kind,data,created_at) VALUES(?,?,?,?)",
                           (row["id"], "global_filter_policy_migrated", store._encode({"step_ids": changed,
                            "paused": True, "effects_replayed": False}), now()))
                dialogue_row = db.execute("SELECT state FROM session_dialogue_state WHERE session_id=?",
                                          (row["session_id"],)).fetchone()
                if dialogue_row:
                    dialogue = store._load(dialogue_row[0])
                    if (dialogue.get("instruction_request") or {}).get("id") == plan.get("id"):
                        dialogue["instruction_request"] = copy.deepcopy(plan)
                        dialogue["revision"] = int(dialogue.get("revision", 0)) + 1
                        db.execute("UPDATE session_dialogue_state SET state=?,updated_at=? WHERE session_id=?",
                                   (store._encode(dialogue), now(), row["session_id"]))
                        result["sessions_updated"].append(row["session_id"])
                result["plans_updated"].append(row["id"])
                result["steps_updated"] += len(changed)
            except ValueError:
                result["issues"].append({"scope": "plan:" + row["id"], "error": "混合条件需内部重新解释，原记录保留"})
    result["sessions_updated"] = sorted(set(result["sessions_updated"]))
    return result
