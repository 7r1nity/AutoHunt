from pathlib import Path
from types import SimpleNamespace

from cryptography.fernet import Fernet

from app.core.main_agent import AutoHuntAgent, _compose_query
from app.core.models import CandidateEvidenceItem, CandidateProfileView, JobPreferenceUpdate
from app.llm.base import LLMResult
from app.memory.candidate_state import CandidateStateStore
from app.memory.stores import MemoryStore, SkillStore
from app.sandbox_backend.filesystem import WorkspaceSandbox


class FakeEvidenceExtractor:
    async def extract(self, text: str, *, title: str, kind: str, source: str) -> CandidateEvidenceItem:
        return CandidateEvidenceItem(
            kind="internship",
            title="智能问答实习项目",
            summary="使用 Python 和 RAG 开发智能问答服务。",
            source="conversation",
        )


class RecordingLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, request):
        self.calls += 1
        return LLMResult(text="mock reply", provider="mock", model="mock-autohunt-v1")


def build_agent(tmp_path: Path):
    sandbox = WorkspaceSandbox(tmp_path / "data")
    state = CandidateStateStore(tmp_path / "data", Fernet.generate_key().decode())
    llm = RecordingLLM()
    agent = AutoHuntAgent(
        MemoryStore(sandbox),
        SkillStore(sandbox),
        state,
        FakeEvidenceExtractor(),
        llm,
    )
    return agent, state, llm


async def test_search_command_with_location_starts_immediately(tmp_path: Path) -> None:
    agent, _state, llm = build_agent(tmp_path)
    response = await agent.handle("搜索深圳的前端工程师岗位")
    assert response.action == "confirm_search"
    assert response.action_query == "前端工程师"
    assert response.action_locations == ["深圳"]
    assert llm.calls == 0


async def test_bare_search_command_falls_back_to_saved_keyword(tmp_path: Path) -> None:
    agent, state, llm = build_agent(tmp_path)
    state.confirm_search_query("AI算法工程师")
    response = await agent.handle("开始搜索")
    assert response.action == "confirm_search"
    assert response.action_query == "AI算法工程师"
    assert llm.calls == 0


async def test_generic_search_uses_current_finance_resume_instead_of_old_ai_keyword(tmp_path: Path) -> None:
    agent, state, llm = build_agent(tmp_path)
    state.confirm_search_query("AI Agent")
    state.apply_resume_profile(CandidateProfileView(
        resume_id="f" * 32,
        role_suggestions=["风险策略分析师", "信用风险分析师", "金融分析师"],
        skills=["Python", "SQL"],
        summary="金融风控与信用风险分析经验。",
    ), "软件工程师 Python")

    response = await agent.handle("帮我找工作")

    assert response.action == "confirm_search"
    assert response.action_queries == ["风险策略分析师", "信用风险分析师", "金融分析师"]
    assert "AI Agent" not in response.action_queries
    assert state.read().preferences.search_keywords.values[0] == "AI Agent"
    assert llm.calls == 0

    explicit = await agent.handle("搜索 AI Agent 岗位")
    assert explicit.action_query == "AI Agent"


async def test_quick_action_phrase_starts_search_without_llm(tmp_path: Path) -> None:
    agent, state, llm = build_agent(tmp_path)
    state.confirm_search_query("AI算法工程师")
    response = await agent.handle("开始职位搜索")
    assert response.action == "confirm_search"
    assert response.action_query == "AI算法工程师"
    assert llm.calls == 0


async def test_keyword_only_command_starts_without_extra_confirmation(tmp_path: Path) -> None:
    agent, _state, llm = build_agent(tmp_path)
    response = await agent.handle("搜索前端工程师")
    assert response.action == "confirm_search"
    assert response.action_query == "前端工程师"
    assert llm.calls == 0


async def test_embedded_start_search_phrase_is_deterministic(tmp_path: Path) -> None:
    agent, _state, llm = build_agent(tmp_path)
    response = await agent.handle("不需要，开始搜索职位")
    assert response.action is None
    assert "没有启动搜索" in response.message
    assert llm.calls == 0


async def test_full_dialog_flow_extracts_keyword_location_and_direction(tmp_path: Path) -> None:
    agent, _state, llm = build_agent(tmp_path)
    response = await agent.handle("搜索AI大模型算法岗位，地点:深圳，岗位方向:大模型算法研究/训练")
    assert response.action == "confirm_search"
    assert response.action_query == "AI大模型算法"
    assert response.action_locations == ["深圳"]
    assert llm.calls == 0


async def test_multi_role_multi_city_keeps_cities_out_of_keywords(tmp_path: Path) -> None:
    agent, _state, llm = build_agent(tmp_path)
    response = await agent.handle("我想找AI agent AI大模型算法 还有Ai算法工程师的工作 工作地点深圳或者杭州")
    assert response.action == "confirm_search"
    assert response.action_queries == ["AI agent", "AI大模型算法", "AI算法工程师"]
    assert response.action_locations == ["深圳", "杭州"]
    assert response.action_query == "AI agent"
    assert llm.calls == 0


async def test_explicit_preference_update_only_when_requested(tmp_path: Path) -> None:
    agent, state, llm = build_agent(tmp_path)
    response = await agent.handle("更新偏好：地点深圳，岗位方向大模型算法，最低薪资30K")
    assert response.action is None
    assert "已按你的明确指示更新求职偏好" in response.message
    preferences = state.read().preferences
    assert preferences.locations.values == ["深圳"]
    assert preferences.target_roles.values == ["大模型算法"]
    assert preferences.salary_min.value == 30_000
    assert llm.calls == 0


async def test_question_about_search_is_not_a_command(tmp_path: Path) -> None:
    agent, _state, llm = build_agent(tmp_path)
    response = await agent.handle("搜索关键词怎么设置？")
    assert response.action is None
    assert llm.calls == 1


async def test_progress_question_still_uses_llm(tmp_path: Path) -> None:
    agent, _state, llm = build_agent(tmp_path)
    response = await agent.handle("查看当前申请进度")
    assert response.action is None
    assert response.lane == "application"
    assert llm.calls == 1


async def test_evidence_update_still_wins_over_search_prefix(tmp_path: Path) -> None:
    agent, state, llm = build_agent(tmp_path)
    response = await agent.handle("帮我找工作经历，请加入简历")
    assert response.action is None
    assert "已把" in response.message
    assert "智能问答实习项目" in state.read().resume_draft
    assert llm.calls == 0


class FakeStateStore:
    def __init__(self, keywords=(), roles=(), skills=(), locations=(), source="default", resume_id=None) -> None:
        self.keywords = list(keywords)
        self.roles = list(roles)
        self.skills = list(skills)
        self.locations = list(locations)
        self.source = source
        self.resume_id = resume_id

    def read(self):
        return SimpleNamespace(
            preferences=SimpleNamespace(
                search_keywords=SimpleNamespace(values=self.keywords),
                target_roles=SimpleNamespace(values=[], source="inferred"),
                locations=SimpleNamespace(values=self.locations, source=self.source),
            ),
            profile=SimpleNamespace(resume_id=self.resume_id, role_suggestions=self.roles, skills=self.skills),
        )


def test_bare_command_uses_resume_role_without_overrestrictive_skill_term() -> None:
    store = FakeStateStore(keywords=[], roles=["AI工程师"], skills=["Python", "React"], resume_id="active")
    query = _compose_query(None, None, None, store)
    assert query == "AI工程师"


def test_bare_command_prefers_saved_keyword() -> None:
    store = FakeStateStore(keywords=["AI算法工程师"], roles=["AI工程师"], skills=["Python"])
    query = _compose_query(None, None, None, store)
    assert query == "AI算法工程师"


async def test_search_uses_confirmed_preference_location_immediately(tmp_path: Path) -> None:
    agent, state, llm = build_agent(tmp_path)
    state.update_confirmed(JobPreferenceUpdate(locations=["深圳"]))
    response = await agent.handle("搜索前端工程师")
    assert response.action == "confirm_search"
    assert response.action_query == "前端工程师"
    assert response.action_locations == ["深圳"]
    assert llm.calls == 0


async def test_remove_term_updates_persisted_search_then_restart_uses_clean_keyword(tmp_path: Path) -> None:
    agent, state, llm = build_agent(tmp_path)
    state.confirm_search_query("AI agent 深圳 TypeScript", ["深圳"])

    removed = await agent.handle("去除 typescript")
    restarted = await agent.handle("重新搜索")

    assert "已从持久化搜索关键词中移除" in removed.message
    assert state.read().preferences.search_keywords.values == ["AI agent"]
    assert restarted.action == "confirm_search"
    assert restarted.action_query == "AI agent"
    assert restarted.action_locations == ["深圳"]
    assert llm.calls == 0


async def test_job_search_results_reads_store_instead_of_starting_search(tmp_path: Path) -> None:
    class FakeJobStore:
        def list_jobs(self, stage=None, limit=100):
            return [
                SimpleNamespace(
                    role="AI Agent 开发工程师",
                    company="示例科技",
                    location="深圳",
                    salary="25-40K",
                    score=91,
                )
            ]

    agent, _state, llm = build_agent(tmp_path)
    agent.job_store = FakeJobStore()

    response = await agent.handle("职位搜索结果")

    assert response.action is None
    assert response.lane == "market"
    assert "AI Agent 开发工程师" in response.message
    assert "匹配度 91" in response.message
    assert all(event.tool != "search_intent" for event in response.tool_events)
    assert llm.calls == 0
