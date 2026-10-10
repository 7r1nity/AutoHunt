"""Run with `python -m evaluation.run`; model calls require --models-consented.

The only persistent application store used by offline tests is a new temporary
SQLite database. This module never imports app.main or core.dependencies, never
connects to the user's browser, and never starts a search or sends an application.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import gc
import hashlib
import importlib.metadata
import itertools
import json
import subprocess
import sys
import tempfile
import time
import traceback
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS

from .corpus import VERSION, corpus, partition_key
from app.core.agent_tools import AgentToolOutcome, AgentToolPolicy, AgentToolRegistry, ToolLoopState
from app.core.job_filters import FilterExpression, combine, evaluate_filter
from app.core.search_contract import explicit_search_constraints, monthly_salary_lower_bound
from app.core.search_language import parse_search_message
from app.llm.base import LLMResult
from app.runtime.context import RunContext, current_run, active_run
from app.runtime.dialogue import apply_operation, bounded_data, resolve_reference, slot_answer_patch
from app.runtime.resilience import ProgressWatch, WatchLimits, WatchdogExpired, classify_failure, guarded_operation, safe_retry
from app.runtime.search_state import merge_search_state, prepare_search_state
from app.runtime.store import RunStore
from app.runtime.understanding import turn_role
from app.workflow.execution import advance
from app.workflow.planning import Goal, PlannedStep, RequestDraft, command_grounded, plan_request, validate_draft
from app.workflow.requests import TaskRequest, canonical_request, resolve_continuation
from app.workflow.validation import PlanValidationError, preflight, preflight_warnings

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = AgentToolRegistry(jobs=None, browser=None, candidate_state=None,
    resume_workspace=None, memory=None, learning_plans=NS(), mock_workflow=NS())


class ContractFailure(AssertionError):
    def __init__(self, message, actual):
        super().__init__(message)
        self.actual = actual


def check(condition, actual, message="contract mismatch"):
    if not condition:
        raise ContractFailure(message, actual)
    return actual


def rejects(fn):
    try:
        fn()
        return False
    except (ValueError, KeyError):
        return True


def subset(expected, actual):
    return all(actual.get(key) == value for key, value in expected.items())


def leaf(text="合成筛选条件"):
    return {"op": "condition", "text": text}


async def evaluate(case, scratch):
    kind, p, e = case["kind"], case["payload"], case["expected"]
    if kind == "semantics":
        from .semantics_contract import evaluate as semantics_evaluate
        return await semantics_evaluate(case, REGISTRY)
    if kind == "authority":
        message = p["message"]
        parsed = parse_search_message(message)
        grounded = command_grounded("job_search_start", message, [message])
        allowed, reason = AgentToolPolicy().authorize(REGISTRY._tools["job_search_start"], {}, message, ToolLoopState())
        actual = {"parser": parsed.authorized, "grounding": grounded, "policy": allowed,
                  "complete": parsed.complete, "fields": parsed.fields, "reason": parsed.reason, "policy_reason": reason}
        check(all(actual[x] == e["authorized"] for x in ("parser", "grounding", "policy")), actual,
              "legal command blocked" if e["authorized"] else "non-command authorized")
        if not parsed.complete:
            check(not any(key in parsed.fields for key in ("query", "queries", "direction")), actual, "incomplete prose leaked into keywords")
        return actual
    if kind == "control":
        actual = {"action": parse_search_message(p["message"]).action}
        return check(subset(e, actual), actual)
    if kind == "slots":
        actual = explicit_search_constraints(p["message"])
        expected = {k: v for k, v in e.items() if k != "absent"}
        return check(subset(expected, actual) and all(k not in actual for k in e.get("absent", [])), actual, "slot extraction/scope mismatch")
    if kind == "conflict":
        actual = {"reject": rejects(lambda: explicit_search_constraints(p["message"]))}
        return check(subset(e, actual), actual)
    if kind == "salary":
        actual = {"lower": monthly_salary_lower_bound(p["salary"])}
        return check(subset(e, actual), actual)
    if kind == "state":
        actual = merge_search_state(prepare_search_state(p["base"]), p["patch"])
        return check(subset(e, actual), actual)
    if kind == "state_invalid":
        actual = {"reject": rejects(lambda: merge_search_state(prepare_search_state({"queries": ["金融分析师"]}), p["patch"]))}
        return check(subset(e, actual), actual)
    if kind == "continuation":
        original = "帮我找15份招聘信息，城市上海 深圳 北京 杭州，薪资15k以上，85分以上自动投递，85以下生成成长计划"
        record = TaskRequest(original=original, status=p["status"], resume_stamp="synthetic-v1", execution_started=p["started"], updated_at=1000)
        actual = resolve_continuation({"task_request": record.model_dump()}, None,
            "synthetic-v2" if p["changed"] else "synthetic-v1", now=1100)
        check(actual.action == e["action"], vars(actual), "continuation binds wrong executor/task")
        if actual.action == "replan":
            check(actual.original == original, vars(actual), "immutable request lost")
        return vars(actual)
    if kind == "turn":
        actual = vars(turn_role(p["message"]))
        return check(subset(e, actual), actual)
    if kind == "reference":
        state = apply_operation(None, {"op": "snapshot", "topic": "applications", "kind": "job", "source": "synthetic-list",
            "items": [{"id": 1000 + i, "title": f"合成职位{i}"} for i in range(1, p["size"] + 1)]}, clock=1000)
        try:
            result = resolve_reference(state, kind="job", selector=f"第{p['index']}个", read_only=p["read_only"], clock=3000 if p["expired"] else 1100)
            actual = {"reject": False, "id": result["items"][0]["id"]}
        except ValueError as exc:
            actual = {"reject": True, "detail": str(exc)}
        return check(actual["reject"] == e["reject"] and (e["reject"] or actual["id"] == e["id"]), actual)
    if kind == "pending":
        state = apply_operation(None, {"op": "propose", "topic": "search", "tool": "job_search_start", "arguments": {},
            "description": "合成搜索确认", "source": "synthetic", "conditions": {"revision": 1, "resume_stamp": "v1"}}, clock=1000)
        action_id = state["pending"][0]["id"]
        def claim():
            nonlocal state
            state = apply_operation(state, {"op": "decision", "action_id": action_id, "decision": "claim",
                "conditions": {"revision": 2 if p["changed"] else 1, "resume_stamp": "v1"}}, clock=2000 if p["expired"] else 1100)
            if p["reused"]:
                state = apply_operation(state, {"op": "decision", "action_id": action_id, "decision": "claim", "conditions": {}}, clock=1100)
        actual = {"reject": rejects(claim)}
        return check(subset(e, actual), actual)
    if kind == "slot_answer":
        state = apply_operation(None, {"op": "question", "topic": "search", "question": "请补充合成槽位",
            "missing_slots": [p["missing"]], "source": "synthetic"}, clock=1000)
        actual = {"patch": slot_answer_patch(state, p["message"], clock=2000 if p["stale"] else 1100)}
        return check(subset(e, actual), actual)
    if kind == "truth":
        expr = FilterExpression.model_validate({"op": p["op"], "children": [leaf(str(i)) for i, _ in enumerate(p["verdicts"])]})
        actual = {"status": combine(expr, iter(p["verdicts"]))}
        return check(subset(e, actual), actual)
    if kind == "evidence":
        flavor, skill = p["flavor"], p["skill"]
        item = {"index": 0, "status": "fail" if flavor == "valid_fail" else "pass", "reason": "合成模型响应",
                "evidence": [{"field": "description", "quote": f"要求{skill}"}]}
        if flavor == "no_evidence": item["evidence"] = []
        if flavor == "fabricated_quote": item["evidence"][0]["quote"] = "伪造的原文"
        if flavor == "wrong_field": item["evidence"][0]["field"] = "invented"
        response = {"conditions": [] if flavor == "missing_index" else [item, item] if flavor == "duplicate_index" else [item]}
        async def generated(_):
            if flavor == "cancel": raise asyncio.CancelledError()
            return LLMResult("not JSON" if flavor == "invalid_json" else json.dumps(response, ensure_ascii=False),
                "mock" if flavor == "mock_provider" else "synthetic-stub", "fixture")
        try:
            actual = await evaluate_filter(generated, leaf(f"具备{skill}"), {"description": f"要求{skill}"})
        except asyncio.CancelledError:
            actual = {"status": "cancel"}
        return check(subset(e, actual), actual)
    if kind == "failure":
        err = {"timeout": TimeoutError(), "connection": ConnectionError(), "permission": PermissionError(), "invalid": ValueError(),
               "generic": RuntimeError(), "429": RuntimeError(), "503": RuntimeError(), "401": RuntimeError()}[p["error"]]
        if p["error"].isdigit(): err.status_code = int(p["error"])
        actual = classify_failure(err, effect=p["effect"], started=p["started"]).metadata()
        return check(subset(e, actual), actual)
    if kind == "context_budget":
        output = bounded_data({"status": "synthetic", "items": [{"id": f"case-{i}", "text": "评估文本" * p["size"]} for i in range(p["size"])]}, budget=p["budget"])
        value = json.loads(output)
        return check(len(output) <= p["budget"] and value.get("status") == "synthetic", {"chars": len(output), "value": value})
    if kind == "preflight":
        steps = [PlannedStep(id="a", tool="job_stats_read", command_quote="查看统计").model_dump(),
                 PlannedStep(id="b", tool="job_results_read", command_quote="查看结果", depends_on=["a"]).model_dump()]
        goals = [Goal(quote="查看统计", disposition="steps", step_ids=["a", "b"]).model_dump()]
        flaw = p["flaw"]
        if flaw == "cycle": steps[0]["depends_on"] = ["b"]
        if flaw == "duplicate": steps[1]["id"] = "a"
        if flaw == "orphan": goals[0]["step_ids"] = ["a"]
        if flaw == "invalid_citation": steps[1]["command_quote"] = "不存在的命令"
        if flaw == "unknown_tool": steps[1]["tool"] = "unknown"
        if flaw == "missing_dependency": steps[1]["depends_on"] = ["missing"]
        actual = {"reject": rejects(lambda: preflight(steps, goals, ["查看统计，查看结果"], {"job_stats_read", "job_results_read"}))}
        if flaw == "invalid_citation":
            # Citation fidelity is reported, not enforced. No regex can promise that a
            # generated plan is faithful to the user's phrasing, and refusing on it
            # produced one rejection per paraphrase (a double space was enough), so the
            # finding is surfaced for the confirmation step instead of blocking the plan.
            # Reported alongside `reject` so the corpus states the new expectation
            # instead of silently dropping the check.
            actual["warned"] = any(
                issue.get("code") == "invalid_citation" and issue.get("severity") == "warn"
                for issue in preflight_warnings())
        return check(subset(e, actual), actual)
    if kind == "database":
        store = RunStore(scratch / f"{case['id']}.db")
        row, created = store.create("default", "synthetic-key", "chat", {"message": "合成请求"})
        same, again = store.create("default", "synthetic-key", "chat", {"message": "合成请求"})
        conflict = rejects(lambda: store.create("default", "synthetic-key", "chat", {"message": "其他请求"}))
        store.update(row["id"], status=p["status"])
        other = store.create_session("合成第二会话")
        store.save_search_state("default", {"queries": ["金融分析师"], "result_limit": 15})
        fresh = RunStore(store.path)
        actual = {"deduplicated": created and not again and same["id"] == row["id"], "conflict_rejected": conflict,
            "status": fresh.update(row["id"], status="running")["status"], "session_isolated": fresh.search_state(other["id"]) is None,
            "persisted_limit": fresh.search_state("default")["result_limit"]}
        return check(actual["deduplicated"] and conflict and actual["status"] == p["status"] and actual["session_isolated"] and actual["persisted_limit"] == 15, actual)
    if kind == "execution":
        return await execution_case(p["scenario"], scratch / (case["id"] + ".db"))
    if kind == "gold_plan":
        draft = make_gold_plan(p)
        actual = validate_draft(draft, REGISTRY, p["message"], dialogue={}, conditions={"queries": ["金融分析师"], "source": "mock"}, stamp="synthetic")
        by_tool = {s["tool"]: s for s in actual["steps"]}
        search, delivery, learning = (by_tool[t] for t in ("job_search_start", "mock_delivery_start", "learning_plan_generate"))
        checks = {"ready": actual["status"] == "ready", "count": search["arguments"].get("result_limit") == p["count"],
            "search_salary": search["arguments"].get("salary_min") == p["floor"], "cities": search["arguments"].get("locations") == ["深圳", "杭州"],
            "delivery": delivery["arguments"].get("min_score") == p["score"], "learning_score": learning["arguments"].get("max_score") == e["learning_max"],
            "learning_salary": learning["arguments"].get("salary_min") == p["branch_floor"],
            "dependencies": all(search["id"] in s["depends_on"] for s in actual["steps"] if s != search)}
        return check(all(checks.values()), {"checks": checks, "plan": actual})
    raise ValueError(f"Unknown evaluator {kind}")


def make_gold_plan(p):
    clauses = p["message"].split("，")
    commands = ["，".join(clauses[:3]), clauses[3], clauses[4], clauses[5]]
    steps = [PlannedStep(id="search", tool="job_search_start", command_quote=commands[0],
                arguments={"result_limit": p["count"], "salary_min": p["floor"], "locations": ["深圳", "杭州"]}),
             PlannedStep(id="delivery", tool="mock_delivery_start", command_quote=commands[1], scope_quote=commands[1],
                arguments={"min_score": p["score"]}, depends_on=["search"]),
             PlannedStep(id="learning", tool="learning_plan_generate", command_quote=commands[2], scope_quote=commands[2],
                arguments={"max_score": p["score"] - 1, "salary_min": p["branch_floor"]}, depends_on=["search"]),
             PlannedStep(id="summary", tool="job_results_read", command_quote=commands[3], arguments={"summary": True}, depends_on=["search"])]
    return RequestDraft(goals=[Goal(quote=s.command_quote, disposition="steps", step_ids=[s.id]) for s in steps], steps=steps)


async def execution_case(scenario, database):
    """Production DAG + context + durable SQLite; only executors are stubs."""
    events, calls = [], []
    store = RunStore(database)
    row, _ = store.create("default", "synthetic-workflow", "chat", {"message": "合成任务"})
    initial_state = store.mutate_dialogue("default", {"op": "task", "kind": "plan_id", "id": "synthetic-plan", "bind": True})
    def observe(kind, data):
        events.append({"kind": kind, **copy.deepcopy(data)})
        store.event(row["id"], kind, data)
    candidate = NS(read=lambda: NS(profile=NS(resume_id="synthetic-resume")))
    document = NS(id="synthetic-document", version=1)
    resumes = NS(read_document=lambda: document)
    stamp = "synthetic-resume:synthetic-document:1"
    context = RunContext(row["id"], asyncio.current_task(), observe, lambda _: None,
        dialogue=initial_state, mutate_dialogue=lambda op: store.mutate_dialogue("default", op), user_message="合成任务")
    token = current_run.set(context)
    plan = dict(id="synthetic-plan", original="合成任务", answers=[], questions=[], resume_stamp=stamp, turn=0,
        expires_at=time.time() + 600, status="ready", goals=[], steps=[], revoked_tools=[])
    for i, name in enumerate(("job_search_start", "job_results_read", "mock_delivery_start", "learning_plan_generate")):
        plan["steps"].append(dict(id=str(i), tool=name, arguments={}, command_quote="合成任务", scope_quote="", missing_slots=[],
            depends_on=[] if i == 0 else ["0"], status="pending", effect="read" if i == 1 else "external" if i == 0 else "write", result=None))
    if scenario == "restored_claim": plan["steps"][0]["status"] = "claimed"
    if scenario == "expired": plan["expires_at"] = time.time() - 1
    if scenario == "resume_changed": document.version = 2
    if scenario == "revoked": plan["revoked_tools"] = ["job_search_start"]
    if scenario == "independent_branch": plan["steps"][1]["depends_on"] = []

    async def execute(name, arguments, **_):
        # Claim must already be committed before any synthetic effect runs.
        durable_steps = store.dialogue_state("default")["instruction_request"]["steps"]
        check(any(s["tool"] == name and s["status"] == "claimed" for s in durable_steps), durable_steps, "executor ran before durable claim")
        calls.append(name)
        if name == "job_search_start":
            if scenario in {"failure", "independent_branch"}: raise RuntimeError("合成搜索失败")
            if scenario == "cancel": raise asyncio.CancelledError()
            if scenario in {"denied", "uncertain"}:
                return AgentToolOutcome(name, "denied" if scenario == "denied" else "outcome_indeterminate", "合成拒绝/不确定", "external")
            return AgentToolOutcome(name, "ok", {"id": "synthetic-browser"}, "external")
        return AgentToolOutcome(name, "ok", {"synthetic": True}, "read")
    registry = NS(resume_workspace=resumes, candidate_state=candidate, execute=execute)
    cancelled = False
    try:
        try:
            await advance(plan, registry)
            first_calls = list(calls)
            if scenario in {"success", "repeat_advance"}:
                check(first_calls == ["job_search_start"], first_calls, "dependents ran before async completion")
                plan["steps"][0]["status"] = "completed"  # Observed synthetic completion.
                await advance(plan, registry)
                await advance(plan, registry)
        except asyncio.CancelledError:
            cancelled = True
        statuses = [s["status"] for s in plan["steps"]]
        reopened = RunStore(database)
        persisted = [s["status"] for s in reopened.dialogue_state("default")["instruction_request"]["steps"]]
        actual = {"calls": calls, "statuses": statuses, "database_statuses": persisted, "cancelled": cancelled, "trace": events}
        check(persisted == statuses, actual, "database final state differs from DAG")
        if scenario in {"success", "repeat_advance"}:
            check(calls == [s["tool"] for s in plan["steps"]] and statuses == ["completed"] * 4, actual, "repeat or lost dependent")
        elif scenario == "waiting": check(calls == ["job_search_start"] and statuses[1:] == ["pending"] * 3, actual)
        elif scenario == "cancel": check(cancelled and statuses[0] == "claimed" and len(calls) == 1, actual, "cancel must preserve uncertain claim")
        elif scenario == "independent_branch": check(calls == ["job_search_start", "job_results_read"] and statuses[2:] == ["skipped"] * 2, actual)
        else:
            check(len(calls) <= 1 and statuses[1:] == ["skipped"] * 3, actual, "effect continued after blocked/failed predecessor")
        return actual
    finally:
        current_run.reset(token)


async def supplementary(scratch):
    """Adversarial lifecycle, idempotency, routing and schema cases."""
    specs = []
    from .semantics_contract import cases as semantic_cases
    specs.extend(semantic_cases())
    for scenario in ("retry_read", "no_retry_write", "permission_no_retry", "cancel_retry", "watch_idle", "watch_human", "watch_active", "joined_cancel", "root_after_repair", "retained_idle", "duplicate_tool", "loop_budget", "resume_refresh", "filter_source", "filter_depth", "filter_unknown_operator"):
        specs.append(dict(id="adversarial-" + scenario, kind="adversarial", family=scenario, split="challenge", payload={"scenario": scenario}, expected={"contract": scenario}))
    # Small-model policy, with no small-model inference: this tests degradation,
    # not the accuracy or calibration of a configured small model.
    for confidence, margin, source, intent in itertools.product((0.1, 0.6, 0.99, float("nan")), (0.01, 0.3), ("reported", "logprobs"), ("search_jobs", "job_stats", "none")):
        specs.append(dict(id=f"routing-{len(specs)}", kind="routing", family="small_policy", split="development",
            payload={"confidence": confidence if confidence == confidence else "nan", "margin": margin, "source": source, "intent": intent}, expected={"no_unsafe_direct": True}))
    from .browser_fixture import SCENARIOS
    for scenario in SCENARIOS:
        specs.append(dict(id="browser-" + scenario, kind="browser", family="dom_adapter", split="challenge", payload={"scenario": scenario}, expected={"contract": scenario}))
    for scenario in ("one_use", "wrong_token", "expired", "resume_version", "resume_digest", "preview_tamper", "facts_changed", "unknown_filter"):
        specs.append(dict(id="delivery-" + scenario, kind="delivery_contract", family="preview_authorization", split="challenge", payload={"scenario": scenario}, expected={"reject": scenario != "one_use"}))
    for scenario in ("correct", "empty", "version", "digest", "document"):
        specs.append(dict(id="binding-" + scenario, kind="delivery_contract", family="resume_binding", split="challenge", payload={"scenario": scenario}, expected={"reject": scenario != "correct"}))
    for source in ("BOSS直聘", "智联招聘", "真实网站", "模拟网站"):
        specs.append(dict(id="delivery-policy-" + source, kind="delivery_contract", family="real_site_disabled", split="challenge",
            payload={"source": source}, expected={"allowed": source == "模拟网站"}))
    return specs


async def delivery_contract(case, scratch):
    from app.core.models import ResumeDocumentView
    from app.delivery.resume_binding import mock_resume_payload
    from app.memory.resume_workspace import resume_document_digest
    from app.jobs.store import JobStore
    p, e = case["payload"], case["expected"]
    if case["family"] == "real_site_disabled":
        allowed, reason = AgentToolPolicy().authorize(REGISTRY._tools["mock_delivery_start"], {"min_score": 90},
            f"在{p['source']}匹配90分以上自动投递", ToolLoopState())
        return check(allowed == e["allowed"], {"allowed": allowed, "reason": reason})
    scenario = p["scenario"]
    if case["family"] == "resume_binding":
        doc = ResumeDocumentView(summary="合成候选人的真实报告证据" if scenario != "empty" else "")
        digest = resume_document_digest(doc)
        rejected = rejects(lambda: mock_resume_payload(doc, document_id="changed" if scenario == "document" else doc.id,
            version=2 if scenario == "version" else doc.version, digest="changed" if scenario == "digest" else digest))
        return check(rejected == e["reject"], {"reject": rejected})
    store = JobStore(scratch / (case["id"] + ".db"))
    # Completely synthetic database; no browser/server executor exists here.
    with store._connect() as db:
        db.execute("INSERT INTO jobs (id,dedupe_key,source,source_url,title,company,location,salary,description,tags,content_hash,first_seen_at,last_seen_at) VALUES (1,'synthetic','mock','http://127.0.0.1:8080/job.html?id=999999','合成职位','合成企业','上海','15-25K','合成JD','[]','synthetic','2026-01-01','2026-01-01')")
    preview = store.create_delivery_preview(1, "合成简历", resume_document_id="synthetic", resume_version=1, resume_digest="synthetic-digest")
    if scenario in {"expired", "preview_tamper", "facts_changed", "unknown_filter"}:
        with store._connect() as db:
            if scenario == "expired": db.execute("UPDATE delivery_previews SET expires_at='2026-01-01T00:00:00+00:00'")
            if scenario == "preview_tamper": db.execute("UPDATE delivery_previews SET payload_json='{}'")
            if scenario == "facts_changed": db.execute("UPDATE jobs SET salary='10-15K'")
            if scenario == "unknown_filter": db.execute("UPDATE jobs SET filter_status='unknown'")
    def attempt():
        authority, token, job_id, _ = store.authorize_delivery(preview.id, resume_document_id="synthetic",
            resume_version=2 if scenario == "resume_version" else 1, resume_digest="changed" if scenario == "resume_digest" else "synthetic-digest")
        consumed = store.consume_delivery_authorization(authority, "wrong" if scenario == "wrong_token" else token)
        check(consumed == job_id == 1, {"consumed": consumed})
        if scenario == "one_use":
            check(rejects(lambda: store.consume_delivery_authorization(authority, token)), {"one_use": True}, "token replay allowed")
    rejected = rejects(attempt)
    return check(rejected == e["reject"], {"reject": rejected, "scenario": scenario})


async def adversarial(case, scratch):
    from app.core.job_filters import validate_filter
    from app.intent.router import IntentRouter
    from app.intent.store import RouterStore
    if case["kind"] == "routing":
        p = case["payload"]
        router = IntentRouter(rule_engine=NS(), small_model=NS(), store=RouterStore(scratch / "router.db"))
        decision = NS(intent=p["intent"], confidence=float(p["confidence"]), margin=p["margin"], confidence_source=p["source"])
        actual = vars(router._apply_small_policy(decision))
        low = p["source"] == "reported" or decision.confidence != decision.confidence or decision.confidence < 0.9 or decision.margin < 0.15
        return check(not low or actual["decision"] == "llm", actual, "weak/reported confidence bypassed main model")
    scenario = case["payload"]["scenario"]
    if scenario in {"retry_read", "no_retry_write", "permission_no_retry", "cancel_retry"}:
        attempts = 0
        async def operation():
            nonlocal attempts
            attempts += 1
            if scenario == "permission_no_retry": raise PermissionError()
            if scenario == "cancel_retry": raise asyncio.CancelledError()
            if attempts < 3: raise ConnectionError()
            return "ok"
        try:
            await safe_retry(operation, retry_safe=scenario == "retry_read" or scenario in {"permission_no_retry", "cancel_retry"})
        except (ConnectionError, PermissionError, asyncio.CancelledError):
            pass
        return check(attempts == (3 if scenario == "retry_read" else 1), {"attempts": attempts})
    if scenario.startswith("watch_"):
        watch = ProgressWatch(WatchLimits(active=10, idle=5, human=7), now=0)
        watch.check("same", now=0, human=scenario == "watch_human")
        try:
            watch.check("new" if scenario == "watch_active" else "same", now=11 if scenario == "watch_active" else 8 if scenario == "watch_human" else 6, human=scenario == "watch_human")
            actual = {"code": None}
        except WatchdogExpired as exc: actual = {"code": exc.code}
        return check(actual["code"] == {"watch_idle": "no_progress", "watch_human": "human_timeout", "watch_active": "active_timeout"}[scenario], actual)
    if scenario == "joined_cancel":
        joined = asyncio.Event()
        async def worker():
            try: await asyncio.Future()
            finally: joined.set()
        task = asyncio.create_task(guarded_operation(worker(), lambda: ("same", False), limits=WatchLimits(), interval=0.01))
        await asyncio.sleep(0.01)
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
        return check(joined.is_set(), {"worker_joined": joined.is_set()})
    if scenario == "root_after_repair":
        original = "帮我找15份金融分析师工作，月薪15k以上，85分以上自动投递，低于85分生成成长计划"
        record = TaskRequest(original=original, status="timed_out", resume_stamp="synthetic")
        history = [{"role": "user", "content": original}, {"role": "assistant", "content": "没有执行任务：规划超时"},
                   {"role": "user", "content": "？？？"}, {"role": "user", "content": "我要你继续我第一次发送给你的任务"}]
        selected = canonical_request({"task_request": record.model_dump()}, history)
        return check(selected is not None and selected.original == original, {"original": selected.original if selected else None})
    if scenario == "retained_idle":
        s = apply_operation(None, {"op": "slots", "conditions": {"queries": ["金融分析师"], "result_limit": 15, "salary_min": 15000}}, clock=1000)
        s = apply_operation(s, {"op": "begin", "message": "再次搜索"}, clock=100000)
        return check(s["frames"]["search"]["slots"]["result_limit"] == 15, s)
    if scenario in {"duplicate_tool", "loop_budget"}:
        state = ToolLoopState()
        count = 2 if scenario == "duplicate_tool" else 9
        outcomes = [AgentToolPolicy().authorize(REGISTRY._tools["job_stats_read"], {} if scenario == "duplicate_tool" else {"x": i}, "查看统计", state)[0] for i in range(count)]
        return check(outcomes[0] and not outcomes[-1], {"allowed": outcomes})
    if scenario == "resume_refresh":
        prefs = NS(target_roles=NS(source="resume", values=[]), search_keywords=NS(source="resume", values=[]), locations=NS(source="resume", values=[]), salary_min=NS(source="resume", value=None))
        candidate = NS(profile=NS(resume_id="r2", updated_at=datetime.now(UTC), role_suggestions=["金融分析师"]), preferences=prefs)
        state = prepare_search_state({"queries": ["AI工程师"], "query_source": "resume"}, candidate, 2)
        pinned = prepare_search_state({"queries": ["AI工程师"], "query_source": "session"}, candidate, 2)
        return check(state["queries"] == ["金融分析师"] and pinned["queries"] == ["AI工程师"], {"derived": state, "pinned": pinned})
    if scenario.startswith("filter_"):
        node = leaf("用户没有说的条件")
        sources = ["用户实际条件"] if scenario == "filter_source" else ()
        if scenario == "filter_depth":
            for _ in range(4): node = {"op": "not", "children": [node]}
        if scenario == "filter_unknown_operator": node["op"] = "xor"
        return check(rejects(lambda: validate_filter(node, sources)), {"rejected": True})
    raise ValueError(scenario)


class ModelBudgetExceeded(PermissionError):
    pass


def consented_provider(limit, traces):
    """Read ONLY model configuration/credentials, never application content.

    The live dependency graph is intentionally not imported. No settings are
    saved. Actual outbound requests (including planner repair) share one budget.
    """
    from dotenv import dotenv_values
    from cryptography.fernet import Fernet
    from app.llm.deepseek_provider import DeepSeekProvider
    env = dotenv_values(ROOT / ".env")
    import os
    env = {**env, **os.environ}
    data_root = Path(env.get("AUTOHUNT_DATA_ROOT") or str(ROOT / "data"))
    if not data_root.is_absolute(): data_root = ROOT / data_root
    config = json.loads((data_root / "settings/llm.json").read_text(encoding="utf-8"))
    if config["provider"] != "deepseek":
        raise RuntimeError("Configured provider is not consented DeepSeek")
    key = env.get("DEEPSEEK_API_KEY")
    credential = data_root / "private/llm-credentials.json.enc"
    if credential.exists():
        cipher = Fernet(env["AUTOHUNT_DATA_KEY"].encode())
        key = json.loads(cipher.decrypt(credential.read_text(encoding="utf-8").encode()).decode()).get("deepseek") or key

    class BudgetProvider(DeepSeekProvider):
        def __init__(self):
            super().__init__(api_key=key, model=config["model"])
            self.calls = 0
        async def _completion(self, **kwargs):
            if self.calls >= limit: raise ModelBudgetExceeded("Synthetic model evaluation call budget exhausted")
            self.calls += 1
            started = time.perf_counter()
            context = active_run()
            entry = {"call": self.calls, "case_id": context.run_id if context else None,
                     "model": self.model, "endpoint": "https://api.deepseek.com", "request": kwargs}
            traces.append(entry)
            try:
                # No transport retry: keep physical request count and cost bounded.
                result = await self.client.chat.completions.create(**kwargs)
                entry.update(response=result.model_dump(mode="json"), latency_ms=round((time.perf_counter() - started) * 1000, 2))
                return result
            except Exception as exc:
                entry.update(error_type=type(exc).__name__, latency_ms=round((time.perf_counter() - started) * 1000, 2))
                raise
    return BudgetProvider()


async def model_case(case, provider):
    p, e = case["payload"], case["expected"]
    if case["kind"] == "model_filter":
        actual = await evaluate_filter(provider.generate, {**leaf(p["criterion"]), "quote": p["criterion"]}, p["facts"])
        return check(subset(e, actual), actual, "model JD verdict incorrect")
    if case["kind"] == "model_expression":
        try:
            plan = await plan_request(provider, REGISTRY, p["message"], dialogue={}, conditions={"queries": ["金融分析师"], "source": "mock"}, stamp="synthetic")
        except PlanValidationError as exc:
            return check(e["behavior"] == "clarify" and exc.issue.category == "clarify", {"decision": exc.issue.public()}, "clear command rejected by validator")
        steps = {s["tool"]: s for s in plan["steps"]}
        actual = {"plan": plan}
        if e["behavior"] == "no_mutation":
            return check(not any(s["effect"] != "read" for s in plan["steps"] if s["status"] == "pending") or plan["status"] == "clarifying", actual, "quoted/withdrawn instruction planned mutation")
        if e["behavior"] == "clarify": return check(plan["status"] == "clarifying", actual, "ambiguous condition silently guessed")
        search = steps.get("job_search_start", {})
        checks = {"ready": plan["status"] == "ready", "slots": subset({k: e[k] for k in ("result_limit", "salary_min")}, search.get("arguments", {})),
                  "coverage": set(e.get("tools", ["job_search_start"])) <= steps.keys()}
        return check(all(checks.values()), {"checks": checks, **actual}, "expression family failed")
    if case["kind"] == "model_dialogue":
        return await dialogue_model_case(case, provider)
    dialogue = apply_operation(None, {"op": "begin", "message": p["message"], "source": "synthetic-model-eval"})
    plan = await plan_request(provider, REGISTRY, p["message"], dialogue=dialogue,
        conditions={"queries": [p["role"]], "source": "mock"}, stamp="synthetic-resume-v1")
    steps = {s["tool"]: s for s in plan["steps"]}
    checks = {"ready": plan["status"] == "ready", "goal_coverage": set(e["tools"]) <= steps.keys()}
    search = steps.get("job_search_start", {})
    checks["search_slots"] = subset(e["search"], search.get("arguments", {}))
    checks["criterion_retained"] = bool(search.get("arguments", {}).get("filters"))
    for tool, key in (("mock_delivery_start", "delivery"), ("learning_plan_generate", "learning")):
        if tool in e["tools"]:
            step = steps.get(tool, {})
            checks[key + "_slots"] = subset(e[key], step.get("arguments", {}))
            checks[key + "_dependency"] = search.get("id") in step.get("depends_on", [])
    if "job_results_read" in e["tools"]:
        checks["summary_dependency"] = search.get("id") in steps.get("job_results_read", {}).get("depends_on", [])
    actual = {"checks": checks, "plan": plan}
    return check(all(checks.values()), actual, "validated plan incomplete/incorrect")


async def dialogue_model_case(case, provider):
    scenario, message = case["payload"]["scenario"], case["payload"]["message"]
    original = "帮我找15份工作，地点深圳 杭州，薪资15k以上，匹配分85以上自动投递，低于85分且薪资25k以上的分析不足并生成成长计划，最后总结搜索结果"
    previous = validate_draft(make_gold_plan(dict(message=original, count=15, floor=15000, score=85, branch_floor=25000)),
        REGISTRY, original, dialogue={}, conditions={"queries": ["金融分析师"], "source": "mock"}, stamp="synthetic")
    previous["status"] = "failed"
    state = apply_operation(None, {"op": "request_state", "request": TaskRequest(original=original, status="failed", resume_stamp="synthetic").model_dump()})
    state = apply_operation(state, {"op": "instruction", "request": previous})
    state = apply_operation(state, {"op": "slots", "conditions": {"queries": ["金融分析师"], "locations": ["深圳", "杭州"], "salary_min": 15000, "result_limit": 15}})
    if scenario == "learning_scope":
        original = "生成成长计划，分析匹配度80以下的职位"
        previous = validate_draft(RequestDraft(goals=[Goal(quote=original, disposition="steps", step_ids=["learning"])],
            steps=[PlannedStep(id="learning", tool="learning_plan_generate", command_quote=original, scope_quote=original, arguments={"max_score": 79})]),
            REGISTRY, original, dialogue={}, conditions={"queries": ["金融分析师"]}, stamp="synthetic")
        previous.update(status="clarifying", questions=["分析哪个分段？"])
        state = apply_operation(state, {"op": "learning_request", "message": original, "resume_stamp": "synthetic"})
    if scenario == "detail":
        state = apply_operation(state, {"op": "snapshot", "topic": "applications", "kind": "job", "source": "synthetic-result",
            "items": [{"id": 1000+i, "title": f"合成职位{i}", "company": "合成企业"} for i in range(1, 31)]})
    state = apply_operation(state, {"op": "begin", "message": message})
    if scenario.startswith("update_"):
        previous["condition_overrides"] = {"update_city": {"locations": ["杭州", "深圳"]}, "update_count": {"result_limit": 18}, "update_salary": {"salary_min": 20000}}[scenario]
    plan = await plan_request(provider, REGISTRY, message, dialogue=state, conditions=state["frames"]["search"]["slots"],
        stamp="synthetic", previous=previous if scenario in {"retry", "update_city", "update_count", "update_salary", "learning_scope", "cancel"} else None)
    steps = {s["tool"]: s for s in plan["steps"]}
    actual = {"plan": plan}
    if scenario in {"recall", "repair", "new_topic", "detail"}:
        check(not any(s["effect"] != "read" for s in plan["steps"]), actual, "read/repair turn inherited mutation authority")
        if scenario == "detail":
            detail = steps.get("job_detail_read", {})
            index = 24 if "24" in message else 8
            check(detail.get("arguments", {}).get("selector") == f"第{index}个", actual, "ordinal not grounded to displayed result")
        return actual
    if scenario == "learning_scope":
        return check(plan["status"] == "ready" and steps.get("learning_plan_generate", {}).get("arguments", {}).get("max_score") == 89, actual, "elliptical scope update lost explicit plan request")
    if scenario == "cancel":
        return check("mock_delivery_start" not in steps or "mock_delivery_start" in plan["revoked_tools"], actual, "withdrawn branch survives")
    expected = {"result_limit": 18 if scenario == "update_count" else 15, "salary_min": 20000 if scenario == "update_salary" else 15000,
                "locations": ["杭州", "深圳"] if scenario == "update_city" else ["深圳", "杭州"]}
    return check(plan["status"] == "ready" and subset(expected, steps.get("job_search_start", {}).get("arguments", {})) and
        {"job_search_start", "mock_delivery_start", "learning_plan_generate", "job_results_read"} <= steps.keys(), actual, "root goals/slots lost on continuation")


def percentile(values, q):
    return round(sorted(values)[max(0, int(len(values) * q + 0.999) - 1)], 2) if values else None


def write_report(output, records, cases, metadata, model_traces):
    by_kind = defaultdict(list)
    for row in records: by_kind[row["kind"]].append(row)
    metrics = {kind: {"cases": len(rows), "outcomes": dict(Counter(r["status"] for r in rows)),
        "p50_ms": percentile([r["latency_ms"] for r in rows], .5), "p95_ms": percentile([r["latency_ms"] for r in rows], .95)} for kind, rows in by_kind.items()}
    summary = {**metadata, "dataset_cases": len(cases), "evaluated_cases": len(records), "outcomes": dict(Counter(r["status"] for r in records)),
        "families": len({(c["kind"], c["family"]) for c in cases}), "industries": len({c["industry"] for c in cases if c.get("industry")}), "metrics": metrics,
        "physical_model_calls": len(model_traces)}
    summary["distinct_evaluated_ids"] = len({r["id"] for r in records if r["status"] != "skipped"})
    authority = [r for r in records if r["kind"] == "authority"]
    summary["authority_contract"] = {"scope": "parser/grounding/policy given full message; NOT full Agent completion",
        "positive_cases": sum(r["expected"]["authorized"] for r in authority),
        "negative_cases": sum(not r["expected"]["authorized"] for r in authority),
        "legal_command_disagreements": sum(r["status"] == "fail" and r["expected"]["authorized"] for r in authority),
        "non_command_disagreements": sum(r["status"] == "fail" and not r["expected"]["authorized"] for r in authority)}
    # Usage includes nested dictionaries from some providers; report token fields only.
    summary["usage"] = {k: sum(t.get("response", {}).get("usage", {}).get(k, 0) or 0 for t in model_traces if isinstance(t.get("response", {}).get("usage"), dict)) for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    failures = [r for r in records if r["status"] in {"fail", "error"}]
    (output / "failures.json").write_text(json.dumps(failures, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# AutoHunt evaluation", "", f"Dataset: {VERSION}; cases: {len(cases)}; executed: {len(records)}.", "",
        "Generated variants are NOT independent human-reviewed samples. A pass checks only the labelled contract.", "",
        "| Layer | Cases | Pass | Fail | Error | Skipped | p50 ms | p95 ms |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for kind, values in metrics.items():
        outcomes = values["outcomes"]
        lines.append(f"| {kind} | {values['cases']} | {outcomes.get('pass',0)} | {outcomes.get('fail',0)} | {outcomes.get('error',0)} | {outcomes.get('skipped',0)} | {values['p50_ms']} | {values['p95_ms']} |")
    lines += ["", "## Failure families", ""]
    for (kind, family, error), number in Counter((r["kind"], r["family"], r.get("error", "")) for r in failures).most_common():
        lines.append(f"- {kind}/{family}: {number}; {error[:180]}")
    lines += ["", "## Boundaries", "", "No live recruitment website, current browser, resume, board or chat was modified.",
        "Execution scenarios use the production DAG with synthetic executors, NOT full browser E2E.",
        "No real-site submission, UI, model small-router accuracy or full resume/growth content quality claim.",
        "Model challenge gold labels are synthetic and need independent human review; no monetary cost is invented from token usage."]
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


async def main(args):
    cases = corpus()
    with tempfile.TemporaryDirectory(prefix="autohunt-evaluation-") as temporary:
        scratch = Path(temporary)
        cases += await supplementary(scratch)
        dataset_json = json.dumps(cases, ensure_ascii=False, sort_keys=True)
        output = (ROOT / "test-results/evaluation" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")).resolve()
        output.mkdir(parents=True)
        (output / "dataset.json").write_text(dataset_json, encoding="utf-8")
        metadata = {"version": VERSION, "created_at": datetime.now(UTC).isoformat(), "dataset_sha256": hashlib.sha256(dataset_json.encode()).hexdigest(),
            "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(), "seed": 20261010,
            "model_consent": args.models_consented, "model_call_limit": args.model_call_limit,
            "coverage": "component contracts + synthetic model challenge; not full browser E2E", "label_status": "provisional synthetic gold"}
        source_files = sorted([*(ROOT / "app").rglob("*.py"), *(ROOT / "evaluation").rglob("*.py")])
        metadata["source_hashes"] = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_files}
        metadata["python"] = sys.version
        metadata["dependencies"] = {name: importlib.metadata.version(name) for name in ("pydantic", "openai", "playwright", "httpx", "cryptography")}
        metadata["git_status"] = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).splitlines()
        records, model_traces = [], []
        try:
            provider = consented_provider(args.model_call_limit, model_traces) if args.models_consented else None
        except Exception as exc:
            error = {"phase": "model_setup", "error_type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}
            (output / "bootstrap-error.json").write_text(json.dumps(error, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"output": str(output), "bootstrap_error": type(exc).__name__}), flush=True)
            return 2
        browser = None
        if args.browser_fixtures:
            from .browser_fixture import BrowserFixture
            browser = BrowserFixture()
            try:
                await browser.start()
            except Exception as exc:
                error = {"phase": "browser_setup", "error_type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}
                (output / "bootstrap-error.json").write_text(json.dumps(error, ensure_ascii=False, indent=2), encoding="utf-8")
                if provider is not None: await provider.client.close()
                print(json.dumps({"output": str(output), "bootstrap_error": type(exc).__name__}), flush=True)
                return 2
        # Round-robin families and industries, not the first N easy cases.
        model_groups = defaultdict(list)
        for case in sorted(cases, key=partition_key):
            if case["kind"].startswith("model_"): model_groups[case["kind"], case["family"]].append(case)
        ordered_models = [c for row in itertools.zip_longest(*model_groups.values()) for c in row if c]
        ordinary = [c for c in cases if not c["kind"].startswith("model_")]
        order = ordered_models if args.models_only else ordinary + ordered_models
        if args.kinds: order = [c for c in order if c["kind"] in args.kinds]
        if args.repetitions > 1:
            order = [dict(c, attempt=attempt) for attempt in range(1, args.repetitions + 1) for c in order]
        metadata["repetitions"] = args.repetitions
        with (output / "cases.jsonl").open("w", encoding="utf-8") as stream:
            for case in order:
                started = time.perf_counter()
                row = {k: case[k] for k in ("id", "kind", "family", "split", "payload", "expected")}
                row["attempt"] = case.get("attempt", 1)
                events = []
                context = RunContext(case["id"], asyncio.current_task(), lambda k, d: events.append({"kind": k, **d}), lambda _: None, dialogue={})
                token = current_run.set(context)
                try:
                    if case["kind"].startswith("model_"):
                        if provider is None or provider.calls >= args.model_call_limit:
                            row.update(status="skipped", reason="model consent missing or call budget exhausted")
                        else:
                            row.update(actual=await model_case(case, provider), status="pass")
                    elif case["kind"] == "browser":
                        if browser is None:
                            row.update(status="skipped", reason="isolated browser fixtures not selected")
                        else:
                            actual, expected = await browser.run(case["payload"]["scenario"])
                            if "delivery" in actual:
                                observed = {**actual["delivery"], "takeover": actual["delivery"]["intervention_reason"] is not None}
                            else: observed = {**actual, "count": len(actual.get("cards", []))}
                            check(subset(expected, observed), actual, "browser observation contract mismatch")
                            row.update(actual=actual, status="pass")
                    else:
                        row.update(actual=await (delivery_contract(case, scratch) if case["kind"] == "delivery_contract" else
                            adversarial(case, scratch) if case["kind"] in {"adversarial", "routing"} else evaluate(case, scratch)), status="pass")
                except ContractFailure as exc:
                    row.update(status="fail", error=str(exc), actual=exc.actual)
                except PlanValidationError as exc:
                    row.update(status="fail", error=str(exc), validation_issues=[issue.public() for issue in exc.issues])
                except Exception as exc:
                    row.update(status="error", error_type=type(exc).__name__, error=str(exc), traceback=traceback.format_exc())
                finally:
                    current_run.reset(token)
                row.update(latency_ms=round((time.perf_counter() - started) * 1000, 2), trace=events)
                records.append(row)
                stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n"); stream.flush()
                # Preserve partial model logs after every case, including errors.
                (output / "model-calls.jsonl").write_text("".join(json.dumps(t, ensure_ascii=False, default=str) + "\n" for t in model_traces), encoding="utf-8")
                if row["status"] != "skipped" and (case["kind"].startswith("model_") or len(records) % 250 == 0):
                    print(json.dumps({"progress": len(records), "case": case["id"], "status": row["status"], "calls": len(model_traces)}, ensure_ascii=False), flush=True)
        if provider is not None: await provider.client.close()
        if browser is not None: await browser.close()
        metadata["source_changed_during_run"] = [str(p.relative_to(ROOT)) for p in source_files
            if metadata["source_hashes"].get(str(p.relative_to(ROOT))) != hashlib.sha256(p.read_bytes()).hexdigest()]
        summary = write_report(output, records, cases, metadata, model_traces)
        print(json.dumps({"output": str(output), "outcomes": summary["outcomes"], "physical_model_calls": len(model_traces)}, ensure_ascii=False), flush=True)
        # sqlite connection context managers commit but do not close. Release
        # unreachable temporary connections before Windows directory cleanup.
        gc.collect()
        return 1 if any(r["status"] in {"fail", "error"} for r in records) else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models-consented", action="store_true", help="Explicit consent to send only synthetic evaluation data to configured DeepSeek")
    parser.add_argument("--models-only", action="store_true")
    parser.add_argument("--browser-fixtures", action="store_true", help="Launch fresh headless Chromium; all HTTP requests intercepted, never live mock site")
    parser.add_argument("--kinds", nargs="+", help="Run only these evaluator kinds; useful for bounded follow-up runs")
    parser.add_argument("--repetitions", type=int, choices=range(1, 4), default=1, help="Repeated attempts are reported separately, not new independent cases")
    parser.add_argument("--model-call-limit", type=int, choices=range(1, 121), default=120)
    options = parser.parse_args()
    raise SystemExit(asyncio.run(main(options)))
