from __future__ import annotations

import asyncio
from openai import AsyncOpenAI

from .base import LLMProvider, LLMRequest, LLMResult, LLMToolCall, LLMToolResult
from ..core.security import redact_text
from ..runtime.resilience import safe_retry
from ..runtime.context import emit_diagnostic


DEEPSEEK_BASE_URL = "https://api.deepseek.com"


class DeepSeekProvider(LLMProvider):
    name = "deepseek"

    def __init__(self, *, api_key: str, model: str) -> None:
        if not api_key:
            raise ValueError("DeepSeek API key is required")
        self.model = model
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=DEEPSEEK_BASE_URL,
            timeout=60.0,
            max_retries=0,
        )

    async def _completion(self, **kwargs):
        # Pure inference / stream creation only. Once streaming begins, never
        # replay received tokens. Outer planner/run deadlines remain authoritative.
        async with asyncio.timeout(60):
            return await safe_retry(lambda: self.client.chat.completions.create(**kwargs), retry_safe=True,
                on_retry=lambda data: emit_diagnostic("model_retry", provider=self.name, **data))

    async def generate(self, request: LLMRequest) -> LLMResult:
        kwargs: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": request.instructions},
                {
                    "role": "user",
                    "content": (
                        f"<trusted_context>\n{request.context}\n</trusted_context>\n\n"
                        f"<user_request>\n{request.user_message}\n</user_request>"
                    ),
                },
            ],
            "stream": False,
            # Planning and extraction must be repeatable: the same request should
            # yield the same plan, otherwise intent handling looks unreliable even
            # when it is correct. Measured variance before this: 1 extra benign
            # step in 3 identical runs.
            "temperature": 0,
        }
        if request.fast:
            # Omitting thinking parameters does NOT disable the provider's
            # default reasoning mode. Planning/scoring must explicitly opt out.
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        else:
            kwargs["reasoning_effort"] = "high"
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        if request.json_output:
            # DeepSeek's JSON mode rejects requests whose prompts do not name
            # JSON. Own this transport-format requirement here, rather than
            # making every feature prompt know the provider's precondition.
            kwargs["messages"][0]["content"] += "\nReturn exactly one valid JSON object, without Markdown or surrounding text."
            kwargs["response_format"] = {"type": "json_object"}
        if request.max_output_tokens is not None:
            kwargs["max_tokens"] = request.max_output_tokens
        response = await self._completion(**kwargs)
        text = response.choices[0].message.content or ""
        if not text.strip():
            raise RuntimeError("DeepSeek returned no text output")
        return LLMResult(
            text=text,
            provider=self.name,
            model=response.model or self.model,
            response_id=response.id,
            usage=response.usage.model_dump(mode="json") if response.usage else {},
        )

    async def generate_with_tools(
        self,
        *,
        instructions: str,
        messages: list[dict],
        tools: list[dict],
        max_output_tokens: int = 8192,
    ) -> LLMToolResult:
        response = await self._completion(
            model=self.model,
            messages=[{"role": "system", "content": instructions}, *messages],
            tools=tools,
            tool_choice="auto",
            stream=False,
            max_tokens=max_output_tokens,
        )
        message = response.choices[0].message
        calls = [
            LLMToolCall(
                id=call.id,
                name=call.function.name,
                arguments=call.function.arguments,
            )
            for call in (message.tool_calls or [])
        ]
        assistant_message = {
            "role": "assistant",
            "content": message.content or "",
        }
        reasoning_content = getattr(message, "reasoning_content", None)
        if reasoning_content is not None:
            assistant_message["reasoning_content"] = reasoning_content
        if calls:
            assistant_message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in calls
            ]
        return LLMToolResult(
            text=message.content or "",
            provider=self.name,
            model=response.model or self.model,
            tool_calls=calls,
            assistant_message=assistant_message,
            response_id=response.id,
            usage=response.usage.model_dump(mode="json") if response.usage else {},
        )

    async def generate_with_tools_stream(self, *, instructions: str, messages: list[dict], tools: list[dict], on_delta,
                                         max_output_tokens: int = 8192) -> LLMToolResult:
        """Stream display text, assemble native tool calls, never expose reasoning."""
        stream = await self._completion(
            model=self.model, messages=[{"role": "system", "content": instructions}, *messages],
            tools=tools, tool_choice="auto", stream=True,
            max_tokens=max_output_tokens,
            stream_options={"include_usage": True},
        )
        content, reasoning, pending = "", "", ""
        calls: dict[int, dict] = {}
        response_id, usage, response_model = None, {}, self.model
        try:
            async for chunk in stream:
                response_id = chunk.id
                response_model = getattr(chunk, "model", None) or response_model
                if getattr(chunk, "usage", None):
                    usage = chunk.usage.model_dump(mode="json")
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                text = delta.content or ""
                content += text
                pending += text
                # Redact complete text units so a key/phone spanning provider chunks
                # cannot escape redaction. Token chunks are not safe text boundaries.
                boundary = max(pending.rfind("\n"), pending.rfind("。"), pending.rfind("！"), pending.rfind("？"))
                if boundary >= 0:
                    on_delta(redact_text(pending[:boundary + 1]))
                    pending = pending[boundary + 1:]
                reasoning += getattr(delta, "reasoning_content", None) or ""
                for call in delta.tool_calls or []:
                    item = calls.setdefault(call.index, {"id": "", "name": "", "arguments": ""})
                    if call.id:
                        item["id"] = call.id
                    if call.function:
                        item["name"] += call.function.name or ""
                        item["arguments"] += call.function.arguments or ""
            if pending:
                on_delta(redact_text(pending))
        finally:
            await stream.close()
        tool_calls = [LLMToolCall(**calls[index]) for index in sorted(calls)]
        assistant = {"role": "assistant", "content": content}
        if reasoning:
            assistant["reasoning_content"] = reasoning
        if tool_calls:
            assistant["tool_calls"] = [{"id": call.id, "type": "function", "function": {
                "name": call.name, "arguments": call.arguments}} for call in tool_calls]
        return LLMToolResult(text=content, provider=self.name, model=response_model,
                             tool_calls=tool_calls, assistant_message=assistant, response_id=response_id, usage=usage)

