"""One corpus must agree across parser, router, policy, API and session replay."""
import pytest
from pydantic import ValidationError

from app.core.agent_tools import AgentToolPolicy, ToolLoopState
from app.core.main_agent import _fast_search_hint, _search_command
from app.core.models import BrowserSearchRequest, CandidateProfileView
from app.core.search_language import parse_search_message
from app.intent.rules import RuleEngine
from test_agent_tool_loop import ScriptedLLM, make_agent, tool_result

RESTARTS = (
    "再次检索", "重新检索", "再检索一次", "再次搜索", "重新搜索", "再搜一次", "再搜一遍",
    "再找一次", "重新查找", "再次启动搜索", "重新开始检索", "开始检索", "确认开始搜索",
    "请再次检索", "麻烦你再搜一次", "按现有条件再次检索", "按照刚才的条件再搜一遍",
)
ROLES = ("外贸业务员", "信用风险分析师", "护士", "机械设计工程师", "法务专员", "食品质量专员",
         "光伏工程师", "UI/UX设计师", "HRBP", "店长", "医学编辑", "AI Agent", "城市规划师", "薪资福利专员")
NON_COMMANDS = (
    "不要再次检索", "先别重新搜索", "不用再搜一次", "能不能再次检索？", "再次检索是什么意思",
    "如果再次检索会怎样", "我刚才说“再次检索”", "重新搜索有没有风险？", "再次检索的规则是什么",
    "检索结果", "查看检索结果", "职位搜索结果", "行吧", "这些职位怎么样", "任务状态",
)
UNPARSED = (
    "找15份我的专业相关工作 薪资水平在14k以上 并生成总结",
    "帮我找一个能够兼顾家庭的工作", "搜索外贸业务员并总结JD", "搜索外贸业务员并总结搜索结果",
    "搜索能够远程工作的风控分析师", "搜索外贸业务员，城市深圳，城市杭州",
    "搜索外贸业务员，数量15个，数量20个", "搜索护士，薪资14k以下",
    "搜索外贸业务员然后修改简历", "帮我找工作顺便更新偏好",
)


@pytest.mark.parametrize("message", RESTARTS)
async def test_restart_contract_agrees_across_routing_and_policy(tmp_path, message):
    parsed = parse_search_message(message)
    assert parsed.authorized and parsed.complete and not parsed.fields.get("queries")
    assert _fast_search_hint(message) is not None
    assert RuleEngine().match(message).intent == "search_jobs"
    agent, _jobs, _browser = make_agent(tmp_path, ScriptedLLM(tool_result("job_search_start")))
    allowed, _ = AgentToolPolicy().authorize(agent.tool_registry._tools["job_search_start"], {}, message, ToolLoopState())
    assert allowed  # Not dependent on a separately guessed explicit_search_intent flag.


@pytest.mark.parametrize("role", ROLES)
def test_cross_industry_parameters_are_fully_separated(role):
    message = f"检索{role}，城市杭州，目标数量15个，月薪12k以上"
    parsed = parse_search_message(message)
    assert parsed.complete and parsed.fields["queries"] == [role]
    assert parsed.fields["locations"] == ["杭州"]
    assert parsed.fields["result_limit"] == 15 and parsed.fields["salary_min"] == 12000


@pytest.mark.parametrize("message", NON_COMMANDS)
async def test_model_flag_cannot_authorize_negation_questions_or_quotes(tmp_path, message):
    agent, _jobs, _browser = make_agent(tmp_path, ScriptedLLM(tool_result("job_search_start")))
    assert not parse_search_message(message).authorized
    assert _search_command(message) is None and _fast_search_hint(message) is None
    allowed, _ = AgentToolPolicy().authorize(agent.tool_registry._tools["job_search_start"], {}, message,
                                           ToolLoopState(explicit_search_intent=True))
    assert not allowed


@pytest.mark.parametrize("message", UNPARSED)
def test_unconsumed_prose_never_leaks_into_any_hint(message):
    parsed = parse_search_message(message)
    assert parsed.authorized and not parsed.complete
    assert _fast_search_hint(message) is None
    assert _search_command(message)["query"] is None
    assert not parsed.fields.get("queries")
    assert RuleEngine().match(message) is None


@pytest.mark.parametrize("query", [
    "15份我的专业相关工作 薪资水平在14k以上 并生成总结", "再次检索", "外贸业务员 薪资12k以上",
    "my profession jobs above 12k", "根据我的简历", "职位搜索结果", "外贸业务员，数量15个",
])
def test_api_rejects_sentence_keywords_and_hidden_bad_secondary_query(query):
    with pytest.raises(ValidationError):
        BrowserSearchRequest(query=query)
    with pytest.raises(ValidationError):
        BrowserSearchRequest(query="外贸业务员", queries=["外贸业务员", query])


async def test_multiturn_restart_preserves_conditions_without_models(tmp_path):
    from test_intent_router import FakeSmallModel, build_router
    small = FakeSmallModel(error=AssertionError("simple commands must stay off models"))
    llm = ScriptedLLM(tool_result("job_search_start", '{"query":"wrong"}'))
    agent, _jobs, browser = make_agent(tmp_path / "agent", llm)
    agent.router, _ = build_router(tmp_path / "router", small)
    agent.candidate_state.apply_resume_profile(CandidateProfileView(resume_id="trade", role_suggestions=["外贸业务员"]), "外贸业务员")
    before = agent.candidate_state.read().preferences.model_dump()
    await agent.handle("帮我找工作，目标数量15个，月薪12k以上")
    await agent.handle("地点深圳、上海、杭州、武汉")
    for message in RESTARTS:
        result = await agent.handle(message)
        assert "已启动" in result.message
        request = browser.requests[-1]
        assert request.queries == ["外贸业务员"] and request.result_limit == 15
        assert request.salary_min == 12000 and request.locations == ["深圳", "上海", "杭州", "武汉"]
    assert browser.starts == 1 + len(RESTARTS)
    assert llm.calls == [] and small.calls == 0
    assert agent.candidate_state.read().preferences.model_dump() == before


async def test_incomplete_request_uses_model_for_slots_not_whole_sentence(tmp_path):
    text = "找15份我的专业相关工作 薪资水平在14k以上 并生成总结"
    llm = ScriptedLLM(tool_result("job_search_start", '{"query":"信用风险分析师"}'))
    agent, _jobs, browser = make_agent(tmp_path, llm)
    agent.candidate_state.apply_resume_profile(CandidateProfileView(resume_id="finance", role_suggestions=["信用风险分析师"]), "金融风控")
    await agent.handle(text)
    request = browser.requests[0]
    assert request.query == "信用风险分析师" and request.queries == ["信用风险分析师"]
    assert request.salary_min == 14000 and request.result_limit == 15
    assert llm.calls


async def test_complete_user_slots_override_model_guesses(tmp_path):
    agent, _jobs, browser = make_agent(tmp_path, ScriptedLLM(tool_result("job_search_start")))
    result = await agent.tool_registry.execute("job_search_start", {"query": "Java开发工程师", "salary_min": 0},
        user_message="检索外贸业务员，城市杭州，目标数量15个，月薪12k以上", state=ToolLoopState())
    assert result.status == "ok"
    request = browser.requests[0]
    assert request.query == "外贸业务员" and request.salary_min == 12000 and request.result_limit == 15
    assert request.locations == ["杭州"]


async def test_model_sentence_keyword_is_rejected_without_poisoning_session(tmp_path):
    text = "找15份我的专业相关工作 薪资水平在14k以上 并生成总结"
    import json
    llm = ScriptedLLM(tool_result("job_search_start", json.dumps({"query": text}, ensure_ascii=False)))
    agent, _jobs, browser = make_agent(tmp_path, llm)
    agent.candidate_state.apply_resume_profile(CandidateProfileView(resume_id="finance", role_suggestions=["信用风险分析师"]), "金融风控")
    response = await agent.handle(text)
    assert browser.starts == 0
    assert any(event.tool == "job_search_start" and event.status == "error" for event in response.tool_events)
    assert text not in agent._local_search_state["queries"]


async def test_conflicting_slots_cannot_execute_even_with_model_parameters(tmp_path):
    agent, _jobs, browser = make_agent(tmp_path, ScriptedLLM(tool_result("job_search_start")))
    message = "搜索护士，数量15个，数量20个"
    parsed = parse_search_message(message)
    assert parsed.reason == "conflicting_parameters" and "result_limit" not in parsed.fields
    result = await agent.tool_registry.execute("job_search_start", {"query": "护士", "result_limit": 20},
        user_message=message, state=ToolLoopState(explicit_search_intent=True))
    assert result.status == "denied" and "冲突" in result.content
    assert browser.starts == 0


async def test_legacy_incomplete_search_never_builds_a_browser_action(tmp_path):
    from test_chat_search_intent import build_agent
    agent, _state, llm = build_agent(tmp_path)
    response = await agent.handle("找15份我的专业相关工作 薪资水平在14k以上 并生成总结")
    assert response.action is None and response.action_query is None
    assert llm.calls == 1
