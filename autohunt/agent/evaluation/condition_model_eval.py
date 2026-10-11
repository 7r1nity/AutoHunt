"""Consented synthetic planning evaluation. Never executes generated tools."""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from .run import REGISTRY, consented_provider
from app.core.job_filters import evaluate_filter
from app.llm.base import LLMResult
from app.workflow.planning import plan_request


async def employment_judge(request):
    """Scripted JD adapter: isolate the model-produced expression's truth table."""
    body = json.loads(request.user_message)
    description = body["facts"]["description"]
    internship = "招聘类型：实习" in description
    rows = []
    for leaf in body["conditions"]:
        text = leaf["text"]
        if "实习" in text:
            value = not internship if any(word in text for word in ("非实习", "不是实习", "非实习岗位")) else internship
        elif any(word in text for word in ("正式", "全职", "社招", "常规")):
            value = not internship
        else:
            value = None
        rows.append({"index": leaf["index"], "status": "unknown" if value is None else "pass" if value else "fail",
                     "reason": "合成岗位类型", "evidence": [{"field": "description", "quote": description}] if value is not None else []})
    return LLMResult(json.dumps({"conditions": rows}, ensure_ascii=False), "scripted", "synthetic-employment")


CASES = [
    {"id": "scoped_salary_finance", "role": "合成金融分析师", "kind": "alternatives",
     "message": "帮我找15份金融分析师工作。正式岗月薪15k以上或者面议也可；实习也接受，但实习日薪不得低于300元。匹配85分以上自动投递。"},
    {"id": "scoped_salary_manufacturing", "role": "合成机械工程师", "kind": "alternatives",
     "message": "找15个机械工程岗位，正式岗位月薪至少15000元或薪资面议，实习岗位每天至少300元也可以，高于等于85分的直接投递。"},
    {"id": "permission_not_requirement", "role": "合成外贸业务员", "kind": "allowance",
     "message": "帮我找外贸工作，月薪15k以上或者面议也可，也可以是实习岗位。85分以上自动投递。"},
    {"id": "refresh_before_delivery", "role": "合成数据分析师", "kind": "refresh",
     "message": "条件改一下：正式岗月薪15k以上或面议，实习日薪300元以上也可以。对已经搜到的岗位按新条件重新筛选，85分以上自动投递，不要重新搜索。"},
]


async def run(limit, selected, replay=None):
    traces = []
    provider = None if replay else consented_provider(limit, traces)
    cached = {item["id"]: item["plan"] for item in json.loads(replay.read_text(encoding="utf-8")) if "plan" in item} if replay else {}
    output = Path("test-results/condition-model") / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    results = []
    try:
        cases = [case for name in selected for case in CASES if case["id"] == name]
        for case in cases:
            before = provider.calls if provider else 0
            started = time.perf_counter()
            item = {"id": case["id"], "request": case["message"]}
            conditions = {"queries": [case["role"]], "source": "mock", "locations": [], "result_limit": 15}
            if case["kind"] == "refresh":
                conditions["filters"] = {"op": "condition", "text": "接受实习岗位"}
            try:
                plan = cached[case["id"]] if replay else await plan_request(provider, REGISTRY, case["message"],
                    dialogue={"tasks": {"browser_id": "synthetic-completed-search"}}, conditions=conditions, stamp="synthetic-resume-v1")
                item["plan"] = plan
                tools = {step["tool"] for step in plan["steps"]}
                checks = {"ready": plan["status"] == "ready", "delivery": "mock_delivery_start" in tools}
                steps = [step for step in plan["steps"] if step["tool"] in {"job_search_start", "search_state_update"}]
                expression = next((step["arguments"].get("filters") for step in steps if step["arguments"].get("filters")), None)
                checks["typed_filters"] = expression is not None
                if case["kind"] in {"alternatives", "refresh"} and expression:
                    grid = [("社招", "28-33K", "pass"), ("社招", "12-20K", "fail"), ("社招", "面议", "pass"),
                            ("实习", "300-500元/天", "pass"), ("实习", "200-500元/天", "fail"), ("实习", "面议", "unknown"),
                            ("实习", "5-8K", ["fail", "unknown"])]
                    item["truth_table"] = []
                    for employment, salary, expected in grid:
                        verdict = await evaluate_filter(employment_judge, expression, {"salary": salary, "description": f"招聘类型：{employment}"})
                        item["truth_table"].append({"employment": employment, "salary": salary, "expected": expected, "actual": verdict["status"]})
                    # Both strict daily-unit mismatch and scoped unverifiable
                    # conversion are valid, but neither may qualify for delivery.
                    checks["truth_table"] = all(row["actual"] in row["expected"] if isinstance(row["expected"], list)
                        else row["actual"] == row["expected"] for row in item["truth_table"])
                if case["kind"] == "allowance" and expression:
                    verdict = await evaluate_filter(employment_judge, expression, {"salary": "28-33K", "description": "招聘类型：社招"})
                    checks["regular_job_not_excluded"] = verdict["status"] == "pass"
                    item["regular_job_verdict"] = verdict
                    def leaves(node):
                        return [node] if node["op"] == "condition" else [leaf for child in node.get("children", []) for leaf in leaves(child)]
                    checks["no_invented_daily_threshold"] = not any(leaf.get("salary", {}) and leaf["salary"].get("unit") == "daily" for leaf in leaves(expression))
                if case["kind"] == "refresh":
                    checks["refresh_dependency"] = {"search_state_update", "job_results_refilter"} <= tools and "job_search_start" not in tools
                    refresh = next((step for step in plan["steps"] if step["tool"] == "job_results_refilter"), {})
                    delivery = next((step for step in plan["steps"] if step["tool"] == "mock_delivery_start"), {})
                    checks["delivery_uses_refresh"] = bool(refresh) and delivery.get("evidence_step") == refresh.get("id")
                item.update(checks=checks, passed=all(checks.values()))
            except Exception as exc:
                item.update(passed=False, error_type=type(exc).__name__, error=str(exc))
            item.update(calls=provider.calls-before if provider else 0, latency_ms=round((time.perf_counter()-started)*1000, 2))
            results.append(item)
            (output / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            (output / "traces.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in traces), encoding="utf-8")
            print(json.dumps({key: item[key] for key in ("id", "passed", "calls", "latency_ms")}), flush=True)
            if provider and provider.calls >= limit:
                break
    finally:
        if provider: await provider.client.close()
    summary = {"cases": len(results), "passed": sum(row["passed"] for row in results), "physical_requests": provider.calls if provider else 0,
               "request_budget": limit, "browser_runs": 0, "submissions": 0,
               "unrun_cases": [case["id"] for case in cases[len(results):]],
               "replayed_from": str(replay) if replay else None,
               "note": "Real model planning only; JD truth table uses scripted evidence, not real browser end-to-end coverage."}
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), **summary}), flush=True)
    return 0 if summary["passed"] == len(cases) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--models-consented", action="store_true")
    parser.add_argument("--max-calls", type=int, choices=range(1, 9))
    parser.add_argument("--replay", type=Path, help="Offline replay of saved synthetic plans; no credentials or model requests")
    parser.add_argument("--cases", nargs="+", choices=[case["id"] for case in CASES], default=[case["id"] for case in CASES])
    args = parser.parse_args()
    if not args.replay and (not args.models_consented or not args.max_calls):
        parser.error("External inference requires --models-consented and explicit --max-calls")
    raise SystemExit(asyncio.run(run(args.max_calls or 0, args.cases, args.replay)))
