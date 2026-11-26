"""Real run/job-store controls under held execution locks, with synthetic workers.

The HTTP case imports the complete production application in a separate process
with a fresh data root and no lifespan. No browser, model, recruiter or existing
queue is touched. These are deterministic engineering contracts, not NLP evals.
"""
from __future__ import annotations

import asyncio
import copy
import gc
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
import uuid

from app.core.models import BrowserTaskView, DeliveryTaskView
from app.jobs.store import JobStore
from app.runtime.control import stop_scope
from app.runtime.manager import AgentRuntime
from app.runtime.store import RunStore, TERMINAL


def vacancy(index, title):
    return {"title": title, "company": "合成控制验收雇主", "location": "深圳", "salary": "20-30K",
        "description": "本岗位只用于隔离控制链路验收，不是真实招聘。", "tags": ["Python"],
        "source": "MockJobSite", "source_url": f"http://127.0.0.1:8080/job.html?id={index}",
        "external_id": str(index), "score": 88, "decision": "REVIEW", "reasoning": "合成证据", "stage": "review"}


class SyntheticBrowser:
    def __init__(self, jobs):
        self.jobs, self.current_id, self.worker, self.stops = jobs, None, None, 0

    def current(self):
        return self.jobs.get_task(self.current_id) if self.current_id else BrowserTaskView()

    async def stop(self):
        self.stops += 1
        if self.worker and not self.worker.done():
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        return self.jobs.update_task(self.current_id, status="stopped", detail="合成搜索已停止") if self.current_id else self.current()


class SyntheticDelivery:
    def __init__(self, jobs):
        self.jobs, self.current_id, self.worker, self.stops = jobs, None, None, 0

    def current(self):
        return self.jobs.get_delivery_run(self.current_id) if self.current_id else DeliveryTaskView()

    async def stop(self):
        self.stops += 1
        if not self.current_id:
            return self.current()
        current = self.current()
        status = "outcome_indeterminate" if current.status == "outcome_indeterminate" else "completed" if current.submitted else "cancelled"
        return self.jobs.update_delivery_run(self.current_id, status=status, detail="合成投递执行器已停止")


class SyntheticCoordinator:
    def __init__(self, jobs, *, gate=None, fail=False):
        self.jobs, self.gate, self.fail = jobs, gate, fail

    def settings(self):
        return self.jobs.mock_workflow_settings()

    async def cancel_delivery_queue(self, session_id):
        if self.fail:
            raise RuntimeError("合成队列清理失败")
        if self.gate is not None:
            await self.gate.wait()
        return {"cancelled_queue": 0}


class ControlHarness:
    def __init__(self, root):
        self.root = Path(root)
        self.jobs = JobStore(self.root / "jobs.db")
        self.store = RunStore(self.root / "runtime.db")
        self.browser = SyntheticBrowser(self.jobs)
        self.delivery = SyntheticDelivery(self.jobs)
        self.model_calls = 0

        async def forbidden(*args, **kwargs):
            self.model_calls += 1
            raise AssertionError("A control request must never enter model planning")

        self.agent = NS(chat=forbidden, tool_registry=NS(mock_workflow=None))
        self.runtime = AgentRuntime(self.store, self.agent, self.jobs, self.browser, NS())
        self.runtime.delivery = self.delivery

    def seed(self, session="default", *, suffix="main", offset=0):
        task_id = "synthetic-search-" + suffix
        self.jobs.create_task(task_id, "mock", "合成数据分析")
        self.jobs.save_search_request(task_id, {"conversation_id": session, "query": "合成数据分析", "max_jobs": 20})
        titles = [f"{suffix}已核对岗位A", f"{suffix}等待回复岗位B", f"{suffix}已核实岗位C", f"{suffix}结果未知岗位D"]
        ids = [self.jobs.upsert_job(vacancy(offset + index + 101, title))[0] for index, title in enumerate(titles)]
        for job_id in ids:
            self.jobs.event(task_id, "job_stored", "合成部分结果", sample={"job_id": job_id, "filter_status": "pass"})
        self.jobs.update_task(task_id, status="running", discovered=9, stored=4, matched=4)
        run, _ = self.store.create(session, "request-" + suffix, "chat", {"message": "找20份合成岗位并投递"})
        self.store.update(run["id"], status="running", checkpoint={"browser_id": task_id, "phase": "action_plan"})
        deliveries = []
        for index, status, submitted in ((1, "waiting_for_reply", False), (2, "completed", True), (3, "outcome_indeterminate", False)):
            delivery_id = f"synthetic-delivery-{suffix}-{index}"
            self.jobs.create_delivery_run(delivery_id, ids[index])
            self.jobs.update_delivery_run(delivery_id, status=status, submitted=submitted,
                checkpoint="waiting_for_reply" if index == 1 else "submitted" if submitted else "submission_unconfirmed",
                detail="合成投递记录")
            self.jobs.update_stage(ids[index], "applying" if index == 1 else "waiting" if submitted else "outcome_indeterminate")
            owner, _ = self.store.create(session, delivery_id, "delivery", {})
            self.store.update(owner["id"], status="waiting_reply" if index == 1 else "completed" if submitted else "outcome_indeterminate",
                checkpoint={"delivery_id": delivery_id}, mutation_started=1)
            deliveries.append(delivery_id)
        return {"run": self.store.get(run["id"]), "task_id": task_id, "titles": titles, "ids": ids, "deliveries": deliveries}

    async def start_worker(self, seeded):
        self.browser.current_id = seeded["task_id"]
        self.browser.worker = asyncio.create_task(asyncio.Event().wait())
        self.runtime.tasks[seeded["run"]["id"]] = self.browser.worker

    async def settle(self, run_id, seconds=2):
        async with asyncio.timeout(seconds):
            while self.store.get(run_id)["status"] not in TERMINAL:
                await asyncio.sleep(.01)
        return self.store.get(run_id)

    async def close(self):
        await self.runtime.shutdown()
        if self.browser.worker and not self.browser.worker.done():
            self.browser.worker.cancel()
            await asyncio.gather(self.browser.worker, return_exceptions=True)


class ControlCommandContracts(unittest.TestCase):
    def test_english_and_chinese_stop_aliases_are_control_commands(self):
        for message in ("stop", "STOP", "Stop for now", "先停止", "中断任务"):
            with self.subTest(message=message):
                self.assertEqual(stop_scope(message), "session")

    def test_mentions_questions_negation_and_quotes_are_not_stop_commands(self):
        for message in ("don't stop", "why did it stop", '"stop"', "stop signal engineer", "不要停止", "为什么停止", "‘停止任务’"):
            with self.subTest(message=message):
                self.assertIsNone(stop_scope(message))


class TaskControlContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="autohunt-control-", ignore_cleanup_errors=True)
        self.h = ControlHarness(self.temp.name)

    async def asyncTearDown(self):
        await self.h.close()
        self.assertEqual(self.h.model_calls, 0)
        self.h = None
        gc.collect()
        self.temp.cleanup()

    def assert_snapshot(self, payload, seeded):
        self.assertIsInstance(payload["snapshot"], dict)
        self.assertTrue(payload["snapshot_id"])
        self.assertTrue(payload["message_id"])
        self.assertEqual(payload["snapshot"]["counts"]["discovered"], 9)
        self.assertEqual(payload["snapshot"]["counts"]["reviewed"], 4)
        self.assertEqual(payload["snapshot"]["counts"]["submitted"], 1)
        self.assertEqual(payload["snapshot"]["counts"]["uncertain"], 1)
        message = payload.get("result_message", payload.get("message", ""))
        self.assertIn(seeded["titles"][0], message)
        self.assertIn(seeded["titles"][1], message)
        self.assertIn("1", message)
        self.assertRegex(message, r"核验|不确定|未知")
        return message

    async def test_progress_bypasses_held_session_lock_and_all_execution_slots(self):
        seeded = self.h.seed()
        await self.h.start_worker(seeded)
        lock = self.h.runtime.sessions.setdefault("default", asyncio.Lock())
        await lock.acquire()
        await self.h.runtime.slots.acquire()
        await self.h.runtime.slots.acquire()
        before = copy.deepcopy(self.h.store.get(seeded["run"]["id"]))
        try:
            started = time.monotonic()
            result = self.h.runtime.progress_result("default", "先给我当前结果")
            self.assertLess(time.monotonic() - started, 1)
            message = self.assert_snapshot(result, seeded)
            self.assertEqual(self.h.store.get(seeded["run"]["id"]), before)
            self.assertFalse(self.h.browser.worker.done())
            self.assertEqual(self.h.browser.stops, 0)
            # The result view must not turn the unknown submission into success.
            self.assertEqual(self.h.jobs.get_delivery_run(seeded["deliveries"][2]).status, "outcome_indeterminate")
            self.assertFalse(self.h.jobs.get_delivery_run(seeded["deliveries"][2]).submitted)
            history = self.h.jobs.conversation_history(conversation_id="default")
            self.assertTrue(any(row["role"] == "user" and row["content"] == "先给我当前结果" for row in history))
            self.assertTrue(any(row["role"] == "assistant" and row["content"] == message for row in history))
            self.assertEqual(self.h.store.get(result["run_id"])["status"], "completed")
        finally:
            lock.release()
            self.h.runtime.slots.release()
            self.h.runtime.slots.release()

    async def test_text_progress_uses_control_submit_and_does_not_cancel_search(self):
        seeded = self.h.seed()
        await self.h.start_worker(seeded)
        before = copy.deepcopy(self.h.store.get(seeded["run"]["id"]))
        for message in ("当前结果", "先给我当前结果", "现在找到了多少岗位"):
            with self.subTest(message=message):
                result = self.h.runtime.submit("default", uuid.uuid4().hex, "chat", {"message": message})
                finished = await self.h.settle(result["id"])
                self.assertEqual(finished["status"], "completed")
                self.assertIn(seeded["titles"][0], finished["result"]["message"])
        self.assertEqual(self.h.store.get(seeded["run"]["id"]), before)
        self.assertFalse(self.h.browser.worker.done())

    async def test_progress_is_scoped_and_unknown_is_never_success(self):
        seeded = self.h.seed()
        other = self.h.store.create_session("合成另一会话")["id"]
        foreign = self.h.seed(other, suffix="foreign-private", offset=1000)
        result = self.h.runtime.progress_result("default")
        message = self.assert_snapshot(result, seeded)
        self.assertNotIn("foreign-private", message)
        self.assertNotIn(foreign["task_id"], json.dumps(result["snapshot"]))
        self.assertIn(seeded["titles"][3], message)
        self.assertRegex(message, r"(?:已核实|已投递|投递成功)[^\n]*1|1[^\n]*(?:已核实|已投递|投递成功)")

    async def test_stop_commands_bypass_locks_stop_worker_and_persist_partial_results(self):
        for message in ("stop", "Stop for now", "先停止", "中断任务"):
            with self.subTest(message=message):
                self.assertEqual(stop_scope(message), "session")
                session = self.h.store.create_session("合成停止会话")["id"]
                seeded = self.h.seed(session, suffix=uuid.uuid4().hex[:6], offset=len(self.h.store.list()) * 20)
                await self.h.start_worker(seeded)
                lock = self.h.runtime.sessions.setdefault(session, asyncio.Lock())
                await lock.acquire()
                await self.h.runtime.slots.acquire()
                await self.h.runtime.slots.acquire()
                try:
                    control = self.h.runtime.submit(session, uuid.uuid4().hex, "chat", {"message": message})
                    finished = await self.h.settle(control["id"])
                    self.assertEqual(finished["status"], "completed")
                    self.assertTrue(self.h.browser.worker.done())
                    self.assertEqual(self.h.store.get(seeded["run"]["id"])["status"], "cancelled")
                    text = finished["result"]["message"]
                    self.assertIn(seeded["titles"][0], text)
                    self.assertRegex(text, r"等待|投递|沟通")
                    self.assertTrue(any(row["role"] == "assistant" and seeded["titles"][0] in row["content"]
                        for row in self.h.jobs.conversation_history(conversation_id=session)))
                finally:
                    lock.release()
                    self.h.runtime.slots.release()
                    self.h.runtime.slots.release()

    async def test_repeated_stop_and_status_reads_reuse_one_result_message(self):
        seeded = self.h.seed()
        await self.h.start_worker(seeded)
        first = await self.h.runtime.stop_tasks("default")
        self.assertEqual(first["status"], "stopped")
        self.assert_snapshot(first, seeded)
        current = self.h.runtime.stop_status("default")
        second = await self.h.runtime.stop_tasks("default")
        self.assertEqual(current["snapshot_id"], first["snapshot_id"])
        self.assertEqual(second["message_id"], first["message_id"])
        history = self.h.jobs.conversation_history(conversation_id="default")
        self.assertEqual(sum(row["role"] == "assistant" and row["content"] == first["result_message"] for row in history), 1)

    async def test_failed_cleanup_reports_failure_and_preserves_confirmed_partial_results(self):
        seeded = self.h.seed()
        await self.h.start_worker(seeded)
        self.h.agent.tool_registry.mock_workflow = SyntheticCoordinator(self.h.jobs, fail=True)
        result = await self.h.runtime.stop_tasks("default")
        if result["status"] == "stopping":
            await self.h.runtime.stop_jobs["default"]
            result = self.h.runtime.stop_status("default")
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["failed_runs"])
        message = self.assert_snapshot(result, seeded)
        self.assertIn("停止未确认", message)
        self.assertTrue(any(row["role"] == "assistant" and row["content"] == message
            for row in self.h.jobs.conversation_history(conversation_id="default")))
        self.assertEqual(self.h.jobs.get_delivery_run(seeded["deliveries"][1]).submitted, True)
        self.assertFalse(self.h.jobs.get_delivery_run(seeded["deliveries"][2]).submitted)

    async def test_inflight_cleanup_exposes_partial_then_final_result_without_duplicate_publication(self):
        seeded = self.h.seed()
        await self.h.start_worker(seeded)
        gate = asyncio.Event()
        self.h.agent.tool_registry.mock_workflow = SyntheticCoordinator(self.h.jobs, gate=gate)
        try:
            initial = await self.h.runtime.stop_tasks("default")
            self.assertEqual(initial["status"], "stopping")
            self.assertFalse(self.h.runtime.stop_jobs["default"].done())
            self.assertEqual(initial["snapshot"]["counts"]["submitted"], 1)
            self.assertEqual(initial["snapshot"]["counts"]["uncertain"], 1)
            self.assertIn(seeded["titles"][0], initial["result_message"])
            self.assertTrue(initial["snapshot_id"])
            self.assertFalse(initial["message_id"])
            self.assertFalse(any(row["role"] == "assistant" and row["content"] == initial["result_message"]
                for row in self.h.jobs.conversation_history(conversation_id="default")))
        finally:
            gate.set()
        final = await asyncio.wait_for(asyncio.shield(self.h.runtime.stop_jobs["default"]), 2)
        self.assertEqual(final["status"], "stopped")
        self.assert_snapshot(final, seeded)
        again = await self.h.runtime.stop_tasks("default")
        self.assertEqual(final["message_id"], again["message_id"])
        self.assertEqual(final["snapshot_id"], again["snapshot_id"])
        self.assertEqual(sum(row["role"] == "assistant" and row["content"] == final["result_message"]
            for row in self.h.jobs.conversation_history(conversation_id="default")), 1)

    async def test_empty_query_and_stop_have_zero_counts_and_query_request_id_is_idempotent(self):
        query = self.h.runtime.progress_result("default", request_id="same-synthetic-query")
        self.assertEqual(query["snapshot"]["jobs"], [])
        self.assertTrue(all(value == 0 for value in query["snapshot"]["counts"].values()))
        self.assertIn("尚无已保存的岗位结果", query["message"])
        duplicate = self.h.runtime.progress_result("default", request_id="same-synthetic-query")
        self.assertEqual(duplicate, query)
        self.assertEqual(len(self.h.store.list("default")), 1)
        history = self.h.jobs.conversation_history(conversation_id="default")
        self.assertEqual(len(history), 2)
        stopped = await self.h.runtime.stop_tasks("default")
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(stopped["snapshot"]["jobs"], [])
        self.assertTrue(all(value == 0 for value in stopped["snapshot"]["counts"].values()))
        self.assertIn("尚无已保存的岗位结果", stopped["result_message"])
        repeated = await self.h.runtime.stop_tasks("default")
        self.assertEqual(repeated["message_id"], stopped["message_id"])

    async def test_new_completed_search_invalidates_old_stopped_result_cache(self):
        first_round = self.h.seed(suffix="old-round")
        await self.h.start_worker(first_round)
        first = await self.h.runtime.stop_tasks("default")
        if first["status"] == "stopping":
            first = await self.h.runtime.stop_jobs["default"]
        self.assertEqual(first["status"], "stopped")
        self.assertIn(first_round["titles"][0], first["result_message"])
        task_id, title = "synthetic-new-finished-search", "new-round独立已完成岗位"
        job_id = self.h.jobs.upsert_job(vacancy(9001, title))[0]
        self.h.jobs.create_task(task_id, "mock", "合成新搜索")
        self.h.jobs.save_search_request(task_id, {"conversation_id": "default", "query": "合成新搜索", "max_jobs": 1})
        self.h.jobs.event(task_id, "job_stored", "合成新结果", sample={"job_id": job_id, "filter_status": "pass"})
        self.h.jobs.update_task(task_id, status="completed", discovered=1, stored=1, matched=1)
        newest, _ = self.h.store.create("default", "independent-new-completed-search", "chat", {"message": "仅搜索一个新岗位"})
        self.h.store.update(newest["id"], status="completed", checkpoint={"browser_id": task_id})
        second = await self.h.runtime.stop_tasks("default")
        if second["status"] == "stopping":
            second = await self.h.runtime.stop_jobs["default"]
        self.assertEqual(second["status"], "stopped")
        self.assertIn(title, second["result_message"])
        self.assertNotIn("old-round", second["result_message"])
        self.assertEqual(second["snapshot"]["counts"]["reviewed"], 1)
        self.assertEqual(second["snapshot"]["counts"]["submitted"], 0)
        self.assertNotEqual(second["message_id"], first["message_id"])
        self.assertNotEqual(second["snapshot_id"], first["snapshot_id"])

    async def test_global_stop_result_cache_and_message_identity_are_bound_to_requesting_session(self):
        seeded = self.h.seed()
        await self.h.start_worker(seeded)
        other = self.h.store.create_session("合成全局停止请求会话B")["id"]
        first = await self.h.runtime.stop_tasks(None, conversation_id="default")
        if first["status"] == "stopping":
            first = await self.h.runtime.stop_jobs["*"]
        self.assertEqual(first["status"], "stopped")
        self.assertEqual(first["conversation_id"], "default")
        second = await self.h.runtime.stop_tasks(None, conversation_id=other)
        if second["status"] == "stopping":
            second = await self.h.runtime.stop_jobs["*"]
        self.assertEqual(second["status"], "stopped")
        self.assertEqual(second["conversation_id"], other)
        self.assertNotEqual(first["message_id"], second["message_id"])
        a_history = self.h.jobs.conversation_history(conversation_id="default")
        b_history = self.h.jobs.conversation_history(conversation_id=other)
        self.assertTrue(any(row["role"] == "assistant" and row["response"]["message_id"] == first["message_id"] for row in a_history))
        self.assertTrue(any(row["role"] == "assistant" and row["response"]["message_id"] == second["message_id"] for row in b_history))
        self.assertFalse(any((row.get("response") or {}).get("message_id") == second["message_id"] for row in a_history))
        repeated = await self.h.runtime.stop_tasks(None, conversation_id=other)
        self.assertEqual(repeated["message_id"], second["message_id"])

    async def test_restart_recovers_pending_controls_without_model_planning_or_duplicate_results(self):
        for status in ("queued", "running"):
            with self.subTest(persisted_status=status):
                seeded = self.h.seed(suffix="restart-" + status, offset=len(self.h.store.list()) * 20)
                await self.h.start_worker(seeded)
                stop, _ = self.h.store.create("default", "restart-stop-" + status, "chat",
                    {"message": "Stop for now", "control_kind": "stop"}, control=True)
                progress, _ = self.h.store.create("default", "restart-progress-" + status, "chat",
                    {"message": "当前结果", "control_kind": "progress"}, control=True)
                if status == "running":
                    for control in (stop, progress):
                        self.h.store.update(control["id"], status=status,
                            checkpoint={"phase": "control_result", "control_kind": control["payload"]["control_kind"]})
                self.h.store = RunStore(self.h.root / "runtime.db")
                self.h.runtime = AgentRuntime(self.h.store, self.h.agent, self.h.jobs, self.h.browser, NS())
                self.h.runtime.delivery = self.h.delivery
                await self.h.runtime.start()
                for control in (stop, progress):
                    recovered = self.h.store.get(control["id"])
                    self.assertEqual(recovered["status"], "completed", recovered)
                    self.assertIn(seeded["titles"][0], recovered["result"]["message"])
                    self.assertEqual(recovered["result"]["snapshot"]["counts"]["submitted"], 1)
                    self.assertEqual(recovered["result"]["snapshot"]["counts"]["uncertain"], 1)
                self.assertTrue(self.h.browser.worker.done())
                self.assertEqual(self.h.store.get(seeded["run"]["id"])["status"], "cancelled")
                history_count = len(self.h.jobs.conversation_history(conversation_id="default"))
                await self.h.runtime.start()
                self.assertEqual(len(self.h.jobs.conversation_history(conversation_id="default")), history_count)
                self.assertEqual(self.h.model_calls, 0)

    async def test_independent_daily_cancel_publishes_snapshot_and_retains_unknown_outcome(self):
        from app.workflow.daily import DailyWorkflow
        seeded = self.h.seed()
        # A daily run owns these checkpoints itself; it does not create the
        # chat/delivery watcher runs used by the ordinary-control fixture.
        # Retain those rows as terminal history rather than manufacturing a
        # second live execution owner of the same synthetic search.
        for previous in self.h.store.list("default"):
            if previous["status"] not in TERMINAL:
                self.h.store.update(previous["id"], status="completed")
        await self.h.start_worker(seeded)
        daily = DailyWorkflow(self.h.jobs, self.h.store, self.h.browser, self.h.delivery, NS(), NS())
        run, _ = self.h.store.create("default", "synthetic-daily-control", "daily", {"trigger": "manual"})
        phases = ("pending", "waiting_reply", "submitted", "uncertain")
        daily_jobs = {str(job_id): {"phase": phase} for job_id, phase in zip(seeded["ids"], phases)}
        for job_id, delivery_id in zip(seeded["ids"][1:], seeded["deliveries"]):
            daily_jobs[str(job_id)]["delivery_id"] = delivery_id
        self.h.store.update(run["id"], status="running", checkpoint={"phase": "daily", "browser_id": seeded["task_id"],
            "selected": seeded["ids"], "jobs": daily_jobs})
        daily.active_id, daily.worker = run["id"], self.h.browser.worker
        final = await daily.cancel(run["id"])
        self.assertEqual(final["status"], "outcome_indeterminate")
        self.assertEqual(final["result"]["snapshot"]["counts"]["submitted"], 1)
        self.assertEqual(final["result"]["snapshot"]["counts"]["uncertain"], 1)
        self.assertEqual(final["result"]["snapshot"]["counts"]["reviewed"], 4)
        self.assertEqual(final["result"]["snapshot"]["status"], "uncertain")
        message = final["result"]["message"]
        self.assertIn(seeded["titles"][0], message)
        self.assertIn("用户停止每日任务", message)
        self.assertTrue(self.h.browser.worker.done())
        repeated = await daily.cancel(run["id"])
        self.assertEqual(repeated["result"]["message_id"], final["result"]["message_id"])
        self.assertEqual(sum(row["role"] == "assistant" and row["content"] == message
            for row in self.h.jobs.conversation_history(conversation_id="default")), 1)

    async def test_legacy_unmarked_stop_is_recovered_as_control_not_model_work(self):
        seeded = self.h.seed(suffix="legacy-stop")
        await self.h.start_worker(seeded)
        stop, _ = self.h.store.create("default", "legacy-stop-control", "chat", {"message": "停止任务"}, control=True)
        await self.h.runtime.start()
        recovered = self.h.store.get(stop["id"])
        self.assertEqual(recovered["status"], "completed")
        self.assertIn(seeded["titles"][0], recovered["result"]["message"])
        self.assertTrue(self.h.browser.worker.done())
        self.assertEqual(self.h.model_calls, 0)

    async def test_recovery_query_read_failure_does_not_prevent_runtime_startup(self):
        from unittest.mock import patch
        query, _ = self.h.store.create("default", "broken-recovered-query", "chat",
            {"message": "当前结果", "control_kind": "progress"}, control=True)
        with patch.object(self.h.runtime, "task_snapshot", side_effect=ValueError("synthetic snapshot unavailable")):
            await self.h.runtime.start()
        recovered = self.h.store.get(query["id"])
        self.assertEqual(recovered["status"], "failed")
        self.assertIn("读取失败", recovered["error"])
        self.assertEqual(self.h.model_calls, 0)


async def http_contract():
    """Executed only in a sanitized child process using a temporary data root."""
    import httpx
    from app.api import application as api

    h = ControlHarness(Path(os.environ["AUTOHUNT_DATA_ROOT"]) / "control-fixture")
    api.agent_runtime, api.run_store, api.job_store = h.runtime, h.store, h.jobs
    seeded = h.seed()
    await h.start_worker(seeded)
    other = h.store.create_session("合成其他会话")["id"]
    h.seed(other, suffix="foreign-private", offset=1000)
    headers = {"X-AutoHunt-Key": "synthetic-task-control-key"}
    checks = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://testserver") as client:
        try:
            assert (await client.post("/api/v1/runs/control/results", json={})).status_code == 401
            checks.append("results_require_authentication")
            assert (await client.post("/api/v1/runs/control/results", headers=headers,
                json={"conversation_id": "missing", "message": "当前结果"})).status_code == 404
            lock = h.runtime.sessions.setdefault("default", asyncio.Lock())
            await lock.acquire()
            await h.runtime.slots.acquire()
            await h.runtime.slots.acquire()
            before = copy.deepcopy(h.store.get(seeded["run"]["id"]))
            try:
                response = await asyncio.wait_for(client.post("/api/v1/runs/control/results", headers=headers,
                    json={"conversation_id": "default", "message": "当前结果"}), timeout=1)
            finally:
                lock.release()
                h.runtime.slots.release()
                h.runtime.slots.release()
            assert response.status_code == 200, response.text
            result = response.json()
            assert all(key in result for key in ("message", "snapshot_id", "snapshot", "run_id", "message_id")), result
            assert seeded["titles"][0] in result["message"] and "foreign-private" not in result["message"]
            assert h.store.get(seeded["run"]["id"]) == before and not h.browser.worker.done()
            checks.append("results_api_bypasses_busy_execution_without_mutation")
            stopped = await client.post("/api/v1/runs/control/stop", headers=headers,
                json={"conversation_id": "default", "scope": "session"})
            assert stopped.status_code == 200, stopped.text
            payload = stopped.json()
            assert payload["status"] == "stopped", payload
            assert seeded["titles"][0] in payload["result_message"], payload
            assert h.browser.worker.done()
            checks.append("button_stop_returns_persisted_partial_result")
            repeated = (await client.post("/api/v1/runs/control/stop", headers=headers,
                json={"conversation_id": "default", "scope": "session"})).json()
            assert repeated["message_id"] == payload["message_id"]
            checks.append("repeated_button_stop_does_not_duplicate_result")
            assert h.model_calls == 0
            assert all(r["status"] == "running" or r["status"] in TERMINAL or r["status"] == "waiting_reply"
                for r in h.store.list(other))
            assert h.store.get(next(r for r in h.store.list(other) if r["kind"] == "chat")["id"])["status"] == "running"
            checks.append("other_session_untouched_and_zero_model_calls")
            slow = h.seed(suffix="slow-cleanup", offset=2000)
            await h.start_worker(slow)
            gate = asyncio.Event()
            h.agent.tool_registry.mock_workflow = SyntheticCoordinator(h.jobs, gate=gate)
            try:
                interim = await client.post("/api/v1/runs/control/stop", headers=headers,
                    json={"conversation_id": "default", "scope": "session"})
                assert interim.status_code == 200
                partial = interim.json()
                assert partial["status"] == "stopping", partial
                assert partial["snapshot"]["counts"]["submitted"] == 1
                assert partial["snapshot"]["counts"]["uncertain"] == 1
                assert slow["titles"][0] in partial["result_message"]
                assert not h.runtime.stop_jobs["default"].done()
                checks.append("slow_post_stop_returns_partial_before_cleanup_finishes")
            finally:
                gate.set()
            await asyncio.wait_for(asyncio.shield(h.runtime.stop_jobs["default"]), 2)
            observed = (await client.get("/api/v1/runs/control/stop", headers=headers,
                params={"conversation_id": "default", "scope": "session"})).json()
            assert observed["status"] == "stopped" and observed["message_id"]
            repeated = (await client.get("/api/v1/runs/control/stop", headers=headers,
                params={"conversation_id": "default", "scope": "session"})).json()
            assert (observed["snapshot_id"], observed["message_id"]) == (repeated["snapshot_id"], repeated["message_id"])
            checks.append("stop_status_poll_returns_final_with_stable_snapshot_and_message")
            return {"passed": True, "checks": checks, "model_calls": h.model_calls, "real_business_actions": 0}
        finally:
            await h.close()


class TaskControlHTTPContracts(unittest.TestCase):
    def test_complete_authenticated_api_in_isolated_subprocess(self):
        with tempfile.TemporaryDirectory(prefix="autohunt-control-http-", ignore_cleanup_errors=True) as root:
            from cryptography.fernet import Fernet
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith("AUTOHUNT_") and not key.endswith(("_API_KEY", "_TOKEN"))}
            env.update(AUTOHUNT_DATA_ROOT=root, AUTOHUNT_DATA_KEY=Fernet.generate_key().decode(),
                AUTOHUNT_API_KEY="synthetic-task-control-key", AUTOHUNT_LLM_PROVIDER="mock",
                AUTOHUNT_LLM_MODEL="mock-autohunt-v1", PYTHONUTF8="1")
            script = "import asyncio,json; from evaluation.task_control_contract import http_contract; print(json.dumps(asyncio.run(http_contract())))"
            result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr[-6000:] + result.stdout[-2000:])
            self.assertTrue(json.loads(result.stdout.strip().splitlines()[-1])["passed"])


if __name__ == "__main__":
    unittest.main()
