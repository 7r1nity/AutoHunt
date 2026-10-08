"""Circuit breaker with sliding-window failure counting and half-open probe.

Used per routing layer so one unhealthy dependency (small model API, LLM)
degrades to the next layer instead of stalling every request. State changes
are reported through ``on_state_change`` for audit logging.
"""

from __future__ import annotations

import time
from collections import deque
from enum import Enum
from typing import Callable


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Sliding-window breaker: open after N failures in the window, then
    cooldown, then a single half-open probe before closing again."""

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 3,
        window_seconds: float = 30.0,
        cooldown_seconds: float = 30.0,
        on_state_change: Callable[[str, CircuitState, CircuitState], None] | None = None,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.window_seconds = window_seconds
        self.cooldown_seconds = cooldown_seconds
        self._on_state_change = on_state_change
        self._failures: deque[float] = deque()
        self._state = CircuitState.CLOSED
        self._opened_at: float | None = None
        self._probing = False

    @property
    def state(self) -> CircuitState:
        return self._state

    def allow(self) -> bool:
        """True when a call may proceed (closed, or the half-open probe)."""
        now = time.monotonic()
        if self._state is CircuitState.CLOSED:
            return True
        if self._state is CircuitState.OPEN:
            if self._opened_at is not None and now - self._opened_at >= self.cooldown_seconds:
                self._transition(CircuitState.HALF_OPEN)
                self._probing = True
                return True
            return False
        # HALF_OPEN: exactly one probe at a time.
        if self._probing:
            return False
        self._probing = True
        return True

    def record_success(self) -> None:
        now = time.monotonic()
        if self._state is CircuitState.HALF_OPEN:
            self._probing = False
            self._failures.clear()
            self._transition(CircuitState.CLOSED)
            return
        self._prune(now)

    def record_failure(self) -> None:
        now = time.monotonic()
        if self._state is CircuitState.HALF_OPEN:
            self._probing = False
            self._open(now)
            return
        self._failures.append(now)
        self._prune(now)
        if len(self._failures) >= self.failure_threshold:
            self._open(now)

    def reset(self) -> None:
        self._failures.clear()
        self._probing = False
        self._opened_at = None
        self._transition(CircuitState.CLOSED)

    def _prune(self, now: float) -> None:
        horizon = now - self.window_seconds
        while self._failures and self._failures[0] < horizon:
            self._failures.popleft()

    def _open(self, now: float) -> None:
        self._failures.clear()
        self._opened_at = now
        self._transition(CircuitState.OPEN)

    def _transition(self, state: CircuitState) -> None:
        if self._state is state:
            return
        previous = self._state
        self._state = state
        if self._on_state_change is not None:
            try:
                self._on_state_change(self.name, previous, state)
            except Exception:
                pass
