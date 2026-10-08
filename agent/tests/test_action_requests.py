"""User action, range continuation and mock-only batch authority regressions."""
import asyncio
from types import SimpleNamespace

import pytest

from app.core.action_language import explicit_mock_delivery, plan_scope_answer, score_scope
from app.core.agent_tools import AgentToolRegistry, ToolLoopState
from app.core.models import BrowserTaskStatus, DeliveryTaskStatus
from app.growth.plans import is_explicit_plan_request
from app.growth.requests import continued_plan_scope, resume_stamp
from app.runtime.context import RunContext, current_run
from app.runtime.dialogue import apply_operation
from app.workflow.mock_coordinator import MockWorkflowCoordinator
from test_agent_tool_loop import make_agent, ScriptedLLM
from test_learning_plans import make_service


@pytest.mark.parametrize("message", ["匹配分数在80以下的分析不足 并生成成长计划", "帮我制定一份成长计划", "给我一份提升计划", "生成学习方案"])
def test_plan_synonyms_are_commands(message):
    assert is_explicit_plan_request(message)


@pytest.mark.parametrize("message", ["不要制定成长计划", "怎么生成学习计划", "可以生成成长计划吗？", "文件说“生成学习计划”", "先别生成成长计划"])
def test_plan_questions_negation_and_quotes_are_not_commands(message):
    assert not is_explicit_plan_request(message)


def test_score_scope_and_delivery_semantics():
    assert score_scope("90分以下的") == {"max_score": 89}
    assert score_scope("不低于90分") == {"min_score": 90}
    assert explicit_mock_delivery("匹配分数在90以上的自动投递") == {"min_score": 90}
    assert explicit_mock_delivery("不要自动投递90分以上的") is None
    assert explicit_mock_delivery("90分以上可以自动投递吗？") is None
    assert plan_scope_answer("那就90分以下的") == {"max_score": 89}
    assert plan_scope_answer("那就90分以下的，先不要生成") is None
    with pytest.raises(ValueError):
        score_scope("90分以上80分以下")


def add_job(jobs, score, external_id, resume_id="finance-resume", real=False):
    task_id = f"task-{external_id}"
    jobs.create_task(task_id, "mock", "风控", resume_id=resume_id)
    job_id, _ = jobs.upsert_job({"source": "MockJobSite", "source_url": f"{'https://www.zhipin.com' if real else 'http://127.0.0.1:8080'}/job.html?id={external_id}",
        "external_id": str(external_id), "title": f"风控分析{external_id}", "company": "银行", "location": "上海", "salary": "15-25K",
        "description": "熟悉信用风险策略分析，能够使用 SQL 构建风险监控指标体系，并撰写风险分析报告，支持业务决策。",
        "tags": ["SQL"], "score": score, "decision": "REVIEW", "reasoning": "简历未体现专项业务经验"})
    jobs.event(task_id, "job_stored", "测试", sample={"job_id": job_id})
    jobs.update_task(task_id, status=BrowserTaskStatus.COMPLETED)
    return job_id


@pytest.mark.asyncio
async def test_no_samples_then_range_answer_executes_original_plan(tmp_path):
    service, jobs, workspace, job_id = make_service(tmp_path / "service")
    with jobs._connect() as db:
        db.execute("UPDATE jobs SET score=88 WHERE id=?", (job_id,))
    add_job(jobs, 90, 3)
    agent, _unused, _browser = make_agent(tmp_path / "agent", ScriptedLLM(SimpleNamespace()))
    registry = AgentToolRegistry(jobs=jobs, browser=agent.tool_registry.browser, candidate_state=service.candidates,
        resume_workspace=workspace, memory=agent.memory, learning_plans=service)
    agent.jobs, agent.tool_registry, agent.learning_plans = jobs, registry, service
    agent.resume_workspace, agent.candidate_state = workspace, service.candidates
    calls = []
    original = service.llm.generate
    async def recording(request):
        calls.append(request.user_message)
        return await original(request)
    service.llm.generate = recording
    first = await agent.handle("匹配分数在80以下的分析不足 并生成成长计划")
    assert "没有生成计划" in first.message and calls == []
    second = await agent.handle("那就90分以下的")
    assert "已根据当前简历" in second.message
    assert len(calls) == 1
    plan = service.list()[0]
    assert plan["score_scope"] == {"min_score": None, "max_score": 89}
    assert [item["id"] for item in plan["job_sources"]] == [job_id]
    assert agent._local_dialogue["learning_request"] is None
    assert agent.llm.calls == []  # No model routing needed; only the plan authoring model ran.


def test_range_answer_does_not_authorize_without_live_original_request(tmp_path):
    service, _jobs, workspace, _id = make_service(tmp_path)
    stamp = resume_stamp(workspace.read_document(), service.candidates)
    state = apply_operation({}, {"op": "begin", "message": "生成成长计划"})
    assert continued_plan_scope(state, "那就90分以下的", stamp) is None
    state = apply_operation(state, {"op": "learning_request", "message": "生成成长计划", "resume_stamp": stamp})
    assert continued_plan_scope(state, "那就90分以下的", stamp) == {"max_score": 89}
    assert continued_plan_scope(state, "那就90分以下的", "new-resume") is None
    expired = {**state, "learning_request": {**state["learning_request"], "expires_at": 0}}
    assert continued_plan_scope(expired, "那就90分以下的", stamp) is None
    state = apply_operation(state, {"op": "begin", "message": "帮我找工作"})
    assert continued_plan_scope(state, "那就90分以下的", stamp) is None


@pytest.mark.asyncio
async def test_mock_batch_threshold_current_resume_and_real_site_exclusion(tmp_path):
    service, jobs, workspace, _finance = make_service(tmp_path)
    jobs.bind_mock_applicant(31)
    high = add_job(jobs, 90, 3)
    add_job(jobs, 89, 4)
    add_job(jobs, 99, 5, resume_id="old-resume")
    add_job(jobs, 99, 6, real=True)
    from app.delivery.automation import DeliveryAutomationService
    delivery = DeliveryAutomationService(jobs, tmp_path, headless=True, cdp_url="http://127.0.0.1:9222")
    coordinator = MockWorkflowCoordinator(jobs, SimpleNamespace(worker=None), delivery, service.candidates, workspace)
    result = await coordinator.queue_deliveries(min_score=90, session_id="test")
    assert result["job_ids"] == [high]
    assert "不是已投递" in result["message"]
    again = await coordinator.queue_deliveries(min_score=90, session_id="test")
    assert again["queued"] == 0
    assert not jobs.mock_workflow_settings()["auto_submit"]
    assert not jobs.mock_workflow_settings()["daily_enabled"]
    assert delivery.worker is None  # Queue registration alone never clicks.
    jobs.update_mock_delivery(high, "dispatched", "interrupted before dispatch")
    await coordinator.process_delivery_queue()
    assert jobs.mock_delivery_queue(active_only=False)[0]["status"] == "blocked"
    assert delivery.worker is None  # No replay of an uncertain dispatch.


@pytest.mark.asyncio
async def test_model_cannot_change_threshold_or_authorize_real_site(tmp_path):
    agent, jobs, _browser = make_agent(tmp_path, ScriptedLLM(SimpleNamespace()))
    calls = []
    async def queue(**kwargs):
        calls.append(kwargs)
        return {"message": "已入队", "queued": 1}
    agent.tool_registry.mock_workflow = SimpleNamespace(queue_deliveries=queue)
    agent.tool_registry._tools = {spec.name: spec for spec in agent.tool_registry._build_specs()}
    response = await agent.handle("匹配分数在90以上的自动投递")
    assert response.message == "已入队" and calls[0]["min_score"] == 90
    context = RunContext("test", asyncio.current_task(), lambda *_: None, lambda _: None)
    token = current_run.set(context)
    try:
        result = await agent.tool_registry.execute("mock_delivery_start", {"min_score": 1},
            user_message="90分以上的自动投递", state=ToolLoopState())
        assert result.status == "ok" and calls[-1]["min_score"] == 90
        result = await agent.tool_registry.execute("mock_delivery_start", {"min_score": 1},
            user_message="好的", state=ToolLoopState())
        assert result.status == "denied"
        result = await agent.tool_registry.execute("mock_delivery_start", {"min_score": 90},
            user_message="BOSS直聘90分以上自动投递", state=ToolLoopState())
        assert result.status == "denied"
    finally:
        current_run.reset(token)


@pytest.mark.asyncio
async def test_queue_communicates_all_jobs_then_submits_only_after_real_reply(tmp_path, monkeypatch):
    from app.delivery.automation import DeliveryAutomationService
    from app.delivery.mock_site_adapter import MockApplyObservation, MockSubmitResult, MockJobSiteAdapter
    from test_delivery_automation import FakeSession
    service, jobs, workspace, _finance = make_service(tmp_path)
    jobs.bind_mock_applicant(31)
    first, second = add_job(jobs, 91, 7), add_job(jobs, 94, 8)
    monkeypatch.setattr("app.delivery.automation.BrowserSession", FakeSession)
    replies, contacts, submissions = set(), [], []
    class Adapter(MockJobSiteAdapter):
        async def prepare(self, _page, url):
            external_id = int(url.rsplit("=", 1)[1])
            if external_id not in contacts:
                contacts.append(external_id)
            if external_id not in replies:
                return MockApplyObservation(True, None, False, True, "等待招聘方回复")
            return MockApplyObservation(True, None, True, False, "发送简历", hr_reply_text="请发送简历")
        async def submit(self, _page):
            submissions.append(1)
            return MockSubmitResult(True, False, "已发送")
    delivery = DeliveryAutomationService(jobs, tmp_path, headless=True, cdp_url="http://127.0.0.1:9222")
    delivery.adapter = Adapter()
    coordinator = MockWorkflowCoordinator(jobs, SimpleNamespace(worker=None), delivery, service.candidates, workspace)
    await coordinator.queue_deliveries(min_score=90, session_id="test")
    await coordinator.process_delivery_queue()
    await delivery.worker
    await coordinator.process_delivery_queue()
    await delivery.worker
    assert sorted(contacts) == [7, 8]
    assert submissions == []
    assert jobs.get_job(first)["stage"] == jobs.get_job(second)["stage"] == "applying"
    replies.add(7)
    for _ in range(4):
        await coordinator.process_delivery_queue()
        if delivery.worker:
            await delivery.worker
    assert submissions == [1]
    assert jobs.get_job(first)["stage"] == "waiting"
    assert jobs.get_job(second)["stage"] == "applying"
    queue = {item["job_id"]: item for item in jobs.mock_delivery_queue(active_only=False)}
    assert queue[first]["status"] == "completed" and queue[second]["status"] == "dispatched"
    await coordinator.cancel_delivery_queue("test")
    assert jobs.mock_delivery_queue() == []
