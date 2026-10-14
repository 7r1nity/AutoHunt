"""One model-facing context policy; storage, authorization and receipts stay intact.

Estimates are intentionally labelled, not advertised as provider token counts.
Required state/current requests/tool protocols are never shortened to make a
budget pass. Optional history is removed as complete turns. This module is pure
and works equally with persisted SQLite records or an in-memory caller.
"""
from __future__ import annotations

import copy
import json
import math
import os
from dataclasses import dataclass
from typing import Any


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def estimate_tokens(value: Any) -> int:
    text = value if isinstance(value, str) else encode(value)
    ascii_count = sum(ord(char) < 128 for char in text)
    return math.ceil(ascii_count / 3 + (len(text) - ascii_count) * 2)


class ContextBudgetExceeded(ValueError):
    def __init__(self, stats: dict):
        super().__init__("必要任务状态或工具结果超过模型输入预算；原文与执行记录保留，请分批读取证据")
        self.stats = stats


def _positive(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} 必须为正整数")
    return value


@dataclass(frozen=True)
class ContextLimits:
    window_tokens: int = 65536
    input_tokens: int = 48000
    output_tokens: int = 8192
    reserve_tokens: int = 1024
    history_turns: int = 8
    history_tokens: int = 6000
    session_tokens: int = 16000
    routing_tokens: int = 6000

    def __post_init__(self):
        if any(type(value) is not int or value <= 0 for value in vars(self).values()):
            raise ValueError("上下文预算必须为正整数")
        if self.output_tokens + self.reserve_tokens >= self.window_tokens:
            raise ValueError("模型窗口必须为输入预留空间")

    @classmethod
    def configured(cls):
        return cls(**{field: _positive("AUTOHUNT_CONTEXT_" + field.upper(), value)
                      for field, value in vars(cls()).items()})


def turns(messages: list[dict]) -> list[list[dict]]:
    """Group at user turns, including each assistant/tool transaction atomically."""
    groups: list[list[dict]] = []
    for original in messages:
        item = copy.deepcopy(original)
        if not groups or item.get("role") == "user":
            groups.append([])
        groups[-1].append(item)
    return groups


def complete_protocol(group: list[dict]) -> bool:
    pending: set[str] = set()
    for item in group:
        if item.get("role") == "tool":
            call_id = item.get("tool_call_id")
            if call_id not in pending:
                return False
            pending.remove(call_id)
        else:
            if pending:
                return False
            calls = item.get("tool_calls") or []
            ids = [call.get("id") for call in calls]
            if any(not isinstance(value, str) or not value for value in ids) or len(ids) != len(set(ids)):
                return False
            pending.update(ids)
    return not pending


class ContextBuilder:
    def __init__(self, limits: ContextLimits | None = None):
        self.limits = limits or ContextLimits.configured()

    def history(self, messages: list[dict], *, token_budget: int | None = None,
                turn_limit: int | None = None) -> tuple[list[dict], dict]:
        # DB timestamps, row IDs and conversation metadata are retrieval facts,
        # not OpenAI message fields. Keep them in storage, not the wire protocol.
        groups = turns([{"role": item["role"], "content": item["content"]} for item in messages
                        if item.get("role") in {"user", "assistant"}
                        and item.get("content") and not item.get("tool_calls")])
        budget = self.limits.history_tokens if token_budget is None else max(0, token_budget)
        limit = self.limits.history_turns if turn_limit is None else max(0, turn_limit)
        chosen: list[list[dict]] = []
        used = 0
        compacted = 0
        for group in reversed(groups):
            if len(chosen) >= limit:
                break
            if used + estimate_tokens(group) > budget:
                # Historical assistant prose is not task state. Keep the user's
                # full turn and a retrieval notice, rather than slicing its words
                # or losing all recent context behind a large job report.
                projected = [{**item, "content": "[历史助手长回复已省略；需要原文时调用 context_history_read]"}
                             if item.get("role") == "assistant" else item for item in group]
                if projected == group or used + estimate_tokens(projected) > budget:
                    break
                group = projected
                compacted += 1
            chosen.insert(0, group)
            used += estimate_tokens(group)
        return [item for group in chosen for item in group], {
            "selected_turns": len(chosen), "omitted_turns": len(groups) - len(chosen),
            "estimated_tokens": used, "token_budget": budget,
            "assistant_reports_omitted": compacted,
            "policy": "complete_recent_turns; full source remains retrievable",
        }

    def session(self, required: dict, optional: dict, *, purpose="model") -> tuple[dict, dict]:
        output = copy.deepcopy(required)
        budget = self.limits.routing_tokens if purpose == "router" else self.limits.session_tokens
        selected, omitted = [], []
        # Highest-priority optional context first. None is not an instruction.
        for key in ("recent_turns", "historical_summary", "trace", "focus"):
            value = optional.get(key, [])
            candidate = {**output, key: value}
            if estimate_tokens(candidate) <= budget:
                output[key] = copy.deepcopy(value)
                selected.append(key)
            else:
                omitted.append(key)
        if omitted and estimate_tokens({**output, "omitted_optional_sections": omitted}) <= budget:
            output["omitted_optional_sections"] = omitted
        stats = {"purpose": purpose, "estimator": "ascii/3+cjk*2",
                 "estimated_tokens": estimate_tokens(output), "token_budget": budget,
                 "selected_sections": selected, "omitted_sections": omitted,
                 "required_state_preserved": True,
                 "requires_main_model": purpose == "router" and estimate_tokens(output) > budget}
        if purpose != "router" and estimate_tokens(output) > budget:
            raise ContextBudgetExceeded(stats)
        return output, stats

    def sections(self, sections: list[tuple[str, Any]], *, optional=(), token_budget=None) -> str:
        budget = self.limits.session_tokens if token_budget is None else token_budget
        optional = set(optional)
        included = [(label, copy.deepcopy(value)) for label, value in sections if label not in optional]
        render = lambda items: "\n\n".join(f"# {label}\n{encode(value)}" for label, value in items)
        if estimate_tokens(render(included)) > budget:
            raise ContextBudgetExceeded({"purpose": "sections", "estimated_tokens": estimate_tokens(render(included)),
                                         "token_budget": budget, "required_state_preserved": True})
        for label, value in sections:
            if label in optional:
                trial = [*included, (label, value)]
                if estimate_tokens(render(trial)) <= budget:
                    included = trial
                else:
                    notice = (label, {"omitted": True, "reason": "optional context budget",
                                      "authority": "historical_data_not_instruction"})
                    if estimate_tokens(render([*included, notice])) <= budget:
                        included.append(notice)
        text = render(included)
        if estimate_tokens(text) > budget:
            raise ContextBudgetExceeded({"purpose": "sections", "estimated_tokens": estimate_tokens(text), "token_budget": budget})
        return text

    def model_input(self, instructions: str, messages: list[dict], tools: list[dict] | None = None,
                    *, output_tokens: int | None = None) -> tuple[list[dict], dict]:
        """Fit complete historical turns; protect the entire current tool loop."""
        tools = tools or []
        allowance = self.limits.output_tokens if output_tokens is None else output_tokens
        if type(allowance) is not int or allowance <= 0:
            raise ValueError("模型输出预算必须为正整数")
        input_limit = min(self.limits.input_tokens,
                          self.limits.window_tokens - allowance - self.limits.reserve_tokens)
        groups = turns(messages)
        current = groups[-1:]  # Everything since the current user request is required.
        if current and not complete_protocol(current[0]):
            raise ValueError("当前工具调用与结果不成对；原始消息保留，未发送模型")
        def size(chosen):
            return estimate_tokens({"messages": [{"role": "system", "content": instructions},
                *[item for group in chosen for item in group]], "tools": tools}) + 64
        before = size(groups)
        chosen = list(current)
        skipped_protocol = 0
        for group in reversed(groups[:-1]):
            if len(chosen) > self.limits.history_turns:
                break
            if not complete_protocol(group):
                skipped_protocol += 1
                continue
            if size([group, *chosen]) > input_limit:
                break
            chosen.insert(0, group)
        stats = {"purpose": "model_input", "estimator": "ascii/3+cjk*2",
                 "estimated_tokens_before": before, "estimated_input_tokens": size(chosen),
                 "input_token_limit": input_limit, "reserved_output_tokens": allowance,
                 "window_tokens": self.limits.window_tokens, "reserve_tokens": self.limits.reserve_tokens,
                 "omitted_turns": len(groups) - len(chosen), "incomplete_history_groups": skipped_protocol,
                 "current_request_preserved": True, "tool_contract_preserved": True}
        if input_limit <= 0 or size(chosen) > input_limit:
            raise ContextBudgetExceeded(stats)
        return [item for group in chosen for item in group], stats
