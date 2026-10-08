from pathlib import Path
import asyncio
import json

from cryptography.fernet import Fernet

from app.core.models import CandidateEvidenceItem, CandidateProfileView, JobPreferenceUpdate
from app.memory.candidate_state import CandidateStateStore


def test_project_metrics_survive_extraction_storage_and_reload(tmp_path: Path) -> None:
    from app.llm.base import LLMResult
    from app.memory.candidate_state import CandidateEvidenceExtractor
    from app.memory.resume_workspace import _entry_from_evidence

    observation = (
        "评测结果显示，相比 Dense only（Recall@5 0.781、MRR@10 0.704、Faithfulness 0.862、"
        "Answer Correctness 0.731），Hybrid 提升至 0.842、0.768、0.895、0.812；"
        "在同一个包含 600 条问题的模拟数据集上进行评测，保留不可回答问题，结论仅适用于该测试集。"
    )
    assert len(observation) > 120

    class Model:
        async def generate(self, request):
            return LLMResult(provider="fixture", model="fixture", text=json.dumps({
                "title": "金融文档检索系统", "summary": "实现金融文档检索与评测。",
                "features": [observation], "optimizations": [observation],
                "achievements": [observation], "skills": ["Python"],
            }, ensure_ascii=False))

    item = asyncio.run(CandidateEvidenceExtractor(Model(), "本地自定义写作指令").extract(
        "项目简介：实现金融文档检索与评测。\nPython\n" + observation,
        title="README.md", kind="project", source="file",
    ))
    assert item.features == item.optimizations == item.achievements == [observation]
    key = Fernet.generate_key().decode()
    state = CandidateStateStore(tmp_path, key)
    state.apply_evidence(item)
    restored = CandidateStateStore(tmp_path, key).read().profile.evidence[0]
    assert restored.features == restored.optimizations == restored.achievements == [observation]
    # Both evidence sanitization and the resume entry conversion retain the tail.
    entry = _entry_from_evidence(restored)
    assert entry.highlights == [observation]
    assert entry.optimizations == [observation]


def test_resume_preferences_are_inferred_then_user_confirmed(tmp_path: Path) -> None:
    key = Fernet.generate_key().decode()
    store = CandidateStateStore(tmp_path, key)
    profile = CandidateProfileView(
        resume_id="a" * 32,
        skills=["Python", "FastAPI", "LLM"],
        role_suggestions=["AI工程师", "后端工程师"],
        summary="具有后端与大模型项目经验。",
    )

    inferred = store.apply_resume_profile(profile, "AI工程师 Python LLM")
    assert inferred.preferences.skills.source == "resume"
    assert inferred.preferences.target_roles.source == "inferred"
    assert inferred.preferences.search_keywords.values == ["AI工程师 Python LLM"]
    assert "待确认" in inferred.prompt_context

    confirmed = store.update_confirmed(JobPreferenceUpdate(
        target_roles=["AI工程师"],
        locations=["上海", "杭州"],
        search_keywords=["AI工程师 RAG"],
    ))
    assert confirmed.preferences.version == inferred.preferences.version + 1
    assert confirmed.preferences.target_roles.source == "user_confirmed"
    assert confirmed.preferences.search_keywords.values == ["AI工程师 RAG"]
    assert "用户已确认" in confirmed.prompt_context

    restored = CandidateStateStore(tmp_path, key)
    assert restored.read().preferences.search_keywords.values == ["AI工程师 RAG"]


def test_reset_stale_keywords_uses_current_resume_and_preserves_other_preferences(tmp_path: Path) -> None:
    store = CandidateStateStore(tmp_path, Fernet.generate_key().decode())
    store.apply_resume_profile(CandidateProfileView(
        resume_id="a" * 32,
        skills=["Python", "LangChain"],
        role_suggestions=["AI工程师"],
        summary="AI方向简历",
    ), "AI工程师")
    store.update_confirmed(JobPreferenceUpdate(
        search_keywords=["AI Agent", "算法工程师"],
        locations=["深圳"],
    ))
    store.apply_resume_profile(CandidateProfileView(
        resume_id="b" * 32,
        skills=["Python", "金融风控"],
        role_suggestions=["风险策略分析师", "信用风险分析师"],
        summary="金融风控简历",
    ), "风险策略分析师")

    state = store.reset_search_keywords_from_resume()

    assert state.profile.resume_id == "b" * 32
    assert state.preferences.search_keywords.values == ["风险策略分析师"]
    assert state.preferences.search_keywords.source == "inferred"
    assert state.preferences.locations.values == ["深圳"]
    assert state.preferences.locations.source == "user_confirmed"
    assert state.preferences.target_roles.values == ["风险策略分析师", "信用风险分析师"]
    assert store.read().preferences.search_keywords.values == ["风险策略分析师"]


def test_evidence_updates_resume_draft_and_search_history_is_capped(tmp_path: Path) -> None:
    store = CandidateStateStore(tmp_path, Fernet.generate_key().decode())
    evidence = CandidateEvidenceItem(
        kind="internship",
        title="推荐系统实习项目",
        summary="开发推荐接口并完成性能优化。",
        architecture="FastAPI 服务连接推荐模型与 PostgreSQL 数据层。",
        skills=["Python", "FastAPI"],
        role_suggestions=["后端工程师"],
        features=["支持可配置推荐策略"],
        optimizations=["通过缓存减少重复计算"],
        achievements=["开发推荐接口并完成性能优化"],
        source="conversation",
    )
    state = store.apply_evidence(evidence)
    assert "推荐系统实习项目" in state.resume_draft
    assert "基础架构：FastAPI 服务连接推荐模型与 PostgreSQL 数据层" in state.resume_draft
    assert "技术栈：Python、FastAPI" in state.resume_draft
    assert "项目特点：支持可配置推荐策略" in state.resume_draft
    assert "调优：通过缓存减少重复计算" in state.resume_draft
    assert "Python" in state.profile.skills

    for index in range(7):
        state = store.confirm_search_query(f"关键词-{index}")
    assert state.preferences.search_keywords.values == [
        "关键词-6", "关键词-5", "关键词-4", "关键词-3", "关键词-2"
    ]


def test_large_resume_and_material_skill_sets_are_capped_only_in_preferences(tmp_path: Path) -> None:
    store = CandidateStateStore(tmp_path, Fernet.generate_key().decode())
    resume_skills = [f"ResumeSkill{index}" for index in range(80)]
    state = store.apply_resume_profile(CandidateProfileView(
        resume_id="b" * 32,
        skills=resume_skills,
        role_suggestions=["AI工程师"],
        summary="大量技能边界测试",
    ), "AI工程师")
    assert len(state.profile.skills) == 80
    assert len(state.preferences.skills.values) == 50

    store.apply_evidence(CandidateEvidenceItem(
        kind="project", title="项目一", summary="第一批技术栈",
        skills=[f"ProjectA{index}" for index in range(40)], source="file",
    ))
    state = store.apply_evidence(CandidateEvidenceItem(
        kind="project", title="项目二", summary="第二批技术栈",
        skills=[f"ProjectB{index}" for index in range(40)], source="file",
    ))
    assert len(state.profile.skills) == 100
    assert len(state.preferences.skills.values) == 50


def test_large_resume_and_material_skill_sets_are_capped_only_in_preferences(tmp_path: Path) -> None:
    store = CandidateStateStore(tmp_path, Fernet.generate_key().decode())
    resume_skills = [f"ResumeSkill{index}" for index in range(80)]
    state = store.apply_resume_profile(CandidateProfileView(
        resume_id="b" * 32,
        skills=resume_skills,
        role_suggestions=["AI工程师"],
        summary="大量技能边界测试",
    ), "AI工程师")
    assert len(state.profile.skills) == 80
    assert len(state.preferences.skills.values) == 50

    first = CandidateEvidenceItem(
        kind="project", title="项目一", summary="第一批技术栈",
        skills=[f"ProjectA{index}" for index in range(40)], source="file",
    )
    second = CandidateEvidenceItem(
        kind="project", title="项目二", summary="第二批技术栈",
        skills=[f"ProjectB{index}" for index in range(40)], source="file",
    )
    store.apply_evidence(first)
    state = store.apply_evidence(second)
    assert len(state.profile.skills) == 100
    assert len(state.preferences.skills.values) == 50


def test_resume_draft_can_be_edited_saved_and_regenerated(tmp_path: Path) -> None:
    key = Fernet.generate_key().decode()
    store = CandidateStateStore(tmp_path, key)
    store.apply_evidence(CandidateEvidenceItem(
        kind="project",
        title="智能推荐系统",
        summary="实现候选集召回与排序。",
        source="file",
    ))

    edited = store.update_resume_draft("# 我的简历\n\n## 项目经历\n已人工校对。")
    assert "已人工校对" in edited.resume_draft
    assert CandidateStateStore(tmp_path, key).read().resume_draft == edited.resume_draft

    regenerated = store.apply_evidence(CandidateEvidenceItem(
        kind="project",
        title="工程监控平台",
        summary="实现指标采集与告警。",
        source="file",
    ))
    assert "工程监控平台" in regenerated.resume_draft
    assert "已人工校对" not in regenerated.resume_draft


def test_project_fallback_uses_content_title_and_removes_unrelated_sections() -> None:
    import asyncio

    from app.memory.candidate_state import CandidateEvidenceExtractor

    class FailingLLM:
        async def generate(self, _request):
            raise RuntimeError("offline")

    material = """
## 个人信息
姓名：张三
电话：13800138000
## 求职意向
期望岗位：后端工程师
# AutoHunt 智能求职平台
使用 FastAPI 与 Playwright 实现职位搜索、匹配和人工接管。
"""
    evidence = asyncio.run(CandidateEvidenceExtractor(FailingLLM()).extract(
        material,
        title="course-report-final-v3.pdf",
        kind="project",
        source="file",
    ))

    assert evidence.title == "AutoHunt 智能求职平台"
    assert "course-report-final-v3" not in evidence.title
    assert "张三" not in evidence.summary
    assert "13800138000" not in evidence.summary
    assert "求职意向" not in evidence.summary
    assert "期望岗位" not in evidence.summary


def test_legacy_file_title_is_migrated_from_evidence_content(tmp_path: Path) -> None:
    store = CandidateStateStore(tmp_path, Fernet.generate_key().decode())
    state = store.apply_evidence(CandidateEvidenceItem(
        kind="project",
        title="final-project-report-v7.pdf",
        summary="实现推荐召回、排序与缓存优化。",
        skills=["Python"],
        source="file",
    ))

    assert state.profile.evidence[0].title == "智能推荐系统"
    assert ".pdf" not in state.resume_draft



def test_single_line_resume_noise_is_not_mixed_into_project() -> None:
    import asyncio

    from app.memory.candidate_state import CandidateEvidenceExtractor

    class FailingLLM:
        async def generate(self, _request):
            raise RuntimeError("offline")

    material = """
### 智能求职 Agent
**项目简介** Nifty Composer 2.0 后端开发和AI功能集成 2026.02-2026.05
郑子燊 男 | 26岁 [REDACTED_PHONE] [REDACTED_EMAIL] 求职意向：AI应用开发/AI算法工程师
编程基础 精通 Python，熟悉 C 语言。 深度学习框架 熟练使用 PyTorch。
"""
    evidence = asyncio.run(CandidateEvidenceExtractor(FailingLLM()).extract(
        material,
        title="resume-project.pdf",
        kind="project",
        source="file",
    ))

    assert evidence.title == "Nifty Composer 2.0 智能作曲平台"
    assert evidence.summary == "完成 Nifty Composer 2.0 后端开发与 AI 功能集成，支持智能作曲业务流程。"
    assert "郑子燊" not in evidence.summary
    assert "求职意向" not in evidence.summary
    assert "编程基础" not in evidence.summary


def test_english_cefr_report_has_chinese_project_title_and_summary() -> None:
    import asyncio

    from app.memory.candidate_state import CandidateEvidenceExtractor

    class FailingLLM:
        async def generate(self, _request):
            raise RuntimeError("offline")

    material = """
COMP6713 Assignment Report: Lexical CEFR Transformation
Student Name: [Zishen Zheng] Student ID (zID): [z5499343]
1. Technical Details
Preprocessing and Standardization: spaCy lemmatization creates a unified vocabulary.
Density-based Level Assignment: normalize appearances in varying CEFR levels.
"""
    evidence = asyncio.run(CandidateEvidenceExtractor(FailingLLM()).extract(
        material,
        title="COMP6713-final.pdf",
        kind="course",
        source="file",
    ))

    assert evidence.title == "CEFR 词汇难度转换系统"
    assert evidence.summary == "基于词形还原与 CEFR 分级密度统计，构建词汇难度识别、转换与评估流程。"
    assert "Student Name" not in evidence.summary
    assert "Student ID" not in evidence.summary


def test_duplicate_project_content_reuses_evidence_id(tmp_path: Path) -> None:
    store = CandidateStateStore(tmp_path, Fernet.generate_key().decode())
    first = CandidateEvidenceItem(
        kind="project",
        title="AutoHunt 智能求职平台",
        summary="实现职位搜索、匹配和人工接管。",
        skills=["Python", "FastAPI"],
        source="file",
    )
    second = CandidateEvidenceItem(
        kind="project",
        title="AutoHunt 智能求职平台",
        summary="实现职位搜索、匹配和人工接管。",
        skills=["Python", "FastAPI"],
        source="file",
    )

    store.apply_evidence(first)
    state = store.apply_evidence(second)

    assert len(state.profile.evidence) == 1
    assert second.id == first.id



def test_llm_output_is_corrected_to_document_domain_and_chinese() -> None:
    import asyncio
    import json

    from app.llm.base import LLMResult
    from app.memory.candidate_state import CandidateEvidenceExtractor

    class WrongLLM:
        async def generate(self, _request):
            return LLMResult(
                text=json.dumps({
                    "title": "候选人应用系统",
                    "summary": "Lexical CEFR transformation course assignment.",
                    "architecture": "spaCy preprocessing pipeline.",
                    "skills": ["spaCy", "Python"],
                    "role_suggestions": [],
                    "features": ["Density based assignment"],
                    "optimizations": [],
                    "achievements": [],
                }, ensure_ascii=False),
                provider="fake",
                model="fake",
            )

    evidence = asyncio.run(CandidateEvidenceExtractor(WrongLLM()).extract(
        "Lexical CEFR Transformation uses spaCy lemmatization and density-based level assignment.",
        title="assignment.pdf",
        kind="course",
        source="file",
    ))

    assert evidence.title == "CEFR 词汇难度转换系统"
    assert evidence.summary == "基于词形还原与 CEFR 分级密度统计，构建词汇难度识别、转换与评估流程。"
    assert evidence.architecture == ""
    assert evidence.features == []
