"""Model-owned interpretation / action-owned approval regression runner.

Run: python -m evaluation.action_contract
Scripted model responses and synthetic executors only. No API, browser or user
stores are accessed. JSONL includes each assertion and its application events.
These contracts do not measure a real model's semantic accuracy.
"""
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
from unittest.mock import patch

from app.core.agent_tools import AgentToolRegistry, AgentToolSpec, ToolLoopState
from app.core.main_agent import AutoHuntAgent, _fast_search_hint
from app.core.job_filters import validate_filter
from app.core.search_contract import browser_search_keyword
from app.intent.router import IntentRouter
from app.intent.rules import RuleEngine
from app.intent.small_model import SmallModelDecision
from app.intent.store import RouterStore
from app.llm.base import LLMResult
from app.memory.resume_store import analyze_resume_text
from app.runtime.context import RunContext, current_run
from app.workflow.execution import advance, begin, persist
from app.workflow.planning import RequestDraft, plan_request, shortcut_safe, validate_draft
from app.workflow.validation import PlanValidationError, preflight, preflight_warnings
from app.workflow.requests import canonical_request, amended_request

RECORDS = []


class Model:
    def __init__(self, *drafts):
        self.drafts = list(drafts)
        self.requests = []

    async def generate(self, request):
        self.requests.append(json.loads(request.user_message))
        return LLMResult(json.dumps(self.drafts.pop(0), ensure_ascii=False), "scripted", "contract")


def candidate(*, role="金融风控", delivery=True, requested=True):
    steps = [{"id": "search", "tool": "job_search_start", "arguments": {
        "queries": [role], "locations": ["深圳", "杭州"], "result_limit": 18, "salary_min": 13000},
        "command_quote": "按专业寻找机会", "user_requested": True}]
    if delivery:
        steps += [{"id": "delivery", "tool": "mock_delivery_start", "arguments": {"min_score": 85},
            "command_quote": "为高匹配岗位提交", "user_requested": requested, "depends_on": ["search"]},
            {"id": "growth", "tool": "learning_plan_generate", "arguments": {
                "max_score": 84, "salary_min": 25000, "focus": "不足与提升"},
             "user_requested": True, "depends_on": ["search"]}]
    return {"steps": steps, "goals": [{"quote": "岗位与后续行动", "disposition": "steps",
        "step_ids": [step["id"] for step in steps]}]}


class Contracts(unittest.IsolatedAsyncioTestCase):
    def _callTestMethod(self, method):
        async def owned_test():
            # IsolatedAsyncioTestCase runs setup and the test in different Tasks.
            self.context.owner = asyncio.current_task()
            await method()
        self._callAsync(owned_test)

    async def asyncSetUp(self):
        self.events = []
        self.calls = []
        self.document = NS(id="synthetic-document", version=1)
        self.candidate = NS(read=lambda: NS(profile=NS(resume_id="synthetic-resume")))
        self.resumes = NS(read_document=lambda: self.document)
        self.registry = AgentToolRegistry(jobs=None, browser=None, candidate_state=self.candidate,
            resume_workspace=self.resumes, memory=None, learning_plans=NS(), mock_workflow=NS())
        self.context = RunContext("synthetic-run", asyncio.current_task(),
            lambda k, d: self.events.append({"kind": k, **d}), lambda _: None,
            session_id="synthetic-session", dialogue={"turn": 1}, search_state={"queries": ["金融分析师"]})
        self.token = current_run.set(self.context)
        self.stamp = "synthetic-resume:synthetic-document:1"

    async def asyncTearDown(self):
        RECORDS.append({"test": self.id(), "events": self.events, "executor_calls": self.calls})
        current_run.reset(self.token)

    def validate(self, payload=None, message="找十八个工作，85分以上投递，以下且25k以上生成提升计划", **kwargs):
        return validate_draft(RequestDraft.model_validate(payload or candidate()), self.registry, message,
            dialogue=self.context.dialogue, conditions=self.context.search_state, stamp=self.stamp, **kwargs)

    def bind(self, plan, step):
        self.context.dialogue["tasks"] = {"plan_id": plan["id"]}
        step["status"] = "claimed"
        persist(plan)
        self.context.action_authority = {"tool": step["tool"], "arguments": copy.deepcopy(step["arguments"]),
            "plan_id": plan["id"], "step_id": step["id"], "binding": step["authority_binding"],
            "resume_stamp": self.stamp, "resumes": self.resumes, "candidate": self.candidate,
            "expires_at": plan["expires_at"], "requested": step.get("user_requested", False)}

    async def test_cross_industry_paraphrases_not_vetoed(self):
        roles = ["金融风控", "国际销售", "医学检验", "建筑设计", "机械研发", "物流规划",
                 "法务", "教师", "酒店运营", "农业技术", "算法工程师", "电力工程师"]
        verbs = ["替我挑", "推荐", "寻找", "筛一批", "给我安排", "帮忙查查", "检索", "找"]
        for role in roles:
            for verb in verbs:
                for whitespace in [" ", "  ", "\n"]:
                    with self.subTest(role=role, verb=verb, whitespace=whitespace):
                        plan = self.validate(candidate(role=role), f"{verb}{whitespace}专业相关机会，满足条件后投递并制定提升安排")
                        self.assertEqual(plan["status"], "ready")
                        self.assertEqual(plan["steps"][0]["arguments"]["queries"], [role])
                        self.assertEqual(plan["steps"][0]["arguments"]["salary_min"], 13000)

    async def test_model_extraction_not_overwritten_by_rules(self):
        model = Model(candidate(delivery=False))
        with patch("app.workflow.planning.source_search_constraints", side_effect=AssertionError("legacy extraction")):
            plan = await plan_request(model, self.registry, "请结合专业推荐机会，预算用月薪",
                dialogue={}, conditions=self.context.search_state, stamp=self.stamp)
        self.assertEqual(len(model.requests), 1)
        self.assertNotIn("action_semantics", model.requests[0])
        self.assertEqual(plan["steps"][0]["arguments"]["queries"], ["金融风控"])

    async def test_rules_only_reuse_saved_keywords(self):
        for message in ["帮我找工作", "再次检索", "再次搜索", "按现有条件重新搜索"]:
            self.assertEqual(_fast_search_hint(message), {})
        for message in ["找风险策略岗位", "找15个工作", "再次搜索，薪资改为15k", "帮我找工作并投递", "他说‘再次搜索’"]:
            self.assertIsNone(_fast_search_hint(message))
            self.assertFalse(shortcut_safe(message))
        self.assertEqual(browser_search_keyword("高级 LLM 算法工程师"), "高级 LLM 算法工程师")
        self.assertEqual(analyze_resume_text("Python FastAPI LLM 风控" )[1:], ([], ""))

    async def test_small_model_miss_and_low_confidence_escalate(self):
        with tempfile.TemporaryDirectory() as temp:
            store = RouterStore(Path(temp) / "router.sqlite")
            seen = []
            class Small:
                available = True
                async def classify(self, message):
                    seen.append(message)
                    return SmallModelDecision("search_jobs", .3, .02)
            router = IntentRouter(rule_engine=RuleEngine(), small_model=Small(), store=store)
            outcome = await router.route("替我挑一批合适的机会")
            self.assertEqual(seen, ["替我挑一批合适的机会"])
            self.assertEqual(outcome.decision, "llm")
            del router, store
            gc.collect()  # Release SQLite context-manager connections on Windows.

    async def test_summary_added_and_branches_wait_for_search(self):
        plan = self.validate()
        summary = next(step for step in plan["steps"] if step["tool"] == "job_results_read")
        self.assertEqual(summary["depends_on"], ["search"])
        self.assertTrue(summary["arguments"]["summary"])
        for name in ["job_search_start", "mock_delivery_start", "learning_plan_generate", "job_results_read"]:
            spec = self.registry._tools[name]
            async def executor(args, tool=name):
                self.calls.append(tool)
                return {"id": "synthetic-search", "message": "ok"}
            self.registry._tools[name] = AgentToolSpec(name, spec.description, spec.parameters, spec.effect, executor)
        self.context.dialogue["tasks"] = {"plan_id": plan["id"]}
        await advance(plan, self.registry)
        self.assertEqual(self.calls, ["job_search_start"])
        plan["steps"][0]["status"] = "completed"
        await advance(plan, self.registry)
        self.assertEqual(set(self.calls), {"job_search_start", "mock_delivery_start", "learning_plan_generate", "job_results_read"})
        self.assertEqual(plan["status"], "finished")
        await advance(plan, self.registry)
        self.assertEqual(len(self.calls), 4)

    async def test_valid_bound_action_does_not_reparse_quotes(self):
        plan = self.validate()
        step = plan["steps"][1]
        self.bind(plan, step)
        with patch("app.core.agent_tools.command_grounded", create=True, side_effect=AssertionError("language veto")):
            ok, _ = self.registry.call_guard.check(self.registry._tools[step["tool"]], step["arguments"], ToolLoopState())
        self.assertTrue(ok)

    async def test_unknown_consent_asks_then_context_confirmation(self):
        plan = self.validate(candidate(requested=False))
        self.assertEqual(plan["status"], "clarifying")
        amended = candidate(requested=True)
        amended["continuation"] = True
        confirmed = self.validate(amended, message="行，那就按这个范围执行", previous=plan)
        self.assertEqual(confirmed["status"], "ready")
        self.assertEqual(confirmed["original"], plan["original"])
        self.assertEqual(confirmed["id"], plan["id"])

    async def test_audit_binding_is_not_a_tool_authorization_gate(self):
        plan = self.validate()
        step = plan["steps"][0]
        self.bind(plan, step)
        self.context.action_authority["binding"] = {"source": "old-quote"}
        self.context.dialogue["instruction_request"]["id"] = "old-clarification"
        spec = self.registry._tools[step["tool"]]
        async def search(args):
            self.calls.append(args)
            return {"id": "synthetic-browser"}
        self.registry._tools[spec.name] = AgentToolSpec(spec.name, spec.description, spec.parameters, spec.effect, search)
        outcome = await self.registry.execute(spec.name, step["arguments"], user_message="合成搜索", state=ToolLoopState())
        self.assertEqual(outcome.status, "ok")
        self.assertEqual(len(self.calls), 1)

    async def test_submission_scope_is_enforced_by_submission_executor(self):
        from app.runtime.resilience import PreflightRefused
        for metadata in (None, {"requested": False}):
            self.context.action_authority = metadata
            with self.assertRaises(PreflightRefused):
                await self.registry._mock_delivery_start({"min_score": 85})
        self.context.action_authority = {"requested": True, "evidence_task_id": "original-search"}
        async def queue(**args):
            self.calls.append(args)
            return {"queued": 1}
        self.registry.mock_workflow = NS(remember_authorization_scope=lambda *a, **k: None, queue_deliveries=queue)
        outcome = await self.registry.execute("mock_delivery_start", {"min_score": 85}, user_message="合成投递", state=ToolLoopState())
        self.assertEqual(outcome.status, "ok")
        self.assertEqual(self.calls[0]["task_id"], "original-search")

    async def test_loop_budget_and_duplicate_protection_remain(self):
        spec = self.registry._tools["job_stats_read"]
        state = ToolLoopState()
        self.assertTrue(self.registry.call_guard.check(spec, {}, state)[0])
        self.assertFalse(self.registry.call_guard.check(spec, {}, state)[0])
        self.assertEqual(state.denial_code, "duplicate_tool_call")
        state = ToolLoopState()
        for number in range(8):
            self.assertTrue(self.registry.call_guard.check(spec, {"number": number}, state)[0])
        self.assertFalse(self.registry.call_guard.check(spec, {"number": 8}, state)[0])
        self.assertEqual(state.denial_code, "tool_budget_exhausted")

    async def test_old_background_plan_does_not_overwrite_foreground_question(self):
        old = self.validate(candidate(delivery=False))
        current = self.validate(candidate(delivery=False))
        current.update(status="clarifying", questions=["合成缺失参数"])
        persist(current, activate=True)
        persist(old)
        self.assertEqual(self.context.dialogue["instruction_request"]["id"], current["id"])
        self.assertEqual(self.context.dialogue["tasks"]["plan_id"], current["id"])

    async def test_missing_roles_clarify_real_site_delivery_stays_disabled(self):
        self.context.search_state = {}
        payload = candidate(delivery=False)
        payload["steps"][0]["arguments"].pop("queries")
        self.assertEqual(self.validate(payload)["status"], "clarifying")
        payload = candidate()
        payload["steps"][0]["arguments"]["source"] = "boss"
        self.assertEqual(self.validate(payload)["status"], "clarifying")

    async def test_structure_errors_repair_not_language_reject(self):
        for changed in ["cycle", "unknown_tool", "bad_type", "bad_range", "duplicate"]:
            with self.subTest(changed=changed):
                payload = candidate()
                if changed == "cycle": payload["steps"][0]["depends_on"] = ["delivery"]
                elif changed == "unknown_tool": payload["steps"][0]["tool"] = "invented_tool"
                elif changed == "bad_type": payload["steps"][0]["arguments"]["result_limit"] = True
                elif changed == "bad_range": payload["steps"][1]["arguments"]["min_score"] = 101
                else: payload["steps"][1]["id"] = "search"
                with self.assertRaises(PlanValidationError) as failure:
                    self.validate(payload)
                self.assertEqual(failure.exception.issue.category, "repair")

    async def test_schema_repair_bounded_and_conditions_inherit(self):
        bad = candidate(delivery=False)
        bad["steps"][0]["arguments"]["result_limit"] = True
        good = candidate(delivery=False)
        good["steps"][0]["arguments"] = {}
        self.context.search_state.update(result_limit=15, salary_min=12000, locations=["武汉"])
        model = Model(bad, good)
        plan = await plan_request(model, self.registry, "再次搜索", dialogue={},
            conditions=self.context.search_state, stamp=self.stamp)
        self.assertEqual(len(model.requests), 2)
        self.assertEqual(plan["steps"][0]["arguments"]["result_limit"], 15)
        self.assertEqual(plan["steps"][0]["arguments"]["locations"], ["武汉"])

    async def test_filter_semantics_model_owned_but_shape_bounded(self):
        node = validate_filter({"op": "condition", "text": "可包含实习岗位", "mode": "allow"}, ["我愿意考虑暑期机会"])
        self.assertEqual(node.mode, "allow")
        with self.assertRaises(ValueError):
            validate_filter({"op": "not", "children": []})

    async def test_actual_dispatch_retains_model_understood_task(self):
        agent = AutoHuntAgent.__new__(AutoHuntAgent)
        agent.tool_registry = self.registry
        agent.resume_workspace = self.resumes
        agent.candidate_state = self.candidate
        agent.router = None
        agent._conversation_history = []
        agent.llm = Model(candidate(delivery=False))
        spec = self.registry._tools["job_search_start"]
        async def search(arguments):
            self.calls.append({"tool": "job_search_start", "arguments": arguments})
            return {"id": "synthetic-browser", "status": "waiting_for_user", "detail": "等待登录"}
        self.registry._tools[spec.name] = AgentToolSpec(spec.name, spec.description, spec.parameters, spec.effect, search)
        message = "替我挑一批职业机会，结合专业与待遇综合安排"
        response = await agent._dispatch_contextual_message(message, None)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("搜索", response.message)
        self.assertEqual(self.context.dialogue["task_request"]["interpretation"], "model")
        anchor = canonical_request(self.context.dialogue, [])
        self.assertEqual(anchor.original, message)
        amended = amended_request(self.context.dialogue, {"locations": ["武汉"]}, "换武汉", "next-run")
        self.assertEqual(amended.original, message)
        self.assertEqual(amended.conditions["locations"], ["武汉"])
        response = await agent._dispatch_contextual_message("我的需求是什么", None)
        self.assertIn(message, response.message)
        self.assertEqual(len(agent.llm.requests), 1)
        self.assertEqual(len(self.calls), 1)

    async def test_upload_followup_activates_new_plan_over_old_clarification(self):
        # The old request has no resume; upload changes its stamp, so the model
        # produces a new execution plan. Exercise begin -> persist -> registry.
        self.context.dialogue["instruction_request"] = {
            "id": "before-upload", "status": "clarifying", "resume_stamp": "None:master:1"}
        plan = self.validate(candidate(delivery=False))
        spec = self.registry._tools["job_search_start"]
        async def search(args):
            self.calls.append(args)
            return {"id": "synthetic-browser", "status": "waiting_for_user"}
        self.registry._tools[spec.name] = AgentToolSpec(spec.name, spec.description, spec.parameters, spec.effect, search)
        await begin(plan, self.registry)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.context.dialogue["instruction_request"]["id"], plan["id"])
        self.assertEqual(self.context.dialogue["tasks"]["plan_id"], plan["id"])

    async def test_upload_followup_persists_one_owner_before_dispatch_and_reopen(self):
        from app.runtime.store import RunStore
        with tempfile.TemporaryDirectory() as temp:
            store = RunStore(Path(temp) / "runtime.db")
            self.context.mutate_dialogue = lambda operation: store.mutate_dialogue("default", operation)
            before = self.validate(candidate(delivery=False))
            before.update(status="clarifying", questions=["请上传合成简历"], resume_stamp="None:master:1")
            await begin(before, self.registry)
            self.assertEqual(self.calls, [])
            self.document.version = 2
            self.stamp = "synthetic-resume:synthetic-document:2"
            data = candidate()
            data["steps"][0]["arguments"]["result_limit"] = 15
            after = self.validate(data)
            for name in ("job_search_start", "job_results_read", "mock_delivery_start", "learning_plan_generate"):
                spec = self.registry._tools[name]
                async def executor(args, tool=name):
                    saved = store.dialogue_state("default")
                    self.assertEqual(saved["instruction_request"]["id"], saved["tasks"]["plan_id"])
                    self.assertEqual(saved["tasks"]["plan_id"], after["id"])
                    claimed = next(s for s in saved["instruction_request"]["steps"] if s["tool"] == tool)
                    self.assertEqual(claimed["status"], "claimed")
                    self.calls.append({"tool": tool, "args": args})
                    return {"id": "synthetic-search", "message": "ok"}
                self.registry._tools[name] = AgentToolSpec(name, spec.description, spec.parameters, spec.effect, executor)
            await begin(after, self.registry)
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(self.calls[0]["args"]["result_limit"], 15)
            after["steps"][0]["status"] = "completed"
            await advance(after, self.registry)
            self.assertEqual(len(self.calls), 4)
            await advance(after, self.registry)
            self.assertEqual(len(self.calls), 4)
            reopened = RunStore(store.path).dialogue_state("default")
            self.assertEqual(reopened["instruction_request"]["id"], reopened["tasks"]["plan_id"])
            self.assertEqual(reopened["instruction_request"]["status"], "finished")
            self.context.mutate_dialogue = None
            del store
            gc.collect()

    async def test_waiting_does_not_expire_ordinary_plan_execution(self):
        plan = self.validate(candidate(delivery=False))
        plan["expires_at"] = 0
        spec = self.registry._tools["job_search_start"]
        async def search(args):
            self.calls.append(args)
            return {"id": "synthetic-browser", "status": "waiting_for_user"}
        self.registry._tools[spec.name] = AgentToolSpec(spec.name, spec.description, spec.parameters, spec.effect, search)
        await begin(plan, self.registry)
        self.assertEqual(len(self.calls), 1)

    async def test_citation_warnings_never_skip_cycles_and_are_task_local(self):
        payload = RequestDraft.model_validate(candidate(delivery=False)).model_dump()
        steps = payload["steps"]
        goals = payload["goals"]
        tools = set(self.registry._tools)
        async def check_warning(source):
            preflight(steps, goals, [source], tools)
            await asyncio.sleep(0)
            return preflight_warnings()
        warnings, clean = await asyncio.gather(
            check_warning("不是模型引用的原句"), check_warning("按专业寻找机会 岗位与后续行动"))
        self.assertTrue(warnings)
        self.assertFalse(clean)
        steps[0]["depends_on"] = [steps[0]["id"]]
        with self.assertRaises(PlanValidationError):
            preflight(steps, goals, ["引用不一致"], tools)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Contracts)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    output = Path(__file__).resolve().parents[1] / "test-results" / "action-contract" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    summary = {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
        "cross_industry_scripted_variants": 288, "real_model_calls": 0, "browser_runs": 0,
        "limitations": "Scripted component contracts, not real-model accuracy or browser E2E."}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "traces.jsonl").write_text("".join(json.dumps(record, ensure_ascii=False, default=str) + "\n" for record in RECORDS), encoding="utf-8")
    (output / "failures.json").write_text(json.dumps([{"test": str(test), "traceback": error} for test, error in result.failures + result.errors], ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), **summary}, ensure_ascii=False))
    raise SystemExit(0 if result.wasSuccessful() else 1)
