"""Consented synthetic planning only; never execute generated recruitment tools."""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from .run import REGISTRY, consented_provider
from app.workflow.planning import plan_request


CASES = [
    ("five_each", "帮我找5份正式岗位、5份实习，不要外包。", "groups"),
    ("three_industries", "找机械工程师岗位，正式4个、实习3个、短期合同2个，地点深圳上海。", "three_groups"),
    ("ordinal_33", "自动投递第33个", "selector"),
    ("top_five", "把前五个直接投递", "selector"),
    ("replace_total", "按原来的城市薪资条件再搜索15个职位，不再按正式实习分组", "total"),
    ("named_job", "投递合成岗位甲", "selector"),
]


async def run(budget):
    traces, results = [], []
    provider = consented_provider(budget, traces)
    try:
        for ident, message, kind in CASES:
            conditions = {"queries": ["合成数据分析师"], "source": "mock", "locations": ["深圳"], "salary_min": 15000}
            if kind == "total":
                conditions.update(result_limit=10, result_groups=[
                    {"label": "正式", "count": 5, "filters": {"op": "condition", "text": "正式岗位"}},
                    {"label": "实习", "count": 5, "filters": {"op": "condition", "text": "实习岗位"}}])
            dialogue = {"tasks": {"browser_id": "synthetic-completed-search"},
                        "focus": [{"kind": "job", "id": "synthetic-list", "count": 40,
                                   "items": [{"id": 100, "title": "合成岗位甲"}]}]}
            item = {"id": ident, "message": message, "kind": kind}
            before = provider.calls
            try:
                plan = await plan_request(provider, REGISTRY, message, dialogue=dialogue,
                                          conditions=conditions, stamp="synthetic-resume")
                item["plan"] = plan
                searches = [step for step in plan["steps"] if step["tool"] == "job_search_start"]
                deliveries = [step for step in plan["steps"] if step["tool"] == "mock_delivery_start"]
                checks = {"ready": plan["status"] == "ready"}
                if kind in {"groups", "three_groups"}:
                    groups = searches[0]["arguments"].get("result_groups") or [] if searches else []
                    checks["quotas"] = sorted(group["count"] for group in groups) == ([5, 5] if kind == "groups" else [2, 3, 4])
                    checks["total"] = bool(searches) and searches[0]["arguments"].get("result_limit") == (10 if kind == "groups" else 9)
                elif kind == "selector":
                    checks["selected_not_batch"] = len(deliveries) == 1 and bool(deliveries[0]["arguments"].get("selector"))
                    checks["no_new_search"] = not searches
                elif kind == "total":
                    checks["total"] = bool(searches) and searches[0]["arguments"].get("result_limit") == 15
                    checks["old_quota_cleared"] = bool(searches) and not searches[0]["arguments"].get("result_groups")
                item.update(checks=checks, passed=all(checks.values()))
            except Exception as error:
                item.update(passed=False, error_type=type(error).__name__, error=str(error))
            item["calls"] = provider.calls - before
            results.append(item)
            print(json.dumps({key: item[key] for key in ("id", "passed", "calls")}), flush=True)
    finally:
        await provider.client.close()
        output = Path(__file__).parent / "results" / "selection-quota-model" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        output.mkdir(parents=True, exist_ok=True)
        (output / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        (output / "requests.json").write_text(json.dumps(traces, ensure_ascii=False, indent=2), encoding="utf-8")
        summary = {"cases": len(results), "passed": sum(item["passed"] for item in results), "calls": provider.calls, "budget": budget}
        (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(json.dumps({"output": str(output), **summary}), flush=True)
    return 0 if all(item["passed"] for item in results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--models-consented", action="store_true")
    parser.add_argument("--max-calls", type=int, choices=range(1, 9), required=True)
    args = parser.parse_args()
    if not args.models_consented:
        parser.error("External synthetic model evaluation requires explicit consent")
    raise SystemExit(asyncio.run(run(args.max_calls)))
