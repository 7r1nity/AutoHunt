"""Trust-boundary regressions: real SQLite, scripted model/executors, no network.

Run from agent/: python -m evaluation.privacy_contract
All data is synthetic. Results go only to the ignored test-results directory.
"""
from __future__ import annotations

import asyncio
import copy
import gc
import hashlib
import importlib.util
import inspect
import json
import random
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace as NS
from unittest.mock import patch

from cryptography.fernet import Fernet

from app.core.agent_tools import AgentToolRegistry, AgentToolSpec
from app.core.security import redact, redact_text
from app.llm.base import LLMRequest, LLMResult, LLMToolResult
from app.llm.manager import LLMManager
from app.runtime.context import RunContext, current_run
from app.runtime.dialogue import DialogueState, bounded_data, trace
from app.runtime.models import public_run
from app.runtime.store import RunStore, safe_json
from app.security.action_grants import binding_matches
from app.workflow.execution import advance
from app.workflow.planning import RequestDraft, validate_draft
from evaluation.action_contract import candidate

PHONE = "13800138000"
EMAIL = "synthetic@example.test"
DIGESTS = [
    "0e2c5469899a98a0113b5fdc42fab2b6b63d1b054893bacf50a5b99273493964",
    "8a662b5f6ec4b74bced1378481120bc331d522ad55d170707a108982c1f06446",
]
TRACES = []


class StorageContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "runtime.db"
        self.key = Fernet.generate_key().decode()
        self.store = RunStore(self.path, self.key)

    def tearDown(self):
        self.store = None
        gc.collect()
        self.temp.cleanup()

    def test_original_payload_checkpoint_result_and_error_survive_reopen(self):
        original = {"message": f"电话：{PHONE}，薪资15000，85分以上投递", "email": EMAIL}
        run, _ = self.store.create("default", "synthetic", "chat", original)
        checkpoint = {"arbitrary_reference": DIGESTS, "tool_arguments": original}
        self.store.update(run["id"], checkpoint=checkpoint, result={"message": original["message"]},
                          error=f"synthetic phone {PHONE}")
        reopened = RunStore(self.path, self.key)
        saved = reopened.get(run["id"])
        self.assertEqual(saved["payload"], original)
        self.assertEqual(saved["checkpoint"], checkpoint)
        self.assertEqual(saved["error"], f"synthetic phone {PHONE}")
        self.assertIn(PHONE, reopened.request_records("default")[0]["message"])
        self.assertNotIn(PHONE, safe_json(public_run(saved)))

    def test_contact_edit_is_not_an_idempotency_collision(self):
        run, _ = self.store.create("default", "same-key", "chat", {"message": PHONE})
        again, created = self.store.create("default", "same-key", "chat", {"message": PHONE})
        self.assertFalse(created)
        self.assertEqual(again["id"], run["id"])
        with self.assertRaises(ValueError):
            self.store.create("default", "same-key", "chat", {"message": "13900139000"})

    def test_internal_events_and_public_projection_are_distinct(self):
        run, _ = self.store.create("default", "event", "chat", {})
        data = {"contact_phone": PHONE, "arguments_digest": DIGESTS[0],
                "arguments": {"salary_min": 15000, "min_score": 85}, "token": "synthetic-private-token"}
        self.store.event(run["id"], "tool_started", data)
        internal = self.store.events(run["id"])[-1]["data"]
        projected = self.store.public_events(run["id"])[-1]["data"]
        self.assertEqual(internal, data)
        self.assertNotIn(PHONE, safe_json(projected))
        self.assertNotIn("synthetic-private-token", safe_json(projected))
        self.assertEqual(projected["arguments_digest"], DIGESTS[0])
        self.assertEqual(projected["arguments"], data["arguments"])
        self.assertEqual(self.store.events(run["id"])[-1]["data"], data)

    def test_encrypted_at_rest_wrong_or_missing_key_cannot_read(self):
        run, _ = self.store.create("default", "private", "chat", {"message": PHONE})
        self.store.save_search_state("default", {"queries": ["金融分析师"], "locations": ["深圳"]})
        self.store.mutate_dialogue("default", {"op": "instruction", "request": {"id": DIGESTS[0], "text": EMAIL}})
        self.store.event(run["id"], "synthetic", {"email": EMAIL})
        with self.store.connect() as db:
            stored = db.execute("SELECT payload FROM agent_runs WHERE id=?", (run["id"],)).fetchone()[0]
            self.assertTrue(stored.startswith("enc:v1:"))
            self.assertNotIn(PHONE, stored)
            self.assertTrue(db.execute("SELECT state FROM session_dialogue_state").fetchone()[0].startswith("enc:v1:"))
        self.assertEqual(self.store.search_state("default")["queries"], ["金融分析师"])
        for key in (None, Fernet.generate_key().decode()):
            with self.subTest(key_configured=bool(key)), self.assertRaises(RuntimeError):
                RunStore(self.path, key).get(run["id"])

    def test_legacy_plaintext_and_new_encrypted_rows_can_coexist(self):
        legacy = RunStore(self.path)
        run, _ = legacy.create("default", "legacy", "chat", {"message": "金融分析师"})
        legacy.update(run["id"], checkpoint={"reference": DIGESTS[0]})
        self.assertEqual(self.store.get(run["id"])["checkpoint"], {"reference": DIGESTS[0]})
        self.store.update(run["id"], checkpoint={"reference": DIGESTS[1]})
        self.assertEqual(self.store.get(run["id"])["checkpoint"]["reference"], DIGESTS[1])

    def test_masked_legacy_binding_is_not_repaired_into_permission(self):
        legacy = {"arguments_digest": "hash[REDACTED_PHONE]", "kind": "action", "version": 1,
                  "tool": "mock_delivery_start", "session_id": "default"}
        self.assertFalse(binding_matches(legacy, "mock_delivery_start", {"min_score": 85}, "default"))

    def test_arbitrary_field_identifiers_do_not_need_a_whitelist(self):
        randomizer = random.Random(20261010)
        identifiers = DIGESTS + [hashlib.sha256(randomizer.randbytes(40)).hexdigest() for _ in range(256)]
        original = {"future_fields": identifiers, "nested": [{"new_key": value} for value in identifiers]}
        self.assertEqual(json.loads(safe_json(original)), original)
        self.assertEqual(redact(original), original)

    def test_dialogue_trace_and_bounded_model_copy_do_not_mutate_source(self):
        original = f"电话：{PHONE}，找15个，薪资15000以上"
        state = DialogueState()
        trace(state, "synthetic", DIGESTS[0], text=original)
        saved = state.model_dump(mode="json")
        self.assertIn(PHONE, saved["trace"][0]["text"])
        before = copy.deepcopy(saved)
        projected = bounded_data({"saved": saved, "original": original}, budget=5000)
        self.assertNotIn(PHONE, projected)
        self.assertEqual(saved, before)


class BoundaryContracts(unittest.TestCase):
    def test_every_mcp_result_is_a_boundary_copy_with_original_schema(self):
        original = {"id": DIGESTS[0], "company": "华为", "salary": "15-25K", "phone": PHONE,
                    "description": f"金融分析；联系人邮箱：{EMAIL}"}
        dependencies = ModuleType("app.core.dependencies")
        for name in ("browser_service", "candidate_state_store", "memory_store", "resume_workspace_store", "skill_store"):
            setattr(dependencies, name, NS())
        dependencies.job_store = NS(list_jobs=lambda **_: [NS(model_dump=lambda **_: original)])
        class Server:
            def __init__(self):
                self.functions = {}
            def tool(self):
                def register(function):
                    self.functions[function.__name__] = function
                    return function
                return register
        path = Path(__file__).resolve().parents[1] / "app" / "mcp" / "tools.py"
        spec = importlib.util.spec_from_file_location("app.mcp.synthetic_privacy_tools", path)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"app.core.dependencies": dependencies}):
            spec.loader.exec_module(module)
        server = Server()
        module.register_tools(server)
        self.assertEqual(len(server.functions), 18)
        self.assertEqual(list(inspect.signature(server.functions["job_results_read"]).parameters), ["limit", "stage"])
        result = server.functions["job_results_read"]()[0]
        self.assertEqual(result["id"], DIGESTS[0])
        self.assertEqual(result["salary"], "15-25K")
        self.assertEqual(result["company"], "华为")
        self.assertNotIn(PHONE, safe_json(result))
        self.assertNotIn(EMAIL, safe_json(result))
        self.assertEqual(original["phone"], PHONE)

    def test_business_numbers_dates_opaque_ids_and_company_names_are_untouched(self):
        cases = ["薪资15000，数量18，匹配度85，年薪250000", "2026-10-10T10:42:55.590575+00:00",
                 "项目编号12345678901，资产1378481120元", "身份证字段暂未提供，华为、外企德科",
                 "138001380001234567890", *DIGESTS]
        for value in cases:
            with self.subTest(value=value):
                self.assertEqual(redact_text(value), value)
        self.assertEqual(redact({"id": PHONE, "future_operation_id": PHONE, "references_ids": [PHONE]}),
                         {"id": PHONE, "future_operation_id": PHONE, "references_ids": [PHONE]})
        # A nested contact under a misleading reference key is not exempt.
        self.assertNotIn(PHONE, safe_json(redact({"id": {"phone": PHONE}})))

    def test_known_contact_formats_and_typed_fields_are_masked(self):
        cases = [PHONE, "+86 138 0013 8000", "+1 (415) 555-2671", "010-12345678",
                 "电话：12345678", "手机" + PHONE, "我的邮箱是" + EMAIL,
                 "邮箱：" + EMAIL, "姓名：张测试", "家庭住址：测试街1号"]
        for value in cases:
            with self.subTest(value=value):
                self.assertIn("[REDACTED_", redact_text(value))
        projected = redact({"phone": "12345678", "email": EMAIL, "candidate_name": "张测试",
                            "company": "华为", "location": "深圳", "salary_min": 15000})
        self.assertEqual(projected["company"], "华为")
        self.assertEqual(projected["salary_min"], 15000)
        self.assertNotIn(EMAIL, safe_json(projected))

    def test_json_native_tool_arguments_secrets_and_nested_contacts_are_projected(self):
        original = {"tool_call_id": DIGESTS[0], "function": {"name": "synthetic_tool", "arguments":
                    json.dumps({"email": EMAIL, "api_key": "synthetic-api-key", "min_score": 85})}}
        projected = redact(original)
        self.assertNotIn(EMAIL, safe_json(projected))
        self.assertNotIn("synthetic-api-key", safe_json(projected))
        self.assertEqual(projected["tool_call_id"], DIGESTS[0])
        self.assertEqual(json.loads(projected["function"]["arguments"])["min_score"], 85)
        self.assertIn(EMAIL, original["function"]["arguments"])
        self.assertEqual(redact(projected), projected)
        pem = "-----BEGIN PRIVATE KEY-----\nsynthetic-key-material\n-----END PRIVATE KEY-----"
        self.assertNotIn("synthetic-key-material", redact_text(pem))
        self.assertNotIn("synthetic-auth-token", redact_text("Authorization: Bearer synthetic-auth-token"))


class ModelAndExecutionContracts(unittest.IsolatedAsyncioTestCase):
    async def test_model_boundary_uses_a_copy_of_content_and_native_arguments(self):
        seen = []
        class Provider:
            name, model = "synthetic", "local"
            async def generate(self, request):
                seen.append(request)
                return LLMResult("ok", self.name, self.model)
            async def generate_with_tools(self, **kwargs):
                seen.append(kwargs)
                return LLMToolResult("ok", self.name, self.model)
        manager = LLMManager(None, None)
        with patch.object(manager, "_provider", return_value=Provider()):
            request = LLMRequest("处理专业条件", json.dumps({"email": EMAIL, "result_limit": 15}),
                                 f"月薪15000，电话：{PHONE}")
            await manager.generate(request)
            messages = [{"role": "assistant", "tool_calls": [{"id": DIGESTS[0], "function": {
                "name": "synthetic", "arguments": json.dumps({"email": EMAIL, "salary_min": 15000})}}]}]
            original = copy.deepcopy(messages)
            await manager.generate_with_tools(instructions="执行只读", messages=messages, tools=[])
        self.assertNotIn(EMAIL, seen[0].user_message)
        self.assertNotIn(PHONE, seen[0].context)
        self.assertEqual(json.loads(seen[0].user_message)["result_limit"], 15)
        self.assertNotIn(EMAIL, safe_json(seen[1]))
        self.assertEqual(messages, original)
        self.assertEqual(seen[1]["messages"][0]["tool_calls"][0]["id"], DIGESTS[0])
        TRACES.append({"test": self.id(), "projected_request": seen[0].user_message,
                       "projected_tool_messages": seen[1]["messages"], "source_unchanged": messages == original})

    async def test_real_sqlite_claim_allows_delivery_and_growth_once(self):
        temp = tempfile.TemporaryDirectory()
        key = Fernet.generate_key().decode()
        store = RunStore(Path(temp.name) / "run.db", key)
        run, _ = store.create("default", "workflow", "chat", {"message": "85以上投递，以下且25k成长"})
        calls = []
        document = NS(id="synthetic-document", version=1)
        candidate_state = NS(read=lambda: NS(profile=NS(resume_id="synthetic-resume")))
        resumes = NS(read_document=lambda: document)
        registry = AgentToolRegistry(jobs=None, browser=None, candidate_state=candidate_state,
                                     resume_workspace=resumes, memory=None, learning_plans=NS(), mock_workflow=NS())
        context = RunContext(run["id"], asyncio.current_task(),
            lambda kind, data: store.event(run["id"], kind, data), lambda checkpoint: store.update(run["id"], checkpoint=checkpoint),
            session_id="default", dialogue=store.dialogue_state("default"),
            mutate_dialogue=lambda operation: store.mutate_dialogue("default", operation),
            search_state={"queries": ["金融分析师"]})
        token = current_run.set(context)
        plan = {}
        try:
            plan = validate_draft(RequestDraft.model_validate(candidate()), registry,
                "找18份工作，85以上投递，以下且25k以上成长", dialogue=context.dialogue,
                conditions=context.search_state, stamp="synthetic-resume:synthetic-document:1")
            context.dialogue = store.mutate_dialogue("default", {"op": "task", "kind": "plan_id", "id": plan["id"], "bind": True})
            for step in plan["steps"]:
                spec = registry._tools[step["tool"]]
                async def executor(arguments, name=spec.name):
                    persisted = store.dialogue_state("default")["instruction_request"]
                    current = next(item for item in persisted["steps"] if item["tool"] == name)
                    self.assertEqual(current["status"], "claimed")
                    self.assertEqual(current["arguments"], arguments)
                    calls.append(name)
                    return {"id": "synthetic-browser", "message": "ok"}
                registry._tools[spec.name] = AgentToolSpec(spec.name, spec.description, spec.parameters, spec.effect, executor)
            await advance(plan, registry)
            self.assertEqual(calls, ["job_search_start"])
            plan["steps"][0]["status"] = "completed"
            # Simulate reopening the actual durable state between foreground and watcher.
            context.dialogue = RunStore(store.path, key).dialogue_state("default")
            await advance(plan, registry)
            self.assertEqual(set(calls), {"job_search_start", "mock_delivery_start", "learning_plan_generate", "job_results_read"})
            self.assertEqual(plan["status"], "finished")
            self.assertEqual(plan["steps"][1]["authority_binding"]["arguments_digest"], DIGESTS[0])
            await advance(plan, registry)
            self.assertEqual(len(calls), 4)
            self.assertFalse(any(event["kind"] == "tool_denied" for event in store.events(run["id"])))
            # Boundary views cannot mutate claim or authority state.
            store.public_events(run["id"])
            self.assertEqual(store.dialogue_state("default")["instruction_request"]["steps"][1]["authority_binding"],
                             plan["steps"][1]["authority_binding"])
        finally:
            TRACES.append({"test": self.id(), "executor_calls": calls,
                           "events": store.public_events(run["id"]), "plan_status": plan.get("status")})
            current_run.reset(token)
            del context, registry, store
            gc.collect()
            temp.cleanup()


if __name__ == "__main__":
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(cls)
        for cls in (StorageContracts, BoundaryContracts, ModelAndExecutionContracts))
    class RecordedResult(unittest.TextTestResult):
        def addSuccess(self, test):
            super().addSuccess(test)
            TRACES.append({"test": test.id(), "status": "pass"})
        def addFailure(self, test, error):
            super().addFailure(test, error)
            TRACES.append({"test": test.id(), "status": "fail"})
        def addError(self, test, error):
            super().addError(test, error)
            TRACES.append({"test": test.id(), "status": "error"})
    result = unittest.TextTestRunner(verbosity=2, resultclass=RecordedResult).run(suite)
    output = Path(__file__).resolve().parents[1] / "test-results" / "privacy-contract" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    summary = {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
               "synthetic_opaque_identifiers": 258, "real_model_calls": 0, "browser_runs": 0,
               "coverage": "Encrypted/plain SQLite round-trip, boundary projections, scripted model and DAG; not browser E2E"}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "traces.jsonl").write_text("".join(json.dumps(trace, ensure_ascii=False) + "\n" for trace in TRACES), encoding="utf-8")
    (output / "failures.json").write_text(json.dumps([{"test": str(test), "traceback": error}
        for test, error in result.failures + result.errors], ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), **summary}, ensure_ascii=False))
    raise SystemExit(0 if result.wasSuccessful() else 1)
