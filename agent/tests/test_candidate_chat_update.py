from pathlib import Path

from cryptography.fernet import Fernet

from app.core.main_agent import AutoHuntAgent
from app.core.models import CandidateEvidenceItem
from app.memory.candidate_state import CandidateStateStore
from app.memory.stores import MemoryStore, SkillStore
from app.sandbox_backend.filesystem import WorkspaceSandbox


class FakeEvidenceExtractor:
    async def extract(self, text: str, *, title: str, kind: str, source: str) -> CandidateEvidenceItem:
        return CandidateEvidenceItem(
            kind="internship",
            title="智能问答实习项目",
            summary="使用 Python 和 RAG 开发智能问答服务。",
            architecture="FastAPI API 连接向量检索与大模型服务。",
            skills=["Python", "RAG"],
            role_suggestions=["AI工程师"],
            features=["支持基于知识库的问答"],
            optimizations=["优化检索上下文长度"],
            achievements=["开发智能问答服务"],
            source="conversation",
        )


class NeverCalledLLM:
    async def generate(self, request):
        raise AssertionError("explicit resume update should not require a second chat completion")


async def test_chat_can_add_internship_evidence_to_resume(tmp_path: Path) -> None:
    sandbox = WorkspaceSandbox(tmp_path / "data")
    state = CandidateStateStore(tmp_path / "data", Fernet.generate_key().decode())
    agent = AutoHuntAgent(
        MemoryStore(sandbox),
        SkillStore(sandbox),
        state,
        FakeEvidenceExtractor(),
        NeverCalledLLM(),
    )
    response = await agent.handle("我在实习中做了一个 RAG 问答项目，请加入简历")
    assert "已把" in response.message
    assert any(event.tool == "resume_draft_update" for event in response.tool_events)
    assert "智能问答实习项目" in state.read().resume_draft
