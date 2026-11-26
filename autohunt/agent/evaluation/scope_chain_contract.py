"""Stateful scope/chain regression against production planning and execution.

Scripted candidate plans are inputs, never model-quality claims. The real DAG,
revision compiler, approval decisions and encrypted SQLite RunStore run unchanged.
Only recruitment/browser/model boundaries are synthetic; no .env is loaded.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import gc
import hashlib
import json
import socket
import tempfile
import time
import traceback
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from app.core.agent_tools import AgentToolSpec
from app.core.job_filters import validate_filter
from app.core.models import BrowserTaskView
from app.jobs.store import JobStore
from app.runtime.context import RunContext, current_run, effect_checkpoint, save_search_state
from app.runtime.store import RunStore
from app.workflow.execution import advance
from app.workflow.lifecycle import activate_staged_searches
from app.workflow.planning import RequestDraft, validate_draft
from app.workflow.recovery import recover_plan
from evaluation.stateful_scenarios import Harness, STAMP, check, step

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "evaluation/corpora/scope-chain-v1.json"


def canonical_filter(value):
    return validate_filter(value).model_dump(mode="json") if value is not None else None


def job_rows():
    cities = ["深圳", "深圳", "深圳", "上海", "深圳", "北京", "深圳", "深圳"]
    scores = [90, 88, 82, 88, 88, 88, 78, 88]
    names = ["一", "二", "三", "四", "五", "六", "七", "八"]
    return {i + 1: {"id": i + 1, "title": f"合成职位{names[i]}", "company": f"合成企业{names[i]}",
        "location": cities[i], "salary": "30-40K", "score": scores[i], "source_url": f"https://example.invalid/jobs/{i+1}",
        "content_hash": f"synthetic-jd-{i+1}", "filter_status": "unknown" if i == 7 else "pass",
        "filter_evidence": [], "description": "仅用于隔离工程测试的虚构JD"} for i in range(8)}


class ScopeHarness(Harness):
    def __init__(self, directory, defaults):
        super().__init__(directory)
        self.directory = directory
        self.defaults = defaults
        self.rows = job_rows()
        self.prior_runs = []
        self.queues, self.browser_joins, self.hr_records = [], [], []
        self.searches = {"synthetic-search": {"task_id": "synthetic-search", "status": "completed",
            "session_id": "default", "resume_id": "synthetic-resume"}}
        self.browser_views = {}
        self.store.save_search_state("default", defaults["search"])
        self.runtime.search_state = lambda _: self.store.search_state("default")
        self.jobs.get_job = lambda ident: copy.deepcopy(self.rows[ident])
        self.jobs.review_fingerprint = JobStore.review_fingerprint
        self.jobs.search_source = lambda ident: copy.deepcopy(self.searches.get(ident))
        self.jobs.get_task = lambda ident: copy.deepcopy(self.searches[ident])
        self.registry.mock_workflow.remember_authorization_scope = lambda *a, **k: None
        self.registry.mock_workflow.queue_deliveries = self.queue
        self.registry.interview_preparations = NS(generate=self.interview)
        for spec in self.registry._build_specs():
            if spec.name.startswith("interview_preparation"):
                self.registry._tools[spec.name] = spec
        self.replace("mock_delivery_start", self.registry._mock_delivery_start)
        self.replace("job_target_resolve", self.registry._job_target_resolve)
        self.replace("job_search_start", self.search)
        self.replace("job_results_read", self.summary)
        self.browser.current = lambda: BrowserTaskView(id="synthetic-search", status="running", source="mock")
        self.browser.can_start_search = lambda: True
        self.browser.stop = self.stop_browser
        self.runtime.observe_browser = lambda ident: self.browser_views.get(ident,
            BrowserTaskView(id=ident, status="running", source="mock"))
        self.runtime.browser_summary = lambda run_id, view: self.store.event(run_id, "browser_summary",
            {"task_id": view.id, "report_rendered": False, "synthetic": True})
        self.snapshot()

    def replace(self, name, executor):
        spec = self.registry._tools[name]
        self.registry._tools[name] = AgentToolSpec(spec.name, spec.description, spec.parameters, spec.effect, executor)

    def log_call(self, tool, arguments):
        item = {"tool": tool, "arguments": copy.deepcopy(arguments)}
        self.calls.append(item)
        self.store.event(self.run["id"], "synthetic_adapter_called", item)

    def snapshot(self, task_id="synthetic-search"):
        state = self.store.mutate_dialogue("default", {"op": "snapshot", "topic": "applications", "kind": "job",
            "source": "synthetic-fixture", "version": task_id, "displayed": True,
            "items": [{**{k: row[k] for k in ("id", "title", "company", "location", "salary", "score")},
                       "content_hash": self.jobs.review_fingerprint(row)} for row in self.rows.values()]})
        self.snapshot_id = state["snapshots"][-1]["id"]

    def remember_scope(self, filters=None, score=85):
        self.scope = {"id": "previous-batch", "session_id": "default", "resume_stamp": STAMP,
            "task_id": "synthetic-search", "filters": canonical_filter(filters if filters is not None else self.defaults["delivery_scope"]["filters"]),
            "min_score": score}
        self.store.mutate_dialogue("default", {"op": "delivery_scope", "scope": self.scope})

    @contextmanager
    def context(self, message="合成指令"):
        ctx = RunContext(self.run["id"], asyncio.current_task(),
            lambda k, d: self.runtime.emit(self.run["id"], k, d), lambda value: self.runtime.save(self.run["id"], value),
            dialogue=self.store.dialogue_state("default"), search_state=self.store.search_state("default"),
            persist_search=lambda value: self.store.save_search_state("default", value),
            mutate_dialogue=lambda op: self.store.mutate_dialogue("default", op),
            authorize_action=lambda *args: self.runtime.authorize_action(self.run["id"], *args),
            cancellation_requested=lambda: bool(self.store.get(self.run["id"])["cancel_requested"]),
            plan_reader=lambda: copy.deepcopy((self.store.get(self.run["id"])["checkpoint"] or {}).get("plan")),
            user_message=message)
        token = current_run.set(ctx)
        try:
            yield ctx
        finally:
            current_run.reset(token)

    def compile(self, planned, message, *, paused=False):
        candidate = {"steps": [{k: v for k, v in row.items() if k in
            {"id", "tool", "arguments", "depends_on", "command_quote", "scope_quote", "missing_slots", "user_requested"}}
            for row in planned], "goals": [{"quote": message, "disposition": "steps", "step_ids": [row["id"] for row in planned]}],
            "pause": paused}
        self.turns.append({"message": message, "scripted_model_plan": copy.deepcopy(candidate)})
        with self.context(message):
            plan = validate_draft(RequestDraft.model_validate(candidate), self.registry, message,
                dialogue=self.store.dialogue_state("default"), conditions=self.store.search_state("default"), stamp=STAMP)
        self.plan = plan
        self.store.save_plan(self.run["id"], plan)
        self.record("initial_plan_normalized")
        return plan

    async def queue(self, **kwargs):
        self.log_call("mock_delivery_start", kwargs)
        effect_checkpoint("preparing")
        ids = list(kwargs.get("job_ids") or [1, 2, 3])
        if kwargs.get("selection_evidence", {}).get("allow_outside_search"):
            check(len(ids) == 1, "single_exception_expanded", "one concrete ID", ids)
        self.queues.append(copy.deepcopy(kwargs))
        effect_checkpoint("committed")
        self.hr_records.extend({"job_id": ident, "phase": "waiting_hr", "sent": 1} for ident in ids)
        return {"queued": len(ids), "selected_ids": ids, "message": "隔离模拟适配器已入队，未进行真实沟通或投递"}

    async def search(self, args):
        self.log_call("job_search_start", args)
        effect_checkpoint("preparing")
        ident = f"synthetic-search-{sum(c['tool'] == 'job_search_start' for c in self.calls)}"
        self.searches[ident] = {"task_id": ident, "status": "running", "session_id": "default", "resume_id": "synthetic-resume"}
        save_search_state({**self.store.search_state("default"), **{k: v for k, v in args.items()
            if k in self.store.search_state("default") and k != "revision"}})
        return {"id": ident, "status": "running"}

    async def summary(self, args):
        self.log_call("job_results_read", args)
        return {"message": "合成搜索整体总结（非真实职位）", "jobs": [self.rows[1], self.rows[2]]}

    async def interview(self, job_id, focus):
        self.log_call("interview_preparation_generate", {"job_id": job_id, "focus": focus})
        effect_checkpoint("committed")
        return {"id": "synthetic-interview", "job_id": job_id, "summary": "合成面试准备，非实际面试题", "questions": []}

    async def stop_browser(self):
        self.browser_joins.append("synthetic-search")
        self.store.event(self.run["id"], "synthetic_worker_joined", {"task_id": "synthetic-search"})

    def sync(self):
        self.plan = copy.deepcopy(self.store.get(self.run["id"])["checkpoint"]["plan"])

    async def observe(self):
        await asyncio.wait_for(self.runtime.watch_plan(self.run["id"]), 5)
        self.sync()
        self.record("watcher_settled")

    async def decision(self, approved):
        pending = [a for a in self.store.approvals(self.run["id"]) if a["status"] == "pending"]
        check(bool(pending), "missing_human_approval", "pending durable approval", self.store.approvals(self.run["id"]))
        current_id = next(s["approval_id"] for s in self.plan["steps"] if s["status"] == "waiting_approval")
        await self.runtime.decide_approval(self.run["id"], current_id, "default", approved)
        self.turns.append({"human_approval": approved, "approval_id": current_id})
        await self.observe()

    def reopen(self):
        self.store = RunStore(Path(self.directory) / "runtime.db", self.key)
        self.runtime.store = self.store
        self.sync()
        self.record("encrypted_store_reopened")

    def next_run(self, message):
        # Production terminal runs are immutable. A later request gets a child
        # attempt, instead of silently mutating a completed execution record.
        self.prior_runs.append({"run": self.store.get(self.run["id"]),
            "events": self.store.all_events(self.run["id"]), "approvals": self.store.approvals(self.run["id"])})
        parent_id = self.run["id"]
        self.run, _ = self.store.create("default", "followup-" + str(len(self.prior_runs)), "chat", {"message": message}, parent_id)
        self.store.update(self.run["id"], status="running")
        self.plan = None

    def trace(self):
        return {**super().trace(), "prior_runs": self.prior_runs, "queues": self.queues, "browser_joins": self.browser_joins,
            "hr_records": self.hr_records, "final_search": self.store.search_state("default"),
            "final_delivery_scope": self.store.dialogue_state("default").get("delivery_scope")}


async def run_case(h, case, phase):
    family, p, oracle = case["family"], case["payload"], case["oracle"]
    original_search = h.store.search_state("default")
    h.turns.append({"original_user_turns": case["turns"], "phase": phase, "oracle": oracle})
    if family == "search_update":
        original = {**copy.deepcopy(h.defaults["search"]), **p.get("initial_search", {})}
        h.install([step("search", "job_search_start", original, status=phase),
                   step("summary", "job_results_read", {"summary": True}, ["search"])])
        if phase == "waiting_external":
            h.plan["steps"][0]["result"] = {"id": "synthetic-search", "status": "running"}
            h.store.save_plan(h.run["id"], h.plan)
        h.edit({"pause": True, "updates": [{"step_id": "search", "arguments": p["patch"]}]}, case["turns"][-1])
        target = h.plan["steps"][0]
        expected = {**original, **p["patch"]}
        actual = target.get("staged_arguments", target["arguments"])
        for field in original:
            eq = canonical_filter if field == "filters" else lambda x: x
            check(eq(actual.get(field)) == eq(expected.get(field)), "search_patch_lost_or_unrelated_field_changed", expected, actual)
        await h.tick()
        check(not h.calls, "paused_search_executed", [], h.calls)
        if phase == "waiting_external":
            check(target["arguments"] == original, "active_search_rewritten_in_place", original, target)
            await activate_staged_searches(h.plan, h.browser)
            check(h.browser_joins == ["synthetic-search"], "old_worker_not_joined", ["synthetic-search"], h.browser_joins)
            check(target["attempt_history"][-1]["task_id"] == "synthetic-search", "old_receipt_lost", "synthetic-search", target)
        h.plan["paused"] = False
        h.store.save_plan(h.run["id"], h.plan)
        await h.tick()
        check([c["tool"] for c in h.calls] == ["job_search_start"], "search_restart_count", ["job_search_start"], h.calls)
        check(h.plan["steps"][1]["status"] == "pending", "summary_ran_before_search", "pending", h.plan)
    elif family == "delivery_update":
        h.remember_scope(p.get("previous_filter"))
        old = {"scope_mode": "new", "source_task_id": "synthetic-search", "min_score": 85,
            "filters": copy.deepcopy(h.scope["filters"])}
        h.install([step("delivery", "mock_delivery_start", old, requested=phase != "waiting_approval")])
        if phase == "waiting_approval":
            await h.tick()
            check(h.plan["steps"][0]["status"] == "waiting_approval", "initial_approval_not_waiting", "waiting_approval", h.plan)
            old_approval = h.plan["steps"][0]["approval_id"]
        h.edit({"updates": [{"step_id": "delivery", "arguments": p["arguments"], "user_requested": True}]}, case["turns"][-1])
        if phase == "waiting_approval":
            # A formerly displayed approval cannot approve different parameters.
            try:
                await h.runtime.decide_approval(h.run["id"], old_approval, "default", True)
            except ValueError:
                pass
            else:
                raise AssertionError("A superseded approval accepted a changed action")
        args = h.plan["steps"][0]["arguments"]
        check(args["min_score"] == oracle["min_score"], "updated_threshold_reverted", oracle["min_score"], args)
        check(canonical_filter(args.get("filters")) == canonical_filter(oracle["filters"]), "updated_filter_not_replaced_or_inherited", oracle["filters"], args)
        await h.tick()
        if oracle["needs_approval"]:
            check(h.plan["steps"][0]["status"] == "waiting_approval" and not h.queues,
                "scope_expansion_self_authorized", "waiting_approval with no adapter call", h.trace())
            await h.decision(True)
        check(len(h.queues) == 1, "delivery_update_not_executed_once", 1, h.queues)
        check(h.queues[0]["min_score"] == oracle["min_score"], "queue_threshold_wrong", oracle["min_score"], h.queues)
        check(h.store.search_state("default") == original_search, "delivery_changed_search_scope", original_search, h.store.search_state("default"))
    elif family == "outside_single":
        h.remember_scope()
        h.compile([step("exception", "mock_delivery_start", {"selector": p["selector"], "snapshot_id": h.snapshot_id,
            "allow_outside_search": True, "min_score": 0, "scope_mode": "new"})], case["turns"][-1])
        await h.tick()
        check(h.plan["steps"][0]["status"] == "waiting_approval" and not h.queues,
            "model_granted_business_exception", "waiting_approval; no queue calls", h.trace())
        await h.decision(phase == "approve")
        expected = [p["target_id"]] if phase == "approve" else []
        actual = [ident for q in h.queues for ident in q.get("job_ids", [])]
        check(actual == expected, "exception_target_or_rejection_wrong", expected, actual)
        if h.queues:
            check(h.queues[0]["selection_evidence"]["allow_outside_search"] is True,
                "explicit_exception_not_forwarded", True, h.queues)
        check(h.store.search_state("default") == original_search, "exception_changed_search", original_search, h.store.search_state("default"))
        check(h.store.dialogue_state("default").get("delivery_scope") == h.scope,
            "single_exception_leaked_into_batch", h.scope, h.store.dialogue_state("default").get("delivery_scope"))
        old_calls = len(h.queues)
        message = "继续给原批次里符合条件的补投；不沿用刚才的单岗例外"
        h.next_run(message)
        h.compile([step("next_batch", "mock_delivery_start", {"min_score": 85})], message)
        following = next(s for s in h.plan["steps"] if s["id"] == "next_batch")
        check(not following["arguments"].get("allow_outside_search") and canonical_filter(following["arguments"].get("filters")) == h.scope["filters"],
            "exception_inherited_by_later_batch", h.scope["filters"], following)
        await h.tick()
        check(len(h.queues) == old_calls + 1, "later_batch_not_executed_once", old_calls + 1, h.queues)
        check(not h.queues[-1].get("selection_evidence", {}).get("allow_outside_search"),
            "later_batch_received_exception_permission", False, h.queues[-1])
    elif family == "full_chain":
        search = copy.deepcopy(h.defaults["search"])
        if p.get("grouped"):
            search["result_groups"] = [{"label": "正式", "count": 5, "filters": {"op": "condition", "text": "正式岗位"}},
                {"label": "实习", "count": 5, "filters": {"op": "condition", "text": "实习岗位"}}]
            search["result_limit"] = 10
        planned = [step("search", "job_search_start", search),
            step("summary", "job_results_read", {"summary": True}, ["search"]),
            step("delivery", "mock_delivery_start", {"min_score": p["threshold"]}, ["summary"]),
            step("interview", "interview_preparation_generate", {"selector": f"第{p['interview_ordinal']}个", "snapshot_id": h.snapshot_id}, ["delivery"])]
        h.compile(planned, case["turns"][0], paused=phase == "paused")
        await h.tick()
        if phase == "paused":
            check(not h.calls, "initial_pause_ignored", [], h.calls)
            h.edit({"pause": False}, "继续任务，保留全部分支")
            await h.tick()
        check([c["tool"] for c in h.calls] == ["job_search_start"], "chain_ignored_search_wait", ["job_search_start"], h.calls)
        ident = next(s["task_id"] for s in h.plan["steps"] if s["id"] == "search")
        h.searches[ident]["status"] = "completed"
        h.browser_views[ident] = BrowserTaskView(id=ident, status="completed", source="mock", detail="合成搜索完成")
        await h.observe()
        check([c["tool"] for c in h.calls] == oracle["order"], "chain_missing_or_out_of_order", oracle["order"], h.calls)
        check(all(s["status"] == "completed" for s in h.plan["steps"]), "chain_incomplete", "all completed", h.plan)
        check(h.queues[0]["min_score"] == p["threshold"], "chain_threshold_wrong", p["threshold"], h.queues)
        check(h.calls[-1]["arguments"]["job_id"] == p["interview_ordinal"], "interview_target_wrong", p, h.calls[-1])
        count = len(h.calls)
        with h.context():
            await advance(h.plan, h.registry)
        check(len(h.calls) == count, "completed_chain_replayed", count, h.calls)
        summaries = [c for c in h.calls if c["tool"] == "job_results_read"]
        check(len(summaries) == 1, "summary_tool_duplicated", 1, summaries)
    elif family == "recovery_control":
        main = step("action", p["tool"], {"min_score": 85} if p["tool"] == "mock_delivery_start" else {"summary": True}, status=p["status"])
        main.update(effect=p["effect"], execution_phase=p["execution_phase"])
        if p["status"] in {"failed", "outcome_indeterminate"}:
            main["failure"] = {"code": "synthetic_interruption", "effect_started": p["execution_phase"] != "preparing",
                "uncertain": p["execution_phase"] != "preparing" and p["effect"] != "read"}
        h.install([main, step("analysis", "job_stats_read")])
        if p["variant"] == "withdraw":
            h.edit({"remove_steps": ["action"]}, "取消投递，继续独立分析")
        if p["variant"] == "hr-wait":
            h.hr_records = [{"job_id": 1, "phase": "waiting_hr", "sent": 1}]
        if phase == "reopen":
            h.reopen()
        decision = recover_plan(h.store.get(h.run["id"]), session_id="default", stamp=STAMP,
            events=h.store.all_events(h.run["id"]))
        h.turns.append({"recovery_decision": decision.action, "message": decision.message})
        check(decision.action == "resume", "independent_analysis_not_resumed", "resume", decision.action)
        h.plan = decision.plan
        h.store.save_plan(h.run["id"], h.plan)
        await h.tick()
        action_calls = [c for c in h.calls if c["tool"] == p["tool"]]
        expected_calls = 1 if p["variant"] in {"preparing-send", "read-failure"} else 0
        check(len(action_calls) == expected_calls, "recovery_replayed_send_or_lost_safe_retry", expected_calls, h.calls)
        check(any(c["tool"] == "job_stats_read" for c in h.calls), "independent_analysis_lost", "analysis call", h.calls)
        if p["variant"] == "unknown-send":
            check(h.plan["steps"][0]["status"] == "outcome_indeterminate", "unknown_send_retried", "outcome_indeterminate", h.plan)
        if p["variant"] == "hr-wait":
            check(h.hr_records == [{"job_id": 1, "phase": "waiting_hr", "sent": 1}], "hr_monitoring_resend", "one old sent message", h.hr_records)
    elif family == "source_isolation":
        h.remember_scope()
        planned = [step("expanded_search", "job_search_start", {**h.defaults["search"], "locations": ["深圳", "北京"]}),
            step("old_delivery", "mock_delivery_start", {"source_task_id": p["source_task_id"], "min_score": 85, "scope_mode": "inherit"})]
        h.compile(planned, case["turns"][-1])
        delivery = next(s for s in h.plan["steps"] if s["id"] == "old_delivery")
        if p["source_task_id"] == "synthetic-search":
            check("expanded_search" not in delivery["depends_on"] and not delivery.get("evidence_step"),
                "old_receipt_rebound_to_new_search", "no dependency on new search", delivery)
        if phase == "waiting_external":
            # The first observation has already persisted the new search receipt;
            # only the batch's explicit old source may authorize an independent send.
            h.plan["steps"][0].update(status="waiting_external", task_id="synthetic-search-new")
            h.searches["synthetic-search-new"] = {"task_id": "synthetic-search-new", "status": "running",
                "session_id": "default", "resume_id": "synthetic-resume"}
            h.store.save_plan(h.run["id"], h.plan)
        await h.tick()
        if p["source_task_id"] == "synthetic-search":
            check(len(h.queues) == 1 and h.queues[0]["task_id"] == "synthetic-search",
                "old_result_delivery_not_independent", "one old-source queue", h.queues)
        else:
            check(not h.queues, "fabricated_source_authorized_during_search", "no queue until trusted search completion", h.queues)
            check(all(a["snapshot"].get("evidence_task_id") != "fabricated-model-task" for a in h.store.approvals(h.run["id"])),
                "fabricated_evidence_in_approval", "no fabricated evidence ID", h.store.approvals(h.run["id"]))
    elif family == "selector_scope":
        h.remember_scope()
        h.compile([step("selected", "mock_delivery_start", {**p, "snapshot_id": h.snapshot_id})], case["turns"][-1])
        args = h.plan["steps"][0]["arguments"]
        check(args.get("min_score") == oracle["min_score"], "selector_threshold_inheritance_wrong", oracle["min_score"], args)
        expected_filter = h.scope["filters"] if oracle["inherit_filter"] else None
        check(canonical_filter(args.get("filters")) == expected_filter, "selector_filter_inheritance_wrong", expected_filter, args)
        if phase == "reopen":
            h.reopen()
        await h.tick()
        check(len(h.queues) == 1 and h.queues[0].get("job_ids") == oracle["selected_ids"],
            "selector_scope_target_wrong", oracle["selected_ids"], h.queues)
        check(h.store.search_state("default") == original_search, "selector_scope_mutated_search", original_search, h.trace())
    elif family == "prepared_exception":
        h.remember_scope()
        h.compile([step("resolve", "job_target_resolve", {"selector": p["selector"], "snapshot_id": h.snapshot_id}),
            step("delivery", "mock_delivery_start", {"target_step_id": "resolve", "allow_outside_search": True,
                "source_task_id": "synthetic-search"}, ["resolve"])], case["turns"][-1])
        await h.tick()
        check([s["status"] for s in h.plan["steps"]] == ["completed", "waiting_approval"] and not h.queues,
            "prepared_target_not_waiting_approval", ["completed", "waiting_approval"], h.plan)
        if phase == "reopen":
            h.reopen()
        if phase == "other_current_plan":
            foreign_projection = copy.deepcopy(h.plan)
            foreign_projection.update(id="new-session-current-plan", steps=[], original="另一个独立的合成只读任务")
            h.store.mutate_dialogue("default", {"op": "instruction", "activate": True, "request": foreign_projection})
        await h.decision(phase != "reject")
        expected = oracle["selected_ids"] if phase != "reject" else []
        check([ident for q in h.queues for ident in q.get("job_ids", [])] == expected,
            "prepared_target_lost_after_approval", expected, h.trace())
    elif family == "selected_expansion":
        h.remember_scope()
        h.compile([step("selected_expansion", "mock_delivery_start", p)], case["turns"][-1])
        node = h.plan["steps"][0]
        check(node["arguments"]["min_score"] == oracle["min_score"] and canonical_filter(node["arguments"].get("filters")) == h.scope["filters"],
            "selected_update_parameters_lost", {"min_score": 70, "filters": h.scope["filters"]}, node)
        check("selection_inherits_scope" not in node["arguments"] and node.get("selection_inherits_scope") is True,
            "selection_provenance_not_server_owned", "server step metadata, not tool argument", node)
        await h.tick()
        check(node["status"] == "waiting_approval" and not h.queues, "selected_scope_expansion_self_authorized", "waiting_approval", h.trace())
        pending = h.store.approval(node["approval_id"])
        check(pending["snapshot"].get("selection_inherits_scope") is True, "selection_provenance_not_bound_to_approval", True, pending)
        if phase == "reopen":
            h.reopen()
        await h.decision(False)
        check(not h.queues and h.plan["steps"][0].get("withdrawn"), "rejected_selected_expansion_sent", [], h.trace())
    else:
        raise ValueError(f"Unknown family {family}")


def current_case(source):
    """Version engineering chains after retiring a former goal; keep source cases archival."""
    case = copy.deepcopy(source)
    if case["family"] == "full_chain":
        case["source_case_id"] = source["id"]
        case["id"] += "-current-chain"
        case["retired_goals"] = ["learning_plan_generate"]
        case["turns"] = ["按原条件搜索并汇总结果，按指定门槛投递，然后准备指定岗位的面试。"]
        case["oracle"]["order"] = [name for name in case["oracle"]["order"] if name != "learning_plan_generate"]
    return case


async def main_async(args):
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    selected = [current_case(c) for c in corpus["cases"] if not args.family or c["family"] == args.family]
    if args.case:
        selected = [c for c in selected if c["id"] == args.case]
    matrices = [(case, phase) for case in selected for phase in case["phases"]]
    if args.limit:
        matrices = matrices[:args.limit]
    run_dir = ROOT / "evaluation/results/scope-chain" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    run_dir.mkdir(parents=True)
    sources = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for folder in (ROOT / "app/workflow", ROOT / "app/security", ROOT / "app/runtime", ROOT / "app/core")
        for path in folder.glob("*.py")}
    def deny_network(*_args, **_kwargs):
        raise RuntimeError("Scope contract benchmark forbids network; synthetic adapters only")
    results = []
    for case, phase in matrices:
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="autohunt-scope-") as directory:
            h = ScopeHarness(directory, corpus["defaults"])
            error = None
            try:
                with patch.object(socket, "create_connection", deny_network), patch.object(socket.socket, "connect", deny_network):
                    await asyncio.wait_for(run_case(h, case, phase), 15)
            except Exception as exc:
                error = {"type": type(exc).__name__, "message": str(exc), "code": getattr(exc, "code", None),
                    "expected": getattr(exc, "expected", None), "actual": getattr(exc, "actual", None), "traceback": traceback.format_exc()}
            finally:
                await h.close()
            result = {"id": case["id"] + ":" + phase, "family": case["family"], "phase": phase,
                "passed": error is None, "elapsed_ms": round((time.monotonic() - started) * 1000),
                "error": error, "case": case, "trace": h.trace()}
            results.append(result)
            with (run_dir / "cases.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(result, ensure_ascii=False, default=str) + "\n")
            print(json.dumps({k: result[k] for k in ("id", "passed", "elapsed_ms")} | {"error": error and error["message"]}, ensure_ascii=False), flush=True)
            h = None
            gc.collect()  # CPython sqlite context managers commit, but close on GC.
    summary = {"evaluation_type": corpus["evaluation_type"], "external_model_requests": 0, "real_recruitment_actions": 0,
        "base_cases": len(selected), "adapted_cases": sum(bool(c.get("source_case_id")) for c in selected), "scenarios": len(results), "passed": sum(r["passed"] for r in results),
        "failed": sum(not r["passed"] for r in results), "by_family": {},
        "failure_codes": dict(Counter((r["error"]["code"] or r["error"]["type"]) for r in results if r["error"])),
        "scope": "Production compile/revise/DAG/approval/persistence contracts with synthetic browser, recruitment and analysis adapters. Not real-model intent accuracy or browser submission E2E.",
        "logs": str(run_dir / "cases.jsonl"), "corpus_sha256": hashlib.sha256(CORPUS.read_bytes()).hexdigest(),
        "source_sha256": sources, "source_changed_during_run": [path for path, digest in sources.items()
            if hashlib.sha256((ROOT / path).read_bytes()).hexdigest() != digest]}
    for family in sorted({r["family"] for r in results}):
        subset = [r for r in results if r["family"] == family]
        summary["by_family"][family] = {"passed": sum(r["passed"] for r in subset), "total": len(subset)}
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "source_sha256"}, ensure_ascii=False, indent=2), flush=True)
    return 0 if summary["failed"] == 0 else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--family")
    parser.add_argument("--case")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
