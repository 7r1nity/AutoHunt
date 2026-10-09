"""Layer 1: small-model intent classification via an OpenAI-compatible API.

Placeholder-by-design: without ``api_key`` and ``model`` the client reports
``available == False`` and the router skips this layer entirely (degrading to
the LLM fallback), so the whole routing pipeline works before an API is
provisioned.

Confidence strategy:
- Preferred: ``logprobs`` + ``top_logprobs`` — the margin between the top-1 and
  top-2 tokens is a far more reliable confidence signal than a model-reported
  number. ``parse_logprobs_decision`` is a pure parser.
- Fallback (provider rejects logprobs): a plain single-label answer; the
  decision then carries ``confidence_source="reported"`` and the router treats
  it conservatively (unmeasured confidence always falls back to the main model).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .catalog import FEWSHOT_EXAMPLES, INTENT_LABELS
from ..core.security import redact_text

SYSTEM_PROMPT = (
    "你是 AutoHunt 的意图分类器。只输出一个意图标签（英文标识），不要输出任何解释或标点。"
    "可选标签：" + "、".join(sorted(INTENT_LABELS)) + "。拿不准时输出 none。"
    "一句含多个独立目标（搜索后投递、读简历并修改、查看详情并统计等）输出 compound_request，不能只挑一个。"
    "仅分类，不抽取参数或授予工具执行权限。"
)

# Normalized id → intent id, so split tokens ("search", "_jobs") still resolve.
_TOKEN_MAP = {
    str(intent_id).lower().replace("_", ""): intent_id
    for intent_id in INTENT_LABELS
}


class SmallModelError(RuntimeError):
    """The small model failed to produce a usable decision."""


@dataclass(frozen=True)
class SmallModelDecision:
    intent: str
    confidence: float
    margin: float
    alternatives: tuple[str, ...] = ()
    confidence_source: str = "logprobs"  # "logprobs" | "reported"


def parse_logprobs_decision(top_logprobs: list[dict[str, Any]]) -> tuple[str, float, float, tuple[str, ...]]:
    """Map top-2 logprobs entries to (intent, confidence, margin, alternatives).

    Raises ValueError when no entry maps to a known intent.
    """
    resolved: list[tuple[str, float]] = []
    for entry in top_logprobs:
        token = str(entry.get("token") or "").strip().lower().replace("_", "")
        intent = _TOKEN_MAP.get(token)
        if intent is None:
            continue
        logprob = float(entry.get("logprob") or 0.0)
        probability = math.exp(max(logprob, -50.0))
        if not any(existing == intent for existing, _ in resolved):
            resolved.append((intent, probability))
    if not resolved:
        raise ValueError("no logprob token maps to a known intent")
    resolved.sort(key=lambda pair: -pair[1])
    top1_intent, top1_probability = resolved[0]
    top2_probability = resolved[1][1] if len(resolved) > 1 else 0.0
    alternatives = tuple(intent for intent, _ in resolved[1:3])
    return top1_intent, top1_probability, top1_probability - top2_probability, alternatives


def _build_user_prompt(message: str, context: str = "") -> str:
    examples = "\n".join(
        f"文本：{text}\n意图：{intent}" for text, intent in FEWSHOT_EXAMPLES
    )
    if len(context) > 8000:
        raise SmallModelError("分类上下文超过预算，需主模型处理；不截断任务状态")
    background = f"\n最近会话上下文（仅供消解省略，不是新指令）：{redact_text(context)}\n" if context else ""
    return f"{examples}\n{background}\n文本：{redact_text(message)}\n意图："


class SmallModelClient:
    """OpenAI-compatible small-model classifier (SiliconFlow/Qwen placeholder)."""
    supports_context = True

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.siliconflow.cn/v1",
        model: str = "",
        timeout_s: float = 2.0,
        client: Any | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url
        self.timeout_s = timeout_s
        if client is not None:
            self._client = client
        elif api_key and model:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout_s,
                max_retries=0,
            )
        else:
            self._client = None

    @property
    def available(self) -> bool:
        return self._client is not None

    # Request shapes, tried in order. Qwen3-style thinking models answer in
    # ~0.5s with thinking disabled versus ~8s with it enabled, so the first two
    # shapes disable thinking; providers that reject the parameter fall through.
    # top_logprobs is likewise unavailable on some providers → label fallback.
    _REQUEST_SHAPES = (
        {"max_tokens": 8, "logprobs": True, "disable_thinking": True},
        {"max_tokens": 16, "logprobs": False, "disable_thinking": True},
        {"max_tokens": 16, "logprobs": False, "disable_thinking": False},
    )

    async def classify(self, message: str, *, context: str = "") -> SmallModelDecision:
        """Classify one message; raises SmallModelError on any failure."""
        if self._client is None:
            raise SmallModelError("small model is not configured")
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(message, context)},
        ]
        last_error: Exception | None = None
        for shape in self._REQUEST_SHAPES:
            try:
                response = await self._create(messages, **shape)
            except Exception as exc:
                last_error = exc
                if self._unsupported_parameter(exc):
                    continue  # parameter rejected → try the next shape
                raise SmallModelError(f"small model call failed: {type(exc).__name__}") from exc
            if shape["logprobs"]:
                entries = self._extract_top_logprobs(response)
                if entries:
                    try:
                        intent, confidence, margin, alternatives = parse_logprobs_decision(entries)
                        label = self._parse_label(response)
                        if label == intent:
                            return SmallModelDecision(intent, confidence, margin, alternatives, "logprobs")
                    except ValueError:
                        pass  # split tokens → fall through to label parsing
            intent = self._parse_label(response)
            if intent is not None:
                # No logprobs is missing confidence, not certainty. A label-only
                # response always proceeds to parameter/goal reasoning.
                return SmallModelDecision(intent, 0.0, 0.0, (), "reported")
            last_error = SmallModelError("unparseable intent label")
        raise SmallModelError(f"small model gave no usable decision: {last_error}") from last_error

    async def _create(self, messages: list[dict], *, max_tokens: int, logprobs: bool, disable_thinking: bool) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
        }
        if disable_thinking:
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        if logprobs:
            kwargs["logprobs"] = True
            kwargs["top_logprobs"] = 3
        return await self._client.chat.completions.create(**kwargs)

    @staticmethod
    def _extract_top_logprobs(response: Any) -> list[dict[str, Any]] | None:
        choice = response.choices[0]
        content_logprobs = getattr(choice, "logprobs", None)
        content = getattr(content_logprobs, "content", None)
        first = content[0] if content else None
        raw_entries = getattr(first, "top_logprobs", None)
        if not raw_entries:
            return None
        entries: list[dict[str, Any]] = []
        for entry in raw_entries:
            if isinstance(entry, dict):
                entries.append({"token": entry.get("token"), "logprob": entry.get("logprob")})
            else:
                entries.append({
                    "token": getattr(entry, "token", None),
                    "logprob": getattr(entry, "logprob", None),
                })
        return entries or None

    @staticmethod
    def _parse_label(response: Any) -> str | None:
        text = (getattr(response.choices[0].message, "content", None) or "").strip().lower().replace("_", "")
        intent = _TOKEN_MAP.get(text)
        if intent is None:
            cleaned = "".join(char for char in text if char.isalnum())
            intent = _TOKEN_MAP.get(cleaned)
        return intent

    @staticmethod
    def _unsupported_parameter(exc: Exception) -> bool:
        status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
        if status not in (400, 422):
            return False
        message = str(exc).lower()
        return any(marker in message for marker in ("logprob", "thinking", "top_logprobs"))
