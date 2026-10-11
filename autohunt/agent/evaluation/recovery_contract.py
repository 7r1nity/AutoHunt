"""Offline recovery benchmark: state matrix plus production SQLite/async paths.

Run from agent/: python -m evaluation.recovery_contract
--generate updates the checked-in synthetic corpus. No models, network or real jobs.
Executors/pages are scripted: this is not real-browser end-to-end evidence.
"""
from __future__ import annotations

import asyncio
import copy
import gc
import hashlib
import itertools
import json
import sys
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet

from app.browser.automation import BrowserAutomationService
from app.browser.lock import BrowserLockBusy
from app.core.agent_tools import AgentToolRegistry, AgentToolSpec
from app.core.models import BrowserSearchRequest, BrowserTaskView, DeliveryTaskStatus, DeliveryTaskView
from app.delivery.automation import DeliveryAutomationService
from app.jobs.store import JobStore
from app.runtime.context import RunContext, current_run
from app.runtime.manager import AgentRuntime
from app.runtime.resilience import PreflightRefused, WatchLimits, WatchdogExpired, safe_retry
from app.runtime.store import RunStore
from app.security.action_grants import action_binding, binding_matches
from app.workflow.execution import advance, completion_lines
from app.workflow.mock_coordinator import MockWorkflowCoordinator
from app.workflow.recovery import recover_plan, reconcile_receipts
from app.workflow.requests import is_continuation

CORPUS = Path(__file__).parent / "corpora" / "recovery.json"
TRACES = []
STAMP = "synthetic-resume:synthetic-document:1"
ERRORS = {
    "timeout_before": {"code": "timeout", "effect_started": False, "uncertain": False},
    "authority_expired": {"code": "authority_expired", "effect_started": False, "uncertain": False},
    "authority_stale": {"code": "authority_stale", "effect_started": False, "uncertain": False},
    "invalid_arguments": {"code": "invalid_arguments", "effect_started": False, "uncertain": False},
    "authority_revoked": {"code": "authority_revoked", "effect_started": False, "uncertain": False},
    "arguments_changed": {"code": "arguments_changed", "effect_started": False, "uncertain": False},
    "timeout_after": {"code": "timeout", "effect_started": True, "uncertain": True},
    "none": {},
}


def step(tool="learning_plan_generate", *, effect="write", status="pending", ident="growth", deps=()):
    args = {"max_score": 84, "salary_min": 25000} if tool == "learning_plan_generate" else {}
    return {"id": ident, "tool": tool, "effect": effect, "arguments": args,
            "status": status, "depends_on": list(deps), "command_quote": "合成用户明确执行请求",
            "authority_binding": action_binding(tool, args, session_id="default", requested=True),
            "missing_slots": []}


def plan(steps=None):
    return {"id": "synthetic-plan", "original": "合成完整搜索、投递和成长任务",
            "answers": [], "questions": [], "resume_stamp": STAMP, "turn": 1,
            "expires_at": 1, "status": "finished", "goals": [], "reply": "合成响应",
            "steps": steps if steps is not None else [step()]}


def run(value):
    return {"id": "original-run", "session_id": "default", "status": "failed",
            "checkpoint": {"phase": "action_plan", "plan": value}, "cancel_requested": False}


def generate_cases():
    cases = []
    for status, effect, error, revoked in itertools.product(
        ["completed", "pending", "waiting_external", "waiting_confirmation", "claimed", "failed",
         "blocked", "skipped", "outcome_indeterminate"], ["read", "write", "external"], ERRORS, [False, True]):
        # Independent expected policy, specified as a table rather than calling recovery.
        if status == "completed":
            expected = "completed"
        elif revoked:
            expected = "completed"  # No runnable scope; withdrawal remains blocked.
        elif status in {"pending", "waiting_external", "waiting_confirmation"}:
            expected = "resume"
        elif status in {"claimed", "outcome_indeterminate"} and effect != "read":
            expected = "verify"
        elif error in {"invalid_arguments", "authority_revoked", "arguments_changed"}:
            expected = "verify"
        elif effect == "read" or error in {"timeout_before", "authority_expired", "authority_stale"}:
            expected = "resume"
        else:
            expected = "verify"
        cases.append({"id": f"state-{len(cases):04d}", "kind": "state", "status": status,
                      "effect": effect, "failure": ERRORS[error], "revoked": revoked, "expected": expected})
    positives = ["继续任务", "继续中断的任务", "继续上次失败的任务", "重试", "重新执行",
                 "我要你继续我第一次发送给你的任务", "恢复之前的任务", "继续未完成的任务",
                 "按原请求重新执行任务", "按原请求重新执行搜索，其他的也要实现", "从断点继续", "按上次的检查点恢复任务"]
    negatives = ["我的需求是什么", "为什么不能继续任务", "不要继续任务", "他说继续任务",
                 "如果失败就重试", "继续任务并把薪资改成20k", "继续找上海15份工作", "重新开始", "？？？"]
    for index, text in enumerate(positives + negatives):
        cases.append({"id": f"continuation-{index:03d}", "kind": "continuation", "text": text,
                      "expected": text in positives})
    for tool, effect, status, changed, cancelled, masked in itertools.product(
        ["job_stats_read", "mock_delivery_start", "learning_plan_generate"], ["read", "write", "external"],
        ["pending", "failed", "completed"], [False, True], [False, True], [False, True]):
        cases.append({"id": f"boundary-{len(cases):04d}", "kind": "boundary", "tool": tool,
                      "effect": effect, "status": status, "changed": changed,
                      "cancelled": cancelled, "masked": masked})
    return cases


class MatrixContracts(unittest.TestCase):
    pass


def matrix_test(case):
    def test(self):
        if case["kind"] == "continuation":
            actual = is_continuation(case["text"])
            TRACES.append({"case_id": case["id"], "expected": case["expected"], "actual": actual})
            self.assertEqual(actual, case["expected"])
            return
        s = step(effect=case["effect"], status=case["status"], tool=case.get("tool", "learning_plan_generate"))
        s["failure"] = case.get("failure", {"effect_started": False, "uncertain": False, "code": "timeout"})
        value = plan([s]); source = run(value)
        if case["kind"] == "boundary":
            if case["masked"]:
                s["arguments"]["synthetic_field"] = "[REDACTED_PHONE]"
            source["cancel_requested"] = case["cancelled"]
            expected = "clarify" if case["changed"] or case["cancelled"] else (
                "completed" if case["status"] == "completed" else "clarify" if case["masked"] else "resume")
        else:
            expected = case["expected"]
        original = copy.deepcopy(source)
        decision = recover_plan(source, session_id="default", stamp="changed" if case.get("changed") else STAMP,
                                revoked_tools=[s["tool"]] if case.get("revoked") else [], now=100)
        TRACES.append({"case_id": case["id"], "expected": expected, "actual": decision.action,
                       "result_status": decision.plan["steps"][0]["status"] if decision.plan else None})
        self.assertEqual(decision.action, expected)
        self.assertEqual(source, original, "parent execution record must remain immutable")
        if decision.plan:
            self.assertEqual(decision.plan["id"], value["id"])
            self.assertEqual(decision.plan["expires_at"], 1900)
            self.assertEqual(decision.plan["steps"][0]["arguments"], s["arguments"])
            self.assertTrue(binding_matches(decision.plan["steps"][0]["authority_binding"], s["tool"], s["arguments"], "default"))
    return test


class DecisionContracts(unittest.TestCase):
    def test_uncertain_ancestor_never_unblocks_descendants_but_independent_read_runs(self):
        value = plan([step(status="outcome_indeterminate", ident="write"),
                      step(ident="dependent", status="skipped", deps=["write"]),
                      step("job_stats_read", effect="read", ident="independent")])
        decision = recover_plan(run(value), session_id="default", stamp=STAMP)
        self.assertEqual(decision.plan["recovery_pending"], ["independent"])
        self.assertEqual(decision.plan["steps"][0]["status"], "outcome_indeterminate")

    def test_only_exact_latest_successful_receipt_repairs_claim(self):
        value = plan([step(status="claimed")]); s = value["steps"][0]
        start = {"kind": "tool_started", "seq": 1, "data": {"plan_id": value["id"], "step_id": s["id"], "tool": s["tool"], "arguments": s["arguments"]}}
        finish = {"kind": "tool_finished", "seq": 2, "data": {**start["data"], "status": "ok", "result": {"title": "已持久化合成计划"}}}
        for field in ["plan_id", "step_id", "tool"]:
            wrong = copy.deepcopy(finish); wrong["data"][field] = "other"
            current = copy.deepcopy(value); reconcile_receipts(current, [start, wrong])
            self.assertEqual(current["steps"][0]["status"], "claimed")
        current = copy.deepcopy(value); reconcile_receipts(current, [finish, start])
        self.assertEqual(current["steps"][0]["status"], "completed")
        self.assertEqual(completion_lines(current, []), [])
        later = copy.deepcopy(finish); later.update(seq=3); later["data"]["status"] = "error"
        current = copy.deepcopy(value); reconcile_receipts(current, [start, finish, later])
        self.assertEqual(current["steps"][0]["status"], "claimed")

    def test_resume_ceiling_session_and_clarification_boundaries(self):
        for change, expected in [("session", "clarify"), ("budget", "clarify"), ("questions", "clarify"), ("no_plan", "no_plan")]:
            source = run(plan())
            if change == "session": source["session_id"] = "another"
            if change == "budget": source["checkpoint"]["plan"]["recovery_count"] = 5
            if change == "questions": source["checkpoint"]["plan"]["questions"] = ["哪个岗位？"]
            if change == "no_plan": source["checkpoint"] = {"phase": "planning"}
            self.assertEqual(recover_plan(source, session_id="default", stamp=STAMP).action, expected)

    def test_search_continuation_requires_no_dry_run_side_effects(self):
        for dry, status in itertools.product([False, True], ["failed", "waiting_external", "claimed"]):
            s = step("job_search_start", effect="read", status=status)
            s.update(task_id="saved-search", arguments={"dry_run": dry})
            decision = recover_plan(run(plan([s])), session_id="default", stamp=STAMP)
            self.assertEqual(decision.action, "verify" if dry else "resume")
            if not dry and status != "waiting_external":
                self.assertEqual(decision.plan["steps"][0]["resume_search_task_id"], "saved-search")


class RuntimeContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.key = Fernet.generate_key().decode()
        self.store = RunStore(Path(self.temp.name) / "runtime.db", self.key)
        self.candidate = NS(read=lambda: NS(profile=NS(resume_id="synthetic-resume")))
        self.resumes = NS(read_document=lambda: NS(id="synthetic-document", version=1))
        self.calls = []
        self.registry = AgentToolRegistry(jobs=None, browser=None, candidate_state=self.candidate,
            resume_workspace=self.resumes, memory=None, learning_plans=NS(), mock_workflow=NS())
        for name in ["job_stats_read", "job_results_read", "mock_delivery_start", "learning_plan_generate"]:
            spec = self.registry._tools[name]
            async def executor(arguments, name=name):
                self.calls.append({"tool": name, "arguments": copy.deepcopy(arguments)})
                return {"title": "合成成长计划", "message": "合成动作已核实完成", "jobs_analyzed": 1}
            self.registry._tools[name] = AgentToolSpec(spec.name, spec.description, spec.parameters, spec.effect, executor)
        self.runtime = AgentRuntime(self.store, NS(tool_registry=self.registry, candidate_state=self.candidate),
            NS(), NS(current=lambda: BrowserTaskView(), worker=None), None, self.resumes)
        self.tokens = []

    def source(self, value, **fields):
        saved, _ = self.store.create("default", f"source-{time.time_ns()}", "chat", {"message": value["original"]})
        self.store.update(saved["id"], checkpoint={"phase": "action_plan", "plan": value}, **fields)
        self.store.update(saved["id"], status="failed")
        return self.store.get(saved["id"])

    def enter(self, source, *, anchor=True):
        saved, _ = self.store.create("default", f"continuation-{time.time_ns()}", "chat", {"message": "继续任务"})
        self.store.update(saved["id"], status="running")
        dialogue = self.store.dialogue_state("default")
        if anchor:
            dialogue["task_request"] = {"run_id": source["id"], "original": source["checkpoint"]["plan"]["original"]}
        context = RunContext(saved["id"], asyncio.current_task(),
            lambda kind, data: self.runtime.emit(saved["id"], kind, data),
            lambda checkpoint: self.runtime.save(saved["id"], checkpoint), session_id="default", dialogue=dialogue,
            mutate_dialogue=lambda operation: self.store.mutate_dialogue("default", operation), search_state={"queries": ["后来改变的方向"]})
        self.tokens.append(current_run.set(context))
        return saved, context

    async def asyncTearDown(self):
        TRACES.append({"test": self.id(), "executor_calls": self.calls,
                       "runs": [{"id": item["id"], "parent_id": item["parent_id"], "status": item["status"],
                                 "events": self.store.public_events(item["id"])} for item in self.store.list()]})
        for token in reversed(self.tokens): current_run.reset(token)
        self.runtime = self.registry = self.store = None
        gc.collect(); self.temp.cleanup()

    async def test_task_continuation_retains_completed_results_renews_binding_and_skips_summary(self):
        search = step("job_search_start", effect="read", ident="search", status="completed")
        search.update(task_id="saved-search", result={"id": "saved-search"})
        summary = step("job_results_read", effect="read", ident="summary", status="completed", deps=["search"])
        summary["result"] = {"message": "old report"}
        delivery = step("mock_delivery_start", ident="delivery", status="blocked", deps=["search"])
        delivery.update(arguments={"min_score": 85}, evidence_step="search", failure=ERRORS["authority_expired"])
        growth = step(status="blocked", deps=["search"])
        growth.update(evidence_step="search", failure=ERRORS["authority_expired"])
        for s in (delivery, growth):
            s["authority_binding"] = action_binding(s["tool"], s["arguments"], session_id="default", requested=True)
            s["authority_binding"]["arguments_digest"] = "old-[REDACTED_PHONE]-digest"
        original = self.source(plan([search, summary, delivery, growth]))
        self.store.event(original["id"], "browser_summary", {"task_id": "saved-search", "report_rendered": True, "message": "old report"})
        child, _ = self.enter(original)
        response = await self.runtime.continue_task(child["id"], "继续中断的任务")
        self.assertEqual([call["tool"] for call in self.calls], ["mock_delivery_start", "learning_plan_generate"])
        saved = self.store.get(child["id"])
        self.assertEqual(saved["parent_id"], original["id"])
        self.assertEqual(saved["checkpoint"]["plan"]["steps"][1]["result"], summary["result"])
        self.assertEqual(saved["checkpoint"]["plan"]["status"], "finished")
        self.assertNotIn("old report", response.message)
        self.assertEqual(completion_lines(saved["checkpoint"]["plan"], self.store.all_events(child["id"])),
                         completion_lines(saved["checkpoint"]["plan"], []))
        self.assertEqual(self.store.get(original["id"]), original)
        self.store.update(child["id"], status="completed")
        # Point at the old parent again: the newest descendant is authoritative.
        again, _ = self.enter(original)
        answer = await self.runtime.continue_task(again["id"], "重试")
        self.assertIn("没有可继续", answer.message)
        self.assertEqual(len(self.calls), 2)

    async def test_uncertain_write_not_replayed_but_independent_read_can_continue(self):
        original = self.source(plan([step(status="claimed"), step("job_stats_read", effect="read", ident="stats")]))
        child, _ = self.enter(original)
        await self.runtime.continue_task(child["id"], "重试")
        self.assertEqual([call["tool"] for call in self.calls], ["job_stats_read"])
        self.assertIn(self.store.get(child["id"])["checkpoint"]["plan"]["steps"][0]["status"], {"claimed", "outcome_indeterminate"})

    async def test_model_task_continue_tool_uses_same_runtime_path_without_new_plan(self):
        s = step(status="blocked"); s["failure"] = ERRORS["authority_expired"]
        original = self.source(plan([s])); child, context = self.enter(original)
        context.user_message = "把没做完的事情接着做完"
        context.resume_task = lambda text: self.runtime.continue_task(child["id"], text)
        from app.core.agent_tools import ToolLoopState
        result = await self.registry.execute("task_continue", {}, user_message=context.user_message, state=ToolLoopState())
        self.assertEqual(result.status, "ok")
        self.assertEqual([call["tool"] for call in self.calls], ["learning_plan_generate"])
        self.assertEqual(self.store.get(child["id"])["checkpoint"]["plan"]["id"], "synthetic-plan")
        context.action_authority = {"plan_id": "new-planner-loop"}
        with self.assertRaises(PreflightRefused): await self.registry._task_continue({})

    async def test_completed_steps_rendered_in_foreground_are_not_rendered_by_watcher(self):
        original = self.source(plan([step("job_stats_read", effect="read", ident="stats")]))
        child, _ = self.enter(original)
        response = await self.runtime.continue_task(child["id"], "继续任务")
        checkpoint = self.store.get(child["id"])["checkpoint"]
        self.store.event(child["id"], "response", {"result": response.model_dump(mode="json"), **checkpoint["response_selection"]})
        self.assertEqual(completion_lines(checkpoint["plan"], self.store.all_events(child["id"])), [])

    async def test_event_paging_preserves_success_receipt_after_200_events(self):
        s = step(status="claimed"); value = plan([s]); original = self.source(value)
        for _ in range(210): self.store.event(original["id"], "synthetic_progress", {})
        base = {"plan_id": value["id"], "step_id": s["id"], "tool": s["tool"], "arguments": s["arguments"]}
        self.store.event(original["id"], "tool_started", base)
        self.store.event(original["id"], "tool_finished", {**base, "status": "ok", "result": {"title": "persisted"}})
        child, _ = self.enter(original)
        response = await self.runtime.continue_task(child["id"], "继续任务")
        self.assertIn("没有可继续", response.message)
        self.assertEqual(self.calls, [])
        self.assertGreater(len(self.store.all_events(original["id"])), 210)

    async def test_no_anchor_multiple_logical_tasks_require_selection(self):
        first = self.source(plan())
        value = plan(); value["id"] = "second-plan"; self.source(value)
        child, _ = self.enter(first, anchor=False)
        response = await self.runtime.continue_task(child["id"], "继续任务")
        self.assertIn("多个执行计划", response.message)
        self.assertEqual(self.calls, [])

    async def test_main_dispatch_shortcut_and_api_resume_use_original_plan(self):
        from app.core.main_agent import AutoHuntAgent
        original = self.source(plan([step("job_stats_read", effect="read", ident="stats")]))
        child, context = self.enter(original)
        context.resume_task = lambda text: self.runtime.continue_task(child["id"], text)
        answer = await AutoHuntAgent._dispatch_contextual_message(NS(), "继续中断的任务", None)
        self.assertIn("已按执行记录", answer.message)
        self.assertEqual([call["tool"] for call in self.calls], ["job_stats_read"])
        with patch.object(self.runtime, "schedule") as schedule:
            api_child = await self.runtime.resume(original["id"])
        self.assertEqual(api_child["parent_id"], original["id"])
        self.assertEqual(api_child["checkpoint"]["resume_source_id"], original["id"])
        schedule.assert_called_once()

    async def test_create_and_update_reject_cross_session_parent_and_terminal_is_immutable(self):
        original = self.source(plan())
        foreign = self.store.create_session("synthetic foreign")
        with self.assertRaises(ValueError):
            self.store.create(foreign["id"], "cross-session", "chat", {}, parent_id=original["id"])
        child, _ = self.enter(original)
        with self.assertRaises(ValueError): self.store.update(child["id"], parent_id=child["id"])
        self.store.update(original["id"], checkpoint={"overwritten": True})
        self.assertEqual(self.store.get(original["id"]), original)

    async def test_declined_and_changed_resume_never_expand_authority(self):
        for changed in [False, True]:
            s = step("mock_delivery_start", effect="external", status="blocked")
            s["arguments"] = {"min_score": 85}
            s["authority_binding"] = action_binding(s["tool"], s["arguments"], session_id="default", requested=False)
            s["failure"] = ERRORS["authority_expired"]
            s["authority_binding"]["user_requested"] = False
            value = plan([s])
            if changed: value["resume_stamp"] = "previous-resume"
            original = self.source(value); child, _ = self.enter(original)
            response = await self.runtime.continue_task(child["id"], "继续任务")
            self.assertEqual(self.calls, [])
            self.assertIn("简历已变化" if changed else "尚待具体范围确认", response.message)

    async def test_search_recovery_uses_saved_request_not_current_session_slots(self):
        saved = BrowserSearchRequest(query="金融分析师", queries=["金融分析师"], result_limit=15, salary_min=15000)
        received = []
        async def continued(ident, request):
            received.append((ident, request.model_dump(mode="json")))
            return BrowserTaskView(id=ident, status="starting", query=request.query)
        self.registry.browser = NS(current=lambda: BrowserTaskView(), can_start_search=lambda: True,
            store=NS(search_recovery=lambda _: {"request": saved.model_dump(mode="json")}), continue_search=continued)
        source = self.source(plan()); child, context = self.enter(source)
        context.action_authority = {"resume_search_task_id": "old-search"}
        await self.registry._job_search_start({"queries": ["金融分析师"], "result_limit": 15, "salary_min": 15000, "locations": []})
        self.assertEqual(received, [("old-search", saved.model_dump(mode="json"))])
        self.assertEqual(context.search_state["queries"], ["后来改变的方向"])
        with self.assertRaises(PreflightRefused): await self.registry._job_search_start({"salary_min": 999})

    async def test_hr_waiting_does_not_consume_task_admission_budget(self):
        for index in range(101):
            saved, _ = self.store.create("default", f"hr-{index}", "chat", {})
            self.store.update(saved["id"], status="waiting_reply")
        admitted, created = self.store.create("default", "new-search", "chat", {})
        self.assertTrue(created); self.assertEqual(admitted["status"], "queued")


class MonitoringContracts(unittest.IsolatedAsyncioTestCase):
    async def test_production_delivery_wait_reply_submit_and_lost_receipts(self):
        from app.core.models import BrowserMode
        from app.delivery.mock_site_adapter import MockApplyObservation, MockSubmitResult
        for scenario in ["no_reply", "reply", "communication_lost", "submission_lost", "account_changed", "page_closed"]:
            with tempfile.TemporaryDirectory() as directory:
                actions = []; saved = []
                service = DeliveryAutomationService.__new__(DeliveryAutomationService)
                service.data_root = Path(directory); (service.data_root / "private").mkdir()
                url = "http://127.0.0.1:8080/job?id=1"
                service.state = DeliveryTaskView(id="synthetic-delivery", job_id=1, target_url=url,
                    mock_applicant_id=31, checkpoint="authorized", status="starting")
                service.gate = asyncio.Event(); service.gate.set(); service.worker = None
                service.lifecycle_lock = asyncio.Lock(); service.monitor_wakeup = asyncio.Event()
                service._authorized_resume_payload = lambda: {"name": "合成候选人"}
                service._retry_or_fail = lambda *args: actions.append("retry")
                service.jobs = NS(get_job=lambda _: {"source_url": url}, mock_workflow_settings=lambda: {"applicant_id": 31},
                    mark_delivery_waiting_reply=lambda *args: actions.append("wait"),
                    mark_recruiter_reply=lambda *args: datetime.now(UTC).isoformat(),
                    mark_delivery_completed=lambda *args: actions.append("completed"),
                    mark_outcome_indeterminate=lambda *args: actions.append("uncertain"),
                    mark_delivery_failed=lambda *args: actions.append("failed"))
                def update(status, detail, **kw):
                    service.state.status = status
                    if "checkpoint" in kw: service.state.checkpoint = kw["checkpoint"]
                    if "submitted" in kw: service.state.submitted = kw["submitted"]
                    saved.append({"status": str(status), "checkpoint": service.state.checkpoint})
                service._update = update
                page = NS(url=url, evaluate=AsyncMock(return_value={"id": 99 if scenario == "account_changed" else 31, "role": "applicant"}))
                service.session = NS(reuse=AsyncMock(return_value=BrowserMode.CDP), ensure_cdp_window=AsyncMock(),
                    page_for_url=lambda _: None if scenario == "page_closed" else page)
                observation = MockApplyObservation(True, None, scenario != "no_reply", scenario == "no_reply", "发送简历",
                    hr_reply_text=None if scenario == "no_reply" else "合成招聘方回复")
                async def prepare(page, url, *, before_communication):
                    before_communication(); actions.append("communicate")
                    if scenario == "communication_lost": raise TimeoutError("synthetic greeting response lost")
                    return observation
                async def bind(*args, **kwargs):
                    actions.append("bind"); return NS(resume_id=1, applicant_id=31)
                async def submit(*args, **kwargs):
                    kwargs["before_click"](); actions.append("submit")
                    return MockSubmitResult(scenario != "submission_lost", False, "synthetic lost receipt")
                service.adapter = NS(normalize_job_url=lambda _: url, prepare=prepare, bind_resume=bind,
                    inspect=AsyncMock(return_value=observation), submit=submit)
                with patch("app.delivery.automation.asyncio.sleep", new=AsyncMock()):
                    await service._run_once(1, require_reply=True)
                expected = {"no_reply": "waiting_for_reply", "reply": "completed", "communication_lost": "outcome_indeterminate",
                            "submission_lost": "outcome_indeterminate", "account_changed": "failed", "page_closed": "failed"}[scenario]
                TRACES.append({"scenario": scenario, "expected": expected, "actual": str(service.state.status), "actions": actions, "checkpoints": saved})
                self.assertEqual(str(service.state.status), expected)
                self.assertEqual(actions.count("submit"), int(scenario in {"reply", "submission_lost"}))
                self.assertNotIn("retry", actions)
                if scenario in {"account_changed", "page_closed"}: self.assertNotIn("communicate", actions)
                if scenario == "communication_lost":
                    await service.stop()
                    self.assertEqual(service.state.status, "outcome_indeterminate")

    async def test_old_wait_remains_monitored_restart_does_not_expire_or_mint_authority(self):
        old = (datetime.now(UTC) - timedelta(days=365)).isoformat()
        saved = {"id": "old-delivery", "job_id": 1, "status": "waiting_for_reply", "submitted": False,
                 "preview_id": "original-preview", "action": "submit_resume", "resume_document_id": "doc",
                 "resume_version": 1, "resume_digest": "digest", "updated_at": old, "last_checked_at": old}
        calls = []
        async def resume(ident, **binding):
            calls.append((ident, binding)); saved["last_checked_at"] = datetime.now(UTC).isoformat()
            return NS(submitted=False)
        jobs = NS(delivery_runs_waiting_reply=lambda **kw: [saved], mock_delivery_queue=lambda **kw: [],
                  update_delivery_run=lambda ident, **changes: saved.update({key: value.isoformat() if isinstance(value, datetime) else value
                                                                           for key, value in changes.items()}),
                  record_mock_workflow_error=lambda text: self.fail(text))
        delivery = NS(worker=None, resume_authorized_run=resume)
        async def operation(owner, fn, **kwargs): return await fn()
        for _ in range(2):  # fresh coordinator = process restart
            coordinator = MockWorkflowCoordinator(jobs, NS(worker=None), delivery, None, None, poll_seconds=30)
            coordinator.delivery_owner = lambda _: "default"; coordinator._operation = operation
            await coordinator.check_reply_queue()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], ("old-delivery", {"resume_document_id": "doc", "resume_version": 1, "resume_digest": "digest"}))
        saved["preview_id"] = None; saved["last_checked_at"] = old
        await coordinator.check_reply_queue(); self.assertEqual(len(calls), 1)
        runtime = AgentRuntime(None, None, NS(get_delivery_run=lambda _: DeliveryTaskView(id="old-delivery",
            status="waiting_for_reply", updated_at=datetime.now(UTC)-timedelta(days=365))), None, None, None)
        self.assertEqual(runtime.observe_delivery("old-delivery").status, "waiting_for_reply")

    async def test_safe_retry_is_bounded_cancel_propagates_and_writes_never_retry(self):
        for retry_safe, error, expected in [(True, TimeoutError(), 3), (False, TimeoutError(), 1),
                                            (True, PermissionError(), 1), (True, ValueError(), 1)]:
            calls = []
            async def fail(): calls.append(1); raise error
            with patch("app.runtime.resilience.asyncio.sleep", new=AsyncMock()):
                with self.assertRaises(type(error)): await safe_retry(fail, retry_safe=retry_safe)
            self.assertEqual(len(calls), expected)
        calls = []
        async def cancel(): calls.append(1); raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError): await safe_retry(cancel, retry_safe=True)
        self.assertEqual(len(calls), 1)

    async def test_watchdog_never_retries_uncertain_communication_or_submission(self):
        for checkpoint in ["communication_started", "communication_unconfirmed", "submit_started", "submission_unconfirmed"]:
            service = DeliveryAutomationService.__new__(DeliveryAutomationService)
            service.state = DeliveryTaskView(id="synthetic", job_id=1, status="running", checkpoint=checkpoint)
            service.watch_limits = WatchLimits(10, 10, 10, 10)
            service.jobs = NS(mark_outcome_indeterminate=lambda *args: None)
            service._update = lambda status, detail, **kw: setattr(service.state, "status", status)
            service._retry_or_fail = lambda *args: self.fail("uncertain click was retried")
            service._run_once = AsyncMock()
            async def expire(operation, *args, **kwargs):
                operation.close(); raise WatchdogExpired("active_timeout")
            with patch("app.delivery.automation.guarded_operation", new=expire):
                await service._run_once_guarded(1)
            self.assertEqual(service.state.status, "outcome_indeterminate")

    async def test_busy_browser_preserves_wait_and_dispatch_releases_worker(self):
        service = DeliveryAutomationService.__new__(DeliveryAutomationService)
        service.state = DeliveryTaskView(id="synthetic", job_id=1, status="waiting_for_reply")
        service.watch_limits = WatchLimits(10, 10, 10, 10)
        service.jobs = NS(mock_delivery_queue=lambda: [])
        service._update = lambda status, detail, **kw: setattr(service.state, "status", status)
        async def busy(*args, **kwargs): raise BrowserLockBusy("synthetic contention")
        service._run_once = busy
        await service._run_once_guarded(1)
        self.assertEqual(service.state.status, "waiting_for_reply")
        service._run_once_guarded = AsyncMock()
        await asyncio.wait_for(service._run_until_terminal(1), timeout=1)
        service._run_once_guarded.assert_awaited_once()


class SearchCheckpointContracts(unittest.IsolatedAsyncioTestCase):
    async def test_reply_polling_rotates_least_recently_checked_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs = JobStore(Path(directory) / "jobs.db")
            job_id, _ = jobs.upsert_job({"source": "MockJobSite", "external_id": "1", "source_url": "http://127.0.0.1:8080/job?id=1",
                "title": "合成职位", "company": "合成公司", "location": "深圳", "score": 88, "decision": "APPLY", "reasoning": "合成匹配"})
            for index in range(4):
                jobs.create_delivery_run(f"wait-{index}", job_id)
                jobs.update_delivery_run(f"wait-{index}", status="waiting_for_reply")
            first = jobs.delivery_runs_waiting_reply(limit=2)
            for item in first: jobs.update_delivery_run(item["id"], last_checked_at=datetime.now(UTC))
            second = jobs.delivery_runs_waiting_reply(limit=2)
            self.assertTrue({item["id"] for item in first}.isdisjoint(item["id"] for item in second))
            self.assertEqual(len(jobs.delivery_runs_waiting_reply(limit=None)), 4)
            for index, checkpoint in enumerate(["failed_before_submit", "failed_precondition", "failed_transient_before_submit"]):
                jobs.update_delivery_run(f"wait-{index}", status="failed", checkpoint=checkpoint)
            self.assertEqual([item["id"] for item in jobs.delivery_runs_failed_before_submit()], ["wait-2"])
            jobs = None; gc.collect()

    async def test_crash_between_job_receipt_and_counter_does_not_lose_target_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs = JobStore(Path(directory) / "jobs.db")
            jobs.create_task("search", "mock", "金融分析师")
            jobs.save_search_request("search", BrowserSearchRequest(query="金融分析师").model_dump(mode="json"))
            job_id, _ = jobs.upsert_job({"source": "MockJobSite", "external_id": "1", "source_url": "http://127.0.0.1:8080/job?id=1",
                "title": "合成金融分析师", "company": "合成公司", "location": "深圳", "salary": "15-20K",
                "description": "合成财务分析 JD", "tags": [], "score": 88, "decision": "APPLY", "reasoning": "合成匹配"})
            jobs.event("search", "job_stored", "合成已存储", sample={"job_id": job_id, "filter_status": "pass"})
            self.assertEqual(jobs.get_task("search").matched, 0)
            reopened = JobStore(jobs.path); reopened.reconcile_search_counts("search")
            self.assertEqual(reopened.get_task("search").matched, 1)
            self.assertEqual(reopened.get_task("search").stored, 1)
            self.assertEqual(reopened.search_recovery("search")["processed_urls"], {"http://127.0.0.1:8080/job?id=1"})
            jobs = reopened = None; gc.collect()

    async def test_real_search_store_reopen_same_id_recheck_access_and_parameter_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs = JobStore(Path(directory) / "jobs.db")
            request = BrowserSearchRequest(query="金融分析师", salary_min=15000, result_limit=15)
            jobs.create_task("search", "mock", request.query); jobs.save_search_request("search", request.model_dump(mode="json"))
            jobs.mark_search_processed("search", "http://127.0.0.1:8080/jobs/1")
            jobs.mark_search_processed("search", "http://127.0.0.1:8080/jobs/1")
            jobs.update_task("search", status="failed", stored=1, matched=1)
            reopened = JobStore(jobs.path)
            self.assertEqual(reopened.search_recovery("search")["processed_urls"], {"http://127.0.0.1:8080/jobs/1"})
            service = BrowserAutomationService(reopened, None, None, None, Path(directory), headless=True)
            observed = []
            async def synthetic_worker(ident, payload): observed.append((ident, payload)); return None
            service._run_guarded = synthetic_worker
            resumed = await service.continue_search("search", request)
            await service.worker
            self.assertEqual(resumed.id, "search"); self.assertTrue(service.resume_access_check)
            self.assertEqual(resumed.stored, 1); self.assertEqual(len(observed), 1)
            with self.assertRaises(ValueError): await service.continue_search("search", request.model_copy(update={"salary_min": 20000}))
            reopened.update_task("search", status="completed")
            self.assertEqual((await service.continue_search("search", request)).status, "completed")
            self.assertEqual(len(observed), 1)
            service = jobs = reopened = None; gc.collect()


def main():
    generated = generate_cases()
    if "--generate" in sys.argv:
        CORPUS.parent.mkdir(parents=True, exist_ok=True)
        CORPUS.write_text(json.dumps({"version": 1, "data": "synthetic only", "cases": generated}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Generated {len(generated)} synthetic cases: {CORPUS}")
        return 0
    cases = json.loads(CORPUS.read_text(encoding="utf-8"))["cases"]
    for case in cases: setattr(MatrixContracts, "test_" + case["id"].replace("-", "_"), matrix_test(case))
    classes = (MatrixContracts, DecisionContracts, RuntimeContracts, MonitoringContracts, SearchCheckpointContracts)
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(cls) for cls in classes)
    class Recorded(unittest.TextTestResult):
        def addSuccess(self, test): super().addSuccess(test); TRACES.append({"test": test.id(), "status": "pass"})
    result = unittest.TextTestRunner(verbosity=1, resultclass=Recorded).run(suite)
    output = Path(__file__).resolve().parents[1] / "test-results" / "recovery-contract" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    summary = {"tests": result.testsRun, "corpus_cases": len(cases), "failures": len(result.failures), "errors": len(result.errors),
               "real_model_calls": 0, "real_browser_runs": 0, "real_submissions": 0,
               "corpus_sha256": hashlib.sha256(CORPUS.read_bytes()).hexdigest(),
               "coverage": "offline state matrix + production runtime/SQLite/registry/checkpoints/HR coordinator with scripted executors; not exhaustive, model eval or browser E2E"}
    summary["source_sha256"] = {str(path.relative_to(Path(__file__).parents[1])): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (Path(__file__).parents[1] / "app").rglob("*.py")}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "traces.jsonl").write_text("".join(json.dumps(item, ensure_ascii=False, default=str) + "\n" for item in TRACES), encoding="utf-8")
    (output / "failures.json").write_text(json.dumps([{"test": str(test), "traceback": error}
        for test, error in result.failures + result.errors], ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), **{key: value for key, value in summary.items() if key != "source_sha256"}}, ensure_ascii=False))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__": raise SystemExit(main())
