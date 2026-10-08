import json
import time

import pytest

from app.browser.automation import BrowserAutomationService
from app.core.models import BrowserSearchRequest, BrowserTaskStatus, CandidateProfileView, JobAnalysis
from app.jobs.search_reporting import jd_evidence
from app.runtime.dialogue import apply_operation, resolve_reference, pending_action
from app.runtime.manager import AgentRuntime
from app.runtime.search_state import merge_search_state, prepare_search_state
from app.runtime.store import RunStore
from test_agent_tool_loop import make_agent, ScriptedLLM, tool_result
from test_job_store import sample_job


async def test_restart_after_long_idle_preserves_all_conditions_and_uses_no_model(tmp_path):
    llm = ScriptedLLM(tool_result("job_search_start"))
    agent, _jobs, browser = make_agent(tmp_path, llm)
    agent.candidate_state.apply_resume_profile(CandidateProfileView(resume_id="trade", role_suggestions=["外贸业务员"]), "外贸")
    await agent.handle("找工作，目标数量15个，月薪12k以上，城市深圳上海杭州武汉")
    previous = browser.requests[-1]
    agent._local_dialogue["last_activity"] = time.time() - 86400
    await agent.handle("再次搜索")
    request = browser.requests[-1]
    assert request.result_limit == previous.result_limit == 15
    assert request.salary_min == previous.salary_min == 12000
    assert request.locations == previous.locations == ["深圳", "上海", "杭州", "武汉"]
    assert request.queries == previous.queries == ["外贸业务员"]
    assert llm.calls == []


def test_api_state_read_is_not_an_idle_reset(tmp_path):
    agent, jobs, browser = make_agent(tmp_path, ScriptedLLM(tool_result("job_search_start")))
    runtime = AgentRuntime(RunStore(tmp_path / "runtime.db"), agent, jobs, browser, None, None)
    saved = runtime.store.save_search_state("default", merge_search_state(prepare_search_state(None),
        {"queries": ["护士"], "locations": ["杭州"], "result_limit": 15, "salary_min": 12000}))
    with runtime.store.connect() as db:
        state = apply_operation(None, {"op": "begin", "message": "找工作"}, clock=1000)
        db.execute("INSERT INTO session_dialogue_state VALUES (?,?,?)", ("default", json.dumps(state), "2000-01-01"))
    actual = runtime.search_state("default")
    assert actual["result_limit"] == 15 and actual["salary_min"] == 12000
    assert actual["queries"] == saved["queries"]


@pytest.mark.parametrize("description,expected", [
    ("【合成测试职位】本岗位仅用于测试。\n【岗位背景】金融行业\n【岗位职责】\n1. 分析信用风险并建立指标。\n2. 监测资产质量。\n【岗位要求】\n1. 熟悉监管政策。", "分析信用风险"),
    ("工作职责：\n1. 评估患者护理需求。\n2. 记录治疗进展。\n任职要求：持有护士资格证。", "评估患者护理需求"),
    ("岗位职责：\n1. 设计课程并组织教学。\n任职资格：具有教师资格证。", "设计课程"),
    ("负责机械结构设计、试制与验证。熟悉质量规范。", "机械结构设计"),
])
def test_industry_neutral_briefs_extract_real_duties_not_boilerplate(description, expected):
    evidence = jd_evidence(description)
    assert expected in evidence["brief"]
    assert "仅用于" not in evidence["brief"] and "合成测试" not in evidence["brief"]
    assert len(evidence["brief"]) <= 140


def test_read_context_survives_but_old_action_authority_does_not():
    state = apply_operation(None, {"op": "begin", "message": "查看职位"}, clock=1000)
    state = apply_operation(state, {"op": "snapshot", "topic": "applications", "kind": "job",
        "items": [{"id": 8, "title": "岗位8"}]}, clock=1000)
    state = apply_operation(state, {"op": "propose", "topic": "applications", "tool": "job_search_start",
        "arguments": {"queries": ["护士"]}, "description": "搜索"}, clock=1000)
    state = apply_operation(state, {"op": "begin", "message": "详细了解第1个"}, clock=86400)
    assert state["snapshots"] and state["pending"][0]["status"] == "expired"
    assert resolve_reference(state, kind="job", selector="第1个", clock=86400, read_only=True)["items"][0]["id"] == 8
    with pytest.raises(ValueError):
        resolve_reference(state, kind="job", selector="第1个", clock=86400)
    with pytest.raises(ValueError):
        pending_action(state, clock=86400)


def test_150_candidates_cannot_store_more_than_15_qualified_jobs(tmp_path):
    service = object.__new__(BrowserAutomationService)
    from app.jobs.store import JobStore
    service.store = JobStore(tmp_path / "jobs.db")
    service.store.create_task("limited", "mock", "外贸业务员")
    request = BrowserSearchRequest(query="外贸业务员", result_limit=15, salary_min=12000)
    for index in range(150):
        job = sample_job(f"http://127.0.0.1:8080/job.html?id={index}")
        job.update(title="外贸业务员", salary="16-21K", external_id=str(index))
        delta = service._persist_analysis("limited", job, JobAnalysis(score=88, decision="APPLY", matched_skills=[], missing_skills=[], reasoning="符合"), job["source_url"], source="MockJobSite", request=request)
        task = service.store.get_task("limited")
        service.store.update_task("limited", stored=task.stored + delta[0], duplicates=task.duplicates + delta[1], matched=task.matched + delta[2])
    assert service.store.get_task("limited").matched == 15
    assert len(service.store.list_jobs()) == 15


async def test_full_report_stable_ordinals_details_and_partial_statistics(tmp_path):
    llm = ScriptedLLM(tool_result("job_search_start"))
    agent, jobs, browser = make_agent(tmp_path, llm)
    runtime = AgentRuntime(RunStore(tmp_path / "runtime.db"), agent, jobs, browser, None, None)
    jobs.create_task("partial", "mock", "外贸业务员", resume_id="trade")
    for index in range(1, 41):
        job = sample_job(f"http://127.0.0.1:8080/job.html?id={index}")
        job.update(title=f"外贸业务员{index}", company=f"公司{index}", score=88, external_id=str(index),
            description="【合成测试职位】本岗位仅用于测试。\n【岗位职责】\n1. 开发海外客户，跟进报价。\n2. 管理合同。\n【岗位要求】\n1. 熟悉商务英语。\n【福利待遇】双休、五险一金。")
        job_id, _ = jobs.upsert_job(job)
        jobs.event("partial", "job_stored", "保存", sample={"job_id": job_id})
    jobs.update_task("partial", status=BrowserTaskStatus.STOPPED, discovered=150, stored=40)
    run, _ = runtime.store.create("default", "summary", "chat", {})
    runtime.browser_summary(run["id"], jobs.get_task("partial"))
    runtime.browser_summary(run["id"], jobs.get_task("partial"))  # Idempotent report/snapshot.
    report = jobs.search_report("partial")
    assert len(report["jobs"]) == len(report["highlights"]) == 40
    assert "整体薪资与 JD 总结" in report["message"] and "40. **" in report["message"]
    assert "开发海外客户（40 个岗位提及）" in report["message"]
    assert report["salary_overview"]["monthly_jobs"] == 40
    assert len(runtime.store.dialogue_state("default")["snapshots"]) == 1
    # A later board reorder must not change ordinal 8 from the shown report.
    other = sample_job("http://127.0.0.1:8080/job.html?id=high")
    other.update(title="后新增高分职位", score=99, external_id="high")
    jobs.upsert_job(other)
    try:
        detail_run = runtime.submit("default", "detail8", "chat", {"message": "我想详细了解一下第8个"})
        response = await runtime.wait_response(detail_run["id"])
        assert "外贸业务员8" in response["message"] and "完整 JD" in response["message"]
        assert "http://127.0.0.1:8080/job.html?id=8" in response["message"]
        assert browser.starts == 0 and llm.calls == []
        assert any(event["kind"] == "tool_finished" for event in runtime.store.events(detail_run["id"]))
    finally:
        await runtime.shutdown()
    insights = jobs.insights(current_only=True, resume_id="trade")
    assert insights["tasks_total"] == 1 and insights["stored_total"] == 40
    assert insights["discovered_total"] == 150
    assert insights["partial_tasks"] == 1
    assert insights["benefit_distribution"]["双休"] == 40
    assert insights["jd_overview"]["responsibilities"][0]["count"] == 40
    assert sum(item["count"] for item in insights["salaries"]) == 40


def test_long_search_report_is_not_truncated_in_persisted_history(tmp_path):
    from app.jobs.store import JobStore
    jobs = JobStore(tmp_path / "jobs.db")
    report = "整体总结\n" + "岗位简短描述\n" * 2500 + "100. 最后一个岗位"
    assert len(report) > 12000
    jobs.add_conversation_message("assistant", report, run_id="summary:large-search")
    assert jobs.conversation_history()[-1]["content"] == report
