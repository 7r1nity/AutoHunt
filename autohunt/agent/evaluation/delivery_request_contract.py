"""User command -> planner -> approval -> real isolated queue -> chat contracts.

All job facts, model outputs and account binding are synthetic. Real RunStore,
JobStore, MainAgent, planner, action policy, coordinator and history persistence
are exercised. No browser, external model, existing data or sending is used.
"""
from __future__ import annotations

import copy
import asyncio
import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS

from cryptography.fernet import Fernet

from app.core.agent_tools import AgentToolRegistry
from app.core.main_agent import AutoHuntAgent
from app.core.models import BrowserTaskStatus
from app.llm.base import LLMResult
from app.memory.resume_identity import resume_stamp
from app.runtime.context import current_run
from app.runtime.store import RunStore
from app.runtime.manager import AgentRuntime
from app.workflow.delivery_quantity import delivery_quantity, normalize_delivery_quantities
from app.workflow.planning import RequestDraft
from evaluation import delivery_limit_contract as fixtures


TRACES = []


class QuantitySyntax(unittest.TestCase):
    def test_literal_quantities_and_prefixes(self):
        for message, limit, prefix in [("投递20个", 20, False), ("投递前20个", 20, True),
                ("请投递二十份", 20, False), ("投递前二十个", 20, True),
                ("帮我找工作，投递20份，不要外包，深圳，月薪12k-15k", 20, False),
                ("最多投递20个", 20, False)]:
            with self.subTest(message=message):
                result = delivery_quantity(message)
                self.assertIsNotNone(result)
                self.assertEqual((result.limit, result.prefix), (limit, prefix))

    def test_ordinal_questions_quotes_and_cancellations_are_not_quantity_commands(self):
        for message in ["投递第20个", "为什么投递20个", "不要投递20个", "如果投递20个会怎样？",
                "他说‘投递20个’", "查看20个职位", "投递匹配分20以上的", "投递20.5个"]:
            with self.subTest(message=message):
                self.assertIsNone(delivery_quantity(message))

    def test_missing_or_wrong_model_count_is_corrected_without_adding_actions(self):
        draft = RequestDraft.model_validate({"steps": [{"id": "delivery", "tool": "mock_delivery_start",
            "arguments": {"limit": 100}, "user_requested": True}]})
        result = normalize_delivery_quantities(draft, "投递前20个")
        self.assertEqual(result.steps[0].arguments, {"limit": 20, "selector": "前20个"})
        self.assertEqual(draft.steps[0].arguments, {"limit": 100})
        read = RequestDraft.model_validate({"steps": [{"id": "read", "tool": "job_stats_read"}]})
        self.assertEqual(normalize_delivery_quantities(read, "投递20个").model_dump(), read.model_dump())

    def test_revision_additions_and_updates_use_same_count_contract(self):
        for revision in [{"add_steps": [{"id": "d", "tool": "mock_delivery_start", "arguments": {}, "user_requested": True}]},
                         {"updates": [{"step_id": "d", "arguments": {}, "user_requested": True}]}]:
            draft = RequestDraft.model_validate({"operation": "revise", "revision": revision})
            result = normalize_delivery_quantities(draft, "投递前20个",
                live_plan={"steps": [{"id": "d", "tool": "mock_delivery_start", "user_requested": True}]})
            entry = (result.revision.get("add_steps") or result.revision["updates"])[0]
            self.assertEqual(entry["arguments"], {"limit": 20, "selector": "前20个"})

    def test_mixed_batch_and_ordinal_never_share_another_clause_count(self):
        for message, quotes in [("投递前20个，并投递第30个", ["投递前20个", "投递第30个"]),
                                 ("投递第20个，再投递前5个", ["投递第20个", "投递前5个"])]:
            with self.subTest(message=message):
                steps = [{"id": str(i), "tool": "mock_delivery_start", "command_quote": quote,
                          "user_requested": True, "arguments": {"selector": quote.removeprefix("投递")}}
                         for i, quote in enumerate(quotes)]
                result = normalize_delivery_quantities(RequestDraft.model_validate({"steps": steps}), message)
                for step in result.steps:
                    self.assertEqual(step.arguments["selector"], step.command_quote.removeprefix("投递"))
                    self.assertEqual("limit" in step.arguments, "前" in step.command_quote)

    def test_multiple_quantities_require_per_branch_literal_evidence(self):
        message = "投递前20个，并投递前5个"
        for quoted in (True, False):
            steps = [{"id": str(i), "tool": "mock_delivery_start", "user_requested": True,
                      "command_quote": quote if quoted else "", "arguments": {}}
                     for i, quote in enumerate(["投递前20个", "投递前5个"])]
            result = normalize_delivery_quantities(RequestDraft.model_validate({"steps": steps}), message)
            self.assertEqual([s.arguments.get("limit") for s in result.steps], [20, 5] if quoted else [None, None])

    def test_whole_request_quote_cannot_turn_an_ordinal_into_a_batch(self):
        message = "投递前20个，并投递第30个"
        draft = RequestDraft.model_validate({"steps": [{"id": str(i), "tool": "mock_delivery_start",
            "user_requested": True, "command_quote": message, "arguments": {"selector": selector}}
            for i, selector in enumerate(["前20个", "第30个"])]})
        result = normalize_delivery_quantities(draft, message)
        self.assertEqual([s.arguments["selector"] for s in result.steps], ["前20个", "第30个"])
        self.assertEqual([s.arguments.get("limit") for s in result.steps], [None, None])

    def test_semantic_qualifiers_are_repaired_instead_of_silently_dropped(self):
        from app.workflow.validation import PlanValidationError
        for selector in ["前20个深圳岗位", "第20个", {"ordinal": 20}]:
            draft = RequestDraft.model_validate({"steps": [{"id": "d", "tool": "mock_delivery_start",
                "user_requested": True, "arguments": {"selector": selector}}]})
            with self.subTest(selector=selector), self.assertRaises(PlanValidationError) as error:
                normalize_delivery_quantities(draft, "投递前20个深圳岗位")
            self.assertEqual(error.exception.issue.category, "repair")
            self.assertEqual(draft.steps[0].arguments, {"selector": selector})

    def test_alternative_quantity_is_not_promoted_to_the_larger_batch(self):
        draft = RequestDraft.model_validate({"steps": [{"id": "d", "tool": "mock_delivery_start",
            "arguments": {"limit": 10}, "user_requested": True}]})
        self.assertIsNone(delivery_quantity("投递前20个或者10个"))
        self.assertEqual(normalize_delivery_quantities(draft, "投递前20个或者10个").steps[0].arguments, {"limit": 10})

    def test_single_quantity_split_into_multiple_steps_requires_internal_repair(self):
        from app.workflow.validation import PlanValidationError
        draft = RequestDraft.model_validate({"steps": [{"id": str(i), "tool": "mock_delivery_start",
            "command_quote": "投递20个", "arguments": {"limit": 20}, "user_requested": True} for i in range(2)]})
        with self.assertRaises(PlanValidationError) as error:
            normalize_delivery_quantities(draft, "投递20个（深圳或杭州）")
        self.assertEqual(error.exception.issue.category, "repair")
        self.assertEqual(error.exception.issue.code, "delivery_quantity_duplicated")


class DeliveryUserChain(unittest.IsolatedAsyncioTestCase):
    make_search = fixtures.DeliveryLimitContracts.make_search
    seed = fixtures.DeliveryLimitContracts.seed

    async def asyncSetUp(self):
        await fixtures.DeliveryLimitContracts.asyncSetUp(self)
        current_run.reset(self.token)
        self.store = RunStore(Path(self.temp.name) / "runtime.db", Fernet.generate_key().decode())
        self.registry = AgentToolRegistry(jobs=self.jobs, browser=self.coordinator.browser,
            candidate_state=self.candidate, resume_workspace=self.resume, memory=NS(), mock_workflow=self.coordinator)
        self.coordinator.remember_authorization_scope = lambda *args, **kwargs: None
        self.requests = []

        class Model:
            async def generate(inner, request):
                self.requests.append(request)
                payload = json.loads(request.user_message)
                if request.context == "条件语义核对，不产生执行授权":
                    output = {"scopes": [{"step_id": entry["step_id"], "status": "pass", "changes": {},
                                         "reason": "仅为合成测试的范围复核"} for entry in payload["scopes"]]}
                else:
                    # Deliberately recreate the live failure: model omits both
                    # quantity and prefix selector. The command contract fixes it.
                    output = {"steps": [{"id": "delivery", "tool": "mock_delivery_start", "arguments": {
                        "scope_mode": "new", "filters": {"op": "condition", "salary": {"unit": "monthly", "minimum": 12000}}},
                        "user_requested": True}], "goals": [{"quote": payload.get("message", "投递20个"),
                        "disposition": "steps", "step_ids": ["delivery"]}]}
                return LLMResult(json.dumps(output, ensure_ascii=False), "scripted", "bounded-delivery-chain")

        self.agent = AutoHuntAgent(NS(), NS(), self.candidate, NS(), Model(), self.resume, self.jobs, self.registry)
        self.runtime = AgentRuntime(self.store, self.agent, self.jobs, self.coordinator.browser, self.resume)
        self.runtime.search_state = lambda session: {"queries": ["合成算法岗位"], "source": "mock", "query_source": "session"}
        self.store.mutate_dialogue("default", {"op": "task", "kind": "browser_id", "id": "current", "status": "completed"})

    async def asyncTearDown(self):
        TRACES.append({"test": self.id(), "events": getattr(self, "last_events", []),
                       "queue_ids": [row["job_id"] for row in self.jobs.mock_delivery_queue(active_only=False)]})
        for task in self.runtime.tasks.values():
            if not task.done(): task.cancel()
        await asyncio.gather(*self.runtime.tasks.values(), return_exceptions=True)
        self.runtime = self.registry = self.agent = self.coordinator = self.jobs = self.store = None
        import gc
        gc.collect()
        self.temp.cleanup()

    def snapshot(self, ids, source="current"):
        items = []
        for ident in ids:
            row = self.jobs.get_job(ident)
            items.append({key: row.get(key) for key in ("id", "title", "company", "location", "salary", "score")})
        self.store.mutate_dialogue("default", {"op": "snapshot", "kind": "job", "topic": "applications",
            "source": "search-results", "version": source, "items": items, "displayed": True})

    async def execute(self, message):
        run, _ = self.store.create("default", "synthetic-command", "chat", {"message": message})
        await self.runtime.execute(run["id"])
        saved = self.store.get(run["id"])
        self.last_events = self.store.all_events(run["id"])
        return saved

    def assert_result(self, run, count, shortfall):
        self.assertEqual(run["status"], "completed", run.get("error"))
        steps = run["checkpoint"]["plan"]["steps"]
        node = next(step for step in steps if step["tool"] == "mock_delivery_start")
        self.assertEqual(node["arguments"]["limit"], 20)
        self.assertEqual(node["arguments"]["source_task_id"], "current")
        self.assertEqual(node["result"]["queued"], count)
        self.assertEqual(node["result"]["shortfall"], shortfall)
        self.assertEqual(len(self.jobs.mock_delivery_queue(active_only=False)), count)
        self.assertEqual(len(self.store.approvals(run["id"])), 1)
        self.assertEqual(self.store.approvals(run["id"])[0]["status"], "approved")
        history = self.jobs.conversation_history(conversation_id="default", limit=20)
        self.assertTrue(any(item["role"] == "assistant" for item in history))
        self.assertFalse(any("请补充filters" in item.get("content", "") for item in history))
        if shortfall:
            self.assertIn(f"尚差{shortfall}个", run["result"]["message"])
        self.assertEqual(set(node["result"]["selected_job_ids"]),
                         {row["job_id"] for row in self.jobs.mock_delivery_queue(active_only=False)})

    async def test_command_twenty_limits_real_queue_to_twenty(self):
        self.seed(25)
        run = await self.execute("投递20个")
        self.assert_result(run, 20, 0)

    async def test_model_split_of_single_quantity_is_repaired_before_any_enqueue(self):
        self.seed(25)
        original = self.agent.llm.generate
        attempts = []

        async def split_once(request):
            response = await original(request)
            if request.context != "条件语义核对，不产生执行授权":
                attempts.append(request)
                if len(attempts) == 1:
                    self.assertEqual(self.jobs.mock_delivery_queue(), [])
                    output = json.loads(response.text)
                    other = copy.deepcopy(output["steps"][0])
                    other["id"] = "duplicate-budget"
                    other["arguments"]["min_score"] = 85
                    output["steps"].append(other)
                    return LLMResult(json.dumps(output), "scripted", "duplicate-budget")
            return response

        self.agent.llm.generate = split_once
        run = await self.execute("投递20个")
        self.assert_result(run, 20, 0)
        self.assertEqual(len(attempts), 2)

    async def test_prefix_twenty_with_one_result_queues_one_and_reports_deficit(self):
        ids = self.seed(1)
        self.snapshot(ids)
        run = await self.execute("投递前20个")
        self.assert_result(run, 1, 19)

    async def test_empty_current_round_reports_deficit_without_sending_or_research(self):
        self.snapshot([])
        run = await self.execute("投递前20个")
        self.assert_result(run, 0, 20)
        self.coordinator._ensure_applicant_bound.assert_not_awaited()

    async def test_unknown_qualification_does_not_reduce_available_batch(self):
        self.seed(7, qualification="unknown")
        self.seed(2, qualification="pass", start=10)
        run = await self.execute("投递20个")
        self.assert_result(run, 9, 11)
        result = run["checkpoint"]["plan"]["steps"][0]["result"]
        self.assertEqual((result["excluded"], result["unknown"], result["insufficient"]), (0, 0, 0))
        self.assertEqual(len(result["qualification_advisories"]), 7)

    async def test_one_unknown_qualification_job_reaches_queue_and_chat_with_shortfall(self):
        ids = self.seed(1, qualification="unknown")
        self.snapshot(ids)
        run = await self.execute("投递前20个")
        self.assert_result(run, 1, 19)
        result = run["checkpoint"]["plan"]["steps"][0]["result"]
        self.assertEqual(result["qualification_advisories"], [{"job_id": ids[0], "status": "unknown"}])
        self.assertEqual(result["insufficient"], 0)
        history = self.jobs.conversation_history(conversation_id="default", limit=20)
        self.assertTrue(any("实际入队1个" in item["content"] and "尚差19个" in item["content"]
            for item in history if item["role"] == "assistant"))
        self.assertFalse(any("资历" in item["content"] and "未入队" in item["content"] for item in history))

    async def test_explicit_user_filter_still_excludes_jobs_with_advisory_qualification(self):
        self.seed(3, qualification="unknown", salary="8-10K")
        wanted = self.seed(1, qualification="fail", start=10)
        run = await self.execute("投递20个")
        self.assert_result(run, 1, 19)
        result = run["checkpoint"]["plan"]["steps"][0]["result"]
        self.assertEqual(result["excluded"], 3)
        self.assertEqual(result["job_ids"], wanted)

    async def test_new_round_explicit_delivery_does_not_reuse_old_salary_ceiling(self):
        self.make_search("old")
        self.seed(30, task_id="old")
        wanted = self.seed(1)
        self.snapshot(wanted)
        self.store.mutate_dialogue("default", {"op": "delivery_scope", "scope": {
            "id": "old-batch", "session_id": "default", "resume_stamp": resume_stamp(self.document, self.candidate),
            "task_id": "old", "min_score": 90, "filters": {"op": "condition", "salary": {
                "unit": "monthly", "minimum": 12000, "maximum": 15000}}}})
        run = await self.execute("投递前20个")
        self.assert_result(run, 1, 19)
        self.assertEqual(self.jobs.mock_delivery_queue()[0]["job_id"], wanted[0])

    async def test_append_while_searching_waits_for_same_round_then_limits_queue(self):
        ids = self.seed(25)
        self.jobs.update_task("current", status=BrowserTaskStatus.RUNNING)
        stamp = resume_stamp(self.document, self.candidate)
        original, _ = self.store.create("default", "synthetic-original", "chat", {"message": "找合成岗位"})
        plan = {"id": "synthetic-search-plan", "session_id": "default", "revision": 0,
            "original": "找合成岗位", "answers": [], "questions": [], "goals": [], "reply": "",
            "resume_stamp": stamp, "turn": 0, "expires_at": __import__("time").time() + 1800,
            "status": "running", "steps": [
                {"id": "search", "tool": "job_search_start", "arguments": {"queries": ["合成算法岗位"]},
                 "status": "waiting_external", "effect": "external", "task_id": "current", "depends_on": [],
                 "command_quote": "找合成岗位", "user_requested": True, "missing_slots": [], "questions": []},
                {"id": "summary", "tool": "job_results_read", "arguments": {"summary": True},
                 "status": "pending", "effect": "read", "depends_on": ["search"], "evidence_step": "search",
                 "command_quote": "读取结果", "user_requested": True, "missing_slots": [], "questions": []}]}
        for node in plan["steps"]:
            node.update(scope_quote="", slot_evidence={}, result=None)
        self.store.update(original["id"], status="running", checkpoint={"phase": "action_plan", "browser_id": "current", "plan": plan})
        self.store.mutate_dialogue("default", {"op": "task", "kind": "plan_id", "id": plan["id"], "status": "running", "bind": True})

        class AppendingModel:
            async def generate(inner, request):
                payload = json.loads(request.user_message)
                if request.context == "条件语义核对，不产生执行授权":
                    output = {"scopes": [{"step_id": s["step_id"], "status": "pass", "reason": "合成复核", "changes": {}}
                                         for s in payload["scopes"]]}
                else:
                    output = {"operation": "revise", "revision": {"add_steps": [
                        {"id": "delivery", "tool": "mock_delivery_start", "arguments": {},
                         "user_requested": True, "depends_on": ["summary"]}]}}
                return LLMResult(json.dumps(output), "scripted", "append-chain")
        self.agent.llm = AppendingModel()
        self.runtime.observe_browser = lambda ident: self.jobs.get_task(ident)
        self.runtime.schedule(original["id"], watch_only=True)
        command = await self.execute("投递前20个")
        self.assertEqual(command["status"], "completed", command.get("error"))
        self.assertEqual(self.jobs.mock_delivery_queue(), [])
        self.make_search("other")
        other_ids = self.seed(30, task_id="other")
        self.snapshot(other_ids, source="other")
        self.jobs.update_task("current", status=BrowserTaskStatus.COMPLETED)
        self.jobs.reconcile_search_counts("current")
        for _ in range(80):
            saved = self.store.get(original["id"])
            if saved["status"] in {"completed", "failed", "waiting_confirmation", "waiting_user"}:
                break
            await asyncio.sleep(.1)
        self.assertEqual(saved["status"], "completed", saved.get("error"))
        selected = [row["job_id"] for row in self.jobs.mock_delivery_queue()]
        self.assertEqual(len(selected), 20)
        self.assertTrue(set(selected) <= set(ids))
        self.assertTrue(set(selected).isdisjoint(other_ids))
        self.assertEqual(saved["checkpoint"]["plan"]["steps"][-1]["arguments"]["limit"], 20)
        self.assertTrue(any("20" in item.get("content", "") and "队列" in item.get("content", "")
            for item in self.jobs.conversation_history(conversation_id="default", limit=30)))


def main():
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(
        __import__(__name__, fromlist=["*"])))
    output = Path("evaluation/results/delivery-request") / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    summary = {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
               "external_model_calls": 0, "browser_actions": 0, "real_recruitment_actions": 0}
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (output / "traces.jsonl").write_text("\n".join(json.dumps(t, ensure_ascii=False, default=str) for t in TRACES), encoding="utf-8")
    print(json.dumps({"output": str(output), **summary}))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__": raise SystemExit(main())
