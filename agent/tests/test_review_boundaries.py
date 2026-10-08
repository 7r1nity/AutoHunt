import json
from types import SimpleNamespace

from app.core.agent_tools import AgentToolOutcome
from app.core.models import JobAnalysisRequest
from app.browser.screening import match_job
from app.llm.base import LLMRequest
from app.llm.manager import LLMManager


PRIVATE = "person@example.com 13000000000 sk-abcdefghijklmnop"


def test_tool_observations_are_sanitized_without_mutating_source():
    outcome = AgentToolOutcome("resume_document_read", "ok", {"raw_content": PRIVATE}, "read")
    assert "example.com" not in outcome.observation()
    assert "13000000000" not in outcome.observation()
    assert "sk-" not in outcome.observation()
    assert outcome.content["raw_content"] == PRIVATE


async def test_llm_gateway_sanitizes_context_and_preserves_tool_ids(monkeypatch):
    calls = []
    class Provider:
        async def generate(self, request):
            calls.append(request)
            return None
        async def generate_with_tools(self, **kwargs):
            calls.append(kwargs)
            return None
    manager = LLMManager(None, None)
    monkeypatch.setattr(manager, "_provider", lambda: Provider())
    request = LLMRequest(PRIVATE, PRIVATE, PRIVATE)
    await manager.generate(request)
    assert "example.com" not in str(calls[0])
    assert request.context == PRIVATE
    await manager.generate_with_tools(
        instructions=PRIVATE,
        messages=[{"role": "tool", "tool_call_id": "call_1234567890", "content": PRIVATE}], tools=[],
    )
    assert "13000000000" not in json.dumps(calls[1])
    assert calls[1]["messages"][0]["tool_call_id"] == "call_1234567890"


async def test_hard_exclusion_cannot_be_overridden_by_model():
    called = False
    async def must_not_call(_request):
        nonlocal called
        called = True
        raise AssertionError("Unexpected model call")
    result = await match_job(must_not_call, JobAnalysisRequest(
        title="AI 工程师 外包", company="测试公司", description="Python AI Agent 开发与部署岗位，负责模型应用开发、数据处理和服务维护。",
        candidate_skills=["Python"], search_queries=["AI 工程师"], excluded_keywords=["外包"],
    ))
    assert result.score == 0
    assert result.decision == "SKIP"
    assert not called


def test_optional_classifier_prompt_is_sanitized():
    from app.intent.small_model import _build_user_prompt
    assert "example.com" not in _build_user_prompt(PRIVATE)


def test_mcp_resume_surfaces_redact_private_data(monkeypatch):
    from app.mcp import tools
    from app.core.models import CandidateProfileView, ResumeDocumentView
    registered = {}
    class Server:
        def tool(self):
            def register(fn):
                registered[fn.__name__] = fn
                return fn
            return register
    profile = CandidateProfileView(resume_draft_override=PRIVATE)
    monkeypatch.setattr(tools, "candidate_state_store", SimpleNamespace(
        read=lambda: SimpleNamespace(profile=profile, resume_draft=PRIVATE),
    ))
    monkeypatch.setattr(tools, "resume_workspace_store", SimpleNamespace(
        read_document=lambda: ResumeDocumentView(source_mode="verbatim", raw_content=PRIVATE),
    ))
    tools.register_tools(Server())
    assert "resume_draft_override" not in registered["candidate_profile_read"]()
    for name in ("candidate_profile_read", "resume_draft_read", "resume_document_read"):
        assert "example.com" not in json.dumps(registered[name]())
    assert profile.resume_draft_override == PRIVATE
