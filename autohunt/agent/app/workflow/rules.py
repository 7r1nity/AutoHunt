"""Observation-driven automation: gather facts, match declarative rules, act, record.

The monitors in this package grew one hardcoded branch per reported symptom (reply
check, failed-start recovery, board sync, retry), each with its own guard, and none of
them could explain why it did or did not fire. This module replaces that shape with
one loop:

    observe  -> a frozen fact set describing the agent, the board and the site
    decide   -> urgent work first, otherwise least recently served ready work
    act      -> the single action the rules selected, if the worker is free
    record   -> both the facts and the decision, so "why did nothing happen" has an
                answer that does not require re-running anything

Rules are data. Adding a behaviour means adding a rule whose condition reads the
facts, not another branch in the loop.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Sequence


@dataclass(frozen=True)
class Observation:
    """Everything a rule is allowed to look at, captured at one instant."""

    at: float = field(default_factory=time.time)
    facts: Mapping[str, Any] = field(default_factory=dict)

    def get(self, name: str, default: Any = None) -> Any:
        return self.facts.get(name, default)

    def __contains__(self, name: str) -> bool:
        return name in self.facts

    def as_dict(self) -> dict[str, Any]:
        return {"at": self.at, **dict(self.facts)}


#: A condition reads facts and returns whether the rule applies.
Condition = Callable[[Observation], bool]
#: An action receives the observation and returns a short, user-facing result string.
Action = Callable[[Observation], Awaitable[str]]


@dataclass(frozen=True)
class Rule:
    name: str
    priority: int
    condition: Condition
    action: Action
    #: A rule that reports a fault (a failed action, a stalled job) must outrank the
    #: routine rules that would otherwise keep re-selecting the same inert job.
    reason: str = ""
    #: Seconds a fired rule stands down, so a rule that cannot make progress does not
    #: starve the lower-priority rules behind it. The highest-priority rule used to
    #: consume every cycle while its action had nothing to change.
    cooldown: float = 20.0
    #: Safety escalation may preempt the normal fair rotation. Routine reply
    #: polling/recovery/dispatch must not opt in merely because it has priority.
    preemptive: bool = False

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return f"{self.name}(p{self.priority})"


@dataclass(frozen=True)
class Decision:
    rule: str | None
    result: str
    considered: tuple[str, ...]
    skipped: tuple[str, ...] = ()
    at: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {"rule": self.rule, "result": self.result, "at": self.at,
                "considered": list(self.considered), "skipped": list(self.skipped)}


class RuleEngine:
    """One action per turn; priority breaks ties, service age prevents starvation."""

    def __init__(self, rules: Sequence[Rule] = (), *, clock: Callable[[], float] | None = None) -> None:
        self.rules: list[Rule] = sorted(rules, key=lambda rule: -rule.priority)
        #: Bounded history so a stall can be diagnosed from the last few decisions.
        self.history: list[dict[str, Any]] = []
        self.last_observation: dict[str, Any] = {}
        #: rule name -> monotonic time until which it stands down after firing.
        self.cooldowns: dict[str, float] = {}
        self.last_served: dict[str, int] = {}
        self.turn = 0
        self.clock = clock

    def register(self, rule: Rule) -> None:
        self.rules.append(rule)
        self.rules.sort(key=lambda item: -item.priority)

    def matching(self, observation: Observation) -> list[Rule]:
        matched = []
        now = self.clock() if self.clock else time.monotonic()
        for rule in self.rules:
            if self.cooldowns.get(rule.name, 0.0) > now:
                continue
            try:
                if rule.condition(observation):
                    matched.append(rule)
            except Exception:  # noqa: BLE001 - a broken condition must not stall the loop
                continue
        return matched

    def record(self, observation: Observation, decision: Decision) -> None:
        self.last_observation = observation.as_dict()
        self.history = [*self.history, decision.as_dict()][-40:]

    async def step(self, observation: Observation, *, busy: bool = False) -> Decision | None:
        """Run one cycle. ``busy`` means an action is still running: record, do not act."""
        matched = self.matching(observation)
        names = tuple(rule.name for rule in matched)
        if busy:
            decision = Decision(None, "busy: 已有动作在执行，本轮只观察", names)
            self.record(observation, decision)
            return decision
        if not matched:
            decision = Decision(None, "无规则匹配：没有需要处理的变化", ())
            self.record(observation, decision)
            return decision
        urgent = [rule for rule in matched if rule.preemptive]
        rule = urgent[0] if urgent else min(
            matched, key=lambda item: (self.last_served.get(item.name, 0), -item.priority))
        try:
            result = await rule.action(observation)
        except Exception as exc:  # noqa: BLE001 - the loop must survive any action
            result = f"动作失败：{type(exc).__name__}：{exc}"
        # Stand the rule down so the rules behind it get a turn. A rule whose condition
        # still holds comes back after the cooldown; one that made progress has usually
        # cleared its own condition by then.
        self.cooldowns[rule.name] = (self.clock() if self.clock else time.monotonic()) + rule.cooldown
        # A failed/no-progress attempt also yields its turn. Cooldown alone cannot
        # ensure fairness when the poll interval exceeds every cooldown.
        self.turn += 1
        self.last_served[rule.name] = self.turn
        decision = Decision(rule.name, result, names)
        self.record(observation, decision)
        return decision


def fact(name: str, observation: Observation, default: Any = None) -> Any:
    """Small helper so rule conditions read like sentences."""
    return observation.get(name, default)


def any_fact(observation: Observation, *names: str) -> bool:
    return any(bool(observation.get(name)) for name in names)


def all_facts(observation: Observation, *names: str) -> bool:
    return all(bool(observation.get(name)) for name in names)


async def run_until(predicate: Callable[[], bool], *, timeout: float = 10.0,
                    interval: float = 0.2) -> bool:
    """Await a state change without blocking the event loop."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()
