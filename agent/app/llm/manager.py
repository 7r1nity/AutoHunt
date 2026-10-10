from __future__ import annotations

from dataclasses import replace

from ..core.security import redact
from .base import LLMProvider, LLMRequest, LLMResult, LLMToolResult
from .deepseek_provider import DeepSeekProvider
from .mock_provider import MockLLMProvider
from ..memory.stores import LLMCredentialStore, LLMSettingsStore
from ..runtime.context import active_run, emit


class LLMManager:
    def __init__(
        self,
        settings_store: LLMSettingsStore,
        credential_store: LLMCredentialStore,
        *,
        deepseek_env_key: str | None = None,
    ) -> None:
        self.settings_store = settings_store
        self.credential_store = credential_store
        self.deepseek_env_key = deepseek_env_key
        # Providers hold network clients; reuse them across concurrent calls instead
        # of constructing an AsyncOpenAI client per request (bulk matching fans out).
        self._providers: dict[tuple, LLMProvider] = {}

    def _provider(self) -> LLMProvider:
        config = self.settings_store.read()
        if config["provider"] == "mock":
            provider_key = ("mock", config["model"], "")
            provider = self._providers.get(provider_key)
            if provider is None:
                provider = MockLLMProvider(config["model"])
                self._providers[provider_key] = provider
            return provider
        api_key = self.credential_store.read("deepseek") or self.deepseek_env_key
        if not api_key:
            raise RuntimeError("请先在模型设置中保存 DeepSeek API Key")
        provider_key = ("deepseek", config["model"], api_key)
        provider = self._providers.get(provider_key)
        if provider is None:
            provider = DeepSeekProvider(api_key=api_key, model=config["model"])
            self._providers[provider_key] = provider
        return provider

    async def generate(self, request: LLMRequest) -> LLMResult:
        emit("model", detail="正在调用模型", strategy="fast" if request.fast else "detailed")
        safe_request = replace(
            request,
            instructions=redact(request.instructions),
            user_message=redact(request.user_message),
            context=redact(request.context),
        )
        return await self._provider().generate(safe_request)

    def provider_for_consent(self, provider: str, model: str) -> LLMProvider:
        pinned = self._provider()
        if pinned.name != provider or pinned.model != model:
            raise RuntimeError("模型配置已变化，请重新确认数据发送目的地。")
        return pinned

    async def generate_with_tools(
        self,
        *,
        instructions: str,
        messages: list[dict],
        tools: list[dict],
    ) -> LLMToolResult:
        provider = self._provider()
        emit("model", detail="正在选择工具或生成回答", provider=getattr(provider, "name", "custom"), model=getattr(provider, "model", "custom"))
        arguments = dict(
            instructions=redact(instructions),
            # Include native tool-call argument JSON in the boundary projection.
            # The original messages/checkpoints remain untouched for execution.
            messages=redact(messages),
            tools=tools,
        )
        if active_run() and isinstance(provider, DeepSeekProvider):
            return await provider.generate_with_tools_stream(**arguments, on_delta=lambda text: emit("delta", text=text))
        return await provider.generate_with_tools(**arguments)
