"""Model trace privacy/correlation tests and a one-call consented synthetic check.

Offline: python -m evaluation.model_trace_contract
Live (only after consent): python -m evaluation.model_trace_contract --models-consented
The live path caps physical outbound requests at ONE and never executes tools.
"""
from __future__ import annotations

import argparse
import asyncio
import gc
import io
import json
import tempfile
import unittest
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS

from cryptography.fernet import Fernet

from app.core.agent_tools import AgentToolRegistry
from app.llm.base import LLMRequest, LLMResult, LLMToolCall, LLMToolResult
from app.llm.manager import LLMManager
from app.runtime.context import RunContext, current_run
from app.runtime.store import RunStore
from app.workflow.planning import _draft_with_repair, plan_request
from app.workflow.validation import PlanValidationError
from .goal_graph_contract import STAMP
from .job_target_model import fixtures
from .run import ModelBudgetExceeded, consented_provider

TRACES = []


class Scripted:
    name, model = "scripted", "synthetic-trace-model"
    def __init__(self, replies): self.replies, self.requests = replies, []
    async def generate(self, request):
        self.requests.append(asdict(request))
        result = self.replies.pop(0)
        if isinstance(result, BaseException): raise result
        return result
    async def generate_with_tools(self, **request):
        self.requests.append(request)
        return self.replies.pop(0)


def manager(provider):
    instance = LLMManager(NS(), NS())
    instance._provider = lambda: provider
    return instance


class Contracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="autohunt-model-trace-")
        self.store = RunStore(Path(self.temp.name)/"runtime.db", Fernet.generate_key().decode())
        self.run, _ = self.store.create("default", "synthetic-trace", "chat", {"message": "合成请求"})
        self.context = RunContext(self.run["id"], asyncio.current_task(),
            lambda kind, data: self.store.event(self.run["id"], kind, data), lambda _: None)
        self.token = current_run.set(self.context)
    async def asyncTearDown(self):
        TRACES.append(dict(test=self.id(), events=self.store.all_events(self.run["id"])))
        current_run.reset(self.token)
        self.store = None; gc.collect(); self.temp.cleanup()
    def own(self): self.context.owner = asyncio.current_task()
    def events(self, kind): return [r["data"] for r in self.store.all_events(self.run["id"]) if r["kind"] == kind]
    def result(self, text="合成原始回复"):
        return LLMResult(text, "scripted", "synthetic-trace-model", "synthetic-response", {"prompt_tokens": 7, "completion_tokens": 3})

    async def test_actual_model_raw_output_usage_and_encrypted_persistence(self):
        self.own()
        provider = Scripted([self.result('{"salary_min":13000,"id":"123456789012345"}')])
        result = await manager(provider).generate(LLMRequest("合成规则", "虚构用户条件", "合成状态", fast=True, json_output=True))
        request, response = self.events("model_request")[0], self.events("model_response")[0]
        self.assertEqual(request["call_id"], response["call_id"])
        self.assertEqual(result.call_id, response["call_id"])
        self.assertEqual(response["response"]["text"], result.text)
        self.assertEqual(response["model"], provider.model)
        self.assertEqual(response["usage"]["prompt_tokens"], 7)
        with self.store.connect() as db:
            values = [r[0] for r in db.execute("SELECT data FROM agent_run_events")]
        self.assertTrue(all(s.startswith("enc:v1:") for s in values))
        self.assertFalse(any("salary_min" in s for s in values))
        self.assertEqual(provider.requests[0]["user_message"], "虚构用户条件")

    async def test_public_projection_hides_prompts_raw_response_and_draft(self):
        self.own()
        await manager(Scripted([self.result("合成内部原文")])).generate(LLMRequest("内部系统prompt", "内部请求", "内部context"))
        self.store.event(self.run["id"], "plan_draft", {"draft": {"reply": "内部草稿"}, "attempt": 0})
        public = json.dumps(self.store.public_events(self.run["id"]), ensure_ascii=False)
        for secret in ("内部系统prompt", "内部请求", "内部context", "合成内部原文", "内部草稿"):
            self.assertNotIn(secret, public)
        self.assertIn("synthetic-trace-model", public)
        self.assertIn("request_digest", public)
        self.assertTrue(self.events("model_request")[0]["request"])

    async def test_unencrypted_store_keeps_only_explicit_diagnostic_metadata(self):
        unencrypted = RunStore(Path(self.temp.name)/"unencrypted.db")
        run, _ = unencrypted.create("default", "unenc", "chat", {})
        unencrypted.event(run["id"], "model_response", {"call_id": "one", "response": {"text": "合成内部原文"}, "model": "synthetic"})
        data = next(r["data"] for r in unencrypted.events(run["id"]) if r["kind"] == "model_response")
        self.assertNotIn("response", data)
        self.assertEqual(data["details_omitted"], "runtime_encryption_not_configured")

    async def test_format_repair_preserves_both_raw_responses_and_call_links(self):
        self.own()
        llm = manager(Scripted([self.result("not JSON"), self.result('{"reply":"修复完成"}')]))
        result = await _draft_with_repair(llm, LLMRequest("合成解释器", '{"message":"合成命令"}', "", json_output=True))
        responses = self.events("model_response")
        self.assertEqual([r["response"]["text"] for r in responses], ["not JSON", '{"reply":"修复完成"}'])
        self.assertNotEqual(responses[0]["call_id"], responses[1]["call_id"])
        self.assertEqual(self.events("plan_parse_error")[0]["call_id"], responses[0]["call_id"])
        self.assertEqual(self.events("plan_repair")[0]["call_id"], responses[0]["call_id"])
        self.assertEqual(self.events("plan_draft")[0]["call_id"], responses[1]["call_id"])
        self.assertEqual(result.reply, "修复完成")

    async def test_semantic_repair_preserves_candidate_and_feedback(self):
        self.own(); count = 0
        def validate(draft):
            nonlocal count
            count += 1
            if count == 1: raise PlanValidationError("invalid_argument_schema", "合成格式缺陷")
            return {"steps": [], "reply": draft.reply}
        provider = Scripted([self.result('{"reply":"候选一"}'), self.result('{"reply":"候选二"}')])
        result = await _draft_with_repair(manager(provider), LLMRequest("合成解释器", '{"message":"合成命令"}', ""), validator=validate)
        self.assertEqual(result["reply"], "候选二")
        self.assertEqual(len(self.events("plan_draft")), 2)
        self.assertEqual(self.events("plan_validation_result")[0]["call_id"], self.events("plan_draft")[0]["call_id"])
        self.assertIn("validation_feedback", json.loads(provider.requests[1]["user_message"]))

    async def test_exception_logged_without_replay_or_secret_exception_text(self):
        self.own(); provider = Scripted([RuntimeError("synthetic-secret-error-body")])
        with self.assertRaises(RuntimeError): await manager(provider).generate(LLMRequest("合成", "合成", ""))
        self.assertEqual(len(provider.requests), 1)
        response = self.events("model_response")[0]
        self.assertEqual(response["status"], "error")
        self.assertEqual(response["error_type"], "RuntimeError")
        self.assertNotIn("synthetic-secret-error-body", json.dumps(response))

    async def test_cancel_logged_and_propagated(self):
        self.own()
        with self.assertRaises(asyncio.CancelledError):
            await manager(Scripted([asyncio.CancelledError()])).generate(LLMRequest("合成", "合成", ""))
        self.assertEqual(self.events("model_response")[0]["status"], "cancelled")

    async def test_native_tool_arguments_logged_without_reasoning_or_execution(self):
        self.own()
        result = LLMToolResult("合成回复", "scripted", "synthetic-trace-model", [LLMToolCall("one", "job_remove_selection", '{"selector":"第2个"}')],
                              {"role": "assistant", "reasoning_content": "不记录的内部推理"}, "synthetic-response")
        returned = await manager(Scripted([result])).generate_with_tools(instructions="合成", messages=[], tools=[])
        trace = self.events("model_response")[0]
        self.assertEqual(trace["response"]["tool_calls"][0]["arguments"], '{"selector":"第2个"}')
        self.assertNotIn("reasoning_content", json.dumps(trace))
        self.assertTrue(returned.call_id)

    async def test_failed_format_repair_keeps_second_parse_error(self):
        self.own()
        provider = Scripted([self.result("bad one"), self.result("bad two")])
        with self.assertRaises(ValueError):
            await _draft_with_repair(manager(provider), LLMRequest("合成", '{"message":"合成"}', ""))
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(len(self.events("model_response")), 2)
        self.assertEqual([e["attempt"] for e in self.events("plan_parse_error")], [0, 1])

    async def test_deepseek_requested_and_response_models_all_modes_without_network(self):
        from app.llm.deepseek_provider import DeepSeekProvider
        self.own()
        provider = DeepSeekProvider(api_key="synthetic-not-a-real-key", model="configured-alias")
        calls=[]
        usage=NS(model_dump=lambda **_: {"prompt_tokens": 11, "completion_tokens": 4})
        response=NS(id="synthetic-response",model="actual-response-model",usage=usage,
                    choices=[NS(message=NS(content=" \n合成 \n",tool_calls=[]))])
        async def completion(**kwargs): calls.append(kwargs); return response
        provider._completion=completion
        try:
            plain=await provider.generate(LLMRequest("合成", "合成", "", fast=True))
            native=await provider.generate_with_tools(instructions="合成",messages=[],tools=[])
            class Stream:
                def __aiter__(self): return self
                async def __anext__(self):
                    if getattr(self,"done",False): raise StopAsyncIteration
                    self.done=True
                    return NS(id="stream-response",model="actual-response-model",usage=usage,
                              choices=[NS(delta=NS(content=" \n合成 \n",tool_calls=[]))])
                async def close(self): pass
            async def streaming(**kwargs): calls.append(kwargs); return Stream()
            provider._completion=streaming
            streamed=await provider.generate_with_tools_stream(instructions="合成",messages=[],tools=[],on_delta=lambda _:None)
            self.assertEqual([c["model"] for c in calls], ["configured-alias"]*3)
            for result in (plain,native,streamed):
                self.assertEqual(result.text," \n合成 \n")
                self.assertEqual(result.model,"actual-response-model")
                self.assertEqual(result.usage["prompt_tokens"],11)
        finally: await provider.client.close()


async def live_check():
    physical = []
    provider = consented_provider(1, physical)
    output = Path(__file__).parent/"results"/"model-trace-live"/datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    store = RunStore(output/"runtime.db", Fernet.generate_key().decode())
    # The ephemeral eval key is deliberately not persisted. Export synthetic
    # events before closing; production uses its configured persistent data key.
    run, _ = store.create("default", "synthetic-remove-one-call", "chat", {"message": "移除第三方或者外包"})
    jobs, dialogue = fixtures()
    registry = AgentToolRegistry(jobs=jobs, browser=NS(), memory=None, mock_workflow=NS(),
        candidate_state=NS(read=lambda: NS(profile=NS(resume_id="synthetic-resume"))),
        resume_workspace=NS(read_document=lambda: NS(id="synthetic-document", version=1)))
    old = dict(id="synthetic-old-plan", session_id="default", resume_stamp=STAMP, revision=0, status="clarifying",
               original="移除这些，其余投递", questions=["这些指哪些岗位？"], steps=[
        dict(id="old-refilter", tool="job_results_refilter", arguments={"task_id":"synthetic-chat-id"}, status="waiting_input", depends_on=[]),
        dict(id="old-delivery", tool="mock_delivery_start", arguments={}, status="pending", depends_on=["old-refilter"])])
    for s in old["steps"]: s.update(command_quote="移除这些，其余投递", scope_quote="", user_requested=True, questions=[], missing_slots=[], slot_evidence={})
    context = RunContext(run["id"], asyncio.current_task(), lambda kind,data: store.event(run["id"],kind,data), lambda _:None,
                         dialogue=dialogue, search_state={"source":"mock"}, plan_reader=lambda:old)
    token = current_run.set(context)
    raw_generate = provider.generate
    async def once(request):
        if provider.calls >= 1: raise ModelBudgetExceeded("One-call evaluation budget exhausted; no repair request sent")
        return await raw_generate(request)
    provider.generate = once
    try:
        try:
            plan = await plan_request(manager(provider), registry, "移除第三方或者外包", dialogue=dialogue,
                                      conditions=context.search_state, stamp=STAMP)
            from app.workflow.revision import RevisionDraft, edited_plan
            if plan.get("operation") == "revise":
                plan = edited_plan(old, RevisionDraft.model_validate(plan["revision"]), registry, stamp=STAMP,
                                   session_id="default", message="移除第三方或者外包")
            active = [s for s in plan.get("steps",[]) if s["status"] == "pending"]
            checks = dict(removal_planned=any(s["tool"]=="job_remove_selection" for s in active),
                          wrong_refilter_not_active=not any(s["tool"]=="job_results_refilter" for s in active),
                          no_global_questions=not plan.get("questions"))
            summary = dict(passed=all(checks.values()), checks=checks, plan=plan)
        except Exception as exc:
            summary = dict(passed=False, error_type=type(exc).__name__, error=str(exc))
        events = store.all_events(run["id"])
        summary.update(physical_calls=provider.calls, real_browser_actions=0, model=provider.model,
                       raw_responses=sum(r["kind"]=="model_response" and r["data"].get("status")=="ok" for r in events))
        (output/"events.jsonl").write_text("".join(json.dumps(r,ensure_ascii=False,default=str)+"\n" for r in events),encoding="utf-8")
        (output/"physical-calls.jsonl").write_text("".join(json.dumps(r,ensure_ascii=False,default=str)+"\n" for r in physical),encoding="utf-8")
        (output/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps({"output":str(output), **{k:v for k,v in summary.items() if k!="plan"}},ensure_ascii=False),flush=True)
        return 0 if summary["passed"] else 1
    finally:
        current_run.reset(token)
        await provider.client.close()


def offline():
    buffer=io.StringIO()
    result=unittest.TextTestRunner(stream=buffer,verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Contracts))
    output=Path(__file__).parent/"results"/"model-trace"/datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    summary=dict(tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),external_calls=0)
    (output/"tests.log").write_text(buffer.getvalue(),encoding="utf-8")
    (output/"traces.jsonl").write_text("".join(json.dumps(r,ensure_ascii=False,default=str)+"\n" for r in TRACES),encoding="utf-8")
    (output/"summary.json").write_text(json.dumps(summary,indent=2),encoding="utf-8")
    if not result.wasSuccessful(): print(buffer.getvalue()[-12000:])
    print(json.dumps(dict(output=str(output),**summary)))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--models-consented",action="store_true")
    args=parser.parse_args()
    raise SystemExit(asyncio.run(live_check()) if args.models_consented else offline())
