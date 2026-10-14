"""Context/ledger contracts, not a live model accuracy or browser success score.

python -m evaluation.context_ledger_contract
Only generated requests, isolated databases and scripted providers. The runner
exports the exercised cases and event traces to ignored evaluation/results/.
"""
from __future__ import annotations

import ast
import asyncio
import copy
import gc
import io
import itertools
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from cryptography.fernet import Fernet

from app.llm.base import LLMRequest, LLMResult, LLMToolResult
from app.runtime.context import RunContext, active_run, current_run, diagnostic_scope, emit, emit_diagnostic
from app.runtime.context_builder import ContextBuilder, ContextBudgetExceeded, ContextLimits, complete_protocol, estimate_tokens
from app.runtime.ledger import execution_ledger
from app.runtime.memory_context import compile_context
from app.runtime.store import RunStore
from evaluation.model_trace_contract import Scripted, manager

TRACES, CASES = [], []


def history(count, size=30):
    return [item for n in range(count) for item in (
        {"role": "user", "content": f"虚构请求{n}"}, {"role": "assistant", "content": "历史回复" * size})]


def protocol():
    return [{"role": "user", "content": "只查看第8个，不投递"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "123456789012345", "type": "function", "function": {"name": "job_detail_read", "arguments": '{"selector":"第8个"}'}},
            {"id": "two", "type": "function", "function": {"name": "context_state_read", "arguments": '{}'}}]},
        {"role": "tool", "tool_call_id": "123456789012345", "content": '{"id":"opaque-hash","salary_min":15000}'},
        {"role": "tool", "tool_call_id": "two", "content": '{"condition":"实习日薪>=300"}'}]


class ContextContracts(unittest.TestCase):
    def test_configuration_rejects_invalid_limits(self):
        for kwargs in ({"history_tokens": 0}, {"output_tokens": -1}, {"input_tokens": True},
                       {"window_tokens": 1024, "output_tokens": 1024}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError): ContextLimits(**kwargs)
        with patch.dict("os.environ", {"AUTOHUNT_CONTEXT_HISTORY_TURNS": "3"}):
            self.assertEqual(ContextLimits.configured().history_turns, 3)

    def test_estimator_labels_cjk_conservatively(self):
        self.assertEqual(estimate_tokens("abc"), 1)
        self.assertEqual(estimate_tokens("中文"), 4)
        self.assertGreater(estimate_tokens("工作" * 100), estimate_tokens("a" * 200))

    def test_long_assistant_report_keeps_original_user_turn(self):
        original = [{"role": "user", "content": "正式15k或面议，实习日薪300；85以上投递"},
                    {"role": "assistant", "content": "很长的岗位报告" * 3000}]
        fitted, stats = ContextBuilder().history(original, token_budget=200)
        self.assertEqual(fitted[0], original[0])
        self.assertEqual(stats["assistant_reports_omitted"], 1)
        self.assertIn("context_history_read", fitted[1]["content"])
        self.assertEqual(len(original[1]["content"]), 21000)

    def test_recent_turns_are_atomic_and_newer_than_distant(self):
        fitted, stats = ContextBuilder().history(history(150), turn_limit=3)
        self.assertEqual(len(fitted), 6)
        self.assertIn("147", fitted[0]["content"])
        self.assertIn("149", fitted[-2]["content"])
        self.assertEqual(stats["omitted_turns"], 147)

    def test_db_history_metadata_is_not_sent_as_invalid_provider_fields(self):
        raw = [{"id": 8, "created_at": "2026-10-14T00:00:00+00:00", "run_id": "raw-run",
                "role": "user", "content": "只修改条件"}]
        fitted, _ = ContextBuilder().history(raw)
        self.assertEqual(fitted, [{"role": "user", "content": "只修改条件"}])
        self.assertEqual(raw[0]["id"], 8)

    def test_optional_omission_notice_cannot_make_valid_required_state_overflow(self):
        required = {"original": "完整原任务" * 100}
        budget = estimate_tokens(required)
        builder = ContextBuilder(replace(ContextLimits(), session_tokens=budget))
        fitted, stats = builder.session(required, {"recent_turns": history(100)})
        self.assertEqual(fitted["original"], required["original"])
        self.assertLessEqual(estimate_tokens(fitted), budget)
        self.assertGreater(len(stats["omitted_sections"]), 0)

    def test_model_fits_old_turns_without_mutating_tool_evidence(self):
        messages = [*history(40), *protocol()]
        saved = copy.deepcopy(messages)
        builder = ContextBuilder(replace(ContextLimits(), input_tokens=1000))
        fitted, stats = builder.model_input("系统说明", messages, [{"name": "tool"}])
        self.assertEqual(fitted[-4:], protocol())
        self.assertTrue(complete_protocol(fitted))
        self.assertGreater(stats["omitted_turns"], 0)
        self.assertLessEqual(stats["estimated_input_tokens"], 1000)
        self.assertEqual(messages, saved)

    def test_tool_catalog_and_output_reservation_count_against_window(self):
        builder = ContextBuilder(replace(ContextLimits(), window_tokens=1500, input_tokens=1200,
                                         output_tokens=200, reserve_tokens=100))
        with self.assertRaises(ContextBudgetExceeded) as caught:
            builder.model_input("", [{"role": "user", "content": "当前请求"}], [{"schema": "中文" * 1000}])
        self.assertEqual(caught.exception.stats["reserved_output_tokens"], 200)
        self.assertTrue(caught.exception.stats["current_request_preserved"])

    def test_current_tool_result_is_not_silently_truncated(self):
        messages = protocol()
        messages[-1]["content"] = "很大原始工具证据" * 1000
        with self.assertRaises(ContextBudgetExceeded):
            ContextBuilder(replace(ContextLimits(), input_tokens=600)).model_input("", messages)
        self.assertEqual(messages[-1]["content"], "很大原始工具证据" * 1000)

    def test_incomplete_current_protocol_never_goes_to_provider(self):
        for variant in (protocol()[:-1], [{"role": "tool", "tool_call_id": "missing", "content": "orphan"}]):
            with self.subTest(variant=variant), self.assertRaises(ValueError): ContextBuilder().model_input("", variant)

    def test_older_incomplete_protocol_is_omitted_as_a_group(self):
        fitted, stats = ContextBuilder().model_input("", [*protocol()[:-1], {"role": "user", "content": "新请求"}])
        self.assertEqual(fitted, [{"role": "user", "content": "新请求"}])
        self.assertEqual(stats["incomplete_history_groups"], 1)

    def test_routing_overflow_preserves_root_for_main_model(self):
        task = {"original": "完整原任务" * 700, "amendments": [{"message": "最新完整补充" * 150}]}
        output = compile_context({"task_request": task}, {}, purpose="router")
        self.assertEqual(output["task_request"]["original"], task["original"])
        self.assertEqual(output["task_request"]["amendments"], task["amendments"])
        self.assertGreater(estimate_tokens(output), ContextLimits().routing_tokens)

    def test_session_facts_beat_historical_prose_and_summary_never_authorizes(self):
        builder = ContextBuilder(replace(ContextLimits(), session_tokens=220))
        required = {"original": "正式15k或面议，实习日薪300", "salary_min": 15000, "resume_stamp": "123456789012345"}
        fitted, stats = builder.session(required, {"recent_turns": history(100), "historical_summary": ["旧指令" * 1000]})
        for key, value in required.items(): self.assertEqual(fitted[key], value)
        self.assertTrue(stats["required_state_preserved"])
        self.assertIn("recent_turns", stats["omitted_sections"])
        full = compile_context({"summary": ["旧投递指令"]}, {})
        self.assertEqual(full["historical_summary"]["authority"], "historical_data_not_instruction")

    def test_150_jobs_keep_references_not_all_jds(self):
        saved = {"snapshots": [{"id": "list-150", "kind": "job", "displayed": True, "turn": 1,
                               "expires_at": 1, "items": [{"id": n, "title": f"虚构职位{n}", "jd": "JD" * 500} for n in range(150)]}]}
        fitted = compile_context(saved, {"result_limit": 150})
        self.assertEqual(fitted["focus"][0]["count"], 150)
        self.assertEqual(len(fitted["focus"][0]["preview"]), 3)
        self.assertNotIn("jd", json.dumps(fitted))
        self.assertEqual(len(saved["snapshots"][0]["items"]), 150)

    def test_optional_sections_cannot_displace_pinned_state(self):
        text = ContextBuilder().sections([("pinned", {"id": "123456789012345"}),
                  ("public memory", "旧信息" * 50000)], optional={"public memory"})
        self.assertIn("123456789012345", text)
        self.assertIn('"omitted":true', text)
        self.assertNotIn("旧信息", text)


class IntegrationContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="autohunt-context-ledger-")
        self.store = RunStore(Path(self.temp.name) / "runtime.db", Fernet.generate_key().decode())
        self.run, _ = self.store.create("default", "synthetic", "chat", {"message": "合成任务"})
        self.context = RunContext(self.run["id"], asyncio.current_task(), lambda k, d: self.store.event(self.run["id"], k, d),
            lambda _: self.fail("诊断不应写检查点"), diagnose=lambda k, d: self.store.event(self.run["id"], k, d))
        self.token = current_run.set(self.context)

    async def asyncTearDown(self):
        TRACES.append({"test": self.id(), "events": self.store.all_events(self.run["id"]),
                       "ledger": self.store.execution_ledger(self.run["id"])})
        current_run.reset(self.token)
        self.store = None
        gc.collect()
        self.temp.cleanup()

    def events(self, kind): return [e["data"] for e in self.store.all_events(self.run["id"]) if e["kind"] == kind]

    async def test_child_model_traces_frozen_scope_without_execution_authority(self):
        self.context.owner = asyncio.current_task()
        client = manager(Scripted([LLMResult("结果", "scripted", "model", usage={"prompt_tokens": 7, "completion_tokens": 3})] * 2))
        scope = diagnostic_scope.set({"plan_id": "old", "revision": 4, "step_id": "score", "execution_id": "attempt"})
        gate = asyncio.Event()
        async def child():
            await gate.wait()
            self.assertIsNone(active_run())
            emit("plan_state", plan={"id": "forbidden"})
            await client.generate(LLMRequest("合成", "评分", ""))
        task = asyncio.create_task(child())
        diagnostic_scope.reset(scope)
        self.context.action_authority = {"plan_id": "new", "step_id": "other"}
        gate.set()
        await task
        request = self.events("model_request")[0]
        self.assertEqual((request["plan_id"], request["revision"], request["execution_id"]), ("old", 4, "attempt"))
        self.assertEqual(self.events("model_response")[0]["plan_id"], "old")
        self.assertFalse(self.events("plan_state"))
        self.assertEqual(self.context.action_authority["plan_id"], "new")

    async def test_manager_blocks_only_oversized_transport_without_touching_original(self):
        provider = Scripted([LLMResult("unused", "scripted", "model")])
        llm = manager(provider)
        llm.context_builder = ContextBuilder(replace(ContextLimits(), input_tokens=150))
        request = LLMRequest("完整系统", "当前完整请求" * 100, "")
        with self.assertRaises(ContextBudgetExceeded): await llm.generate(request)
        self.assertEqual(provider.requests, [])
        self.assertEqual(request.user_message, "当前完整请求" * 100)
        self.assertEqual(self.events("context_budget")[0]["status"], "exceeded")

    async def test_native_transport_keeps_ids_and_paired_results(self):
        provider = Scripted([LLMToolResult("总结", "scripted", "model", [], {"role": "assistant", "content": "总结"})])
        original = [*history(50), *protocol()]
        llm = manager(provider)
        llm.context_builder = ContextBuilder(replace(ContextLimits(), input_tokens=900))
        await llm.generate_with_tools(instructions="合成说明", messages=original, tools=[])
        sent = provider.requests[0]["messages"]
        self.assertTrue(complete_protocol(sent))
        from app.core.security import redact
        self.assertEqual(sent[-4:], redact(protocol()))
        self.assertEqual(len(original), 104)
        self.assertGreater(self.events("context_budget")[0]["omitted_turns"], 0)

    async def test_plain_output_budget_is_actually_sent_to_provider(self):
        provider = Scripted([LLMResult("结果", "scripted", "model")])
        await manager(provider).generate(LLMRequest("合成", "合成", ""))
        self.assertEqual(provider.requests[0]["max_output_tokens"], ContextLimits().output_tokens)

    async def test_invalid_output_limit_is_not_silently_replaced_by_default(self):
        provider = Scripted([])
        with self.assertRaises(ValueError):
            await manager(provider).generate(LLMRequest("合成", "合成", "", max_output_tokens=0))
        self.assertFalse(provider.requests)

    async def test_deepseek_stream_and_native_output_caps_are_enforced_on_wire(self):
        from app.llm.deepseek_provider import DeepSeekProvider
        self.context.owner = asyncio.current_task()
        provider = DeepSeekProvider(api_key="synthetic-not-a-key", model="synthetic")
        captured = []
        class Stream:
            def __aiter__(self): return self
            async def __anext__(self): raise StopAsyncIteration
            async def close(self): pass
        async def completion(**kwargs):
            captured.append(kwargs)
            return Stream() if kwargs.get("stream") else NS(id="native", model="synthetic", usage=None,
                choices=[NS(message=NS(content="合成回复", tool_calls=[]))])
        provider._completion = completion
        llm = manager(provider)
        llm.context_builder = ContextBuilder(replace(ContextLimits(), output_tokens=345))
        try:
            await llm.generate_with_tools(instructions="合成", messages=[{"role": "user", "content": "合成请求"}], tools=[])
            self.context.owner = None  # An inherited scoring task uses non-streaming native transport.
            await llm.generate_with_tools(instructions="合成", messages=[{"role": "user", "content": "合成请求"}], tools=[])
            self.assertEqual([request["max_tokens"] for request in captured], [345, 345])
            self.assertTrue(captured[0]["stream_options"]["include_usage"])
        finally: await provider.client.close()

    async def test_unknown_usage_and_cost_not_zero_and_public_ledger_no_text(self):
        await manager(Scripted([LLMResult("内部虚构结果", "scripted", "model")])).generate(LLMRequest("内部虚构系统", "内部虚构请求", ""))
        ledger = self.store.execution_ledger(self.run["id"])
        self.assertIsNone(ledger["calls"][0]["prompt_tokens"])
        self.assertIsNone(ledger["accounting"]["total_cost"])
        self.assertFalse(ledger["accounting"]["tokens_complete"])
        self.assertNotIn("内部虚构", json.dumps(ledger, ensure_ascii=False))

    async def test_terminal_run_retains_late_child_diagnostics(self):
        self.store.update(self.run["id"], status="cancelled")
        emit_diagnostic("model_response", call_id="late", status="cancelled", provider="scripted", model="model")
        self.assertEqual(self.store.execution_ledger(self.run["id"])["calls"][0]["status"], "cancelled")

    async def test_only_passive_event_kinds_permitted(self):
        with self.assertRaises(ValueError): emit_diagnostic("effect_phase", phase="started")
        self.assertFalse(self.events("effect_phase"))

    async def test_small_classifier_transport_is_traced_without_api_key(self):
        from app.intent.small_model import SmallModelClient
        response = NS(id="small-response", model="small-actual", usage=NS(model_dump=lambda **_: {"prompt_tokens": 100, "completion_tokens": 2}),
                      choices=[NS(message=NS(content="search_jobs"), logprobs=None)])
        async def create(**kwargs): return response
        small = SmallModelClient(model="small-alias", client=NS(chat=NS(completions=NS(create=create))))
        await small.classify("帮我找工作", context="虚构状态")
        self.assertEqual(self.events("model_request")[0]["provider"], "small-model")
        self.assertEqual(self.events("model_response")[0]["usage"]["prompt_tokens"], 100)
        self.assertEqual(self.store.execution_ledger(self.run["id"])["calls"][0]["model"], "small-actual")

    async def test_ledger_parent_chain_read_only_and_session_bound(self):
        child, _ = self.store.create("default", "continue", "chat", {}, parent_id=self.run["id"])
        self.store.event(self.run["id"], "model_response", {"call_id": "parent-call", "status": "error"})
        before = self.store.get(self.run["id"])
        ledger = self.store.execution_ledger(child["id"], include_parents=True)
        self.assertEqual(ledger["covered_run_ids"], [child["id"], self.run["id"]])
        self.assertEqual(len(ledger["calls"]), 1)
        self.assertEqual(self.store.get(self.run["id"]), before)
        foreign_session = self.store.create_session("合成其他会话")
        foreign, _ = self.store.create(foreign_session["id"], "other", "chat", {})
        with self.store.connect() as db:
            db.execute("UPDATE agent_runs SET parent_id=? WHERE id=?", (foreign["id"], child["id"]))
        with self.assertRaises(ValueError): self.store.execution_ledger(child["id"], include_parents=True)

    async def test_ledger_api_success_and_not_found_without_runtime_startup(self):
        from fastapi import FastAPI, HTTPException
        import httpx
        path = Path(__file__).parents[1] / "app" / "api" / "application.py"
        node = next(n for n in ast.parse(path.read_text(encoding="utf-8")).body if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_ledger")
        app = FastAPI()
        namespace = {"app": app, "run_store": self.store, "HTTPException": HTTPException, "__package__": "app.api"}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), str(path), "exec"), namespace)
        before = self.store.get(self.run["id"])
        self.store.event(self.run["id"], "model_response", {"call_id": "123456789012345", "status": "ok",
            "model": "Authorization: Bearer synthetic-auth-token", "usage": {"prompt_tokens": 15000, "completion_tokens": 3}})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"/api/v1/runs/{self.run['id']}/ledger")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["run_id"], self.run["id"])
            self.assertNotIn("synthetic-auth-token", response.text)
            self.assertEqual(response.json()["calls"][0]["call_id"], "123456789012345")
            self.assertEqual(response.json()["calls"][0]["prompt_tokens"], 15000)
            self.assertEqual((await client.get("/api/v1/runs/missing/ledger")).status_code, 404)
        self.assertEqual(self.store.get(self.run["id"]), before)

    async def test_real_executor_binds_worker_call_to_claim_and_revision(self):
        from evaluation.stateful_scenarios import Harness, step
        harness = Harness(Path(self.temp.name) / "executor")
        harness.install([step("read", "job_stats_read")], revision=3)
        async def behavior(_):
            client = manager(Scripted([LLMResult("统计", "scripted", "model")]))
            await asyncio.create_task(client.generate(LLMRequest("合成", "合成", "")))
            return {"message": "已读取统计"}
        harness.behavior["job_stats_read"] = behavior
        try:
            await harness.tick()
            ledger = harness.store.execution_ledger(harness.run["id"])
            self.assertEqual(ledger["calls"][0]["step_id"], "read")
            self.assertEqual(ledger["calls"][0]["revision"], 3)
            self.assertEqual(ledger["calls"][0]["execution_id"], harness.plan["steps"][0]["execution_id"])
            self.assertIsNone(diagnostic_scope.get())
            self.assertEqual(harness.plan["steps"][0]["status"], "completed")
            TRACES.append({"test": self.id(), "executor_events": harness.store.all_events(harness.run["id"]), "ledger": ledger})
        finally:
            await harness.close()

    async def test_parallel_calls_and_retry_scope_accounted_without_extra_replay(self):
        from app.runtime.context import diagnostic_metadata
        class RetryProvider:
            name, model = "scripted", "model"
            async def generate(self, request):
                emit_diagnostic("model_retry", attempt=1)
                self.scope = diagnostic_metadata()
                return LLMResult("合成", self.name, self.model, usage={"prompt_tokens": 1, "completion_tokens": 1})
        provider = RetryProvider()
        returned = await manager(provider).generate(LLMRequest("合成", "合成", ""))
        self.assertEqual(self.events("model_retry")[0]["call_id"], returned.call_id)
        ledger = self.store.execution_ledger(self.run["id"])
        self.assertEqual(ledger["accounting"]["model_retries"], 1)
        self.assertFalse(ledger["accounting"]["tokens_complete"])
        self.assertEqual(ledger["accounting"]["recorded_model_calls"], 1)


class LedgerContracts(unittest.TestCase):
    def event(self, kind, data, seq=1): return {"run_id": "run", "kind": kind, "data": data, "seq": seq}
    def ledger(self, events, **kwargs): return execution_ledger([{"id": "run", "status": "waiting_reply"}], events, **kwargs)

    def test_zero_usage_is_known_but_absent_usage_is_unknown(self):
        events = [self.event("model_response", {"call_id": "zero", "status": "ok", "usage": {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0, "currency": "USD"}})]
        ledger = self.ledger(events)
        self.assertTrue(ledger["accounting"]["tokens_complete"])
        self.assertEqual(ledger["accounting"]["total_cost"], 0)

    def test_duplicate_call_events_do_not_double_count(self):
        event = self.event("model_response", {"call_id": "same", "status": "ok", "usage": {"input_tokens": 100, "output_tokens": 50}})
        ledger = self.ledger([event, copy.deepcopy(event)])
        self.assertEqual(ledger["accounting"]["recorded_model_calls"], 1)
        self.assertEqual(ledger["accounting"]["known_prompt_tokens"], 100)

    def test_cached_tokens_require_explicit_cache_price(self):
        event = self.event("model_response", {"call_id": "one", "provider": "fake", "model": "m", "status": "ok",
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "prompt_cache_hit_tokens": 20}})
        rates = {"fake/m": {"input_per_million": 2, "output_per_million": 4, "currency": "USD"}}
        self.assertIsNone(self.ledger([event], model_rates=rates)["accounting"]["total_cost"])
        rates["fake/m"]["cached_input_per_million"] = 1
        self.assertAlmostEqual(self.ledger([event], model_rates=rates)["accounting"]["total_cost"], 0.00038)

    def test_incomplete_error_and_cancelled_calls_keep_distinct_statuses(self):
        events = [self.event("model_request", {"call_id": "incomplete"}),
                  self.event("model_response", {"call_id": "error", "status": "error"}, 2),
                  self.event("model_response", {"call_id": "cancelled", "status": "cancelled"}, 3)]
        ledger = self.ledger(events)
        self.assertEqual(ledger["accounting"]["model_statuses"], {"incomplete": 1, "error": 1, "cancelled": 1})
        self.assertIsNone(ledger["accounting"]["total_cost"])

    def test_tool_queue_and_external_wait_not_reported_as_completed_delivery(self):
        ledger = self.ledger([self.event("tool_started", {"tool": "mock_delivery_start", "execution_id": "one"}),
            self.event("tool_finished", {"tool": "mock_delivery_start", "execution_id": "one", "status": "ok", "result": {"queued": 5}}, 2),
            self.event("delivery", {"task": {"id": "delivery", "status": "waiting_for_reply", "submitted": False}}, 3)])
        self.assertEqual(len(ledger["tools"]), 1)
        self.assertEqual(ledger["status"], "waiting_reply")
        self.assertFalse(ledger["external_tasks"][0]["submitted"])

    def test_parallel_model_time_not_misreported_as_wall_time(self):
        ledger = self.ledger([self.event("model_response", {"call_id": str(n), "status": "ok", "latency_ms": 1000}, n) for n in range(2)])
        self.assertEqual(ledger["accounting"]["model_time_sum_ms"], 2000)
        self.assertIsNone(ledger["accounting"]["persisted_run_span_ms"])

    def test_invalid_numeric_usage_not_accepted_as_cost_or_tokens(self):
        for value in (True, -10, float("nan"), "100"):
            ledger = self.ledger([self.event("model_response", {"call_id": "one", "status": "ok", "usage": {"prompt_tokens": value, "completion_tokens": value, "cost": value}})])
            self.assertIsNone(ledger["calls"][0]["prompt_tokens"])
            self.assertIsNone(ledger["accounting"]["total_cost"])


def generated_case(case):
    def test(self):
        language, count, salary, threshold, age = case
        original = f"虚构需求 {language}: 找{count}个；月薪{salary}或面议；实习日薪300；{threshold}分以上投递，其余分析"
        conditions = {"result_limit": count, "salary_min": salary, "min_score": threshold,
            "filters": {"op": "any", "children": [{"kind": "formal", "monthly_min": salary}, {"kind": "intern", "daily_min": 300}]}}
        saved = {"task_request": {"id": "123456789012345", "original": original, "conditions": conditions,
            "amendments": [{"message": "不改其他条件，只继续分析"}], "resume_stamp": "hash-123456789012345"}}
        source = history(age, size=100 if language == "CJK" else 10)
        before = copy.deepcopy(saved)
        output = compile_context(saved, conditions, source)
        self.assertEqual(output["task_request"]["original"], original)
        self.assertEqual(output["search_conditions"], conditions)
        self.assertEqual(output["task_request"]["conditions"], conditions)
        self.assertEqual(saved, before)
        fitted, stats = ContextBuilder().model_input("虚构协议", [{"role": "user", "content": json.dumps(output, ensure_ascii=False)}])
        self.assertEqual(json.loads(fitted[-1]["content"])["search_conditions"], conditions)
        self.assertLessEqual(stats["estimated_input_tokens"], stats["input_token_limit"])
    return test


for index, case in enumerate(itertools.product(("CJK", "ASCII"), (15, 150), (13000, 15000, 25000), (80, 85), (8, 40, 150))):
    CASES.append({"case": index, "language": case[0], "count": case[1], "salary": case[2], "threshold": case[3], "history_turns": case[4]})
    setattr(ContextContracts, f"test_state_matrix_{index:03d}", generated_case(case))


def main():
    stream = io.StringIO()
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(cls) for cls in (ContextContracts, IntegrationContracts, LedgerContracts))
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    output = Path(__file__).parent / "results" / "context-ledger" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    summary = {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors), "external_calls": 0,
        "browser_actions": 0, "state_matrix_cases": len(CASES), "scope": "deterministic context/ledger contracts, not model semantic accuracy"}
    (output / "tests.log").write_text(stream.getvalue(), encoding="utf-8")
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (output / "cases.json").write_text(json.dumps(CASES, indent=2), encoding="utf-8")
    (output / "traces.jsonl").write_text("".join(json.dumps(t, ensure_ascii=False, default=str) + "\n" for t in TRACES), encoding="utf-8")
    if not result.wasSuccessful(): print(stream.getvalue()[-14000:])
    print(json.dumps({"output": str(output), **summary}))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__": raise SystemExit(main())
