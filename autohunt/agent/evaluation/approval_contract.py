"""Durable action policy benchmark. Synthetic SQLite + real gateway/runner, no network."""
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

from app.core.agent_tools import AgentToolRegistry, AgentToolSpec, ToolLoopState
from app.core.models import AgentLane, ChatResponse
from app.runtime.context import RunContext, current_run
from app.runtime.manager import AgentRuntime
from app.runtime.models import public_run
from app.runtime.store import RunStore
from app.security.decisions import ActionPolicy, Decision
from app.security.risk import AlwaysConfirm, SecurityRisk
from app.workflow.execution import advance
from app.workflow.recovery import recover_plan
from app.workflow.revision import RevisionDraft, edited_plan

TRACES = []
STAMP = "synthetic-resume:synthetic-document:1"


class DurableApprovalContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.key = Fernet.generate_key().decode()
        self.store = RunStore(Path(self.temp.name) / "runtime.db", self.key)
        self.doc = NS(id="synthetic-document", version=1)
        self.resumes = NS(read_document=lambda: self.doc)
        self.candidate = NS(read=lambda: NS(profile=NS(resume_id="synthetic-resume")))
        self.calls = []
        self.registry = AgentToolRegistry(jobs=NS(), browser=NS(), candidate_state=self.candidate,
            resume_workspace=self.resumes, memory=None, mock_workflow=NS())
        for name in ("mock_delivery_start", "resume_propose_patch", "job_stats_read", "browser_task_stop"):
            spec = self.registry._tools[name]
            async def executor(args, name=name):
                self.calls.append((name, copy.deepcopy(args)))
                return {"message": "合成回执", "title": "合成计划", "jobs_analyzed": 0}
            self.registry._tools[name] = AgentToolSpec(spec.name, spec.description, spec.parameters, spec.effect, executor)
        self.runtime = AgentRuntime(self.store, NS(tool_registry=self.registry, candidate_state=self.candidate),
            NS(add_conversation_message=lambda *a, **kw: None, search_conversation_history=lambda *a, **kw: []), NS(), self.resumes)
        self.runtime.search_state = lambda _: {"queries": ["合成分析师"]}
        self.run, _ = self.store.create("default", "synthetic", "chat", {"message": "合成请求"})
        self.store.update(self.run["id"], status="running")
        self.context = RunContext(self.run["id"], asyncio.current_task(),
            lambda kind, data: self.runtime.emit(self.run["id"], kind, data), lambda value: self.runtime.save(self.run["id"], value),
            dialogue=self.store.dialogue_state("default"), search_state={},
            mutate_dialogue=lambda op: self.store.mutate_dialogue("default", op),
            authorize_action=lambda *args: self.runtime.authorize_action(self.run["id"], *args),
            cancellation_requested=lambda: bool(self.store.get(self.run["id"])["cancel_requested"]), user_message="合成请求")
        self.token = current_run.set(self.context)

    async def asyncTearDown(self):
        TRACES.append({"test": self.id(), "calls": self.calls, "events": self.store.public_events(self.run["id"]),
                       "approvals": self.store.approvals(self.run["id"])})
        current_run.reset(self.token)
        for task in list(self.runtime.tasks.values()): task.cancel()
        await asyncio.gather(*list(self.runtime.tasks.values()), return_exceptions=True)
        gc.collect()
        self.temp.cleanup()

    def enter(self):
        self.context.owner = asyncio.current_task()

    def plan(self, requested=False, extra=False):
        steps = [{"id": "delivery", "tool": "mock_delivery_start", "arguments": {"min_score": 85},
            "depends_on": [], "status": "pending", "effect": "external", "user_requested": requested,
            "command_quote": "合成投递范围", "scope_quote": "", "missing_slots": []}]
        if extra:
            steps.append({"id": "proposal", "tool": "resume_propose_patch", "arguments": {"instruction": "补充合成项目描述"},
                "depends_on": [], "status": "pending", "effect": "write", "user_requested": True,
                "command_quote": "补充合成项目描述", "scope_quote": "", "missing_slots": []})
        value = {"id": "synthetic-plan", "session_id": "default", "original": "合成请求", "answers": [], "questions": [],
            "goals": [], "reply": "", "resume_stamp": STAMP, "revision": 0, "turn": 0,
            "expires_at": time.time()-100, "status": "running", "steps": steps}
        self.store.save_plan(self.run["id"], value)
        return value

    async def tick(self, plan):
        self.enter()
        await advance(plan, self.registry)

    async def finish_approval(self, plan, approve=True):
        watcher = asyncio.create_task(self.runtime.watch_plan(self.run["id"]))
        try:
            await self.runtime.decide_approval(self.run["id"], plan["steps"][0]["approval_id"], "default", approve)
            await asyncio.wait_for(watcher, 4)
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    async def test_broad_delivery_scope_requires_confirmation_even_when_requested(self):
        value = self.plan(requested=True)
        value["steps"][0]["arguments"] = {}
        self.store.save_plan(self.run["id"], value)
        await self.tick(value)
        step = value["steps"][0]
        self.assertEqual(step["status"], "waiting_approval")
        self.assertEqual(self.calls, [])
        record = self.store.approvals(self.run["id"])[0]
        self.assertEqual((record["status"], record["source"]), ("pending", "confirmation"))
        self.assertEqual(record["snapshot"]["arguments"], {})
        self.assertIn("未设置分数门槛", step["detail"])

    def remember_delivery_scope(self):
        filters = {"op": "all", "children": [{"op": "condition", "text": "双休"},
            {"op": "condition", "text": "月薪至少15k", "salary": {"unit": "monthly", "minimum": 15000}}]}
        remembered = {"id": "old-batch", "session_id": "default", "resume_stamp": STAMP,
            "task_id": "synthetic-search", "min_score": 85, "filters": filters}
        self.context.dialogue = self.store.mutate_dialogue("default", {"op": "delivery_scope", "scope": remembered})
        return remembered

    def changed_scope_plan(self, *, extra=False):
        value = self.plan(requested=True, extra=extra)
        value["steps"][0]["arguments"] = {"scope_mode": "new", "min_score": 85, "source_task_id": "synthetic-search"}
        self.store.save_plan(self.run["id"], value)
        return value

    async def test_model_new_and_requested_true_cannot_drop_old_filters(self):
        self.remember_delivery_scope()
        value = self.changed_scope_plan(extra=True)
        await self.tick(value)
        self.assertEqual([s["status"] for s in value["steps"]], ["waiting_approval", "completed"])
        self.assertEqual([c[0] for c in self.calls], ["resume_propose_patch"])
        record = self.store.approvals(self.run["id"])[0]
        self.assertEqual((record["status"], record["source"]), ("pending", "confirmation"))
        self.assertTrue(record["snapshot"]["scope_reset"])
        self.assertIn("上一批", value["steps"][0]["detail"])
        self.assertIn("15,000", value["steps"][0]["detail"])
        self.assertIn("本次未设置额外语义筛选", value["steps"][0]["detail"])

    async def test_model_new_with_proved_preserved_filters_has_no_extra_confirmation(self):
        remembered = self.remember_delivery_scope()
        value = self.changed_scope_plan()
        value["steps"][0]["arguments"].update(filters=remembered["filters"], min_score=88)
        self.store.save_plan(self.run["id"], value)
        await self.tick(value)
        self.assertEqual(value["steps"][0]["status"], "completed")
        self.assertEqual(self.store.approvals(self.run["id"])[0]["source"], "instruction")
        self.assertEqual(len(self.calls), 1)

    async def test_scope_change_confirmation_survives_reopen_and_pointer_change(self):
        self.remember_delivery_scope()
        value = self.changed_scope_plan()
        await self.tick(value)
        ident = value["steps"][0]["approval_id"]
        self.store = RunStore(self.store.path, self.key)
        self.runtime.store = self.store
        self.context.dialogue = self.store.mutate_dialogue("default", {"op": "delivery_scope", "scope": None})
        value = self.store.get(self.run["id"])["checkpoint"]["plan"]
        value["steps"][0].update(status="pending", user_requested=True)
        self.store.save_plan(self.run["id"], value)
        await self.tick(value)
        self.assertEqual(value["steps"][0]["status"], "waiting_approval")
        self.assertEqual(value["steps"][0]["approval_id"], ident)
        self.assertEqual(self.calls, [])
        await self.finish_approval(value)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.store.approval(ident)["source"], "confirmation")
        self.assertEqual(self.store.get(self.run["id"])["status"], "completed")

    async def test_legacy_instruction_autoapproval_is_not_a_human_scope_reset(self):
        self.remember_delivery_scope()
        value = self.changed_scope_plan()
        args = value["steps"][0]["arguments"]
        original = self.store.ensure_approval(self.run["id"], "default", "delivery", {
            "plan_id": value["id"], "tool": "mock_delivery_start", "arguments": args,
            "resume_stamp": STAMP, "evidence_task_id": None}, requested=True)
        await self.tick(value)
        self.assertEqual(self.store.approval(original["id"])["status"], "invalidated")
        self.assertNotEqual(value["steps"][0]["approval_id"], original["id"])
        self.assertEqual(value["steps"][0]["status"], "waiting_approval")
        self.assertEqual(self.calls, [])

    async def test_scope_reset_rejection_never_sends(self):
        self.remember_delivery_scope()
        value = self.changed_scope_plan()
        await self.tick(value)
        await self.finish_approval(value, False)
        self.assertEqual(self.calls, [])

    async def test_scope_reset_confirmation_cannot_approve_changed_parameters(self):
        self.remember_delivery_scope()
        value = self.changed_scope_plan()
        await self.tick(value)
        ident = value["steps"][0]["approval_id"]
        saved = self.store.get(self.run["id"])["checkpoint"]["plan"]
        saved["steps"][0]["arguments"]["min_score"] = 50
        self.store.save_plan(self.run["id"], saved)
        with self.assertRaises(ValueError):
            await self.runtime.decide_approval(self.run["id"], ident, "default", True)
        self.assertEqual(self.calls, [])

    async def test_omitting_scope_mode_does_not_bypass_scope_change_confirmation(self):
        self.remember_delivery_scope()
        value = self.changed_scope_plan()
        value["steps"][0]["arguments"].pop("scope_mode")
        self.store.save_plan(self.run["id"], value)
        await self.tick(value)
        self.assertEqual(value["steps"][0]["status"], "waiting_approval")
        self.assertEqual(self.calls, [])

    async def test_single_selected_job_keeps_independent_authorization(self):
        self.remember_delivery_scope()
        value = self.changed_scope_plan()
        value["steps"][0]["arguments"] = {"selector": "第七个", "scope_mode": "new"}
        self.store.save_plan(self.run["id"], value)
        await self.tick(value)
        self.assertEqual(value["steps"][0]["status"], "completed")
        self.assertEqual(len(self.calls), 1)

    async def test_explicit_scope_runs_without_second_confirmation_and_without_task_ttl_expiry(self):
        value = self.plan(requested=True)
        await self.tick(value)
        self.assertEqual(value["steps"][0]["status"], "completed")
        self.assertEqual(self.store.approvals(self.run["id"])[0]["source"], "instruction")
        self.assertEqual(len(self.calls), 1)
        await self.tick(value)
        self.assertEqual(len(self.calls), 1)

    async def test_pending_confirmation_does_not_claim_effect_or_stop_independent_local_proposal(self):
        value = self.plan(extra=True)
        await self.tick(value)
        self.assertEqual([s["status"] for s in value["steps"]], ["waiting_approval", "completed"])
        self.assertEqual([c[0] for c in self.calls], ["resume_propose_patch"])
        starts = [e for e in self.store.all_events(self.run["id"]) if e["kind"] == "tool_started"]
        self.assertFalse(any(e["data"]["tool"] == "mock_delivery_start" for e in starts))
        self.assertEqual(len(public_run(self.store.get(self.run["id"]))["approvals"]), 1)

    async def test_button_confirmation_resumes_once_through_gateway_and_runner(self):
        value = self.plan()
        await self.tick(value)
        await self.finish_approval(value)
        self.assertEqual(self.store.get(self.run["id"])["status"], "completed")
        self.assertEqual(self.calls, [("mock_delivery_start", {"min_score": 85})])

    async def test_double_confirm_before_watch_is_idempotent(self):
        value = self.plan()
        await self.tick(value)
        for _ in range(2):
            await self.runtime.decide_approval(self.run["id"], value["steps"][0]["approval_id"], "default", True)
        await self.runtime.watch_plan(self.run["id"])
        self.assertEqual(len(self.calls), 1)

    async def test_reject_preserves_independent_branch_and_never_replays(self):
        value = self.plan(extra=True)
        await self.tick(value)
        await self.finish_approval(value, False)
        self.assertEqual([c[0] for c in self.calls], ["resume_propose_patch"])
        self.assertTrue(self.store.get(self.run["id"])["checkpoint"]["plan"]["steps"][0]["withdrawn"])

    async def test_foreign_session_and_action_tampering_cannot_approve(self):
        value = self.plan()
        await self.tick(value)
        approval_id = value["steps"][0]["approval_id"]
        with self.assertRaises(ValueError):
            await self.runtime.decide_approval(self.run["id"], approval_id, "foreign", True)
        saved = self.store.get(self.run["id"])["checkpoint"]["plan"]
        saved["steps"][0]["arguments"]["min_score"] = 60
        self.store.save_plan(self.run["id"], saved)
        with self.assertRaises(ValueError):
            await self.runtime.decide_approval(self.run["id"], approval_id, "default", True)
        self.assertEqual(self.calls, [])

    async def test_resume_change_and_cancellation_never_approve_old_scope(self):
        value = self.plan()
        await self.tick(value)
        approval_id = value["steps"][0]["approval_id"]
        self.doc.version = 2
        with self.assertRaises(ValueError):
            await self.runtime.decide_approval(self.run["id"], approval_id, "default", True)
        self.doc.version = 1
        self.store.update(self.run["id"], cancel_requested=1)
        with self.assertRaises(ValueError):
            await self.runtime.decide_approval(self.run["id"], approval_id, "default", True)
        self.assertEqual(self.calls, [])

    async def test_database_reopen_and_service_start_preserve_pending_approval(self):
        value = self.plan()
        await self.tick(value)
        self.runtime.store = self.store = RunStore(self.store.path, self.key)
        self.runtime.recover_search_summaries = lambda: None
        await self.runtime.start()
        self.assertEqual(self.store.approval(value["steps"][0]["approval_id"])["status"], "pending")
        await self.runtime.decide_approval(self.run["id"], value["steps"][0]["approval_id"], "default", True)
        await asyncio.wait_for(next(iter(self.runtime.tasks.values())), 4)
        self.assertEqual(len(self.calls), 1)

    async def test_short_chat_confirmation_uses_persisted_scope(self):
        value = self.plan()
        await self.tick(value)
        self.enter()
        answer = await self.runtime.resolve_approval("default", "可以")
        self.assertIn("原步骤", answer.message)
        await self.runtime.watch_plan(self.run["id"])
        self.assertEqual(self.calls[0][1], {"min_score": 85})

    async def test_multiple_pending_scopes_require_selection(self):
        value = self.plan()
        await self.tick(value)
        other, _ = self.store.create("default", "other", "chat", {"message": "另一个合成请求"})
        self.store.ensure_approval(other["id"], "default", "other", {"tool": "mock_delivery_start"})
        answer = await self.runtime.resolve_approval("default", "确认")
        self.assertIn("多个", answer.message)
        self.assertTrue(all(r["status"] == "pending" for r in self.store.approvals(session_id="default")))

    async def test_unrelated_plan_revision_preserves_manual_approval(self):
        value = self.plan()
        await self.tick(value)
        await self.runtime.decide_approval(self.run["id"], value["steps"][0]["approval_id"], "default", True)
        saved = self.store.get(self.run["id"])["checkpoint"]["plan"]
        saved.update(revision=2, reply="修改回复格式")
        self.store.save_plan(self.run["id"], saved)
        await self.runtime.watch_plan(self.run["id"])
        self.assertEqual(len(self.calls), 1)

    async def test_tool_loop_confirmation_uses_same_runner_and_does_not_execute_before_approval(self):
        self.enter()
        outcome = await self.registry.execute("mock_delivery_start", {"min_score": 85}, user_message="合成请求", state=ToolLoopState())
        self.assertEqual(outcome.status, "awaiting_approval")
        self.assertFalse(self.store.get(self.run["id"])["mutation_started"])
        self.runtime.save(self.run["id"], {"phase": "tool_loop", "messages": []})
        # A late model checkpoint cannot destroy the authoritative action plan.
        self.assertEqual(self.store.get(self.run["id"])["checkpoint"]["phase"], "action_plan")
        await self.runtime.decide_approval(self.run["id"], outcome.approval["id"], "default", True)
        await self.runtime.watch_plan(self.run["id"])
        self.assertEqual(len(self.calls), 1)

    async def test_unknown_risk_is_not_self_authorized_by_model_flag(self):
        spec = self.registry._tools["mock_delivery_start"]
        self.registry._tools["new_external"] = AgentToolSpec("new_external", "合成新工具", spec.parameters, "external", spec.executor)
        value = self.plan(requested=True)
        value["steps"][0]["tool"] = "new_external"
        self.store.save_plan(self.run["id"], value)
        await self.tick(value)
        self.assertEqual(value["steps"][0]["status"], "waiting_approval")
        self.assertEqual(self.calls, [])

    async def test_always_policy_confirms_even_explicit_request_but_stop_bypasses(self):
        self.registry.action_policy = ActionPolicy(AlwaysConfirm())
        value = self.plan(requested=True)
        await self.tick(value)
        self.assertEqual(value["steps"][0]["status"], "waiting_approval")
        self.enter()
        outcome = await self.registry.execute("browser_task_stop", {}, user_message="停止", state=ToolLoopState())
        self.assertEqual(outcome.status, "ok")

    async def test_plan_cas_ignores_stale_writes_and_reports_conflict(self):
        value = self.plan()
        newer = copy.deepcopy(value)
        newer["revision"] = 1
        self.assertTrue(self.store.save_plan(self.run["id"], newer, expected_revision=0))
        self.assertFalse(self.store.save_plan(self.run["id"], value))
        with self.assertRaises(ValueError): self.store.save_plan(self.run["id"], newer, expected_revision=0)

    async def test_encrypted_internal_scope_is_lossless(self):
        value = self.plan()
        value["steps"][0]["arguments"] = {"min_score": 85}
        await self.tick(value)
        self.assertEqual(self.store.approvals(self.run["id"])[0]["snapshot"]["arguments"]["min_score"], 85)
        self.assertNotIn("min_score", self.store.path.read_bytes().decode("latin1"))

    async def test_modify_pending_scope_invalidates_only_that_confirmation(self):
        value = self.plan(extra=True)
        await self.tick(value)
        old_id = value["steps"][0]["approval_id"]
        self.enter()
        await self.runtime.revise_plan(self.run["id"], {"plan_id": value["id"], "expected_revision": 0,
            "updates": [{"step_id": "delivery", "arguments": {"min_score": 90}, "user_requested": True}]}, "明确按90分以上投递")
        saved = self.store.get(self.run["id"])["checkpoint"]["plan"]
        self.assertEqual(self.store.approval(old_id)["status"], "invalidated")
        with self.assertRaises(ValueError):
            await self.runtime.decide_approval(self.run["id"], old_id, "default", True)
        await self.tick(saved)
        self.assertEqual(self.calls, [("resume_propose_patch", {"instruction": "补充合成项目描述"}),
                                     ("mock_delivery_start", {"min_score": 90})])

    async def test_explicit_followup_same_scope_resolves_pending_without_extra_question(self):
        value = self.plan()
        await self.tick(value)
        self.enter()
        await self.runtime.revise_plan(self.run["id"], {"plan_id": value["id"], "expected_revision": 0,
            "updates": [{"step_id": "delivery", "user_requested": True}]}, "明确按上述范围投递")
        await self.tick(self.store.get(self.run["id"])["checkpoint"]["plan"])
        self.assertEqual(len(self.calls), 1)

    async def test_recompute_analysis_keeps_old_receipt_and_does_not_replay_delivery(self):
        value = self.plan(requested=True, extra=True)
        value["steps"][1].update(tool="job_stats_read", arguments={}, effect="read")
        await self.tick(value)
        original = copy.deepcopy(value)
        updated = edited_plan(value, RevisionDraft(plan_id=value["id"], expected_revision=0,
            invalidated_steps=["proposal"]), self.registry, stamp=STAMP, session_id="default", message="刷新职位统计")
        self.assertEqual(updated["steps"][0], original["steps"][0])
        self.assertEqual(updated["steps"][1]["result"], original["steps"][1]["result"])
        self.assertEqual(updated["steps"][1]["status"], "completed")
        await self.tick(updated)
        self.assertEqual([c[0] for c in self.calls], ["mock_delivery_start", "job_stats_read", "job_stats_read"])
        with self.assertRaises(ValueError):
            edited_plan(updated, RevisionDraft(plan_id=value["id"], expected_revision=1, invalidated_steps=["delivery"]),
                self.registry, stamp=STAMP, session_id="default", message="刷新投递")

    async def test_scope_refresh_after_revision_binds_new_actions_to_new_evidence(self):
        value = self.plan(requested=True)
        value["steps"][0].update(status="completed", result={"queued": 1})
        updated = edited_plan(value, RevisionDraft(plan_id=value["id"], expected_revision=0, add_steps=[
            {"id": "update", "tool": "search_state_update", "arguments": {"salary_min": 20000}, "user_requested": True},
            {"id": "new_summary", "tool": "job_results_read", "arguments": {"summary": True}, "user_requested": True}]),
            self.registry, stamp=STAMP, session_id="default", message="更新条件并读取结果")
        summary = next(step for step in updated["steps"] if step["id"] == "new_summary")
        refresh = next(step for step in updated["steps"] if step["tool"] == "job_results_refilter")
        self.assertEqual(refresh["depends_on"], ["update"])
        self.assertEqual(summary["evidence_step"], refresh["id"])

    async def test_shutdown_keeps_approval_checkpoint_even_before_wait_status_is_published(self):
        value = self.plan()
        await self.tick(value)
        self.store.update(self.run["id"], status="running")
        self.runtime.tasks[self.run["id"]] = asyncio.create_task(asyncio.sleep(5))
        await self.runtime.shutdown()
        self.assertNotIn(self.store.get(self.run["id"])["status"], {"interrupted", "outcome_indeterminate"})
        self.assertEqual(self.store.approval(value["steps"][0]["approval_id"])["status"], "pending")

    async def test_terminal_task_child_recovery_keeps_pending_scope_then_continues_once(self):
        value = self.plan()
        await self.tick(value)
        source = self.store.get(self.run["id"])
        old_id = value["steps"][0]["approval_id"]
        self.store.update(source["id"], status="failed")
        child, _ = self.store.create("default", "continue", "chat", {"message": "继续任务"}, parent_id=source["id"])
        self.store.update(child["id"], status="running")
        self.run = child
        self.context.run_id = child["id"]
        self.context.dialogue = {"task_request": {"run_id": source["id"], "original": value["original"]}}
        self.enter()
        answer = await self.runtime.continue_task(child["id"], "继续任务")
        saved = self.store.get(child["id"])["checkpoint"]["plan"]
        self.assertEqual(saved["steps"][0]["status"], "waiting_approval", answer.message)
        self.assertEqual(self.store.approval(old_id)["status"], "invalidated")
        new_id = saved["steps"][0]["approval_id"]
        self.assertNotEqual(old_id, new_id)
        self.assertEqual(self.calls, [])
        await self.runtime.decide_approval(child["id"], new_id, "default", True)
        await self.runtime.watch_plan(child["id"])
        self.assertEqual(len(self.calls), 1)

    async def test_stop_invalidates_pending_approval_and_preserves_completed_receipts(self):
        value = self.plan(extra=True)
        await self.tick(value)
        await self.runtime._stop_bound_tasks(self.run["id"])
        saved = self.store.get(self.run["id"])
        self.assertEqual(saved["status"], "cancelled")
        self.assertEqual(self.store.approval(value["steps"][0]["approval_id"])["status"], "invalidated")
        self.assertEqual(saved["checkpoint"]["plan"]["steps"][1]["status"], "completed")
        with self.assertRaises(ValueError):
            await self.runtime.decide_approval(self.run["id"], value["steps"][0]["approval_id"], "default", True)
        self.assertEqual([c[0] for c in self.calls], ["resume_propose_patch"])

    async def test_generic_confirmation_does_not_choose_between_delivery_and_resume_proposal(self):
        value = self.plan()
        await self.tick(value)
        other, _ = self.store.create("default", "resume", "chat", {"message": "合成简历修改"})
        self.store.update(other["id"], status="waiting_confirmation", result={"resume_proposal": {"id": "synthetic"}})
        answer = await self.runtime.resolve_approval("default", "同意")
        self.assertIn("多个", answer.message)
        self.assertEqual(self.store.approval(value["steps"][0]["approval_id"])["status"], "pending")

    async def test_old_confirmation_refusal_becomes_durable_wait_without_self_approval(self):
        value = self.plan()
        value["steps"][0].update(status="blocked", failure={"code": "confirmation_required", "effect_started": False, "uncertain": False})
        source = {**self.store.get(self.run["id"]), "checkpoint": {"plan": value}}
        decision = recover_plan(source, session_id="default", stamp=STAMP)
        self.assertEqual(decision.action, "resume")
        await self.tick(decision.plan)
        self.assertEqual(decision.plan["steps"][0]["status"], "waiting_approval")
        self.assertEqual(self.calls, [])


def main():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(DurableApprovalContracts)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    output = Path("test-results/action-policy") / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True, exist_ok=True)
    report = {"passed": result.wasSuccessful(), "tests": result.testsRun, "failures": len(result.failures),
              "errors": len(result.errors), "coverage": "offline real gateway/SQLite/DAG; synthetic executors; no external model/browser",
              "failures_detail": [{"test": str(test), "traceback": error} for test,error in result.failures + result.errors], "traces": TRACES}
    (output / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "tests": result.testsRun, "passed": result.wasSuccessful()}))
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__": main()
