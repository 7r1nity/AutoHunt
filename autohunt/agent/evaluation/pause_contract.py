"""New-task pause: real HTTP handler/router/runtime, isolated synthetic adapters.

The route is loaded from its AST, as in context_ledger_contract, to avoid importing
application globals that open the user's databases. Middleware/startup/browser UI
are outside this contract. Scripted model responses do not measure NLP accuracy.
"""
from __future__ import annotations

import ast
import asyncio
import copy
import gc
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace as NS

import httpx
from fastapi import FastAPI, HTTPException

from app.core.main_agent import AutoHuntAgent
from app.core.models import BrowserTaskView, BrowserTaskStatus, ChatRequest, ChatResponse
from app.core.task_semantics import understand
from app.intent.router import IntentRouter
from app.intent.rules import RuleEngine
from app.intent.store import RouterStore
from app.llm.base import LLMResult
from app.runtime.store import RunStore
from app.workflow.execution import advance, begin
from app.workflow.planning import RequestDraft, validate_draft
from evaluation.stateful_scenarios import Harness, STAMP

MESSAGE = "在模拟站找合肥Python后端岗位，月薪至少14000元，数量7个，先暂停不要执行，也不要投递。"
CONTINUE = "现在继续执行刚才的搜索，所有条件不变。"
TRACES = []


def draft(**extra):
    return {"steps": [{"id": "search", "tool": "job_search_start", "arguments": {
        "queries": ["Python后端工程师"], "locations": ["合肥"], "salary_min": 14000,
        "result_limit": 7, "source": "mock"}, "user_requested": True,
        "command_quote": "在模拟站找合肥Python后端岗位", "slot_evidence": {
            "salary_min": "月薪至少14000元", "result_limit": "数量7个"}}],
        "reply": "已经开始搜索。", **extra}


class Scripted:
    def __init__(self, value=None):
        self.value = value if value is not None else draft()
        self.calls = 0

    async def generate(self, request):
        self.calls += 1
        payload = json.loads(request.user_message)
        value = ({"scopes": [{"step_id": s["step_id"], "status": "pass", "reason": "synthetic"}
                              for s in payload["scopes"]]} if "scopes" in payload else self.value)
        return LLMResult(text=json.dumps(value, ensure_ascii=False), provider="synthetic", model="scripted")


class ChatHarness(Harness):
    def __init__(self, directory, model):
        super().__init__(directory)
        # The base harness's seed run is not an active chat request.
        self.store.update(self.run["id"], status="completed")
        self.jobs.conversation_history = lambda **_: []
        self.browser.current = lambda: BrowserTaskView(id="synthetic-search", status=BrowserTaskStatus.RUNNING)
        self.browser.can_start_search = lambda: True
        router = IntentRouter(rule_engine=RuleEngine(), small_model=NS(available=False),
                              store=RouterStore(Path(directory) / "router.db"))
        self.agent = AutoHuntAgent(memory=None, skills=None, candidate_state=self.candidate,
            evidence_extractor=None, llm=model, resume_workspace=self.resumes,
            job_store=self.jobs, tool_registry=self.registry, router=router)
        self.runtime.agent = self.agent
        self.runtime.observe_browser = lambda _: self.browser.current()
        self.app = FastAPI()
        source = Path(__file__).parents[1] / "app/api/application.py"
        node = next(n for n in ast.parse(source.read_text(encoding="utf-8")).body
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == "chat")
        namespace = {"app": self.app, "agent_runtime": self.runtime, "ChatRequest": ChatRequest,
                     "ChatResponse": ChatResponse, "HTTPException": HTTPException, "uuid": uuid}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), str(source), "exec"), namespace)

    async def chat(self, message, request_id=None):
        request_id = request_id or uuid.uuid4().hex
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://synthetic") as client:
            response = await client.post("/api/v1/chat", json={"message": message, "request_id": request_id})
        if response.status_code != 200:
            raise AssertionError(f"chat {response.status_code}: {response.text}; runs={self.store.list()}")
        run = next(r for r in self.store.list() if r["request_key"] == request_id)
        return run, response.json()

    async def wait_for(self, predicate, timeout=5):
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(.02)


class PauseContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="autohunt-pause-")
        self.model = Scripted()
        self.h = ChatHarness(self.temp.name, self.model)

    async def asyncTearDown(self):
        TRACES.append({"test": self.id(), "calls": self.h.calls, "runs": self.h.store.list()})
        await self.h.close()
        self.h = None
        gc.collect()
        self.temp.cleanup()

    def plan(self, message=MESSAGE, **extra):
        with self.h.context(message):
            return validate_draft(RequestDraft.model_validate(draft(**extra)), self.h.registry,
                                  message, dialogue={}, conditions={}, stamp=STAMP)

    async def test_explicit_pause_vetoes_false_or_omitted_model_field(self):
        for extra in ({}, {"pause": False}, {"pause": True}):
            plan = self.plan(**extra)
            self.assertTrue(plan["paused"])
            self.assertEqual(plan["status"], "paused")
            with self.h.context(MESSAGE):
                response = await begin(plan, self.h.registry)
                await advance(plan, self.h.registry)
            self.assertIn("没有执行", response.message)
            self.assertNotIn("已经开始", response.message)
            self.assertEqual(self.h.calls, [])
            self.assertEqual(self.h.store.approvals(self.h.run["id"]), [])

    async def test_semantic_pause_field_blocks_paraphrase(self):
        message = MESSAGE.replace("先暂停不要执行", "先保存方案，等我确认后再动")
        plan = self.plan(message, pause=True)
        with self.h.context(message):
            await begin(plan, self.h.registry)
        self.assertTrue(plan["paused"])
        self.assertEqual(self.h.calls, [])

    async def test_ordinary_new_task_still_starts(self):
        message = MESSAGE.replace("先暂停不要执行，", "")
        plan = self.plan(message)
        with self.h.context(message):
            await begin(plan, self.h.registry)
        self.assertFalse(plan["paused"])
        self.assertEqual([c["tool"] for c in self.h.calls], ["job_search_start"])

    async def test_pause_applies_to_independent_read_steps_too(self):
        value = draft()
        value["steps"].append({"id": "stats", "tool": "job_stats_read", "arguments": {}, "user_requested": True})
        plan = self.plan(steps=value["steps"])
        with self.h.context(MESSAGE):
            await begin(plan, self.h.registry)
            await advance(plan, self.h.registry)
        self.assertEqual(self.h.calls, [])

    async def test_continuation_replanning_does_not_implicitly_clear_pause(self):
        old = self.plan()
        with self.h.context("补充条件"):
            plan = validate_draft(RequestDraft.model_validate(draft(continuation=True)), self.h.registry,
                "补充条件", dialogue={}, conditions={}, stamp=STAMP, previous=old)
            await begin(plan, self.h.registry)
        self.assertTrue(plan["paused"])
        self.assertEqual(self.h.calls, [])

    async def test_explicit_revision_pause_vetoes_model_resume(self):
        old = self.plan()
        with self.h.context("先暂停不要执行") as context:
            context.plan_reader = lambda: copy.deepcopy(old)
            plan = validate_draft(RequestDraft(operation="revise", revision={"pause": False, "updates": []}),
                self.h.registry, "先暂停不要执行", dialogue={}, conditions={}, stamp=STAMP)
        self.assertTrue(plan["revision"]["pause"])

    async def test_invalid_model_output_fails_closed_through_http(self):
        self.model.value = {"unsupported_field": "pause"}
        run, _ = await self.h.chat(MESSAGE)
        await self.h.wait_for(lambda: self.h.store.get(run["id"])["status"] == "failed")
        self.assertEqual(self.h.calls, [])
        self.assertFalse(self.h.store.get(run["id"])["mutation_started"])

    async def test_quote_question_negation_are_not_pause_commands(self):
        for suffix in ("不要暂停", "为什么暂停", "如何暂停", "如果暂停", "‘暂停’", "岗位描述提到暂停"):
            message = MESSAGE.replace("先暂停不要执行", suffix)
            self.assertFalse(understand(message).commands("browser_task_pause"), suffix)
            self.assertFalse(self.plan(message)["paused"], suffix)

    async def test_http_pause_persists_without_tools_or_approvals(self):
        run, response = await self.h.chat(MESSAGE)
        await self.h.wait_for(lambda: self.h.store.get(run["id"])["status"] == "waiting_user")
        plan = self.h.store.get(run["id"])["checkpoint"]["plan"]
        self.assertTrue(plan["paused"])
        self.assertEqual(plan["status"], "paused")
        self.assertTrue(all(s["status"] == "pending" for s in plan["steps"]))
        self.assertEqual(self.h.store.dialogue_state("default")["task_request"]["status"], "paused")
        self.assertIn("暂停", response["message"])
        self.assertEqual(self.h.calls, [])
        self.assertEqual(self.h.store.approvals(run["id"]), [])
        self.assertFalse(self.h.store.get(run["id"])["mutation_started"])

    async def test_http_idempotent_retry_does_not_start_paused_plan(self):
        first, _ = await self.h.chat(MESSAGE, "same-request")
        second, _ = await self.h.chat(MESSAGE, "same-request")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.model.calls, 2)  # planner + scope review, no repeated planning
        self.assertEqual(self.h.calls, [])

    async def test_http_continue_preserves_conditions_and_starts_once(self):
        run, _ = await self.h.chat(MESSAGE)
        before = copy.deepcopy(self.h.store.get(run["id"])["checkpoint"]["plan"])
        await self.h.chat("继续任务")
        await self.h.wait_for(lambda: bool(self.h.calls))
        await self.h.chat("继续任务")
        self.assertEqual(len(self.h.calls), 1)
        self.assertEqual(self.h.calls[0]["tool"], "job_search_start")
        self.assertEqual(self.h.calls[0]["arguments"], before["steps"][0]["arguments"])
        self.assertEqual(self.model.calls, 2)  # durable resume, not replanning

    async def test_http_semantic_resume_revises_original_plan(self):
        run, _ = await self.h.chat(MESSAGE)
        before = copy.deepcopy(self.h.store.get(run["id"])["checkpoint"]["plan"])
        self.model.value = {"operation": "revise", "revision": {"pause": False, "updates": []}}
        await self.h.chat(CONTINUE)
        await self.h.wait_for(lambda: bool(self.h.calls))
        after = self.h.store.get(run["id"])["checkpoint"]["plan"]
        self.assertEqual(after["id"], before["id"])
        self.assertFalse(after["paused"])
        self.assertEqual([c["tool"] for c in self.h.calls], ["job_search_start"])
        self.assertEqual(self.h.calls[0]["arguments"], before["steps"][0]["arguments"])

    async def test_restart_preserves_pause_until_http_continue(self):
        run, _ = await self.h.chat(MESSAGE)
        await self.h.runtime.shutdown()
        self.h.runtime.store = RunStore(self.h.store.path, self.h.key)
        self.h.store = self.h.runtime.store
        await self.h.runtime.start()
        await self.h.wait_for(lambda: self.h.store.get(run["id"])["status"] == "waiting_user")
        # More than a watcher interval, so a dropped pause would invoke adapters.
        await asyncio.sleep(1.1)
        self.assertEqual(self.h.calls, [])
        self.assertTrue(self.h.runtime.active_plan("default")["paused"])
        await self.h.chat("继续任务")
        await self.h.wait_for(lambda: bool(self.h.calls))
        self.assertEqual(len(self.h.calls), 1)


if __name__ == "__main__":
    unittest.main()
