from app.llm.base import LLMToolCall, LLMToolResult
from app.memory.document_router import DocumentRouter


class RouteLLM:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def generate_with_tools(self, **_kwargs):
        self.calls += 1
        return self.result


async def test_source_archive_is_deterministically_classified_as_code():
    llm = RouteLLM(None)
    result = await DocumentRouter(llm).classify("project.zip", "")

    assert result.document_type == "material"
    assert result.material_kind == "code"
    assert result.classifier == "deterministic"
    assert llm.calls == 0


async def test_native_route_tool_can_identify_complete_resume():
    llm = RouteLLM(LLMToolResult(
        text="",
        provider="deepseek",
        model="test",
        tool_calls=[LLMToolCall(
            id="route-1",
            name="route_resume",
            arguments='{"confidence":0.94,"reason":"完整候选人履历"}',
        )],
    ))

    result = await DocumentRouter(llm).classify("candidate.pdf", "教育经历 项目经历 技能")

    assert result.document_type == "resume"
    assert result.confidence == 0.94
    assert result.classifier == "deepseek/test"


async def test_low_confidence_model_route_uses_safe_fallback():
    llm = RouteLLM(LLMToolResult(
        text="",
        provider="deepseek",
        model="test",
        tool_calls=[LLMToolCall(
            id="route-1",
            name="route_resume",
            arguments='{"confidence":0.2,"reason":"不确定"}',
        )],
    ))

    result = await DocumentRouter(llm).classify(
        "assignment.pdf",
        "Assignment Student ID Methodology References",
    )

    assert result.document_type == "material"
    assert result.material_kind == "course"
    assert result.classifier == "deterministic"
