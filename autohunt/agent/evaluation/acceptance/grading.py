"""Acceptance predicates independent of model grades and implementation parsing.

Only grades the explicitly declared numeric monthly salary cases. Unknown forms
fail closed; no keyword-based inference of unstated semantic requirements.
"""
from __future__ import annotations


def salary_allowed(args, amount):
    def visit(node):
        if node.get("op") in {"all", "any", "not"}:
            values = [visit(child) for child in node.get("children", [])]
            if not values or any(v is None for v in values): return None
            if node["op"] == "all": return all(values)
            if node["op"] == "any": return any(values)
            return not values[0] if len(values) == 1 else None
        salary = node.get("salary")
        if not salary or node.get("mode", "require") != "require": return None
        unit = salary.get("unit")
        if unit == "negotiable": return amount is None
        if unit != "monthly": return False
        if amount is None: return False
        return ((salary.get("minimum") is None or amount >= salary["minimum"])
                and (salary.get("maximum") is None or amount <= salary["maximum"]))
    if args.get("filters"):
        return visit(args["filters"])
    floor = args.get("salary_min")
    return (amount is not None and amount >= floor) if floor else None


def grade(plan, expected):
    active = [s for s in plan.get("steps", []) if not s.get("withdrawn")
              and s.get("status") not in {"skipped", "blocked", "cancelled"}]
    searches = [s for s in active if s["tool"] == "job_search_start"]
    args = (searches[0].get("staged_arguments") or searches[0].get("arguments", {})) if len(searches) == 1 else {}
    checks = {"one_search": len(searches) == 1,
              "locations": sorted(args.get("locations", [])) == sorted(expected["locations"]),
              "result_limit": args.get("result_limit") == expected["result_limit"]}
    floor = expected["monthly_floor"]
    checks["salary_scope"] = all(salary_allowed(args, value) is wanted for value, wanted in
                                  [(floor-1, False), (floor, True), (floor+10000, True), (None, False)])
    sends = [s for s in active if s["tool"] == "mock_delivery_start"]
    if expected.get("no_delivery"):
        checks["no_delivery"] = not sends
    for tool in expected.get("required_tools", []):
        checks["has_"+tool] = any(s["tool"] == tool for s in active)
    if "delivery_min_score" in expected:
        checks["delivery_min_score"] = len(sends) == 1 and sends[0].get("arguments", {}).get("min_score") == expected["delivery_min_score"]
    if "paused" in expected:
        checks["paused"] = bool(plan.get("paused")) == expected["paused"]
    if "query_contains" in expected:
        checks["query"] = any(expected["query_contains"].lower() in q.lower() for q in args.get("queries", []))
    if expected.get("dependent_tool"):
        by_id = {s["id"]: s for s in active}
        def depends(ident, target, seen=None):
            seen = set() if seen is None else seen
            if ident in seen: return False
            seen.add(ident)
            return any(d == target or depends(d, target, seen) for d in (by_id.get(ident, {}).get("depends_on") or []))
        checks["search_dependency"] = len(searches) == 1 and any(
            depends(s["id"], searches[0]["id"]) for s in active if s["tool"] == expected["dependent_tool"])
    return checks
