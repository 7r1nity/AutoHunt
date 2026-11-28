"""Isolated salary boolean and durable plan-edit contracts. No external models or recruiting."""
from __future__ import annotations

import asyncio
import copy
import gc
import json
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS

from cryptography.fernet import Fernet

from app.browser.automation import BrowserAutomationService
from app.core.agent_tools import AgentToolRegistry, AgentToolSpec
from app.core.main_agent import AutoHuntAgent
from app.core.job_filters import evaluate_filter, partial_filter_status, validate_filter, filter_summary
from app.core.replies import tool_reply
from app.core.models import BrowserSearchRequest, BrowserTaskStatus, BrowserTaskView
from app.core.salary_conditions import SalaryPredicate, salary_facts, normalized_search_filters
from app.runtime.context import RunContext, current_run, dialogue_change
from app.runtime.manager import AgentRuntime
from app.runtime.store import RunStore
from app.security.action_grants import action_binding, binding_matches
from app.workflow.execution import advance, persist
from app.workflow.planning import RequestDraft, validate_draft, plan_request
from app.workflow.revision import RevisionDraft, edited_plan
from app.llm.base import LLMResult

STAMP = "synthetic-resume:synthetic-document:1"
TRACES = []


def leaf(unit, **bounds):
    return {"op": "condition", "text": "合成薪资条件", "salary": {"unit": unit, **bounds}}


def either():
    return {"op": "any", "children": [leaf("monthly", minimum=15000), leaf("negotiable")]}


def task_plan():
    steps = []
    for ident, tool, args, status, deps, effect in [
        ("search", "job_search_start", {"queries": ["数据分析师"], "result_limit": 15, "salary_min": 15000, "filters": either()}, "waiting_external", [], "external"),
        ("summary", "job_results_read", {"summary": True}, "pending", ["search"], "read"),
        ("delivery", "mock_delivery_start", {"min_score": 85}, "pending", ["summary"], "external"),
        ("inspection", "job_results_read", {"summary": False, "limit": 10, "stage": "review"}, "pending", ["summary"], "read")]:
        steps.append(dict(id=ident, tool=tool, arguments=args, status=status, depends_on=deps, effect=effect,
            missing_slots=[], user_requested=True, command_quote="合成用户明确要求执行", scope_quote="", task_id="browser-1" if ident == "search" else None,
            authority_binding=action_binding(tool, args, session_id="default", requested=True)))
    return dict(id="plan-1", revision=0, paused=False, original="合成完整搜索、投递和成长任务", answers=[], questions=[],
                resume_stamp=STAMP, turn=1, expires_at=time.time()+1800, status="running", goals=[], reply="", steps=steps)


class SalaryContracts(unittest.IsolatedAsyncioTestCase):
    async def test_boolean_salary_matrix_without_model(self):
        calls = []
        async def forbidden(request):
            calls.append(request)
            raise AssertionError("typed salary must not call a model")
        cases = [
            ("18-25K", "pass"), ("12-20K", "fail"), ("面议", "pass"),
            ("薪资未注明", "unknown"), ("300元/天", "fail"), ("年薪30万", "unknown"),
            ("15-40K", "pass"), ("25-30k·13薪", "pass"), ("2-3万美元/月", "unknown")]
        for salary, expected in cases:
            result = await evaluate_filter(forbidden, either(), {"salary": salary})
            self.assertEqual(result["status"], expected, salary)
            TRACES.append(dict(kind="salary_or", salary=salary, expected=expected, actual=result["status"]))
        self.assertEqual(calls, [])

    async def test_ranges_and_boundary_grid(self):
        for mode in ("contained", "overlap"):
            expression = leaf("monthly", minimum=15000, maximum=40000, range_mode=mode)
            for low in (10000, 14999, 15000, 25000, 40000, 40001):
                for high in (low, max(low, 15000), max(low, 40000), max(low, 50000)):
                    expected = (low >= 15000 and high <= 40000) if mode == "contained" else (high >= 15000 and low <= 40000)
                    salary = f"{low}-{high}元/月"
                    actual = partial_filter_status(expression, {"salary": salary})
                    self.assertEqual(actual, "pass" if expected else "fail")
                    TRACES.append(dict(kind="salary_range", mode=mode, salary=salary, actual=actual))

    async def test_daily_threshold_keeps_unit_without_employment_applicability(self):
        expression = leaf("daily", minimum=300, applies_to="internship")
        cases = [("300-500元/天", "实习", "pass"), ("200-400元/天", "实习", "fail"),
                 ("299元/天", "实习", "fail"), ("300元/天", "实习", "pass"),
                 ("面议", "实习", "unknown"), ("未注明", "实习", "unknown"),
                 ("13-20K", "全职", "fail"), ("13-20K", "社招", "fail"),
                 ("13-20K", "应届/校招", "fail"), ("5-8K", "实习", "fail"), ("15-20K", "", "fail")]
        for salary, employment, expected in cases:
            self.assertEqual(partial_filter_status(expression, {"salary": salary, "employment_type": employment}), expected)

    async def test_mixed_conditions_preserve_deterministic_evidence_on_model_failure(self):
        async def unavailable(_):
            raise TimeoutError()
        mixed = {"op": "any", "children": [leaf("negotiable"), {"op": "condition", "text": "某复杂公司条件"}]}
        self.assertEqual((await evaluate_filter(unavailable, mixed, {"salary": "面议"}))["status"], "pass")
        mixed["op"] = "all"
        self.assertEqual((await evaluate_filter(unavailable, mixed, {"salary": "面议"}))["status"], "unknown")
        self.assertEqual((await evaluate_filter(unavailable, mixed, {"salary": "15-20K"}))["status"], "fail")

    async def test_legacy_bounds_can_change_and_clear_without_duplicate_predicates(self):
        first = normalized_search_filters(None, 15000)
        second = normalized_search_filters(first, 20000)
        self.assertEqual(len(validate_filter(second).leaves()), 1)
        self.assertEqual(partial_filter_status(second, {"salary": "18-25K"}), "fail")
        self.assertIsNone(normalized_search_filters(second, 0))
        self.assertEqual(partial_filter_status(normalized_search_filters(either(), 15000), {"salary": "面议"}), "pass")
        self.assertEqual(normalized_search_filters(second, 20000), second)

    async def test_browser_early_filter_does_not_veto_negotiable_or_unknown(self):
        browser = object.__new__(BrowserAutomationService)
        events = []
        browser.store = NS(increment_funnel=lambda *a, **kw: None, event=lambda *a, **kw: events.append(kw))
        request = BrowserSearchRequest(query="数据分析师", salary_min=15000, filters=either())
        kept = browser._filter_salary_cards("test", [{"salary": v} for v in ("面议", "18-25K", "12-20K", "未知", "300元/天")], request)
        self.assertEqual([row["salary"] for row in kept], ["面议", "18-25K", "未知"])
        self.assertEqual(len(events), 2)

    async def test_invalid_or_ambiguous_facts_are_not_guessed(self):
        for value in ("15", "工资待定", "50-20K", "100元/小时"):
            self.assertEqual(salary_facts(value)["unit"], "unknown")
        with self.assertRaises(ValueError): SalaryPredicate(unit="monthly", minimum=40000, maximum=15000)
        with self.assertRaises(ValueError): SalaryPredicate(unit="negotiable", minimum=15000)

    async def test_single_amount_thousands_and_display_preserve_exceptions(self):
        for text, unit, amount in [("15,000元/月", "monthly", 15000), ("月薪15k", "monthly", 15000),
                                   ("300元/天", "daily", 300), ("1.5万元/月", "monthly", 15000)]:
            self.assertEqual(salary_facts(text), dict(unit=unit, minimum=amount, maximum=amount))
        display = tool_reply("job_search_start", "completed", {"search_conditions": {"salary_min": 15000, "filters": either()}})
        self.assertIn("或 薪资面议", display)
        self.assertNotIn("面议、未知及日薪不算满足", display)
        self.assertNotIn("仅限制", filter_summary(leaf("daily", minimum=300, applies_to="internship")))
        self.assertIn("日薪", filter_summary(leaf("daily", minimum=300, applies_to="internship")))


class RevisionContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.key = Fernet.generate_key().decode()
        self.store = RunStore(Path(self.temp.name)/"runtime.db", self.key)
        self.candidate = NS(read=lambda: NS(profile=NS(resume_id="synthetic-resume")))
        self.resumes = NS(read_document=lambda: NS(id="synthetic-document", version=1))
        self.calls = []
        self.registry = AgentToolRegistry(jobs=None, browser=None, candidate_state=self.candidate,
                                         resume_workspace=self.resumes, memory=None, mock_workflow=NS())
        for name in ("mock_delivery_start", "job_results_read"):
            spec = self.registry._tools[name]
            async def executor(args, name=name):
                self.calls.append((name, copy.deepcopy(args)))
                return {"message": "合成完成", "title": "合成计划"}
            self.registry._tools[name] = AgentToolSpec(spec.name, spec.description, spec.parameters, spec.effect, executor)
        self.view = BrowserTaskView(id="browser-1", status=BrowserTaskStatus.RUNNING)
        self.pause_count = 0
        async def pause():
            self.pause_count += 1
            self.view.status = BrowserTaskStatus.PAUSED
        async def resume():
            self.view.status = BrowserTaskStatus.RUNNING
        self.browser = NS(current=lambda: self.view, pause=pause, resume=resume)
        self.runtime = AgentRuntime(self.store, NS(tool_registry=self.registry, candidate_state=self.candidate), NS(), self.browser, self.resumes)
        self.parent, _ = self.store.create("default", "parent", "chat", {"message": "原请求"})
        self.value = task_plan()
        self.store.update(self.parent["id"], status="running", checkpoint={"plan": self.value, "browser_id": "browser-1"})
        self.edit_run, _ = self.store.create("default", "edit", "chat", {"message": "先暂停，添加条件"})
        self.store.update(self.edit_run["id"], status="running")
        self.context = RunContext(self.edit_run["id"], asyncio.current_task(),
            lambda kind, data: self.runtime.emit(self.edit_run["id"], kind, data), lambda value: None,
            dialogue=self.store.dialogue_state("default"), mutate_dialogue=lambda operation: self.store.mutate_dialogue("default", operation),
            plan_reader=lambda: self.runtime.active_plan("default"), user_message="先暂停 添加条件 85分以下但是薪资25k以上的分析不足和劣势")
        self.context.authorize_action = lambda *args: self.runtime.authorize_action(self.edit_run["id"], *args)
        self.token = current_run.set(self.context)

    async def asyncTearDown(self):
        TRACES.append({"kind": "revision_test", "test": self.id(), "events": self.store.public_events(self.parent["id"]),
                       "calls": self.calls})
        current_run.reset(self.token)
        for task in self.runtime.tasks.values():
            task.cancel()
        await asyncio.gather(*self.runtime.tasks.values(), return_exceptions=True)
        gc.collect()  # sqlite context managers commit but release handles on collection.
        self.temp.cleanup()

    def enter(self):
        # IsolatedAsyncioTestCase runs setup and each test in separate tasks.
        self.context.owner = asyncio.current_task()

    def edit(self, **extra):
        return {"plan_id": "plan-1", "expected_revision": 0, "pause": True,
                "updates": [{"step_id": "inspection", "arguments": {"limit": 20}}], **extra}

    async def test_pause_edit_keeps_original_dependencies_authority_and_progress(self):
        self.enter()
        response = await self.runtime.revise_plan(self.edit_run["id"], self.edit(), self.context.user_message)
        updated = self.store.get(self.parent["id"])["checkpoint"]["plan"]
        self.assertTrue(updated["paused"])
        self.assertEqual(updated["revision"], 1)
        self.assertEqual(updated["steps"][:3], self.value["steps"][:3])
        inspection = updated["steps"][3]
        self.assertEqual(inspection["depends_on"], ["summary"])
        self.assertEqual(inspection["arguments"], {"summary": False, "limit": 20, "stage": "review"})
        self.assertTrue(binding_matches(inspection["authority_binding"], inspection["tool"], inspection["arguments"], "default"))
        self.assertEqual(self.pause_count, 1)
        self.assertEqual(self.calls, [])
        self.assertNotIn("plan", self.store.get(self.edit_run["id"])["checkpoint"] or {})
        self.assertIn("更新", response.message)
        self.assertEqual(await advance(updated, self.registry), [])
        with self.assertRaises(ValueError):
            await self.runtime.revise_plan(self.edit_run["id"], self.edit(), "旧版本修改")

    async def test_reject_started_completed_revoked_or_wrong_version_steps(self):
        self.enter()
        for status in ("completed", "claimed", "outcome_indeterminate", "waiting_external", "blocked"):
            source = task_plan(); source["steps"][3]["status"] = status
            with self.assertRaises(ValueError):
                edited_plan(source, RevisionDraft.model_validate(self.edit()), self.registry, stamp=STAMP, session_id="default", message="修改")
        for extra in ({"expected_revision": 8}, {"plan_id": "other"}):
            with self.assertRaises(ValueError):
                edited_plan(self.value, RevisionDraft.model_validate(self.edit(**extra)), self.registry, stamp=STAMP, session_id="default", message="修改")
        source = task_plan(); source["revoked_tools"] = ["job_results_read"]
        with self.assertRaises(ValueError):
            edited_plan(source, RevisionDraft.model_validate(self.edit()), self.registry, stamp=STAMP, session_id="default", message="修改")
        with self.assertRaises(ValueError):
            edited_plan(self.value, RevisionDraft.model_validate(self.edit()), self.registry, stamp="new-resume", session_id="default", message="修改")

    async def test_model_revision_contract_and_compact_snapshot(self):
        self.enter()
        captured = []
        class SyntheticLLM:
            async def generate(inner, request):
                captured.append(json.loads(request.user_message))
                return LLMResult(text=json.dumps({"operation": "revise", "steps": [], "revision": self.edit()}, ensure_ascii=False), provider="synthetic", model="script")
        draft = await plan_request(SyntheticLLM(), self.registry, self.context.user_message, dialogue={}, conditions={}, stamp=STAMP)
        self.assertEqual(draft["operation"], "revise")
        self.assertEqual(captured[0]["active_plan"]["steps"][0]["status"], "waiting_external")
        self.assertNotIn("result", captured[0]["active_plan"]["steps"][0])
        addition = RequestDraft(operation="revise", revision=self.edit(), steps=[{"id": "new", "tool": "resume_propose_patch", "arguments": {"instruction": "补充合成项目描述"}}])
        normalized = validate_draft(addition, self.registry, "修改", dialogue={}, conditions={}, stamp=STAMP)
        self.assertEqual(normalized["revision"]["add_steps"][0]["id"], "new")
        self.assertEqual(self.calls, [])  # Normalization is never execution.

    async def test_main_planner_dispatch_updates_parent_without_new_plan(self):
        self.enter()
        class SyntheticLLM:
            async def generate(inner, request):
                return LLMResult(text=json.dumps({"operation": "revise", "steps": [], "revision": self.edit()}), provider="synthetic", model="script")
        agent = object.__new__(AutoHuntAgent)
        agent.llm, agent.router, agent.tool_registry = SyntheticLLM(), None, self.registry
        agent.candidate_state, agent.resume_workspace = self.candidate, self.resumes
        agent._conversation_history = []
        self.context.revise_task = lambda edit, message: self.runtime.revise_plan(self.edit_run["id"], edit, message)
        response = await agent._handle_planned(self.context.user_message)
        self.assertIn("可说「继续任务」", response.message)
        self.assertEqual(self.context.dialogue["task_request"]["original"], self.value["original"])
        self.assertNotIn("plan", self.store.get(self.edit_run["id"])["checkpoint"] or {})
        self.assertEqual(self.calls, [])

    async def test_reopened_pause_and_real_watcher_wait_until_explicit_continue(self):
        self.enter()
        await self.runtime.revise_plan(self.edit_run["id"], self.edit(), "暂停修改")
        # Reopen with the original cipher key to prove that edits live on disk.
        self.runtime.store = RunStore(self.store.path, self.key)
        self.assertTrue(self.runtime.active_plan("default")["paused"])
        self.runtime.jobs = NS(search_conversation_history=lambda *a, **kw: [], add_conversation_message=lambda *a, **kw: None)
        self.runtime.browser_summary = lambda *a: None
        self.runtime.observe_browser = lambda _: BrowserTaskView(id="browser-1", status=BrowserTaskStatus.COMPLETED)
        self.runtime.search_state = lambda _: {"queries": ["数据分析师"]}
        watcher = asyncio.create_task(self.runtime.watch_plan(self.parent["id"]))
        self.runtime.tasks[self.parent["id"]] = watcher
        await asyncio.sleep(.05)
        self.assertEqual(self.calls, [])
        await self.runtime.continue_task(self.edit_run["id"], "继续任务")
        await asyncio.wait_for(asyncio.shield(watcher), timeout=4)
        self.assertEqual([name for name, _ in self.calls], ["job_results_read", "mock_delivery_start", "job_results_read"])
        self.assertEqual(self.calls[-1][1]["limit"], 20)
        self.assertEqual(self.runtime.store.get(self.parent["id"])["status"], "completed")

    async def test_explicit_continue_unpauses_same_live_plan(self):
        self.enter()
        await self.runtime.revise_plan(self.edit_run["id"], self.edit(), "暂停修改")
        worker = asyncio.create_task(asyncio.Event().wait())
        self.runtime.tasks[self.parent["id"]] = worker
        answer = await self.runtime.continue_task(self.edit_run["id"], "继续任务")
        updated = self.store.get(self.parent["id"])["checkpoint"]["plan"]
        self.assertFalse(updated["paused"])
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(updated["steps"][3]["arguments"]["limit"], 20)
        self.assertEqual(self.view.status, BrowserTaskStatus.RUNNING)
        self.assertEqual(self.calls, [])
        self.assertIn("原任务", answer.message)

    async def test_stale_background_save_cannot_overwrite_revision(self):
        self.enter()
        await self.runtime.revise_plan(self.edit_run["id"], self.edit(), "暂停修改")
        self.runtime.emit(self.parent["id"], "plan_state", {"plan": self.value})
        self.assertEqual(self.store.get(self.parent["id"])["checkpoint"]["plan"]["revision"], 1)

    async def test_resume_runner_executes_only_after_original_search_summary_complete(self):
        self.enter()
        updated = edited_plan(self.value, RevisionDraft.model_validate(self.edit(pause=False)), self.registry,
                              stamp=STAMP, session_id="default", message="修改")
        dialogue_change("task", kind="plan_id", id="plan-1", status="running", bind=True)
        dialogue_change("instruction", request=updated)
        await advance(updated, self.registry)
        self.assertEqual(self.calls, [])
        updated["steps"][0]["status"] = "completed"
        updated["steps"][1]["status"] = "completed"
        await advance(updated, self.registry)
        self.assertEqual([name for name, _ in self.calls], ["mock_delivery_start", "job_results_read"])
        self.assertEqual(self.calls[-1][1]["limit"], 20)
        await advance(updated, self.registry)
        self.assertEqual(len(self.calls), 2)


def main():
    suite = unittest.defaultTestLoader.loadTestsFromModule(__import__(__name__, fromlist=["*"]))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    root = Path("test-results/salary-revision")/datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    root.mkdir(parents=True)
    (root/"summary.json").write_text(json.dumps({"tests": result.testsRun, "failures": len(result.failures),
        "errors": len(result.errors), "trace_cases": len(TRACES), "external_model_calls": 0}, indent=2), encoding="utf-8")
    (root/"traces.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in TRACES), encoding="utf-8")
    (root/"failures.json").write_text(json.dumps([{ "test": str(test), "detail": detail} for test, detail in result.failures+result.errors], ensure_ascii=False, indent=2), encoding="utf-8")
    print("Results:", root)
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
