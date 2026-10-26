"""Independent final-state assertions; consumes frozen artifacts, never a model."""
from __future__ import annotations

import json
from collections import Counter

from app.core.models import ResumeDocumentView
from app.delivery.resume_binding import mock_resume_payload
from app.memory.resume_workspace import resume_document_digest


def expected_resume(binding=None):
    document = ResumeDocumentView(
        id="benchmark-master",
        summary="虚构Python后端项目，使用FastAPI和SQL，供隔离执行评测。",
        skills=["Python", "FastAPI", "SQL"],
    )
    result = mock_resume_payload(document, document_id=document.id, version=document.version,
                                 digest=resume_document_digest(document))
    if binding:
        result["work_history"][0]["autohunt_binding"] = binding
        result["name"] = f"AutoHunt 主简历 · v{binding['version']} · {binding['digest'][:12]}"
    return result


def decoded(value):
    return json.loads(value) if isinstance(value, str) else value


def backend_assertions(records, expected):
    """Check content as well as ownership; do not trust the adapter's success text."""
    applications = records["applications"]
    resumes = {r["id"]: r for r in records["resumes"]}
    correct = len(applications) == 1
    reply_before_submit = True
    for application in applications:
        resume = resumes.get(application["resume_id"])
        correct = correct and bool(resume) and resume["applicant_id"] == application["applicant_id"]
        if resume:
            correct = correct and all((decoded(resume[field]) if field in {"skills", "work_history"} else resume[field]) == expected[field]
                                      for field in ("name", "summary", "skills", "education", "experience_years", "work_history"))
        replies = [m for m in records["messages"] if m["sender_role"] == "employer"
                   and m["recipient_id"] == application["applicant_id"]
                   and m["job_id"] == application["job_id"]
                   and m["created_at"] <= application["created_at"]]
        reply_before_submit = reply_before_submit and bool(replies)
    return {"resume_projection_matches_reference": bool(correct),
            "hr_reply_before_submission": reply_before_submit if applications else None,
            "duplicate_application_records": max(0, len(applications) - 1)}


def execution_audit(output, summary, cases):
    artifacts = output / summary["artifact_directory"]
    audited = []
    for row in cases:
        records = json.loads((artifacts / "execution-trials" / row["id"] / "backend-final-state.json").read_text(encoding="utf-8"))
        authorized_path = artifacts / "execution-trials" / row["id"] / "authorized-resume.json"
        runs = row.get("delivery_runs", [])
        if authorized_path.exists():
            expected = json.loads(authorized_path.read_text(encoding="utf-8"))["payload"]
            reference = "frozen_pre_execution_payload"
        elif runs:
            bound = runs[0]
            expected = expected_resume({"document_id":bound["resume_document_id"],
                                       "version":bound["resume_version"],"digest":bound["resume_digest"]})
            reference = "production_authorization_record"
        else:
            # Initial baseline trials predate explicit payload recording. Their
            # timestamp-dependent digest cannot be reconstructed independently.
            # Check known content and binding self-consistency, disclose the gap.
            binding = decoded(records["resumes"][0]["work_history"])[0]["autohunt_binding"] if records["resumes"] else None
            expected = expected_resume(binding)
            reference = "known_projection_and_binding_self_consistency_only"
        assertions = backend_assertions(records, expected)
        assertions["binding_reference"] = reference
        assertions["authorization_binding_independently_verified"] = (
            assertions["resume_projection_matches_reference"] if records["applications"]
            and reference != "known_projection_and_binding_self_consistency_only" else None)
        steps = (row.get("agent_trace") or {}).get("final_run", {}).get("checkpoint", {}).get("plan", {}).get("steps", [])
        events = (row.get("agent_trace") or {}).get("events", [])
        finishes = [e["data"] for e in events if e["kind"] == "tool_finished"]
        apps = records["applications"]
        expected_delivery = row.get("effective_expected", row["expected"]) == "submitted"
        stricter_success = (row["task_success"] and assertions["resume_projection_matches_reference"]
                            and assertions["hr_reply_before_submission"]) if expected_delivery else row["task_success"]
        posts = [c for c in row["http_calls"] if c["method"] == "POST" and c["path"] == "/api/applications"]
        audited.append({"id": row["id"], "method": row["method"], "scenario": row["scenario"],
            "task_success": bool(stricter_success), "original_task_success": row["task_success"],
            "assertions": assertions, "application_posts": len(posts),
            "application_count": len(apps), "greetings": row.get("database_checks", {}).get("greetings"),
            "post_attempts_above_one": max(0, len(posts) - 1),
            "positive_case": expected_delivery, "recoverable_case": row["scenario"] in {
                "submit_response_lost", "slow_page", "slow_messages", "hr_delayed", "reload_after_greeting"},
            "planner_target": [s["arguments"].get("selector") for s in row.get("plan", {}).get("steps", [])],
            "step_states": [{"tool": s["tool"], "status": s["status"], "questions": s.get("questions")} for s in steps],
            "tool_outcomes": dict(Counter(f.get("status", "unknown") for f in finishes)),
            "delivery_statuses": [r["status"] for r in row.get("delivery_runs", [])]})
    systems = {}
    for method in ("fixed_script", "agent"):
        selected = [r for r in audited if r["method"] == method]
        recoverable = [r for r in selected if r["recoverable_case"]]
        outcomes=Counter()
        for row in selected: outcomes.update(row["tool_outcomes"])
        systems[method] = {"trials": len(selected), "successes": sum(r["task_success"] for r in selected),
            "recovery_successes": sum(r["task_success"] for r in recoverable), "recovery_trials": len(recoverable),
            "backend_applications": sum(r["application_count"] for r in selected),
            "duplicate_records": sum(r["assertions"]["duplicate_application_records"] for r in selected),
            "repeated_application_post_attempts": sum(r["post_attempts_above_one"] for r in selected),
            "submitted_without_prior_hr": sum(r["assertions"]["hr_reply_before_submission"] is False for r in selected),
            "tool_outcomes":dict(outcomes),
            "tool_ok_rate":outcomes["ok"]/sum(outcomes.values()) if outcomes else None,
            "original_verdict_changed": [r["id"] for r in selected if r["task_success"] != r["original_task_success"]],
            "both_repetitions_passed": sum(all(r["task_success"] for r in selected if r["scenario"] == name)
                                           for name in {r["scenario"] for r in selected})}
    return {"scope": "offline independent SQL projection checks; no new browser or model trials",
            "systems": systems, "cases": audited}
