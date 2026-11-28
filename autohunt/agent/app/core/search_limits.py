"""Shared search result/quota bounds and candidate-budget inheritance.

These are job counts, not score, token, history or active-task admission limits.
"""
from __future__ import annotations

import copy


MAX_SEARCH_RESULTS = 200
MAX_SEARCH_CANDIDATES = 200
MAX_RESULT_GROUPS = 10
DEFAULT_SEARCH_CANDIDATES = 30


def search_target(arguments: dict) -> int | None:
    groups = arguments.get("result_groups")
    if isinstance(groups, list) and groups and all(isinstance(group, dict)
            and type(group.get("count")) is int for group in groups):
        return sum(group["count"] for group in groups)
    count = arguments.get("result_limit")
    return count if type(count) is int else None


def candidate_budget(target: int | None) -> int:
    return min(MAX_SEARCH_CANDIDATES, max(DEFAULT_SEARCH_CANDIDATES, (target or 0) * 5))


def with_target_budget(arguments: dict, previous: dict) -> dict:
    """A new quantity without a new budget must not inherit a smaller old cap.

    Explicit budgets remain intact, including a deliberately smaller budget
    that can only yield partial results. Malformed counts remain for the shared
    schema validator; this helper never coerces or trims them.
    """
    result = copy.deepcopy(arguments)
    target = search_target(result)
    previous_budget = previous.get("max_jobs")
    if ("max_jobs" not in result and target is not None and target != search_target(previous)
            and 1 <= target <= MAX_SEARCH_RESULTS
            and type(previous_budget) is int and previous_budget < target):
        result["max_jobs"] = candidate_budget(target)
    return result
