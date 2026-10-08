import pytest
from pydantic import ValidationError

from app.core.agent_tools import ToolLoopState
from app.core.main_agent import _compose_queries
from app.core.models import BrowserSearchRequest, CandidateProfileView
from app.llm.base import LLMToolResult
from test_agent_tool_loop import ScriptedLLM, make_agent, tool_result
from test_chat_search_intent import build_agent


@pytest.mark.parametrize("message", ["帮我找工作", "我要找工作", "开始搜索", "重新搜索", "找15份工作"])
async def test_empty_resume_and_missing_role_asks_without_models_or_browser(tmp_path, message):
    llm = ScriptedLLM(tool_result("job_search_start", '{"query":"软件工程师"}'))
    agent, _jobs, browser = make_agent(tmp_path, llm)
    response = await agent.handle(message)
    assert browser.starts == 0
    assert llm.calls == []
    assert response.action is None
    assert "没有上传或启用简历" in response.message
    assert "没有启动搜索" in response.message
    assert "软件工程师" not in response.message
    assert agent._local_dialogue["frames"]["search"]["missing_slots"] == ["queries"]
    assert not agent._local_search_state["queries"]


async def test_legacy_and_dry_run_paths_cannot_invent_default_role(tmp_path):
    agent, state, llm = build_agent(tmp_path)
    assert _compose_queries(None, None, [], state) == []
    for message in ("帮我找工作", "不需要，开始搜索职位", "试运行 搜索职位"):
        response = await agent.handle(message)
        assert response.action is None
        assert "没有启动搜索" in response.message
    assert llm.calls == 0


async def test_explicit_role_without_resume_can_search_without_confirmation(tmp_path):
    agent, _jobs, browser = make_agent(tmp_path, ScriptedLLM(tool_result("job_search_start")))
    response = await agent.handle("搜索护士")
    assert browser.starts == 1
    assert browser.requests[0].queries == ["护士"]
    assert "已启动" in response.message
    assert not agent.candidate_state.read().profile.resume_id
    await agent.handle("重新搜索")
    assert browser.starts == 2
    assert browser.requests[-1].queries == ["护士"]


async def test_active_resume_still_drives_generic_search(tmp_path):
    agent, _jobs, browser = make_agent(tmp_path, ScriptedLLM(tool_result("job_search_start")))
    agent.candidate_state.apply_resume_profile(CandidateProfileView(resume_id="finance", role_suggestions=["信用风险分析师"]), "金融风控")
    await agent.handle("帮我找工作")
    assert browser.requests[0].queries == ["信用风险分析师"]


async def test_model_cannot_invent_a_role_for_generic_request(tmp_path):
    llm = ScriptedLLM(tool_result("job_search_start", '{"query":"软件工程师"}'))
    agent, _jobs, browser = make_agent(tmp_path, llm)
    response = await agent.handle("找15份我的专业相关工作 薪资水平在14k以上 并生成总结")
    assert browser.starts == 0
    assert len(llm.calls) == 1
    assert "没有启动搜索" in response.message
    assert any(event.status == "awaiting_user" for event in response.tool_events)


async def test_native_tool_without_context_or_keywords_fails_closed(tmp_path):
    agent, _jobs, browser = make_agent(tmp_path, ScriptedLLM(tool_result("job_search_start")))
    outcome = await agent.tool_registry.execute("job_search_start", {}, user_message="开始搜索", state=ToolLoopState(explicit_search_intent=True))
    assert outcome.status == "awaiting_user"
    assert browser.starts == 0


async def test_role_answer_fills_recent_question_without_losing_context(tmp_path):
    llm = ScriptedLLM(tool_result("job_search_start"))
    agent, _jobs, browser = make_agent(tmp_path, llm)
    await agent.handle("帮我找工作")
    response = await agent.handle("护士")
    assert "未启动新搜索" in response.message
    assert agent._local_search_state["queries"] == ["护士"]
    assert browser.starts == 0
    await agent.handle("开始搜索")
    assert browser.requests[0].queries == ["护士"]
    assert llm.calls == []


async def test_unregistered_confirmation_id_does_not_bypass_preflight(tmp_path):
    agent, _jobs, browser = make_agent(tmp_path, ScriptedLLM(tool_result("job_search_start")))
    outcome = await agent.tool_registry.execute("job_search_start", {"query": "软件工程师"},
        user_message="开始搜索", state=ToolLoopState(explicit_search_intent=True, confirmed_action_id="fake"))
    assert outcome.status == "awaiting_user"
    assert browser.starts == 0


def test_api_requires_a_real_keyword_instead_of_default():
    with pytest.raises(ValidationError):
        BrowserSearchRequest()
    with pytest.raises(ValidationError):
        BrowserSearchRequest(query="   ")
    assert BrowserSearchRequest(query="金融分析师").query == "金融分析师"


def test_unrecognized_resume_never_defaults_to_computing_industry():
    from app.memory.resume_store import analyze_resume_text
    _skills, roles, query = analyze_resume_text("长期参与社区居民健康宣传、志愿服务与活动协调，具备良好沟通能力和团队合作意识。")
    assert roles == []
    assert query == ""


def test_old_invented_resume_defaults_are_reconciled_without_erasing_user_roles(tmp_path):
    from app.runtime.search_state import prepare_search_state
    agent, _jobs, _browser = make_agent(tmp_path, ScriptedLLM(tool_result("job_search_start")))
    candidate = agent.candidate_state.read()
    baseline = prepare_search_state(None, candidate)
    polluted = {**baseline, "queries": ["软件工程师"], "query_source": "resume"}
    assert prepare_search_state(polluted, candidate)["queries"] == []
    pinned = {**polluted, "query_source": "session"}
    assert prepare_search_state(pinned, candidate)["queries"] == ["软件工程师"]
