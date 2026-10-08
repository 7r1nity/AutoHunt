from __future__ import annotations

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.runtime.dialogue import (apply_operation, resolve_reference, pending_action,
    compact_state, bounded_data, compact_model_messages, slot_answer_patch)
from app.runtime.store import RunStore
from app.runtime.context import RunContext, current_run, scoped_browser
from app.runtime.search_state import prepare_search_state
from app.runtime.manager import AgentRuntime
from app.core.models import BrowserTaskView, CandidateProfileView
from app.core.agent_tools import ToolLoopState
from app.llm.base import LLMToolResult
from test_agent_tool_loop import make_agent, ScriptedLLM, tool_result


def op(state, name, clock=1000, **data):
    return apply_operation(state, {"op": name, "source": "run-1", **data}, clock=clock)


def snapshot_state(count=6, **kwargs):
    state = op(None, "begin", message="查看职位结果")
    return op(state, "snapshot", kind="job", topic="applications",
        items=[{"id": i, "title": f"职位{i}", "company": "同名公司"} for i in range(1, count + 1)], **kwargs)


@pytest.mark.parametrize("selector,ids", [("前五个", [1, 2, 3, 4, 5]), ("第2个", [2]), ("这些", [1, 2, 3, 4, 5, 6]), ("职位3", [3])])
def test_reference_exact_order(selector, ids):
    result = resolve_reference(snapshot_state(), kind="job", selector=selector, clock=1001)
    assert [item["id"] for item in result["items"]] == ids
    assert result["source"] == "run-1"


@pytest.mark.parametrize("selector", ["它", "那个", "前七个", "同名公司", "不存在"])
def test_ambiguous_or_out_of_range_never_guesses(selector):
    with pytest.raises(ValueError):
        resolve_reference(snapshot_state(), kind="job", selector=selector, clock=1001)


def test_unpresented_observation_cannot_be_reviewed():
    state = snapshot_state(displayed=False)
    with pytest.raises(ValueError):
        resolve_reference(state, kind="job", selector="前五个", clock=1001)
    state = op(state, "display", snapshot_id=state["snapshots"][-1]["id"])
    assert len(resolve_reference(state, kind="job", selector="前五个", clock=1001)["items"]) == 5


def test_topic_switch_turn_decay_and_ttl():
    state = snapshot_state()
    switched = op(state, "begin", message="修改简历")
    with pytest.raises(ValueError):
        resolve_reference(switched, kind="job", selector="前五个", clock=1001)
    for _ in range(7):
        state = op(state, "begin", message="审核职位")
    with pytest.raises(ValueError):
        resolve_reference(state, kind="job", selector="前五个", clock=1001)
    with pytest.raises(ValueError):
        resolve_reference(snapshot_state(), kind="job", selector="前五个", clock=3000)


def proposal_state():
    state = op(None, "begin", message="建议搜索方案")
    return op(state, "propose", topic="search", tool="job_search_start", arguments={"queries": ["护士"]},
        description="搜索护理岗位", conditions={"revision": 2, "resume_stamp": "resume-v2"})


def test_claim_one_use_and_refusal_trace():
    state = proposal_state()
    item = pending_action(state, clock=1001)
    conditions = {"revision": 2, "resume_stamp": "resume-v2"}
    state = op(state, "decision", action_id=item["id"], decision="claim", conditions=conditions)
    with pytest.raises(ValueError):
        op(state, "decision", action_id=item["id"], decision="claim", conditions=conditions)
    state = op(state, "settle", action_id=item["id"], status="outcome_indeterminate")
    assert state["pending"][-1]["status"] == "consumed"
    with pytest.raises(ValueError):
        pending_action(state, clock=1001)
    rejected = proposal_state()
    rejected = op(rejected, "decision", action_id=rejected["pending"][-1]["id"], decision="reject")
    assert rejected["pending"][-1]["status"] == "rejected"
    assert rejected["trace"][-1]["kind"] == "rejection"


@pytest.mark.parametrize("change", [{"revision": 3, "resume_stamp": "resume-v2"}, {"revision": 2, "resume_stamp": "resume-v3"}])
def test_confirmation_stale_dependencies_rejected(change):
    state = proposal_state()
    with pytest.raises(ValueError):
        op(state, "decision", action_id=state["pending"][-1]["id"], decision="claim", conditions=change)


def test_pending_expires_topic_changes_and_tool_authority_stays_narrow():
    state = proposal_state()
    with pytest.raises(ValueError):
        pending_action(state, clock=1601)
    state = op(state, "begin", message="讨论项目经历")
    with pytest.raises(ValueError):
        pending_action(state, clock=1001)
    with pytest.raises(ValueError):
        op(None, "propose", topic="applications", tool="submit_resume", arguments={}, description="投递")


def test_idle_does_not_clear_context_but_explicit_reset_still_can():
    state = snapshot_state()
    state = op(state, "task", kind="browser_id", id="browser-1", status="running")
    state = op(state, "begin", clock=1901, message="帮我找工作")
    assert state["snapshots"] and not state["pending"]
    assert state["tasks"]["browser_id"] == "browser-1"
    assert state["history_epoch"] == 0
    reset = op(state, "reset", clock=1902)
    assert reset["active_intent"] == "general" and reset["tasks"] == state["tasks"]


def test_old_background_watch_cannot_steal_new_task_binding():
    state = op(None, "task", kind="browser_id", id="old", status="running", bind=True)
    state = op(state, "task", kind="browser_id", id="new", status="running", bind=True)
    state = op(state, "task", kind="browser_id", id="old", status="completed")
    assert state["tasks"]["browser_id"] == "new"


def test_slots_provenance_question_and_short_answer():
    state = op(None, "slots", conditions={"queries": ["护士"], "locations": ["杭州"]})
    state = op(state, "question", topic="search", question="需要多少个岗位？", missing_slots=["result_limit"])
    state = op(state, "begin", message="15个")
    assert state["active_intent"] == "search"
    assert slot_answer_patch(state, "15个", clock=1001) == {"result_limit": 15}
    assert slot_answer_patch(state, "15个再帮我投递", clock=1001) is None
    assert slot_answer_patch(state, "15个", clock=1601) is None
    assert state["frames"]["search"]["slot_sources"]["queries"]["origin"] == "resume"


def test_bounded_extract_summary_only_observed_facts_with_recency():
    state = None
    for index in range(70):
        state = op(state, "begin", clock=1000+index, message="搜索岗位")
        state = op(state, "tool", clock=1000+index, tool="search_state_update", status="ok", detail=f"turn-{index}")
    assert len(state["trace"]) <= 32 and len(state["summary"]) <= 24
    assert state["summary"] and all(item["kind"] != "request" for item in state["summary"])
    view = compact_state(state)
    assert view["trace"][0]["recency_weight"] <= view["trace"][-1]["recency_weight"]


def test_model_budget_keeps_native_protocol_ids_json_and_status():
    messages = [{"role": "user", "content": "old"*10000}, {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "<context_data>current request"},
        {"role": "assistant", "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "job_results_read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call-1", "content": json.dumps({"status": "ok", "tool": "job_results_read", "result": "x"*50000})}]
    compact = compact_model_messages(messages, budget=6000)
    assert len(json.dumps(compact, ensure_ascii=False)) <= 6000
    assert compact[-1]["tool_call_id"] == compact[-2]["tool_calls"][0]["id"]
    assert json.loads(compact[-1]["content"])["status"] == "ok"
    assert len(messages[-1]["content"]) > 50000
    with pytest.raises(ValueError):
        compact_model_messages([{"role": "user", "content": "<context_data>" + "x"*9000}], budget=1000)


def test_redaction_preserves_object_ids_not_private_contacts():
    identity = "12345678901a12345678901a12345678901"
    text = bounded_data({"snapshot_id": identity, "content": "13812345678 example@example.com sk-test-example-key"}, budget=1000)
    assert identity in text and "13812345678" not in text and "example@example.com" not in text


def test_context_persistence_atomic_updates_isolation_and_crash_claim(tmp_path):
    store = RunStore(tmp_path / "runs.db")
    second = store.create_session("other")["id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda n: store.mutate_dialogue("default", {"op": "tool", "source": str(n), "tool": "read", "status": "ok"}), range(12)))
    assert store.dialogue_state("default")["revision"] == 12
    assert store.dialogue_state(second)["revision"] == 0
    state = store.mutate_dialogue("default", {"op": "propose", "topic": "search", "tool": "job_search_start", "arguments": {}, "description": "搜索", "conditions": {}})
    store.mutate_dialogue("default", {"op": "decision", "action_id": state["pending"][-1]["id"], "decision": "claim", "conditions": {}})
    with pytest.raises(ValueError):
        pending_action(RunStore(tmp_path / "runs.db").dialogue_state("default"))
    with pytest.raises(KeyError):
        store.mutate_dialogue("missing", {"op": "reset"})


async def test_generic_confirmation_executes_registered_search_once_not_free_text(tmp_path):
    llm = ScriptedLLM(tool_result("context_action_propose", json.dumps({"tool": "job_search_start", "arguments": {"queries": ["护士"], "locations": ["杭州"]}, "description": "搜索杭州护理岗位"}, ensure_ascii=False)))
    agent, _jobs, browser = make_agent(tmp_path, llm)
    first = await agent.handle("你建议我怎么开始？")
    assert "待确认动作" in first.message and browser.starts == 0
    response = await agent.handle("行吧，那就照你说的办")
    assert browser.starts == 1 and "已执行" in response.message
    assert browser.requests[-1].queries == ["护士"]
    await agent.handle("确认")
    assert browser.starts == 1
    assert len(llm.calls) == 2


async def test_free_assistant_claim_never_becomes_permission(tmp_path):
    agent, _jobs, browser = make_agent(tmp_path, ScriptedLLM(LLMToolResult(text="下一步自动帮你投递。", provider="test", model="test")))
    await agent.handle("你有什么建议？")
    result = await agent.handle("照你说的办")
    assert "没有唯一" in result.message
    assert browser.starts == 0 and not agent._local_dialogue["pending"]


async def test_rejection_then_confirmation_cannot_revive_action(tmp_path):
    llm = ScriptedLLM(tool_result("context_action_propose", '{"tool":"job_search_start","arguments":{"queries":["护士"]},"description":"搜索护理岗位"}'))
    agent, _jobs, browser = make_agent(tmp_path, llm)
    await agent.handle("建议搜索方案")
    await agent.handle("不要")
    await agent.handle("确认")
    assert browser.starts == 0
    assert agent._local_dialogue["pending"][-1]["status"] == "rejected"


async def test_question_bare_count_updates_one_slot_without_model_or_search(tmp_path):
    llm = ScriptedLLM(tool_result("context_question_ask", '{"topic":"search","question":"需要多少个岗位？","missing_slots":["result_limit"]}'))
    agent, _jobs, browser = make_agent(tmp_path, llm)
    await agent.handle("关键词改为护士")
    await agent.handle("你还需要什么信息？")
    calls = len(llm.calls)
    result = await agent.handle("15个")
    assert agent._local_search_state["result_limit"] == 15
    assert agent._local_search_state["queries"] == ["护士"]
    assert "条件已更新" in result.message and browser.starts == 0 and len(llm.calls) == calls


def add_jobs(jobs, count=6):
    for index in range(count):
        jobs.upsert_job({"source": "mock", "source_url": f"http://localhost/job/{index}", "external_id": str(index),
            "title": f"金融分析师{index}", "company": "示例公司", "location": "深圳", "salary": "20-30k", "description": "风险分析", "tags": [],
            "score": 90-index, "decision": "REVIEW", "reasoning": "示例匹配"})


async def test_batch_review_uses_displayed_order_not_new_sort_and_does_not_submit(tmp_path):
    llm = ScriptedLLM(tool_result("job_results_read"))
    agent, jobs, browser = make_agent(tmp_path, llm)
    add_jobs(jobs)
    first = await agent.handle("查看职位结果")
    assert "可引用条目" in first.message
    original = [item["id"] for item in agent._local_dialogue["snapshots"][-1]["items"]]
    calls = len(llm.calls)
    result = await agent.handle("我觉得前五个不错，可以通过审核")
    assert "已通过 5 个" in result.message
    assert {item["job_id"] for item in jobs.review_decisions()} == set(original[:5])
    assert all(item.stage == "review" for item in jobs.list_jobs())
    assert sum(item.review_approved for item in jobs.list_jobs()) == 5
    assert browser.starts == 0 and len(llm.calls) == calls


async def test_changed_job_rolls_back_entire_batch(tmp_path):
    agent, jobs, _browser = make_agent(tmp_path, ScriptedLLM(tool_result("job_results_read")))
    add_jobs(jobs)
    await agent.handle("查看职位结果")
    ids = [item["id"] for item in agent._local_dialogue["snapshots"][-1]["items"]]
    jobs.update_stage(ids[3], "waiting")
    result = await agent.handle("前五个通过审核")
    assert "阶段已变化" in result.message and not jobs.review_decisions()


async def test_model_cannot_expand_user_review_scope(tmp_path):
    agent, jobs, _browser = make_agent(tmp_path, ScriptedLLM(tool_result("job_results_read")))
    add_jobs(jobs)
    await agent.handle("查看职位结果")
    snapshot = agent._local_dialogue["snapshots"][-1]["id"]
    context = RunContext("run-x", asyncio.current_task(), lambda *_: None, lambda *_: None,
        dialogue=agent._local_dialogue, user_message="前两个审核通过")
    token = current_run.set(context)
    try:
        outcome = await agent.tool_registry.execute("job_review_selection", {"snapshot_id": snapshot, "selector": "这些"}, user_message=context.user_message, state=ToolLoopState())
        assert outcome.status == "error" and not jobs.review_decisions()
    finally:
        current_run.reset(token)


async def test_browser_other_session_cannot_read_or_control(tmp_path):
    class Browser:
        def current(self):
            return BrowserTaskView(id="other-task", status="running")
    context = RunContext("run-x", asyncio.current_task(), lambda *_: None, lambda *_: None,
        dialogue={"tasks": {"browser_id": "my-task"}}, enforce_task_scope=True)
    token = current_run.set(context)
    try:
        with pytest.raises(ValueError, match="不属于"):
            scoped_browser(Browser())
    finally:
        current_run.reset(token)


async def test_runtime_history_retrieval_session_isolated_and_original_retained_after_reset(tmp_path):
    agent, jobs, browser = make_agent(tmp_path, ScriptedLLM(LLMToolResult(text="回答", provider="test", model="test")))
    runtime = AgentRuntime(RunStore(tmp_path / "runs.db"), agent, jobs, browser, None, None)
    second = runtime.store.create_session("second")["id"]
    jobs.add_conversation_message("user", "旧金融偏好深圳", conversation_id="default")
    jobs.add_conversation_message("user", "金融：其他会话秘密", conversation_id=second)
    assert len(jobs.search_conversation_history("default", "金融")) == 1
    run = runtime.submit("default", "reset", "chat", {"message": "重置上下文"})
    result = await runtime.wait_response(run["id"])
    assert "已重置" in result["message"]
    assert jobs.search_conversation_history("default", "旧金融")
    assert not jobs.search_conversation_history("default", "%")
    await runtime.shutdown()


async def test_resume_change_invalidates_pending_even_with_pinned_query(tmp_path):
    llm = ScriptedLLM(tool_result("context_action_propose", '{"tool":"job_search_start","arguments":{},"description":"按当前条件搜索"}'))
    agent, _jobs, browser = make_agent(tmp_path, llm)
    await agent.handle("关键词改为护士")
    await agent.handle("推荐一个搜索方案")
    agent.candidate_state.apply_resume_profile(CandidateProfileView(resume_id="new", role_suggestions=["风险分析师"]), "金融")
    result = await agent.handle("确认")
    assert "已变化" in result.message and browser.starts == 0
    assert agent._local_search_state["queries"] == ["护士"]


async def test_idle_keeps_explicit_session_keywords_and_history(tmp_path):
    agent, _jobs, browser = make_agent(tmp_path, ScriptedLLM(LLMToolResult(text="不用模型", provider="test", model="test")))
    await agent.handle("关键词改为AI Agent")
    agent.candidate_state.apply_resume_profile(CandidateProfileView(resume_id="fin", role_suggestions=["信用风险分析师"]), "金融")
    agent._local_dialogue["last_activity"] = time.time()-901
    await agent.handle("帮我找工作")
    assert browser.requests[-1].queries == ["AI Agent"]
    assert any("AI Agent" in item["content"] for item in agent._conversation_history)
    await agent.handle("根据当前简历搜索")
    await agent.handle("再次搜索")
    assert browser.requests[-1].queries == ["信用风险分析师"]


async def test_registered_dry_run_and_arbitrary_external_tools_rejected(tmp_path):
    agent, _jobs, browser = make_agent(tmp_path, ScriptedLLM(tool_result("context_action_propose", '{"tool":"job_search_start","arguments":{"queries":["护士"],"dry_run":true},"description":"搜索"}')))
    result = await agent.handle("建议一个搜索方案")
    assert any(event.status == "error" for event in result.tool_events)
    assert browser.starts == 0 and not agent._local_dialogue["pending"]


async def test_salary_change_invalidates_displayed_review_and_approval(tmp_path):
    agent, jobs, _browser = make_agent(tmp_path, ScriptedLLM(tool_result("job_results_read")))
    add_jobs(jobs)
    await agent.handle("查看职位结果")
    await agent.handle("前两个通过审核")
    changed_id = agent._local_dialogue["snapshots"][-1]["items"][0]["id"]
    with jobs._connect() as db:
        db.execute("UPDATE jobs SET salary='8-10k' WHERE id=?", (changed_id,))
    response = await agent.handle("前五个通过审核")
    assert "已变化" in response.message
    assert changed_id not in {item["job_id"] for item in jobs.review_decisions()}
    assert not next(item for item in jobs.list_jobs() if item.id == changed_id).review_approved


async def test_low_small_model_confidence_reaches_main_with_dst(tmp_path):
    from test_intent_router import build_router, FakeSmallModel
    from app.intent.small_model import SmallModelDecision
    llm = ScriptedLLM(LLMToolResult(text="主模型回答", provider="test", model="test"))
    agent, _jobs, browser = make_agent(tmp_path / "agent", llm)
    small = FakeSmallModel(decision=SmallModelDecision("task_status", 0.6, 0.05))
    agent.router, _store = build_router(tmp_path / "router", small)
    await agent.handle("换成杭州")
    response = await agent.handle("看看我的情况")
    assert response.message == "主模型回答" and len(llm.calls) == 1 and small.calls == 1
    assert "杭州" in llm.calls[0][0]["content"] or any("杭州" in (item.get("content") or "") for item in llm.calls[0])
    assert browser.starts == 0


def test_context_api_auth_reset_and_current_scope(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.api import application
    from app.core.config import settings
    from app.main import app
    from test_agent_runtime import make_runtime
    runtime, _ = make_runtime(tmp_path)
    monkeypatch.setattr(application, "agent_runtime", runtime)
    monkeypatch.setattr(application, "run_store", runtime.store)
    monkeypatch.setattr(application, "job_store", runtime.jobs)
    runtime.store.mutate_dialogue("default", {"op": "question", "topic": "search", "question": "要多少份？", "missing_slots": ["result_limit"]})
    runtime.jobs.add_conversation_message("user", "历史应保留")
    headers = {"X-AutoHunt-Key": settings.api_key}
    path = "/api/v1/conversations/default/context"
    with TestClient(app) as client:
        assert client.get(path).status_code in {401, 403}
        view = client.get(path, headers=headers).json()
        assert view["active_intent"] == "search" and view["frames"]["search"]["missing_slots"] == ["result_limit"]
        assert client.get("/api/v1/conversations/missing/context", headers=headers).status_code == 404
        reset = client.post(path + "/reset", headers=headers)
        assert reset.status_code == 200 and reset.json()["active_intent"] == "general"
        assert runtime.jobs.search_conversation_history("default", "历史应保留")


async def test_cancel_stops_owned_task_but_not_other_session_browser(tmp_path):
    agent, _jobs, _browser = make_agent(tmp_path, ScriptedLLM(LLMToolResult(text="unused", provider="test", model="test")))
    class RunningBrowser:
        stops = 0
        def current(self):
            return BrowserTaskView(id="running-task", status="running")
        async def stop(self):
            self.stops += 1
            return BrowserTaskView(id="running-task", status="stopped")
    browser = RunningBrowser()
    agent.tool_registry.browser = browser
    agent._local_dialogue = apply_operation(None, {"op": "task", "kind": "browser_id", "id": "someone-else", "status": "running"})
    await agent.handle("取消")
    assert browser.stops == 0
    agent._local_dialogue = apply_operation(None, {"op": "task", "kind": "browser_id", "id": "running-task", "status": "running"})
    response = await agent.handle("退出当前任务")
    assert browser.stops == 1 and "已中断" in response.message


async def test_stale_clarification_click_does_not_route_last_user_message(tmp_path):
    from test_intent_router import build_router
    agent, _jobs, browser = make_agent(tmp_path / "agent", ScriptedLLM(LLMToolResult(text="unused", provider="test", model="test")))
    agent.router, _ = build_router(tmp_path / "router")
    await agent.handle("换成杭州")
    response = await agent.handle("【澄清】选择：搜索职位")
    assert "过期" in response.message and browser.starts == 0


async def test_restored_read_only_loop_with_changed_context_does_not_continue(tmp_path):
    llm = ScriptedLLM(tool_result("job_search_start", '{"queries":["旧方向"]}'))
    agent, _jobs, browser = make_agent(tmp_path, llm)
    context = RunContext("restored", asyncio.current_task(), lambda *_: None, lambda *_: None,
        search_state=prepare_search_state(None), restored={"phase": "tool_loop",
            "context_dependencies": {"revision": 999, "resume_stamp": "old-resume"}})
    token = current_run.set(context)
    try:
        response = await agent.handle("查看职位结果")
        assert "旧检查点" in response.message and browser.starts == 0 and not llm.calls
    finally:
        current_run.reset(token)
