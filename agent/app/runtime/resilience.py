"""Shared fail-closed retry/error/watchdog policies; never replay an effect."""
from __future__ import annotations

import asyncio
import os
import random
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class Failure:
    code: str
    retryable: bool
    effect_started: bool
    uncertain: bool

    def metadata(self) -> dict:
        return asdict(self)


def classify_failure(exc: Exception, *, effect="read", started=False) -> Failure:
    """Retryability is a property of BOTH the error and the operation."""
    uncertain = started and effect in {"write", "external"}
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    name = type(exc).__name__
    transient = False
    if isinstance(exc, TimeoutError) or "Timeout" in name:
        code, transient = "timeout", True
    elif status == 429:
        code, transient = "rate_limited", True
    elif isinstance(status, int) and status in {408, 500, 502, 503, 504}:
        code, transient = "service_unavailable", True
    elif isinstance(exc, ConnectionError) or name in {"APIConnectionError", "ConnectError", "ReadError", "RemoteProtocolError"}:
        code, transient = "connection_failed", True
    elif status in {401, 403} or isinstance(exc, PermissionError):
        code = "permission_denied"
    elif isinstance(exc, (ValueError, KeyError, TypeError)):
        code = "invalid_arguments"
    else:
        code = "execution_failed"
    return Failure(code, transient and effect == "read" and not uncertain, started, uncertain)


async def safe_retry(operation: Callable[[], Awaitable[T]], *, retry_safe=False,
                     attempts=3, on_retry: Callable[[dict], None] | None = None) -> T:
    """Caller owns the overall timeout. Auth/budget are checked ONCE outside.

    Only explicit idempotent reads/inference opt in. Streaming after output and
    browser clicks/writes must never use this helper. Cancellation propagates.
    """
    for attempt in range(1, min(3, max(1, attempts)) + 1):
        try:
            return await operation()
        except Exception as exc:
            failure = classify_failure(exc, started=True)
            if not retry_safe or not failure.retryable or attempt >= min(3, max(1, attempts)):
                raise
            delay = min(2.0, 0.4 * 2 ** (attempt - 1)) + random.uniform(0, 0.2)
            if on_retry:
                on_retry({**failure.metadata(), "attempt": attempt, "next_attempt": attempt + 1,
                          "delay_seconds": round(delay, 3)})
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


def _seconds(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
        return value if 0 < value < float("inf") else default
    except ValueError:
        return default


@dataclass(frozen=True)
class WatchLimits:
    active: float = 900
    idle: float = 180
    human: float = 900
    reply: float = 86400
    confirmation: float = 86400

    @classmethod
    def configured(cls):
        return cls(**{key: _seconds("AUTOHUNT_" + key.upper() + "_TIMEOUT_SECONDS", value)
                      for key, value in asdict(cls()).items()})


class WatchdogExpired(TimeoutError):
    def __init__(self, code: str):
        self.code = code
        super().__init__({"no_progress": "任务长时间无进展，已安全停止；请核对页面后重新下达指令",
            "active_timeout": "浏览器执行预算耗尽，已停止并保留部分结果",
            "human_timeout": "人工接管等待已过期，未绕过验证，请重新下达指令"}[code])


class ProgressWatch:
    """Repeated timestamps/heartbeats are NOT progress. Human time is separate."""
    def __init__(self, limits: WatchLimits, *, now: float | None = None):
        self.limits = limits
        self.last_tick = time.monotonic() if now is None else now
        self.changed_at = self.last_tick
        self.key = None
        self.phase = None
        self.active_elapsed = 0.0

    def check(self, key, *, human=False, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if self.phase == "active":
            self.active_elapsed += max(0, now - self.last_tick)
        self.last_tick = now
        phase = "human" if human else "active"
        if self.phase != phase or (not human and key != self.key):
            self.changed_at = now
        self.phase, self.key = phase, key
        if human:
            if now - self.changed_at >= self.limits.human:
                raise WatchdogExpired("human_timeout")
        elif self.active_elapsed >= self.limits.active:
            raise WatchdogExpired("active_timeout")
        elif now - self.changed_at >= self.limits.idle:
            raise WatchdogExpired("no_progress")


async def guarded_operation(operation: Awaitable[T], observe: Callable[[], tuple], *,
                            limits: WatchLimits, interval=1.0) -> T:
    """Observe without touching the browser; cancel and JOIN before releasing lease."""
    worker = asyncio.create_task(operation)
    watch = ProgressWatch(limits)
    try:
        while not worker.done():
            key, human = observe()
            watch.check(key, human=human)
            await asyncio.wait({worker}, timeout=interval)
        return await worker
    finally:
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


def wait_expired(started_at, seconds: float) -> bool:
    """Persisted wall clock deadline, not reset by polling/restart."""
    if not started_at:
        return False
    value = datetime.fromisoformat(started_at.replace("Z", "+00:00")) if isinstance(started_at, str) else started_at
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return (datetime.now(UTC) - value).total_seconds() >= seconds
