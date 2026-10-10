"""Layer orchestration: rules → small model → LLM fallback, with per-layer
timeouts, circuit breakers and fail-safe defaults.

Safety posture:
- P0 rule hits are hard blocks; nothing downstream can override them.
- Side-effect intents require high confidence AND a clear top-1/top-2 margin,
  otherwise the user is asked to clarify (or the request is escalated).
- Every timeout/failure degrades toward the next layer or a safe refusal;
  the router never invents an intent and never executes side effects itself.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field
from typing import Any

from ..core.models import ClarifyOption
from .catalog import DEFAULT_DIRECT_INTENTS, INTENT_LABELS, INTENT_SPECS
from .circuit import CircuitBreaker, CircuitState
from .normalize import normalize_text, query_hash
from .rules import RuleEngine
from .small_model import SmallModelClient, SmallModelError
from .store import RouterStore

SAFE_REFUSAL = (
    "我暂时无法处理这条消息（安全保护已生效），没有执行任何操作。"
    "请稍后重试，或换一种说法。"
)

BLOCK_MESSAGES: dict[str, str] = {
    "injection": "检测到提示注入特征，已按安全策略拒绝处理这条消息。",
    "credentials": "检测到疑似凭据/密钥内容。请不要在对话中粘贴密钥；本条消息未被处理。",
    "pii_out": "出于隐私保护，我不能输出候选人的私人联系方式。",
    "destructive": "这条操作可能造成不可逆影响，已按安全策略拒绝。请在界面中逐项明确确认后再执行。",
    "path_escape": "检测到越界的文件路径访问，已拒绝。",
}


@dataclass(frozen=True)
class RouteThresholds:
    direct_confidence: float = 0.90
    clarify_low: float = 0.45
    clarify_margin: float = 0.15
    side_effect_confidence: float = 0.90
    side_effect_margin: float = 0.20
    small_timeout_s: float = 2.0
    llm_timeout_s: float = 15.0


@dataclass(frozen=True)
class RouteOutcome:
    decision: str  # direct | clarify | llm | blocked | safe_fallback
    intent: str
    layer: str
    confidence: float | None = None
    margin: float | None = None
    clarify_options: tuple[ClarifyOption, ...] = ()
    safe_message: str = ""
    params: dict[str, Any] = field(default_factory=dict)


class IntentRouter:
    """Three-layer intent router. Pure orchestration: no side effects, no I/O
    beyond the injected stores/clients."""

    def __init__(
        self,
        *,
        rule_engine: RuleEngine,
        small_model: SmallModelClient,
        store: RouterStore,
        thresholds: RouteThresholds | None = None,
        direct_intents: frozenset[str] = DEFAULT_DIRECT_INTENTS,
    ) -> None:
        self.rule_engine = rule_engine
        self.small_model = small_model
        self.store = store
        self.thresholds = thresholds or RouteThresholds()
        self.direct_intents = direct_intents
        self.small_circuit = CircuitBreaker(
            "small_model",
            on_state_change=self._on_circuit_change,
        )
        self.llm_circuit = CircuitBreaker(
            "llm_fallback",
            failure_threshold=3,
            window_seconds=60.0,
            cooldown_seconds=60.0,
            on_state_change=self._on_circuit_change,
        )
        # Bounded in-memory pairing of the latest route decision per query, so
        # the LLM fallback's final intent can be compared (three-way drift log).
        self._last_decision: dict[str, RouteOutcome] = {}

    # --------------------------------------------------------------- LLM guard
    @property
    def llm_timeout_s(self) -> float:
        return self.thresholds.llm_timeout_s

    def llm_available(self) -> bool:
        return self.llm_circuit.allow()

    def record_llm_success(self) -> None:
        self.llm_circuit.record_success()

    def record_llm_failure(self) -> None:
        self.llm_circuit.record_failure()

    def _on_circuit_change(self, name: str, previous: CircuitState, current: CircuitState) -> None:
        self.store.record_event(
            query_hash="", layer=f"circuit:{name}", intent="none",
            decision=f"{previous.value}->{current.value}", latency_ms=0.0,
            detail=f"circuit {name} transitioned",
        )

    # ------------------------------------------------------------------ route
    async def route(self, message: str) -> RouteOutcome:
        normalized = normalize_text(message)
        qhash = self._scoped_hash(message)
        started = time.perf_counter()

        # --- Layer 0: rules -------------------------------------------------
        match = self.rule_engine.match(message)
        from ..workflow.requests import is_continuation
        if (not match or match.category != "P0") and is_continuation(message):
            outcome = RouteOutcome("llm", "compound_request", "context:task_reference")
            self._record(outcome, qhash, started)
            return outcome  # Generic continuation must never bind directly to a browser.
        if match is not None:
            outcome = RouteOutcome(
                decision="blocked" if match.category == "P0" else self._decision_for(match.intent),
                intent=match.intent,
                layer=f"rule:{match.category}",
                safe_message=BLOCK_MESSAGES.get(match.reason, SAFE_REFUSAL) if match.category == "P0" else "",
            )
            self._record(outcome, qhash, started)
            if match.category != "P0":
                self._last_decision[qhash] = outcome
            return outcome

        # --- P5: clarify memory ---------------------------------------------
        remembered = self.store.lookup_clarify(self._clarify_key(message))
        if remembered is not None:
            outcome = RouteOutcome(
                decision=self._decision_for(remembered),
                intent=remembered,
                layer="clarify_memory",
            )
            self._record(outcome, qhash, started)
            self._last_decision[qhash] = outcome
            return outcome

        # --- Layer 1: small model -------------------------------------------
        if not self.small_model.available:
            return self._llm_outcome(qhash, started, layer="small_unavailable")
        if not self.small_circuit.allow():
            return self._llm_outcome(qhash, started, layer="small_circuit_open")

        try:
            from ..runtime.context import active_run
            context = active_run()
            classification = self.small_model.classify(message, context=context.routing_context if context else "") if getattr(self.small_model, "supports_context", False) else self.small_model.classify(message)
            decision = await asyncio.wait_for(
                classification,
                timeout=self.thresholds.small_timeout_s,
            )
        except asyncio.TimeoutError:
            self.small_circuit.record_failure()
            return self._llm_outcome(qhash, started, layer="small_timeout")
        except (SmallModelError, Exception) as exc:  # noqa: BLE001 — degrade on anything
            self.small_circuit.record_failure()
            return self._llm_outcome(qhash, started, layer="small_error", detail=type(exc).__name__)

        self.small_circuit.record_success()
        outcome = self._apply_small_policy(decision)
        self._record(outcome, qhash, started, confidence=decision.confidence, margin=decision.margin)
        self._last_decision[qhash] = outcome
        return outcome

    # ------------------------------------------------------------ small policy
    def _apply_small_policy(self, decision) -> RouteOutcome:
        intent = decision.intent
        spec = INTENT_SPECS.get(intent)
        if spec is None:
            intent = "none"
            spec = INTENT_SPECS["none"]
        confidence = min(max(decision.confidence, 0.0), 1.0) if math.isfinite(decision.confidence) else 0.0
        margin = min(max(decision.margin, 0.0), 1.0) if math.isfinite(decision.margin) else 0.0
        thresholds = self.thresholds

        if spec.side_effect:
            if decision.confidence_source == "reported":
                # Reported confidence is uncalibrated (no logprobs): never
                # execute side effects on it directly. The plain path already
                # handles search commands deterministically and the LLM
                # fallback covers the rest — the small intent is still logged
                # for three-way drift comparison.
                return RouteOutcome("llm", intent, "small_model", confidence, margin)
            if confidence >= thresholds.side_effect_confidence and margin >= thresholds.side_effect_margin:
                return RouteOutcome(self._decision_for(intent), intent, "small_model", confidence, margin)
            return RouteOutcome("llm", intent, "small_model", confidence, margin)

        if decision.confidence_source != "reported" and confidence >= thresholds.direct_confidence and margin >= thresholds.clarify_margin:
            return RouteOutcome(self._decision_for(intent), intent, "small_model", confidence, margin)
        # Weak classification is not a reason to ask the user a routing question.
        # The main model receives DST and tools, reasons about intent/slots, and
        # asks only when a real parameter, target or authorization is missing.
        return RouteOutcome("llm", intent, "small_model", confidence, margin)

    def _decision_for(self, intent: str) -> str:
        """Direct only for intents with deterministic handlers; else LLM path."""
        return "direct" if intent in self.direct_intents else "llm"

    def _clarify_outcome(
        self,
        intent: str,
        alternatives: tuple[str, ...],
        confidence: float,
        margin: float,
    ) -> RouteOutcome:
        options = [ClarifyOption(intent=intent, label=INTENT_LABELS.get(intent, intent))]
        for alternative in alternatives:
            if alternative != intent and alternative != "none":
                options.append(ClarifyOption(intent=alternative, label=INTENT_LABELS.get(alternative, alternative)))
        return RouteOutcome(
            "clarify", intent, "small_model", confidence, margin,
            clarify_options=tuple(options[:2]),
        )

    def _llm_outcome(self, qhash: str, started: float, *, layer: str, detail: str = "") -> RouteOutcome:
        outcome = RouteOutcome("llm", "none", layer)
        self._record(outcome, qhash, started, detail=detail)
        return outcome

    # ------------------------------------------------------------- clarify UX
    def resolve_clarify_choice(self, previous_message: str, chosen_label: str) -> str | None:
        """Persist the user's clarification of ``previous_message`` and return
        the chosen intent id (None when the label is unknown)."""
        chosen = next(
            (intent for intent, label in INTENT_LABELS.items() if label == chosen_label.strip()),
            None,
        )
        if chosen is None:
            return None
        self.store.remember_clarify(self._clarify_key(previous_message), chosen, source="user_choice")
        return chosen

    # --------------------------------------------------------------- drift log
    def record_final(self, message: str, final_intent: str, user_outcome: str | None = None) -> None:
        """Pair the last route decision with the final executed intent."""
        qhash = self._scoped_hash(message)
        previous = self._last_decision.pop(qhash, None)
        if previous is None:
            return
        self.store.record_disagreement(
            query_hash=qhash,
            rule_intent=previous.intent if previous.layer.startswith("rule") else None,
            small_intent=previous.intent if previous.layer in {"small_model", "small_timeout", "small_error"} else None,
            small_conf=previous.confidence,
            small_margin=previous.margin,
            final_intent=final_intent,
            user_outcome=user_outcome,
        )
        if len(self._last_decision) > 256:  # bounded memory
            self._last_decision.clear()

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _clarify_key(message: str) -> str:
        # A remembered label is local to its task/focus/conditions, not forever
        # attached to the same short phrase across unrelated contexts.
        import json
        from ..runtime.context import active_run
        context = active_run()
        if not context:
            return query_hash(message)
        state = context.dialogue or {}
        basis = {"session": context.session_id, "topic": state.get("active_intent"),
            "root": (state.get("task_request") or {}).get("id"),
            "focus": state.get("selected_job"), "conditions": context.search_state}
        return query_hash(message + "::" + json.dumps(basis, ensure_ascii=False, sort_keys=True))

    @staticmethod
    def _scoped_hash(message: str) -> str:
        from ..runtime.context import active_run
        context = active_run()
        return query_hash(f"{context.session_id}::{message}" if context else message)

    def _record(
        self,
        outcome: RouteOutcome,
        qhash: str,
        started: float,
        *,
        confidence: float | None = None,
        margin: float | None = None,
        detail: str = "",
    ) -> None:
        self.store.record_event(
            query_hash=qhash,
            layer=outcome.layer,
            intent=outcome.intent,
            confidence=confidence if confidence is not None else outcome.confidence,
            margin=margin if margin is not None else outcome.margin,
            decision=outcome.decision,
            latency_ms=(time.perf_counter() - started) * 1000,
            detail=detail,
        )
