import asyncio
import json
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.core.models import BrowserTaskStatus, CandidateProfileView
from app.growth.plans import LearningCompletion, LearningPlanService, is_explicit_plan_request
from app.jobs.store import JobStore
from app.memory.candidate_state import CandidateStateStore
from app.memory.resume_workspace import ResumeWorkspaceStore
from app.memory.resume_workspace import ResumeDocumentExtractor


class FakeLLM:
    def __init__(self, payload: dict):
        self.payload = payload

    async def generate(self, _request):
        return SimpleNamespace(text=json.dumps(self.payload, ensure_ascii=False), provider="fake", model="test")


def make_service(tmp_path, *, quote="熟悉信用风险策略分析，能够使用 SQL 构建风险监控指标体系"):
    root = tmp_path / "data"
    key = Fernet.generate_key().decode()
    candidates = CandidateStateStore(root, key)
    workspace = ResumeWorkspaceStore(root, key, candidates)
    workspace.ensure_document(candidates.read().profile)
    candidates.apply_resume_profile(
        CandidateProfileView(resume_id="finance-resume", role_suggestions=["风险分析师"], skills=["Python"]),
        "风险分析师",
    )
    jobs = JobStore(root / "jobs.db")
    jobs.create_task("old-ai", "mock", "AI Agent", resume_id="ai-resume")
    ai_id, _ = jobs.upsert_job({
        "source": "MockJobSite", "source_url": "http://127.0.0.1:8080/job.html?id=1", "external_id": "1",
        "title": "AI Agent 工程师", "company": "AI 公司", "location": "北京", "salary": "20-30K",
        "description": "负责 Agent 与 Transformer 平台开发，需要 SFT、RLHF 等经验和项目实践。",
        "tags": ["LLM"], "score": 90, "decision": "REVIEW", "reasoning": "匹配",
    })
    jobs.event("old-ai", "job_stored", "AI", sample={"job_id": ai_id})
    jobs.update_task("old-ai", status=BrowserTaskStatus.COMPLETED)
    jobs.create_task("finance", "mock", "风险分析师", resume_id="finance-resume")
    finance_id, _ = jobs.upsert_job({
        "source": "MockJobSite", "source_url": "http://127.0.0.1:8080/job.html?id=2", "external_id": "2",
        "title": "信用风险分析师", "company": "金融机构", "location": "上海", "salary": "15-25K",
        "description": "岗位职责：" + quote + "，并撰写风险分析报告，支持业务决策。",
        "tags": ["SQL"], "score": 70, "decision": "REVIEW", "reasoning": "待补足",
    })
    jobs.event("finance", "job_stored", "风控", sample={"job_id": finance_id})
    jobs.update_task("finance", status=BrowserTaskStatus.COMPLETED)
    payload = {
        "title": "金融风险分析岗位学习计划",
        "summary": "围绕实际信用风险岗位要求，分阶段补足监控指标、策略分析与实践证据。",
        "gaps": [{
            "category": "domain_knowledge", "name": "信用风险策略分析", "priority": "high",
            "resume_quote": "", "jd_quotes": [{"job_id": finance_id, "quote": quote}],
            "rationale": "JD 明确要求风险监控指标体系，当前简历未体现相关实践。",
            "objective": "能够独立定义并验证一套信用风险监控指标体系。",
            "estimated_hours": 24, "prerequisites": ["统计学基础"],
            "steps": ["阅读风险指标定义并梳理业务目标", "用公开数据计算逾期率及分层指标", "对策略阈值做样本外验证"],
            "practice": "提交一份含数据口径、SQL 和策略回测的分析报告。",
            "acceptance": ["指标口径和查询可复现", "报告包含分层结果及局限性"],
            "resume_target": "skill",
        }],
    }
    return LearningPlanService(jobs, candidates, workspace, FakeLLM(payload)), jobs, workspace, finance_id


def test_plan_command_requires_clear_positive_action():
    assert is_explicit_plan_request("根据最近的 JD 生成学习计划")
    assert is_explicit_plan_request("帮我制定一份学习计划")
    assert not is_explicit_plan_request("怎么生成学习计划？")
    assert not is_explicit_plan_request("不要生成学习计划")


def test_generation_timeout_returns_json_error(monkeypatch):
    from app.api import application
    from app.core.config import settings
    from app.main import app

    async def timed_out(_focus):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(application.learning_plans, "generate", timed_out)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/learning-plans",
            json={"focus": "岗位差距"},
            headers={"X-AutoHunt-Key": settings.api_key or ""},
        )
    assert response.status_code == 504
    assert response.headers["content-type"].startswith("application/json")
    assert "超时" in response.json()["detail"]


@pytest.mark.asyncio
async def test_plan_uses_only_current_resume_jd_and_verifies_quotes(tmp_path):
    service, jobs, _workspace, finance_id = make_service(tmp_path)
    plan = await service.generate()
    assert plan["jobs_analyzed"] == 1
    assert plan["gaps"][0]["frequency"] == 1
    assert plan["gaps"][0]["jd_quotes"][0]["job_id"] == finance_id
    assert "AI Agent" not in str(plan)
    assert service.list()[0]["id"] == plan["id"]


@pytest.mark.asyncio
async def test_plan_rejects_ungrounded_jd_requirement(tmp_path):
    service, jobs, _workspace, _finance_id = make_service(tmp_path)
    service.llm.payload["gaps"][0]["jd_quotes"][0]["quote"] = "必须精通 RLHF 并发表三篇论文"
    with pytest.raises(ValueError, match="原始 JD"):
        await service.generate()
    assert service.list() == []


@pytest.mark.asyncio
async def test_completion_creates_reviewable_skill_patch_not_immediate_resume_edit(tmp_path):
    service, _jobs, workspace, _finance_id = make_service(tmp_path)
    plan = await service.generate()
    gap_id = plan["gaps"][0]["id"]
    result = await service.complete(plan["id"], gap_id, LearningCompletion(
        evidence="完成了公开信用数据的逾期率指标分析，编写 SQL 查询和分层报告，并对不同策略阈值做样本外验证。",
        resume_target="skill",
    ))
    proposal = result["resume_proposal"]
    assert proposal["operations"][0]["path"] == "skills"
    assert "信用风险策略分析" not in workspace.read_document().skills
    workspace.approve(proposal["id"])
    assert "信用风险策略分析" in workspace.read_document().skills
    assert "信用风险策略分析" in service.candidates.read().profile.skills
    with pytest.raises(ValueError, match="不能重复"):
        await service.complete(plan["id"], gap_id, LearningCompletion(
            evidence="再次尝试重复标记完成，此时不应生成第二份简历提案或重复写入同一技能。",
            resume_target="skill",
        ))


@pytest.mark.asyncio
async def test_project_completion_uses_user_evidence_and_waits_for_approval(tmp_path):
    service, _jobs, workspace, _finance_id = make_service(tmp_path)
    plan = await service.generate()
    service.llm.payload = {
        "summary": "基于公开信用数据构建分层风险监控报告，独立设计逾期率指标口径与 SQL 查询，并对不同策略阈值进行回测验证。",
        "architecture": "公开信用数据、SQL 查询、分层分析与策略阈值回测。",
        "skills": ["SQL", "未使用的神奇工具"],
        "highlights": ["统一逾期率口径，完成分层风险监控报告。"],
        "optimizations": ["对不同策略阈值做样本外回测。"],
    }
    evidence = (
        "我使用公开信用数据完成信用风险分析项目，设计逾期率及分层监控指标，"
        "用 SQL 编写可复现查询，整理策略分析报告，并对不同策略阈值做样本外回测。"
        "项目产物包含数据字典、查询脚本和报告，我负责指标设计、实现和局限性分析。"
    )
    result = await service.complete(plan["id"], plan["gaps"][0]["id"], LearningCompletion(
        evidence=evidence, resume_target="project", project_title="信用风险监控与策略回测",
    ))
    proposal = result["resume_proposal"]
    assert proposal["operations"][0]["path"] == "entries"
    assert not workspace.read_document().entries
    entry = proposal["operations"][0]["new_value"]
    assert entry["skills"] == ["SQL"]
    workspace.approve(proposal["id"])
    assert workspace.read_document().entries[0].title == "信用风险监控与策略回测"


def test_approved_skill_is_visible_without_rewriting_original_resume(tmp_path):
    service, _jobs, workspace, _finance_id = make_service(tmp_path)
    original = "候选人原始简历正文\n项目经历：完成风险模型数据清洗和结果分析。"
    imported = ResumeDocumentExtractor.import_verbatim(
        original, service.candidates.read().profile, "风险简历.pdf",
    )
    workspace.replace_from_upload(imported)
    proposal = workspace.propose_learning_completion(
        skill="信用风险策略分析", evidence_summary="我完成了公开数据的风险监控指标研究与策略验证。",
    )
    assert proposal is not None
    workspace.approve(proposal.id)
    rendered = workspace.render_markdown(workspace.read_document())
    assert rendered.startswith(original)
    assert "## 补充技能" in rendered
    assert "信用风险策略分析" in rendered
