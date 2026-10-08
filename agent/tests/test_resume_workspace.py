from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from app.core.main_agent import AutoHuntAgent, _is_resume_review_request, _is_resume_update_request

from app.core.models import CandidateEvidenceItem
from app.memory.candidate_state import CandidateStateStore
from app.memory.resume_workspace import ResumeWorkspaceStore
from app.memory.stores import MemoryStore, SkillStore
from app.sandbox_backend.filesystem import WorkspaceSandbox


def make_workspace(tmp_path: Path) -> tuple[CandidateStateStore, ResumeWorkspaceStore]:
    data_root = tmp_path / "data"
    key = Fernet.generate_key().decode()
    candidate_state = CandidateStateStore(data_root, key)
    workspace = ResumeWorkspaceStore(data_root, key, candidate_state)
    workspace.ensure_document(candidate_state.read().profile)
    return candidate_state, workspace


def make_evidence() -> CandidateEvidenceItem:
    return CandidateEvidenceItem(
        kind="project",
        title="AutoHunt 求职 Agent",
        summary="构建浏览器自动化求职工作流。",
        architecture="Next.js、FastAPI 与 Playwright 通过任务状态机协作。",
        skills=["Python", "TypeScript", "Playwright"],
        role_suggestions=["AI 工程师"],
        features=["职位标准化与去重", "浏览器全局串行锁", "人工接管验证码"],
        optimizations=["不确定结果禁止自动重试"],
        achievements=["打通登录、搜索与统计链路"],
        source="conversation",
    )


def test_evidence_creates_reviewable_patch_before_master_resume_changes(tmp_path: Path) -> None:
    candidate_state, workspace = make_workspace(tmp_path)
    evidence = make_evidence()

    candidate_state.apply_evidence(evidence)
    proposal = workspace.propose_add_evidence(evidence)

    assert proposal is not None
    assert proposal.status == "pending"
    assert proposal.operations[0].operation == "add"
    assert evidence.title not in candidate_state.read().resume_draft
    assert not workspace.read_document().entries

    decided, document = workspace.approve(proposal.id)

    assert decided.status == "approved"
    assert document.version == 2
    assert document.entries[0].title == evidence.title
    assert evidence.title in candidate_state.read().resume_draft


def test_natural_language_patch_and_manual_markdown_share_one_document(tmp_path: Path) -> None:
    candidate_state, workspace = make_workspace(tmp_path)
    evidence = make_evidence()
    candidate_state.apply_evidence(evidence)
    initial = workspace.propose_add_evidence(evidence)
    assert initial is not None
    workspace.approve(initial.id)

    proposal = workspace.propose_update(
        "把 AutoHunt 求职 Agent 项目简介改为「基于浏览器自动化完成可靠的职位搜索与人工审核」"
    )
    assert proposal is not None
    assert proposal.operations[0].path.endswith(".summary")
    workspace.approve(proposal.id)
    assert workspace.read_document().entries[0].summary == "基于浏览器自动化完成可靠的职位搜索与人工审核"

    edited = candidate_state.read().resume_draft.replace(
        "# 候选人简历",
        "# AI Agent 工程师",
    ).replace(
        "Python、TypeScript、Playwright",
        "Python、TypeScript、Playwright、FastAPI",
    )
    document = workspace.update_from_markdown(edited)

    assert document.headline == "AI Agent 工程师"
    assert "FastAPI" in document.skills
    assert document.entries[0].evidence_ids == [evidence.id]
    assert workspace.read_document().version == document.version


def test_rejected_patch_never_changes_master_document(tmp_path: Path) -> None:
    candidate_state, workspace = make_workspace(tmp_path)
    evidence = make_evidence()
    candidate_state.apply_evidence(evidence)
    proposal = workspace.propose_add_evidence(evidence)
    assert proposal is not None

    rejected = workspace.reject(proposal.id)

    assert rejected.status == "rejected"
    assert workspace.read_document().entries == []
    with pytest.raises(RuntimeError):
        workspace.approve(proposal.id)


class NeverCalledForLocalReview:
    async def generate(self, request):
        raise AssertionError("local resume review must not export resume content")


class NeverCalledEvidenceExtractor:
    async def extract(self, text: str, *, title: str, kind: str, source: str):
        raise AssertionError("resume review must not create evidence")


async def test_resume_advice_question_is_review_not_mutation(tmp_path: Path) -> None:
    message = "我想找AI agent的岗位，我现在的简历还有什么需要修改"
    assert _is_resume_review_request(message)
    assert not _is_resume_update_request(message)

    candidate_state, workspace = make_workspace(tmp_path)
    evidence = make_evidence()
    candidate_state.apply_evidence(evidence)
    proposal = workspace.propose_add_evidence(evidence)
    assert proposal is not None
    workspace.approve(proposal.id)
    sandbox = WorkspaceSandbox(tmp_path / "data")
    agent = AutoHuntAgent(
        MemoryStore(sandbox),
        SkillStore(sandbox),
        candidate_state,
        NeverCalledEvidenceExtractor(),
        NeverCalledForLocalReview(),
        workspace,
    )

    response = await agent.handle(message)

    assert response.action is None
    assert "初步诊断" in response.message
    assert "优先修改建议" in response.message
    assert any(event.tool == "resume_review_local" for event in response.tool_events)


def test_full_resume_import_fallback_keeps_all_project_sections() -> None:
    import asyncio

    from app.core.models import CandidateProfileView
    from app.memory.resume_workspace import ResumeDocumentExtractor

    class FailingLLM:
        async def generate(self, _request):
            raise RuntimeError("offline")

    text = """
个人优势
熟悉 Python 与 PyTorch。
项目经历
Nifty Composer 2.0 前后端开发 2026.02-2026.05
项目概述：浏览器端钢琴五线谱编辑器。
技术栈：React、TypeScript、Node.js、Docker
混合式餐厅评论情感分析系统（NLP） 模型微调 + LLM 集成
项目概述：融合 DistilBERT 与本地 LLM 的情感分析系统。
技术栈：Python、PyTorch、LangChain
GTSRB 交通标志识别 — 深度学习图像分类
简介：对比 ResNet18、ConvNeXt 与 ViT。
技术栈：Python、PyTorch
教育经历
某大学
课内项目
CV-虫害检测
项目描述
对比 Faster R-CNN 与 YOLOv8 的农业害虫检测系统。
技术栈
Python、PyTorch、OpenCV
NLP, Embedding-CEFR改写系统
简介：将英文句子改写为指定 CEFR 等级。
技术栈：Python、spaCy、FastText
问题解决
基于词频密度进行 CEFR 单词分级；
"""
    profile = CandidateProfileView(
        skills=["Python", "PyTorch"],
        role_suggestions=["AI应用开发工程师"],
        summary="具备 AI 应用开发经验。",
    )
    document = asyncio.run(ResumeDocumentExtractor(FailingLLM()).extract(text, profile))

    assert [entry.title for entry in document.entries] == [
        "Nifty Composer 2.0 智能乐谱编辑平台",
        "混合式餐厅评论情感分析系统",
        "GTSRB 交通标志识别系统",
        "农业虫害检测系统",
        "CEFR 词汇难度改写系统",
    ]
    assert [entry.kind for entry in document.entries] == [
        "project", "project", "project", "course", "course"
    ]


def test_uploaded_resume_replaces_existing_master_entries(tmp_path: Path) -> None:
    from app.core.models import ResumeDocumentView, ResumeEntryView

    candidate_state, workspace = make_workspace(tmp_path)
    evidence = make_evidence()
    candidate_state.apply_evidence(evidence)
    proposal = workspace.propose_add_evidence(evidence)
    assert proposal is not None
    workspace.approve(proposal.id)

    imported = ResumeDocumentView(
        headline="AI应用开发工程师",
        summary="具备大模型与计算机视觉项目经验。",
        skills=["Python"],
        entries=[
            ResumeEntryView(
                id="uploaded-project",
                kind="project",
                title="新上传简历项目",
                summary="这是上传文件中的项目。",
                skills=["PyTorch"],
            )
        ],
    )
    replaced = workspace.replace_from_upload(imported)

    assert [entry.title for entry in replaced.entries] == ["新上传简历项目"]
    assert "AutoHunt 求职 Agent" not in candidate_state.read().resume_draft
    assert "新上传简历项目" in candidate_state.read().resume_draft
    assert {"Python", "PyTorch"} <= set(replaced.skills)


def test_uploaded_resume_requires_approval_before_replacing_master(tmp_path: Path) -> None:
    from app.core.models import ResumeDocumentView, ResumeEntryView

    candidate_state, workspace = make_workspace(tmp_path)
    original = workspace.read_document()
    imported = ResumeDocumentView(
        headline="AI Agent 算法工程师",
        summary="新的完整简历。",
        skills=["Python", "LLM"],
        entries=[
            ResumeEntryView(
                id="new-project",
                kind="project",
                title="新的 Agent 项目",
                summary="使用原生工具调用完成任务。",
                skills=["Python"],
            )
        ],
    )

    proposal = workspace.propose_replace_from_upload(imported)

    assert proposal.status == "pending"
    assert proposal.operations[0].path == "document"
    assert workspace.read_document().headline == original.headline
    assert "新的 Agent 项目" not in candidate_state.read().resume_draft

    _decided, updated = workspace.approve(proposal.id)

    assert updated.headline == "AI Agent 算法工程师"
    assert [entry.title for entry in updated.entries] == ["新的 Agent 项目"]
    assert "新的 Agent 项目" in candidate_state.read().resume_draft


def test_complete_resume_can_be_imported_verbatim_without_llm_rewriting(tmp_path: Path) -> None:
    from app.core.models import CandidateProfileView
    from app.memory.resume_workspace import ResumeDocumentExtractor

    original = "姓名：测试候选人\n电话：13000000000\n\n项目经历\n原始标题与原始描述，保持不变。"
    profile = CandidateProfileView(
        skills=["Python"],
        role_suggestions=["AI应用开发工程师"],
        summary="旁路画像摘要，不应写入原始简历。",
    )
    imported = ResumeDocumentExtractor.import_verbatim(original, profile, "原始简历.pdf")

    assert imported.source_mode == "verbatim"
    assert imported.source_filename == "原始简历.pdf"
    assert imported.raw_content == original
    assert ResumeWorkspaceStore.render_markdown(imported) == original

    candidate_state, workspace = make_workspace(tmp_path)
    replaced = workspace.replace_from_upload(imported)
    assert replaced.raw_content == original
    assert candidate_state.read().resume_draft == original


def test_verbatim_resume_supplements_survive_approval_and_manual_edit(tmp_path: Path) -> None:
    from app.core.models import CandidateProfileView
    from app.memory.resume_workspace import ResumeDocumentExtractor, SUPPLEMENT_HEADING

    state, workspace = make_workspace(tmp_path)
    original = " 姓名：测试\n电话：13000000000\n原始项目保留。  \n"
    workspace.replace_from_upload(ResumeDocumentExtractor.import_verbatim(original, CandidateProfileView()))
    proposal = workspace.propose_add_evidence(make_evidence())
    assert proposal is not None
    workspace.approve(proposal.id)
    rendered = state.read().resume_draft
    assert rendered.startswith(original + SUPPLEMENT_HEADING)
    assert "AutoHunt 求职 Agent" in rendered
    edited = rendered.replace("构建浏览器自动化求职工作流。", "新增经过验证的描述。")
    document = workspace.update_from_markdown(edited)
    assert document.raw_content == original
    assert document.entries[0].summary == "新增经过验证的描述。"
    workspace.update_from_markdown(state.read().resume_draft)
    assert state.read().resume_draft.count(SUPPLEMENT_HEADING) == 1
    assert len(workspace.read_document().entries) == 1
    with pytest.raises(ValueError, match="分隔标题"):
        workspace.update_from_markdown(original)


def test_deleting_active_resume_removes_base_but_preserves_approved_projects(tmp_path: Path) -> None:
    from app.core.models import CandidateProfileView
    from app.memory.resume_workspace import ResumeDocumentExtractor

    state, workspace = make_workspace(tmp_path)
    profile = CandidateProfileView(
        resume_id="c" * 32,
        skills=["Python"],
        role_suggestions=["AI工程师"],
        summary="来自上传简历的摘要",
    )
    state.apply_resume_profile(profile, "AI工程师 Python")
    workspace.replace_from_upload(ResumeDocumentExtractor.import_verbatim(
        "上传简历原文，不应在删除后保留。", profile, "resume.pdf",
    ))
    evidence = make_evidence()
    state.apply_evidence(evidence)
    proposal = workspace.propose_add_evidence(evidence)
    assert proposal is not None
    workspace.approve(proposal.id)

    updated_state = state.remove_resume_profile(profile.resume_id or "")
    document = workspace.remove_base_resume(updated_state.profile)

    assert updated_state.profile.resume_id is None
    assert all(item.kind != "resume" for item in updated_state.profile.evidence)
    assert document.source_mode == "structured"
    assert document.raw_content == ""
    assert document.source_filename == ""
    assert [entry.title for entry in document.entries] == [evidence.title]
    assert "上传简历原文" not in state.read().resume_draft
    assert evidence.title in state.read().resume_draft


def test_verbatim_manual_save_preserves_whitespace_and_rejects_truncation(tmp_path: Path) -> None:
    from app.core.models import CandidateProfileView
    from app.memory.resume_workspace import ResumeDocumentExtractor
    state, workspace = make_workspace(tmp_path)
    workspace.replace_from_upload(ResumeDocumentExtractor.import_verbatim("原文", CandidateProfileView()))
    content = "  原文\r\n新增文字  \n"
    workspace.update_from_markdown(content)
    assert state.read().resume_draft == content
    with pytest.raises(ValueError):
        workspace.update_from_markdown("中" * 40001)
    assert state.read().resume_draft == content
    with pytest.raises(ValueError):
        ResumeDocumentExtractor.import_verbatim("中" * 40001, CandidateProfileView())
