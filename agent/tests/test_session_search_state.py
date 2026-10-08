from __future__ import annotations

import json

import pytest

from app.core.main_agent import _fast_search_hint
from app.core.models import CandidateProfileView, JobPreferenceUpdate
from app.core.search_contract import simple_search_patch
from app.intent.small_model import _build_user_prompt
from app.llm.base import LLMToolResult
from app.runtime.manager import AgentRuntime
from app.runtime.search_state import bounded_history, merge_search_state, prepare_search_state
from app.runtime.store import RunStore
from test_agent_tool_loop import ScriptedLLM, make_agent, tool_result


@pytest.mark.parametrize("text", ["帮我找工作", "我要找工作", "找15份工作", "找15份信用风险分析师的工作", "搜索深圳的算法工程师", "重新搜索", "搜索信用风险分析师", "搜索护士", "搜索AI Agent", "搜索风控分析师，数量15个，薪资14k以上", "我想找AI agent AI算法工程师的工作，地点深圳杭州北京上海"])
def test_common_search_is_completely_parsed_by_rules(text):
    assert _fast_search_hint(text) is not None


@pytest.mark.parametrize("text", ["找15份我的专业相关工作 薪资水平在14k以上 并生成总结", "找适合我的工作然后分析发展前景", "搜索关键词怎么设置", "不要帮我找工作", "搜索结果", "帮我找一个能够兼顾家庭的工作", "搜索AI Agent岗位并生成总结", "帮我找工作顺便修改简历", "给我一些符合我背景的机会", "搜索AI岗位，方向自由发挥"])
def test_unparsed_prose_never_becomes_a_rule_keyword(text):
    assert _fast_search_hint(text) is None


def test_partial_grammar_does_not_consume_extra_intent():
    cities = ("深圳", "杭州", "北京", "上海")
    assert simple_search_patch("数量改20个", cities) == {"result_limit": 20}
    assert simple_search_patch("换成杭州", cities) == {"locations": ["杭州"]}
    assert simple_search_patch("城市改成杭州，数量20个", cities) == {"locations": ["杭州"], "result_limit": 20}
    assert simple_search_patch("关键词改为信用风险分析师、金融分析师", cities) == {"queries": ["信用风险分析师", "金融分析师"]}
    assert simple_search_patch("月薪14k以上", cities) == {"salary_min": 14000}
    assert simple_search_patch("地点杭州深圳北京上海都可以", cities)["locations"] == list(("杭州", "深圳", "北京", "上海"))
    assert simple_search_patch("换成杭州并更新简历", cities) is None
    assert simple_search_patch("数量改200个", cities) is None
    assert simple_search_patch("月薪14k以下", cities) is None
    assert _fast_search_hint("找工作想了解算法工程师") is None
    assert _fast_search_hint("搜索能够远程工作的风控分析师") is None


async def test_fast_multiturn_updates_only_named_fields_and_never_preferences(tmp_path):
    llm = ScriptedLLM(tool_result("job_search_start", '{"query":"wrong"}'))
    agent, _jobs, browser = make_agent(tmp_path, llm)
    agent.candidate_state.apply_resume_profile(CandidateProfileView(resume_id="finance", role_suggestions=["风险策略分析师"], skills=["SQL"]), "金融风控")
    before = agent.candidate_state.read().preferences.model_dump()
    await agent.handle("搜索风险策略分析师")
    initial = browser.requests[0].model_dump()
    for text in ("换成杭州", "数量改20个", "月薪14k以上"):
        response = await agent.handle(text)
        assert "未启动新搜索" in response.message
    assert browser.starts == 1 and llm.calls == []
    assert agent.candidate_state.read().preferences.model_dump() == before
    await agent.handle("重新搜索")
    assert browser.requests[-1].queries == ["风险策略分析师"]
    assert browser.requests[-1].locations == ["杭州"]
    assert browser.requests[-1].result_limit == 20
    assert browser.requests[-1].salary_min == 14000
    assert browser.requests[-1].max_jobs == 100
    assert browser.requests[0].model_dump() == initial  # Immutable running condition snapshot.
    assert llm.calls == []


async def test_explicit_clear_city_does_not_restore_saved_city(tmp_path):
    agent, _jobs, browser = make_agent(tmp_path, ScriptedLLM(tool_result("job_search_start")))
    agent.candidate_state.apply_resume_profile(CandidateProfileView(resume_id="finance", role_suggestions=["信用风险分析师"]), "金融")
    agent.candidate_state.update_confirmed(JobPreferenceUpdate(locations=["深圳"]))
    await agent.handle("不限城市")
    await agent.handle("开始搜索")
    assert not browser.requests[0].locations
    await agent.handle("月薪14k以上")
    await agent.handle("不限薪资")
    await agent.handle("重新搜索")
    assert browser.requests[-1].salary_min is None


async def test_new_resume_refreshes_derived_roles_but_not_explicit_session_roles(tmp_path):
    agent, _jobs, browser = make_agent(tmp_path, ScriptedLLM(tool_result("job_search_start")))
    agent.candidate_state.apply_resume_profile(CandidateProfileView(resume_id="ai", role_suggestions=["AI算法工程师"]), "AI")
    await agent.handle("帮我找工作")
    await agent.handle("数量改15个")
    agent.candidate_state.apply_resume_profile(CandidateProfileView(resume_id="finance", role_suggestions=["信用风险分析师"]), "金融")
    await agent.handle("重新搜索")
    assert browser.requests[-1].queries == ["信用风险分析师"]
    assert browser.requests[-1].result_limit == 15
    await agent.handle("搜索护士")
    agent.candidate_state.apply_resume_profile(CandidateProfileView(resume_id="finance2", role_suggestions=["风险策略分析师"]), "金融")
    await agent.handle("重新搜索")
    assert browser.requests[-1].queries == ["护士"]
    await agent.handle("恢复简历方向")
    await agent.handle("重新搜索")
    assert browser.requests[-1].queries == ["风险策略分析师"]


async def test_generic_fallback_does_not_pin_old_role_after_first_resume(tmp_path):
    agent, _jobs, browser = make_agent(tmp_path, ScriptedLLM(tool_result("job_search_start")))
    await agent.handle("帮我找工作")
    agent.candidate_state.apply_resume_profile(CandidateProfileView(resume_id="finance", role_suggestions=["信用风险分析师"]), "金融")
    await agent.handle("重新搜索")
    assert browser.requests[-1].queries == ["信用风险分析师"]


async def test_unparsed_followup_uses_model_with_history_and_structured_state(tmp_path):
    llm = ScriptedLLM(tool_result("search_state_update", '{"salary_min":16000}'))
    agent, _jobs, browser = make_agent(tmp_path, llm)
    await agent.handle("搜索护士")
    await agent.handle("换成杭州")
    await agent.handle("数量改20个")
    await agent.handle("薪资至少一万六，其他条件不变")
    prompt = str(llm.calls[0])
    assert "护士" in prompt and "杭州" in prompt and '20' in prompt
    assert "当前会话搜索条件" in prompt
    assert browser.starts == 1
    assert agent._local_search_state["salary_min"] == 16000
    assert agent._local_search_state["result_limit"] == 20


async def test_negated_command_cannot_be_authorized_by_model(tmp_path):
    agent, _jobs, browser = make_agent(tmp_path, ScriptedLLM(tool_result("job_search_start", '{"query":"护士"}')))
    response = await agent.handle("不要帮我找工作")
    assert browser.starts == 0
    assert any(event.status == "denied" for event in response.tool_events)


async def test_actual_router_keeps_common_search_and_results_off_both_models(tmp_path):
    from test_intent_router import build_router, FakeSmallModel
    small = FakeSmallModel(error=AssertionError("高频命令不应调用小模型"))
    llm = ScriptedLLM(tool_result("job_search_start", '{"query":"wrong"}'))
    agent, _jobs, browser = make_agent(tmp_path / "agent", llm)
    agent.router, _ = build_router(tmp_path / "router", small)
    await agent.handle("搜索风控分析师，数量15个，薪资14k以上")
    await agent.handle("换成杭州")
    await agent.handle("数量改20个")
    await agent.handle("职位搜索结果")
    await agent.handle("重新搜索")
    assert browser.requests[-1].queries == ["风控分析师"]
    assert browser.requests[-1].salary_min == 14000
    assert browser.requests[-1].result_limit == 20
    assert browser.requests[-1].locations == ["杭州"]
    assert llm.calls == [] and small.calls == 0


async def test_resume_classifier_gets_short_context_without_skipping_rules(tmp_path):
    from test_intent_router import build_router, FakeSmallModel
    from app.intent.small_model import SmallModelDecision
    class ContextSmall(FakeSmallModel):
        supports_context = True
        async def classify(self, message, *, context=""):
            self.received = context
            return await super().classify(message)
    small = ContextSmall(decision=SmallModelDecision("none", 0.1, 0.0))
    agent, _jobs, _browser = make_agent(tmp_path / "agent", ScriptedLLM(LLMToolResult(text="收到", provider="test", model="test")))
    agent.router, _ = build_router(tmp_path / "router", small)
    await agent.handle("换成杭州")
    await agent.handle("数量改20个")
    await agent.handle("这样是否合理")
    assert "杭州" in small.received and "20" in small.received
    assert "数量改20个" in small.received


async def test_condition_edit_cannot_silently_write_long_term_preferences(tmp_path):
    llm = ScriptedLLM(tool_result("preference_update", '{"locations":["杭州"]}'))
    agent, _jobs, _browser = make_agent(tmp_path, llm)
    before = agent.candidate_state.read().preferences.model_dump()
    response = await agent.handle("把城市调整到杭州并且薪资稍微降低一些")
    assert agent.candidate_state.read().preferences.model_dump() == before
    assert any(event.tool == "preference_update" and event.status == "denied" for event in response.tool_events)


async def test_model_patch_cannot_erase_unmentioned_city(tmp_path):
    agent, _jobs, _browser = make_agent(tmp_path, ScriptedLLM(tool_result("search_state_update", '{"salary_min":16000,"locations":[]}')))
    await agent.handle("换成杭州")
    response = await agent.handle("把最低月薪调整到一万六，其他条件不变")
    assert agent._local_search_state["locations"] == ["杭州"]
    assert any(event.tool == "search_state_update" and event.status == "error" for event in response.tool_events)


async def test_fast_state_rule_cannot_bypass_p0_credentials_guard(tmp_path):
    from test_intent_router import build_router, FakeSmallModel
    agent, _jobs, _browser = make_agent(tmp_path / "agent", ScriptedLLM(tool_result("job_search_start")))
    agent.router, _ = build_router(tmp_path / "router", FakeSmallModel())
    response = await agent.handle("关键词改为sk-example-fake-credential")
    assert any(event.status == "blocked" for event in response.tool_events)
    assert not agent._local_search_state["queries"]


async def test_explicit_same_role_is_still_a_session_override(tmp_path):
    agent, _jobs, browser = make_agent(tmp_path, ScriptedLLM(tool_result("job_search_start")))
    agent.candidate_state.apply_resume_profile(CandidateProfileView(resume_id="ai", role_suggestions=["AI算法工程师"]), "AI")
    await agent.handle("搜索AI算法工程师")
    agent.candidate_state.apply_resume_profile(CandidateProfileView(resume_id="finance", role_suggestions=["信用风险分析师"]), "金融")
    await agent.handle("重新搜索")
    assert browser.requests[-1].queries == ["AI算法工程师"]


async def test_runtime_persists_isolated_search_state_across_restart(tmp_path):
    agent, jobs, browser = make_agent(tmp_path, ScriptedLLM(LLMToolResult(text="不用调用模型", provider="test", model="test")))
    store = RunStore(tmp_path / "runtime.db")
    runtime = AgentRuntime(store, agent, jobs, browser, None, None)
    second = store.create_session("第二个会话")["id"]
    for session, message, key in (("default", "换成杭州", "a"), ("default", "数量改20个", "b"), (second, "换成北京", "c")):
        run = runtime.submit(session, key, "chat", {"message": message})
        await runtime.wait_response(run["id"])
    await runtime.shutdown()
    restored = RunStore(tmp_path / "runtime.db")
    assert restored.search_state("default")["locations"] == ["杭州"]
    assert restored.search_state("default")["result_limit"] == 20
    assert restored.search_state(second)["locations"] == ["北京"]
    assert restored.search_state(second)["result_limit"] is None
    previous = restored.search_state("default")
    assert restored.save_search_state("default", previous)["revision"] == previous["revision"]
    with pytest.raises(KeyError):
        restored.search_state("missing")
    assert browser.starts == 0


def test_context_budget_and_small_model_context():
    history = [{"role": "user", "content": f"turn-{i}"} for i in range(40)]
    limited = bounded_history(history, max_messages=20, max_chars=60)
    assert sum(len(item["content"]) for item in limited) <= 60
    assert limited[-1]["content"] == "turn-39"
    assert len(limited) <= 20
    prompt = _build_user_prompt("数量改20个", json.dumps({"queries": ["护士"], "locations": ["杭州"]}, ensure_ascii=False))
    assert "护士" in prompt and "杭州" in prompt and "数量改20个" in prompt


def test_invalid_patch_cannot_erase_unspecified_conditions():
    state = prepare_search_state(None)
    state = merge_search_state(state, {"queries": ["护士"], "locations": ["杭州"], "salary_min": 14000, "result_limit": 15})
    changed = merge_search_state(state, {"locations": ["北京"]})
    assert changed["salary_min"] == 14000 and changed["result_limit"] == 15
    with pytest.raises(ValueError):
        merge_search_state(state, {"queries": []})
    with pytest.raises(ValueError):
        merge_search_state(state, {"locations": None})
