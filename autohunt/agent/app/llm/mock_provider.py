from __future__ import annotations

from .base import LLMProvider, LLMRequest, LLMResult, LLMToolCall, LLMToolResult
from ..core.replies import public_reply


class MockLLMProvider(LLMProvider):
    name = "mock"

    def __init__(self, model: str = "mock-autohunt-v1") -> None:
        self.model = model

    async def generate(self, request: LLMRequest) -> LLMResult:
        lowered = request.user_message.lower()
        if any(term in lowered for term in ("状态", "进度", "status")):
            text = "后端 Agent 已连接。申请任务队列当前为空；真实职位连接器尚未启用。"
        elif any(term in lowered for term in ("偏好", "memory", "记忆")):
            text = "我已读取非敏感长期记忆。联系方式和模型凭据不会进入提示词。"
        else:
            text = "指令已进入单 Agent 编排器。当前使用 Mock Provider，可在模型设置中接入真实模型。"
        return LLMResult(text=text, provider=self.name, model=self.model)

    async def generate_with_tools(
        self,
        *,
        instructions: str,
        messages: list[dict],
        tools: list[dict],
    ) -> LLMToolResult:
        user_text = next(
            (str(item.get("content", "")) for item in messages if item.get("role") == "user"),
            "",
        )
        observations = [item for item in messages if item.get("role") == "tool"]
        if observations:
            content = observations[-1].get("content", "")
            return LLMToolResult(
                text=public_reply(str(content)),
                provider=self.name,
                model=self.model,
                assistant_message={"role": "assistant", "content": public_reply(str(content))},
            )
        names = {item["function"]["name"] for item in tools}
        compact = user_text.replace(" ", "")
        tool_name = None
        arguments = "{}"
        if any(term in compact for term in ("搜索结果", "找到的职位", "找到的岗位")) and "job_results_read" in names:
            tool_name = "job_results_read"
            arguments = '{"limit": 10}'
        elif any(term in compact for term in ("重新搜索", "开始搜索", "搜索职位", "搜索岗位")) and "job_search_start" in names:
            tool_name = "job_search_start"
            arguments = '{"query": ""}'
        if tool_name is None:
            return LLMToolResult(
                text="当前使用 Mock Provider；未选择需要执行的工具。",
                provider=self.name,
                model=self.model,
                assistant_message={"role": "assistant", "content": "当前使用 Mock Provider；未选择需要执行的工具。"},
            )
        call = LLMToolCall(id="mock-tool-call", name=tool_name, arguments=arguments)
        return LLMToolResult(
            text="",
            provider=self.name,
            model=self.model,
            tool_calls=[call],
            assistant_message={
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }],
            },
        )

