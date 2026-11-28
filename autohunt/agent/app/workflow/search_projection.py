"""Freeze each search scope from its dependency state, never ambient execution state."""
from __future__ import annotations

import copy

from ..runtime.search_state import SearchState, SearchPatch, merge_search_state
from .validation import PlanValidationError, dependency_ancestors


def dependency_order(steps):
    """Stable topological traversal for validation, without rewriting the plan list."""
    by_id = {step["id"]: step for step in steps}
    ordered, visiting, seen = [], set(), set()
    def visit(step):
        if step["id"] in visiting:
            raise PlanValidationError("dependency_cycle", "步骤依赖形成循环，请修正规划")
        if step["id"] in seen:
            return
        visiting.add(step["id"])
        for ident in step["depends_on"]:
            visit(by_id[ident])
        visiting.remove(step["id"])
        seen.add(step["id"])
        ordered.append(step)
    for step in steps:
        visit(step)
    return ordered


def projected_search_state(search, steps, conditions, *, candidate=None, resume_version=None):
    """Apply only ancestor condition writes in declared causal order.

    Unordered conflicting patches need a repaired dependency graph. Array order
    must not decide which user's scope wins. Disjoint/identical writes commute.
    """
    state = SearchState.model_validate(copy.deepcopy(conditions)).model_dump(mode="json")
    ancestors = dependency_ancestors(search["id"], steps)
    updates = [step for step in dependency_order(steps)
               if step["id"] in ancestors and step["tool"] == "search_state_update"]
    patches = {}
    for update in updates:
        patch = SearchPatch.model_validate(update["arguments"]).model_dump(exclude_unset=True)
        writes = copy.deepcopy(patch)
        if "result_groups" in writes or "result_limit" in writes:
            # Quotas derive totals; a total clears old quotas and may grow budget.
            writes.update({key: patch.get(key) for key in ("result_groups", "result_limit", "max_jobs")})
        if patch.get("use_resume_profile"):
            writes["queries"] = {"use_resume_profile": True}
        for previous in updates:
            if previous["id"] not in patches:
                continue
            if previous["id"] in dependency_ancestors(update["id"], steps):
                continue
            overlap = writes.keys() & patches[previous["id"]].keys()
            if any(writes[key] != patches[previous["id"]][key] for key in overlap):
                raise PlanValidationError("unordered_search_state_updates",
                    "多个前置条件更新存在不同写值但没有先后依赖，请明确更新顺序后修正规划",
                    tool="job_search_start")
        patches[update["id"]] = writes
        state = merge_search_state(state, patch, candidate, resume_version)
    return state
