from __future__ import annotations

import asyncio

from cryptography.fernet import Fernet

from app.core.agent_tools import AgentToolRegistry
from app.core.models import BrowserTaskView
from app.core.main_agent import AutoHuntAgent
from app.llm.base import LLMToolCall, LLMToolResult
from app.memory.candidate_state import CandidateStateStore
from app.jobs.store import JobStore
from app.memory.resume_workspace import ResumeWorkspaceStore
from app.sandbox_backend.filesystem import WorkspaceSandbox
from app.memory.stores import MemoryStore, SkillStore
from app.runtime.manager import AgentRuntime
from app.runtime.store import RunStore


class ScriptedLLM:
    def __init__(self, first: LLMToolResult):
        self.first = first
        self.calls: list[list[dict]] = []

    async def generate_with_tools(self, *, instructions, messages, tools):
        self.calls.append(messages)
        if len(self.calls) == 1:
            return self.first
        return LLMToolResult(text="已完成。", provider="test", model="test")


class FakeBrowser:
    def __init__(self):
        self.starts = 0
        self.requests = []

    def current(self):
        return BrowserTaskView()

    async def start(self, request):
        self.starts += 1
        self.requests.append(request)
        return BrowserTaskView(id="task-1", status="running", query=request.query)

    async def resume(self):
        return BrowserTaskView(id="task-1", status="running")

    async def stop(self):
        return BrowserTaskView(id="task-1", status="stopped")


class NoEvidence:
    def list_recent(self, limit=5):
        return []


async def test_native_checkpoint_resumes_after_read_without_repeating_tool(tmp_path):
    entered = asyncio.Event()
    class InterruptedLLM(ScriptedLLM):
        async def generate_with_tools(self, **kwargs):
            if not self.calls:
                return await super().generate_with_tools(**kwargs)
            entered.set()
            await asyncio.Event().wait()
    llm = InterruptedLLM(tool_result("job_results_read"))
    agent, jobs, browser = make_agent(tmp_path, llm)
    runtime = AgentRuntime(RunStore(tmp_path / "runtime.db"), agent, jobs, browser, None, None)
    run = runtime.submit("default", "read", "chat", {"message": "查看职位结果"})
    await entered.wait()
    await runtime.shutdown()
    saved = runtime.store.get(run["id"])
    assert saved["checkpoint"]["step"] == 1
    assert saved["checkpoint"]["messages"][-1]["role"] == "tool"
    agent.llm = ScriptedLLM(LLMToolResult(text="已读到保存的结果。", provider="test", model="test"))
    child = await runtime.resume(run["id"])
    result = await runtime.wait_response(child["id"])
    assert "保存的结果" in result["message"]
    assert not any(event["kind"] == "tool_started" for event in runtime.store.events(child["id"]))
    assert browser.starts == 0
    await runtime.shutdown()


def make_agent(tmp_path, llm):
    data = tmp_path / "data"
    sandbox = WorkspaceSandbox(data)
    data_key = Fernet.generate_key().decode()
    candidate = CandidateStateStore(data, data_key)
    resume = ResumeWorkspaceStore(data, data_key, candidate)
    resume.ensure_document(candidate.read().profile)
    jobs = JobStore(data / "jobs.db")
    browser = FakeBrowser()
    memory = MemoryStore(sandbox)
    registry = AgentToolRegistry(
        jobs=jobs,
        browser=browser,
        candidate_state=candidate,
        resume_workspace=resume,
        memory=memory,
    )
    agent = AutoHuntAgent(
        memory,
        SkillStore(sandbox),
        candidate,
        NoEvidence(),
        llm,
        resume,
        jobs,
        registry,
    )
    return agent, jobs, browser


def tool_result(name: str, arguments: str = "{}") -> LLMToolResult:
    return LLMToolResult(
        text="",
        provider="test",
        model="test",
        tool_calls=[LLMToolCall(id="call-1", name=name, arguments=arguments)],
        assistant_message={"role": "assistant", "content": None},
    )


async def test_results_query_reads_store_without_starting_browser(tmp_path):
    llm = ScriptedLLM(tool_result("job_results_read"))
    agent, jobs, browser = make_agent(tmp_path, llm)
    jobs.upsert_job({
        "source": "BOSS",
        "source_url": "https://www.zhipin.com/job_detail/1.html",
        "external_id": "1",
        "title": "AI Agent Engineer",
        "company": "Example",
        "location": "Shenzhen",
        "salary": "TBD",
        "description": "AI Agent development.",
        "tags": ["Python", "LLM"],
        "score": 90,
        "decision": "APPLY",
        "reasoning": "match",
    })

    response = await agent.handle("职位搜索结果")

    assert browser.starts == 0
    assert response.action is None
    assert any(event.tool == "job_results_read" and event.status == "ok" for event in response.tool_events)
    assert len(llm.calls) == 2
    assert any(message.get("role") == "tool" for message in llm.calls[1])


async def test_complex_search_uses_main_model_and_protects_explicit_conditions(tmp_path):
    from app.core.models import CandidateProfileView
    llm = ScriptedLLM(tool_result("job_search_start", '{"query":"信用风险分析师","result_limit":30,"salary_min":10000}'))
    agent, _jobs, browser = make_agent(tmp_path, llm)
    agent.candidate_state.apply_resume_profile(CandidateProfileView(resume_id="finance", role_suggestions=["信用风险分析师"], skills=["SQL"], summary="金融风控"), "信用风险分析师")
    before = agent.candidate_state.read().preferences.model_dump()
    response = await agent.handle("找15份我的专业相关工作 薪资水平在14k以上 并生成总结")
    assert len(llm.calls) == 1
    assert browser.starts == 1
    request = browser.requests[0]
    assert request.query == "信用风险分析师"
    assert request.result_limit == 15
    assert request.salary_min == 14000
    assert request.max_jobs == 75
    assert "信用风险分析师" in str(llm.calls[0])
    assert agent.candidate_state.read().preferences.model_dump() == before
    assert any(event.tool == "job_search_start" for event in response.tool_events)
    assert "15 个" in response.message and "14,000" in response.message


async def test_direct_router_cannot_bypass_complex_search_model(tmp_path):
    from app.intent.router import RouteOutcome
    class DirectRouter:
        async def route(self, message):
            return RouteOutcome("direct", "search_jobs", "rule")
    llm = ScriptedLLM(tool_result("job_search_start", '{"query":"金融风控"}'))
    agent, _jobs, browser = make_agent(tmp_path, llm)
    from app.core.models import CandidateProfileView
    agent.candidate_state.apply_resume_profile(CandidateProfileView(resume_id="finance", role_suggestions=["金融风控"]), "金融")
    agent.router = DirectRouter()
    await agent.handle("找15份我的专业相关工作 薪资水平在14k以上 并生成总结")
    assert len(llm.calls) == 1
    assert browser.requests[0].result_limit == 15
    assert browser.requests[0].salary_min == 14000


async def test_search_executor_rejects_sentence_as_keyword(tmp_path):
    from app.core.agent_tools import ToolLoopState
    llm = ScriptedLLM(tool_result("job_search_start"))
    agent, _jobs, browser = make_agent(tmp_path, llm)
    outcome = await agent.tool_registry.execute("job_search_start", {"query": "15份我的专业相关工作 薪资水平在14k以上 并生成总结"},
                                               user_message="找15份我的专业相关工作 薪资水平在14k以上 并生成总结", state=ToolLoopState(explicit_search_intent=True))
    assert outcome.status == "error"
    assert browser.starts == 0


async def test_policy_denies_model_attempt_to_search_for_results_query(tmp_path):
    llm = ScriptedLLM(tool_result("job_search_start", '{"query":"结果"}'))
    agent, _jobs, browser = make_agent(tmp_path, llm)

    response = await agent.handle("职位搜索结果")

    assert browser.starts == 0
    assert any(event.tool == "job_search_start" and event.status == "denied" for event in response.tool_events)


async def test_explicit_restart_can_start_browser_once(tmp_path):
    llm = ScriptedLLM(tool_result("job_search_start", '{"query":"AI Agent"}'))
    agent, _jobs, browser = make_agent(tmp_path, llm)

    response = await agent.handle("重新搜索 AI Agent 职位")

    assert browser.starts == 1
    assert any(event.tool == "job_search_start" and event.status == "ok" for event in response.tool_events)


async def test_want_to_find_work_is_an_explicit_search_command(tmp_path):
    llm = ScriptedLLM(tool_result(
        "job_search_start",
        '{"query":"AI agent AI算法工程师","locations":["深圳","杭州","北京","上海"]}',
    ))
    agent, _jobs, browser = make_agent(tmp_path, llm)

    response = await agent.handle("我想找AI agent AI算法工程师的工作，地点深圳杭州北京上海")

    assert browser.starts == 1
    assert any(event.tool == "job_search_start" and event.status == "ok" for event in response.tool_events)
    assert llm.calls == []
    assert browser.requests[0].query == "AI agent"
    assert browser.requests[0].queries == ["AI agent", "AI算法工程师"]
    assert browser.requests[0].locations == ["深圳", "杭州", "北京", "上海"]

async def test_simple_find_work_bypasses_llm(tmp_path):
    llm = ScriptedLLM(tool_result("job_search_start", '{"query":"should-not-be-used"}'))
    agent, _jobs, browser = make_agent(tmp_path, llm)
    from app.core.models import CandidateProfileView
    agent.candidate_state.apply_resume_profile(CandidateProfileView(resume_id="finance", role_suggestions=["金融分析师"]), "金融")

    response = await agent.handle("我要找工作")

    assert browser.starts == 1
    assert llm.calls == []
    assert response.lane == "application"
    assert any(event.tool == "job_search_start" and event.status == "ok" for event in response.tool_events)


async def test_mock_dry_run_search_returns_explicit_confirmation_without_llm(tmp_path):
    llm = ScriptedLLM(tool_result("job_search_start", '{"query":"should-not-be-used"}'))
    agent, _jobs, browser = make_agent(tmp_path, llm)

    response = await agent.handle("Dry Run 搜索 AI Agent 职位，地点深圳")

    assert browser.starts == 0
    assert llm.calls == []
    assert response.action == "confirm_search"
    assert response.action_dry_run is True
    assert response.action_locations == ["深圳"]
    assert "模拟岗位" in response.message


def test_dry_run_counters_are_persisted_and_exposed_in_stats(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    task = store.create_task("dry-task", "mock", "AI Agent", "cdp", dry_run=True)
    assert task.dry_run is True

    store.update_task(
        "dry-task",
        status="completed",
        dry_run_candidates=3,
        dry_run_contacted=3,
        dry_run_replied=1,
        dry_run_submitted=1,
        dry_run_waiting_reply=2,
        dry_run_failed=0,
        dry_run_report="Dry Run 完成",
    )

    saved = store.get_task("dry-task")
    stats = store.stats()
    assert saved.dry_run_report == "Dry Run 完成"
    assert stats.dry_run_candidates == 3
    assert stats.dry_run_contacted == 3
    assert stats.dry_run_replied == 1
    assert stats.dry_run_submitted == 1
    assert stats.dry_run_waiting_reply == 2
    assert stats.dry_run_failed == 0


async def test_direct_stop_returns_partial_search_summary(tmp_path):
    llm = ScriptedLLM(tool_result("browser_task_stop"))
    agent, _jobs, browser = make_agent(tmp_path, llm)

    async def stop_with_progress():
        return BrowserTaskView(
            id="task-1",
            status="stopped",
            discovered=127,
            stored=9,
            matched=8,
            duplicates=2,
        )

    browser.stop = stop_with_progress
    response = await agent._direct_browser_control("stop_search")

    assert "已中断职位搜索" in response.message
    assert "127" in response.message
    assert "已保存到看板：**9**" in response.message
    assert "建议审核：**8**" in response.message
