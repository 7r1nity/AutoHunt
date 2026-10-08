"""No real browser, model, or hiring-site writes in these regressions."""
import asyncio
from types import SimpleNamespace

import pytest

from app.browser.automation import BrowserAutomationService
from app.core.agent_tools import ToolLoopState
from app.core.models import BrowserSearchRequest, BrowserTaskView, CandidateProfileView
from app.jobs.store import JobStore
from app.runtime.context import RunContext, browser_availability, current_run, scoped_browser
from test_agent_tool_loop import ScriptedLLM, make_agent, tool_result


def session_context():
    return RunContext("new-run", asyncio.current_task(), lambda *_: None, lambda *_: None,
                      session_id="new-session", dialogue={"tasks": {"browser_id": "mine"}},
                      enforce_task_scope=True)


@pytest.mark.parametrize("status", ["idle", "completed", "failed", "stopped"])
async def test_foreign_terminal_task_is_available_without_disclosing_details(status):
    view = BrowserTaskView(id="foreign", status=status, query="private-query", discovered=42,
                           detail="private-result", current_url="https://private.test/")
    browser = SimpleNamespace(current=lambda: view)
    token = current_run.set(session_context())
    try:
        data = browser_availability(browser)
        assert data["can_start_search"] and not data["shared_browser_busy"]
        assert not data["task_owned_by_session"] and data["id"] is None
        assert data["status"] == "idle"
        assert all(value not in str(data) for value in ("foreign", "private-query", "private-result", "private.test"))
        # Availability never confers permission to control someone else's task.
        with pytest.raises(ValueError, match="不属于"):
            scoped_browser(browser)
    finally:
        current_run.reset(token)


@pytest.mark.parametrize("status", ["starting", "running", "paused", "waiting_for_user"])
async def test_foreign_active_task_exposes_only_busy_state(tmp_path, status):
    agent, _jobs, browser = make_agent(tmp_path, ScriptedLLM(tool_result("job_search_start")))
    browser.current = lambda: BrowserTaskView(id="foreign", status=status, query="private-query")
    token = current_run.set(session_context())
    try:
        data = await agent.tool_registry._browser_read({})
        assert not data["can_start_search"] and data["shared_browser_busy"]
        assert "private-query" not in str(data) and data["id"] is None
        for control in (agent.tool_registry._browser_resume, agent.tool_registry._browser_stop):
            with pytest.raises(ValueError, match="不属于"):
                await control({})
        outcome = await agent.tool_registry.execute("job_search_start", {"query": "护士"},
            user_message="搜索护士", state=ToolLoopState(explicit_search_intent=True))
        assert outcome.status == "error" and browser.starts == 0
    finally:
        current_run.reset(token)


async def test_complex_search_context_after_foreign_completion_still_starts(tmp_path):
    llm = ScriptedLLM(tool_result("job_search_start", '{"result_limit":15,"salary_min":12000}'))
    agent, _jobs, browser = make_agent(tmp_path, llm)
    agent.candidate_state.apply_resume_profile(CandidateProfileView(resume_id="trade", role_suggestions=["外贸业务员"]), "外贸业务员")
    browser.current = lambda: BrowserTaskView(id="foreign", status="completed", query="secret-old-query")
    token = current_run.set(session_context())
    try:
        result = await agent.handle("帮我找工作，目标数量15个，薪资在12k以上")
        assert browser.starts == 1 and "已启动" in result.message
        request = browser.requests[0]
        assert request.queries == ["外贸业务员"]
        assert request.result_limit == 15 and request.salary_min == 12000
        assert request.conversation_id == "new-session"
        assert "secret-old-query" not in str(llm.calls)
        assert "其他会话正在使用共享浏览器；不可操作" not in str(llm.calls)
    finally:
        current_run.reset(token)


async def test_missing_role_never_creates_a_task_even_with_foreign_old_record(tmp_path):
    agent, _jobs, browser = make_agent(tmp_path, ScriptedLLM(tool_result("job_search_start")))
    browser.current = lambda: BrowserTaskView(id="foreign", status="completed")
    token = current_run.set(session_context())
    try:
        result = await agent.handle("帮我找工作")
        assert "没有启动搜索" in result.message and browser.starts == 0
    finally:
        current_run.reset(token)


async def test_stale_nonterminal_record_reconciled_without_touching_a_browser(tmp_path):
    service = BrowserAutomationService.__new__(BrowserAutomationService)
    service.store = JobStore(tmp_path / "jobs.db")
    service.store.create_task("stale", "mock", "旧方向", "auto")
    service.store.update_task("stale", status="waiting_for_user")
    service.current_id = "stale"
    service.worker = None
    service.lifecycle_lock = asyncio.Lock()
    service.gate = asyncio.Event()
    service.candidate_state = SimpleNamespace(read=lambda: SimpleNamespace(profile=SimpleNamespace(resume_id=None)))
    service.mock_adapter = SimpleNamespace(selector_version="test")

    async def isolated_worker(*_):
        return

    service._run = isolated_worker
    token = current_run.set(session_context())
    try:
        data = browser_availability(service)
        assert data["can_start_search"] and data["stale_task_record"]
        assert data["status"] == "idle"
        task = await service.start(BrowserSearchRequest(query="外贸业务员"))
        assert task.id != "stale"
        assert service.store.get_task("stale").status == "stopped"
        await service.worker
    finally:
        current_run.reset(token)


async def test_terminal_db_record_does_not_bypass_live_worker_cleanup(tmp_path):
    service = BrowserAutomationService.__new__(BrowserAutomationService)
    service.current = lambda: BrowserTaskView(id="foreign", status="completed")
    service.lifecycle_lock = asyncio.Lock()
    service.worker = asyncio.create_task(asyncio.Event().wait())
    token = current_run.set(session_context())
    try:
        data = browser_availability(service)
        assert data["shared_browser_busy"] and not data["can_start_search"]
        with pytest.raises(RuntimeError, match="清理"):
            await service.start(BrowserSearchRequest(query="护士"))
    finally:
        current_run.reset(token)
        service.worker.cancel()
        await asyncio.gather(service.worker, return_exceptions=True)
    assert service.can_start_search()
