from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.core.models import ChatResponse
from app.jobs.store import JobStore
from app.runtime.context import checkpoint, emit
from app.runtime.manager import AgentRuntime
from app.runtime.store import RunStore, TERMINAL


class Agent:
    def __init__(self):
        self.seen = []
        self.history = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    def restore_history(self, history):
        self.history = history

    async def handle(self, message):
        self.seen.append((message, [item["content"] for item in self.history]))
        if message in {"slow", "write"}:
            if message == "write":
                emit("tool_started", tool="preference_update", effect="write")
            checkpoint({"phase": "tool_loop", "step": 0})
            self.entered.set()
            await self.release.wait()
        return ChatResponse(message=f"回答：{message}", lane="candidate")


def make_runtime(tmp_path):
    store = RunStore(tmp_path / "runtime.db")
    jobs = JobStore(tmp_path / "jobs.db")
    agent = Agent()
    runtime = AgentRuntime(store, agent, jobs, SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
    return runtime, agent


async def terminal(runtime, run_id):
    async with asyncio.timeout(3):
        while runtime.store.get(run_id)["status"] not in TERMINAL:
            await asyncio.sleep(.01)
    return runtime.store.get(run_id)


@pytest.mark.asyncio
async def test_sessions_are_isolated_and_same_session_requests_are_ordered(tmp_path):
    runtime, agent = make_runtime(tmp_path)
    other = runtime.store.create_session("另一个会话")["id"]
    first = runtime.submit("default", "one", "chat", {"message": "slow"})
    await agent.entered.wait()
    second = runtime.submit("default", "two", "chat", {"message": "second"})
    independent = runtime.submit(other, "three", "chat", {"message": "independent"})
    await terminal(runtime, independent["id"])
    assert runtime.store.get(second["id"])["status"] == "queued"
    agent.release.set()
    await terminal(runtime, first["id"])
    await terminal(runtime, second["id"])
    assert agent.seen[1] == ("independent", [])
    assert agent.seen[2][1] == ["slow", "回答：slow"]
    assert len(runtime.jobs.conversation_history(conversation_id=other)) == 2


@pytest.mark.asyncio
async def test_idempotency_and_event_replay_do_not_execute_twice(tmp_path):
    runtime, agent = make_runtime(tmp_path)
    first = runtime.submit("default", "same", "chat", {"message": "hello"})
    again = runtime.submit("default", "same", "chat", {"message": "hello"})
    assert first["id"] == again["id"]
    with pytest.raises(ValueError):
        runtime.submit("default", "same", "chat", {"message": "different"})
    await terminal(runtime, first["id"])
    assert len(agent.seen) == 1
    frames = [frame async for frame in runtime.stream(first["id"])]
    last = runtime.store.events(first["id"])[-1]["seq"]
    assert any("event: response" in frame for frame in frames)
    assert [frame async for frame in runtime.stream(first["id"], last)] == []
    assert len(agent.seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("message,expected", [("slow", "cancelled"), ("write", "outcome_indeterminate")])
async def test_cancel_reaches_worker_and_preserves_uncertain_write(tmp_path, message, expected):
    runtime, agent = make_runtime(tmp_path)
    run = runtime.submit("default", "one", "chat", {"message": message})
    await agent.entered.wait()
    result = await runtime.cancel(run["id"])
    assert result["status"] == expected
    assert result["cancel_requested"] == 1
    assert result["result"] is None
    assert len(runtime.jobs.conversation_history()) == 1
    assert await runtime.cancel(run["id"]) == result


@pytest.mark.asyncio
async def test_restart_requeues_only_unstarted_runs_and_does_not_replay_writes(tmp_path):
    runtime, agent = make_runtime(tmp_path)
    queued, _ = runtime.store.create("default", "queued", "chat", {"message": "hello"})
    lost, _ = runtime.store.create("default", "lost", "chat", {"message": "write"})
    runtime.store.update(lost["id"], status="running", mutation_started=1)
    await runtime.start()
    await terminal(runtime, queued["id"])
    assert runtime.store.get(lost["id"])["status"] == "outcome_indeterminate"
    assert agent.seen == [("hello", [])]
    with pytest.raises(ValueError):
        await runtime.resume(lost["id"])


@pytest.mark.asyncio
async def test_shutdown_preserves_read_checkpoint_and_resume_is_idempotent(tmp_path):
    runtime, agent = make_runtime(tmp_path)
    run = runtime.submit("default", "read", "chat", {"message": "slow"})
    await agent.entered.wait()
    await runtime.shutdown()
    assert runtime.store.get(run["id"])["status"] == "interrupted"
    agent.release.set()
    child = await runtime.resume(run["id"])
    same = await runtime.resume(run["id"])
    assert child["id"] == same["id"]
    await terminal(runtime, child["id"])
    assert child["parent_id"] == run["id"]
    assert len(runtime.jobs.conversation_history()) == 2


def test_store_redacts_persistent_events_and_terminal_status_cannot_change(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    run, _ = runtime.store.create("default", "privacy", "chat", {"message": "mail me secret@example.com"})
    runtime.store.event(run["id"], "tool_started", {"arguments": {"email": "secret@example.com"}})
    assert "secret@example.com" not in json.dumps(runtime.store.get(run["id"]))
    assert "secret@example.com" not in json.dumps(runtime.store.events(run["id"]))
    runtime.store.update(run["id"], status="cancelled")
    assert runtime.store.update(run["id"], status="running")["status"] == "cancelled"


def test_checkpoint_keeps_tool_identifiers_but_redacts_text(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    run, _ = runtime.store.create("default", "ids", "chat", {"message": "hello"})
    runtime.save(run["id"], {"messages": [{"role": "tool", "tool_call_id": "call_1234567890",
                                          "content": "contact secret@example.com", "reasoning_content": "private thinking"}],
                              "browser_id_unused": "not bound"})
    saved = runtime.store.get(run["id"])["checkpoint"]["messages"][0]
    assert saved["tool_call_id"] == "call_1234567890"
    assert "secret@example.com" not in saved["content"]
    assert "reasoning_content" not in saved


@pytest.mark.asyncio
async def test_deadline_cancels_model_and_keeps_read_checkpoint(tmp_path):
    runtime, agent = make_runtime(tmp_path)
    runtime.chat_timeout = .03
    run = runtime.submit("default", "deadline", "chat", {"message": "slow"})
    result = await terminal(runtime, run["id"])
    assert result["status"] == "timed_out"
    assert result["checkpoint"]["phase"] == "tool_loop"
    assert result["result"] is None


@pytest.mark.asyncio
async def test_queued_deadline_never_executes_request(tmp_path):
    runtime, agent = make_runtime(tmp_path)
    runtime.queue_timeout = .03
    first = runtime.submit("default", "first", "chat", {"message": "slow"})
    await agent.entered.wait()
    second = runtime.submit("default", "second", "chat", {"message": "must not execute"})
    assert (await terminal(runtime, second["id"]))["status"] == "timed_out"
    assert len(agent.seen) == 1
    await runtime.cancel(first["id"])


@pytest.mark.asyncio
async def test_browser_handoff_and_cancel_preserve_partial_report(tmp_path):
    runtime, _ = make_runtime(tmp_path)
    jobs = runtime.jobs
    jobs.create_task("browser-one", "mock", "金融风控")
    jobs.update_task("browser-one", status="waiting_for_user", discovered=3, stored=0)
    class Browser:
        def current(self):
            return jobs.get_task("browser-one")
        async def resume(self):
            return jobs.update_task("browser-one", status="running")
        async def stop(self):
            return jobs.update_task("browser-one", status="stopped")
    runtime.browser = Browser()
    run, _ = runtime.store.create("default", "external", "chat", {"message": "找工作"})
    runtime.store.update(run["id"], status="running", result={"message": "已开始搜索"}, checkpoint={"browser_id": "browser-one"}, mutation_started=1)
    runtime.schedule(run["id"], watch_only=True)
    async with asyncio.timeout(2):
        while runtime.store.get(run["id"])["status"] != "waiting_user":
            await asyncio.sleep(.01)
    await runtime.resume(run["id"])
    assert jobs.get_task("browser-one").status == "running"
    result = await runtime.cancel(run["id"])
    assert result["status"] == "cancelled"
    assert jobs.get_task("browser-one").status == "stopped"
    assert "发现候选链接 3 个" in runtime.jobs.conversation_history()[-1]["content"]
    assert "已记录并核对 0 个" in runtime.jobs.conversation_history()[-1]["content"]


@pytest.mark.asyncio
async def test_confirmation_is_restored_without_regenerating_proposal(tmp_path):
    runtime, agent = make_runtime(tmp_path)
    proposal = SimpleNamespace(status="pending")
    runtime.resume_workspace = SimpleNamespace(read_proposal=lambda _id: proposal)
    run, _ = runtime.store.create("default", "proposal", "chat", {"message": "修改简历"})
    runtime.store.update(run["id"], status="waiting_confirmation", mutation_started=1,
                         result={"message": "请审核", "resume_proposal": {"id": "proposal-one"}})
    await runtime.start()
    await asyncio.sleep(.02)
    assert agent.seen == []
    assert runtime.store.get(run["id"])["status"] == "waiting_confirmation"
    proposal.status = "approved"
    assert (await terminal(runtime, run["id"]))["status"] == "completed"


@pytest.mark.asyncio
async def test_restart_after_result_does_not_duplicate_assistant_message(tmp_path):
    runtime, agent = make_runtime(tmp_path)
    run, _ = runtime.store.create("default", "done", "chat", {"message": "hello"})
    runtime.store.update(run["id"], status="running", result={"message": "answer"})
    runtime.jobs.add_conversation_message("assistant", "answer", run_id=run["id"])
    await runtime.start()
    assert (await terminal(runtime, run["id"]))["status"] == "completed"
    assert len(runtime.jobs.conversation_history()) == 1
    assert agent.seen == []
