"""Three-layer intent routing: rules (P0-P5) → small model → LLM fallback."""

from .catalog import (
    DEFAULT_DIRECT_INTENTS,
    FEWSHOT_EXAMPLES,
    INTENT_LABELS,
    INTENT_SPECS,
)
from .circuit import CircuitBreaker, CircuitState
from .normalize import normalize_text, query_hash
from .router import (
    BLOCK_MESSAGES,
    SAFE_REFUSAL,
    IntentRouter,
    RouteOutcome,
    RouteThresholds,
)
from .rules import RuleEngine
from .small_model import SmallModelClient, SmallModelDecision, SmallModelError
from .store import RouterStore

__all__ = [
    "BLOCK_MESSAGES",
    "SAFE_REFUSAL",
    "CircuitBreaker",
    "CircuitState",
    "DEFAULT_DIRECT_INTENTS",
    "FEWSHOT_EXAMPLES",
    "INTENT_LABELS",
    "INTENT_SPECS",
    "IntentRouter",
    "RouteOutcome",
    "RouteThresholds",
    "RouterStore",
    "RuleEngine",
    "SmallModelClient",
    "SmallModelDecision",
    "SmallModelError",
    "normalize_text",
    "query_hash",
]
