from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class LLMRequest:
    instructions: str
    user_message: str
    context: str
    # Fast mode: skip slow reasoning/thinking knobs. Used for bulk scoring calls
    # (job pre-screen, per-job matching) where latency matters more than depth.
    fast: bool = False
    # Explicit structured-output bounds, used only by the no-tools planner.
    json_output: bool = False
    max_output_tokens: int | None = None


@dataclass(frozen=True)
class LLMResult:
    text: str
    provider: str
    model: str
    response_id: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    call_id: str | None = None


@dataclass(frozen=True)
class LLMToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class LLMToolResult:
    text: str
    provider: str
    model: str
    tool_calls: list[LLMToolCall] = field(default_factory=list)
    assistant_message: dict[str, Any] = field(default_factory=dict)
    response_id: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    call_id: str | None = None


class LLMProvider(ABC):
    name: str
    model: str

    @abstractmethod
    async def generate(self, request: LLMRequest) -> LLMResult:
        """Generate one bounded response without exposing provider credentials."""

    async def generate_with_tools(
        self,
        *,
        instructions: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> LLMToolResult:
        raise RuntimeError(f"{self.name} provider does not support native tool calls")

