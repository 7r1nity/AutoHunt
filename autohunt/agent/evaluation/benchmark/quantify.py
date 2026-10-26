"""Offline metrics from immutable predictions, tool events and independent SQL.

Definitions are fixed before the run; no model requests or production effects.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

from .analyze import tie_metrics
from .audit import execution_audit
from .common import aggregate, percentile, write
from .planning import check_plan


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def fraction(successes, total):
    result={"successes":successes,"total":total,"rate":successes/total if total else None}
    if total:
        z=1.959963984540054; p=successes/total; denominator=1+z*z/total
        centre=(p+z*z/(2*total))/denominator
        half=z*math.sqrt(p*(1-p)/total+z*z/(4*total*total))/denominator
        result["wilson95"]=[max(0,centre-half),min(1,centre+half)]
    return result


def durations(values):
    return {"n":len(values),"mean_ms":sum(values)/len(values) if values else None,
            "p50_ms":percentile(values,.5),"p95_ms":percentile(values,.95)}


def execution_metrics(cases, audit, specifications):
    trusted={r["id"]:r for r in audit["cases"]}
    recoverable={r["id"] for r in specifications if r["expect_recovery"]}
    # Actual fault/refresh subset, separate from simple latency tolerance.
    strict={"submit_response_lost","reload_after_greeting"}
    systems={}
    for method in ("fixed_script","agent"):
        selected=[r for r in cases if r["method"]==method]
        positive=[r for r in selected if trusted[r["id"]]["positive_case"]]
        protected=[r for r in selected if not trusted[r["id"]]["positive_case"]]
        succeeds=lambda r:trusted[r["id"]]["task_success"]
        events=[e["data"] for r in selected for e in (r.get("agent_trace") or {}).get("events",[]) if e["kind"]=="tool_finished"]
        tools=Counter(e["status"] for e in events)
        by_tool={}
        for tool in sorted({e["tool"] for e in events}):
            subset=[e for e in events if e["tool"]==tool]
            by_tool[tool]={**fraction(sum(e["status"]=="ok" for e in subset),len(subset)),
                           "duration":durations([e["duration_ms"] for e in subset if e.get("duration_ms") is not None])}
        recovery=[r for r in selected if r["scenario"] in recoverable]
        fault=[r for r in selected if r["scenario"] in strict]
        systems[method]={
            "task_success":fraction(sum(succeeds(r) for r in selected),len(selected)),
            "task_failure":fraction(sum(not succeeds(r) for r in selected),len(selected)),
            "end_to_end_delivery_success":fraction(sum(succeeds(r) for r in positive),len(positive)),
            "protected_case_handling":fraction(sum(succeeds(r) for r in protected),len(protected)),
            "task_duration":durations([r["latency_ms"] for r in selected]),
            "positive_delivery_duration":durations([r["latency_ms"] for r in positive]),
            "planning_duration":durations([r["planning_ms"] for r in selected if "planning_ms" in r]),
            "raw_tool_success":fraction(tools["ok"],len(events)),
            "raw_tool_failure":fraction(len(events)-tools["ok"],len(events)),
            "tool_statuses":dict(tools),"by_tool":by_tool,
            "recovery_and_delay_tolerance":fraction(sum(succeeds(r) for r in recovery),len(recovery)),
            "fault_recovery":fraction(sum(succeeds(r) for r in fault),len(fault)),
            "duplicates":sum(trusted[r["id"]]["assertions"]["duplicate_application_records"] for r in selected),
            "unsafe_submissions":sum(r.get("unsafe_submission",False) for r in selected),
            "repeated_post_attempts":sum(trusted[r["id"]]["post_attempts_above_one"] for r in selected),
            "by_scenario":[{"scenario":name,**fraction(sum(succeeds(r) for r in selected if r["scenario"]==name),sum(r["scenario"]==name for r in selected))}
                           for name in sorted({r["scenario"] for r in selected})],
            "failed_cases":[{"id":r["id"],"scenario":r["scenario"],"error_type":r.get("error_type"),
                             "checkpoints":[x.get("checkpoint") for x in r.get("delivery_runs",[])]} for r in selected if not succeeds(r)]}
    return systems


def quantify(output):
    read=lambda name:json.loads((output/name).read_text(encoding="utf-8"))
    data=next(output.glob("dataset-*.json")); data=json.loads(data.read_text(encoding="utf-8"))
    rankings=rows(output/"ranking-queries.jsonl")
    ranking={}
    for method in sorted({r["system"] for r in rankings}):
        subset=[r for r in rankings if r["split"]=="challenge" and r["system"]==method]
        ranking[method]={"queries":len(subset),"pairs":sum(len(r["pool"]) for r in subset),
            "metrics":aggregate([tie_metrics(r) for r in subset]),
            "recall_defined_queries":sum(any(v>=1 for v in r["labels"]) for r in subset),
            "ndcg_defined_queries":sum(any(v>0 for v in r["labels"]) for r in subset)}
    predictions=rows(output/"ranking-predictions.jsonl")
    oracle={(p["candidate_id"],p["job_id"]):p for p in data["pairs"]}
    qualification=Counter()
    for r in predictions:
        expected=oracle[(r["candidate_id"],r["job_id"])]["eligibility"]
        actual=(r["analysis"].get("qualification") or {}).get("status","unknown")
        qualification[(expected,actual)]+=1
    summary=read("execution-summary.json")
    cases=rows(output/summary["artifact_directory"]/"execution-cases.jsonl")
    audit=execution_audit(output,summary,cases); write(output/"execution-audit.json",audit)
    calls=rows(output/"model-calls.jsonl"); usage=Counter()
    for r in calls:
        for key in ("prompt_tokens","completion_tokens","total_tokens","prompt_cache_hit_tokens","prompt_cache_miss_tokens"):
            usage[key]+=(r.get("response",{}).get("usage") or {}).get(key,0) or 0
    plans=rows(output/"planning-cases.jsonl")
    def plan_good(row):
        plan=(row.get("trace") or {}).get("final_run",{}).get("checkpoint",{}).get("plan",{})
        waits=(plan.get("scope_review") or {}).get("waits")
        return row["status"]=="pass" and (not waits or bool(row["expected"].get("clarify")))
    regraded=[]
    for row in plans:
        grade={"id":row["id"],"original_status":row["status"],"status":row["status"]}
        if row.get("trace"):
            plan=row["trace"]["final_run"]["checkpoint"]["plan"]
            checks=check_plan(plan,row["expected"],{},None)
            grade.update(checks=checks,status="pass" if all(checks.values()) else "fail")
        regraded.append(grade)
    result={"production_commit":read("metadata.json")["git_commit"],"dataset_sha256":read("metadata.json")["dataset_sha256"],
        "definitions":{
            "ranking":"Macro on 12 challenge queries, fixed 15-job pools; positives relevance>=1, gains 2^relevance-1; ties uniformly averaged",
            "recall":"Recall@5 within labelled candidate pool, not full-site retrieval Recall; zero-relevant queries omitted with explicit count",
            "task_success":"Expected final result including correct safety waits/stops; independent backend content and HR-order checks",
            "end_to_end_delivery":"Only must-submit cases; selected-job instruction through planner/tool/queue/browser to correct backend receipt, not full upload/search pipeline",
            "raw_tool_success":"tool_finished status=ok / all observed tool_finished; includes protective refusals as raw non-ok, not DOM click accuracy; fixed script not instrumented",
            "duration":"Trial wall time includes fixture setup, real model planning, execution and teardown; HR delay is synthetic, not real recruitment response time",
            "recovery_and_delay_tolerance":"Five preregistered cases: slow_page, slow_messages, submit_response_lost, hr_delayed, reload_after_greeting",
            "fault_recovery":"Strict subset submit_response_lost and reload_after_greeting; not all possible production errors",
            "confidence":"Wilson intervals describe finite trials, repetitions/templates are correlated; not proof of production reliability"},
        "ranking":ranking,"qualification":{"pairs":len(predictions),
            "accuracy":sum(n for (a,b),n in qualification.items() if a==b)/len(predictions) if predictions else None,
            "confusion":[{"expected":a,"actual":b,"count":n} for (a,b),n in sorted(qualification.items())],
            "matcher_duration":durations([r["latency_ms"] for r in predictions])},
        "execution":execution_metrics(cases,audit,data["execution"]),
        "planning":{"cases":len(plans),"raw_fully_passed":sum(plan_good(r) for r in plans),
                    "raw_statuses":dict(Counter(r["status"] for r in plans)),"duration":durations([r["latency_ms"] for r in plans]),
                    "offline_regraded":dict(Counter(r["status"] for r in regraded)),"regraded_cases":regraded,
                    "regrade_reason":"Scope may be enforced by an unchanged search producer and evidence_task_id; no extra API calls or new outcomes",
                    "raw_failed_cases":[{"id":r["id"],"status":r["status"],"error_type":r.get("error_type"),"checks":r.get("checks")} for r in plans if not plan_good(r)]},
        "cost":{"physical_requests":len(calls),"persisted_used":read("budget.json")["used"],"limit":read("budget.json")["limit"],
                "requests_by_phase":dict(Counter(r["case"].split(":")[0] for r in calls)),"usage":dict(usage),
                "model_names":sorted({r["response"]["model"] for r in calls if r.get("response")}),
                "request_latency":durations([r["latency_ms"] for r in calls]),"provider_errors":dict(Counter(r["error_type"] for r in calls if r.get("error_type"))),
                "money":None,"money_reason":"No verified provider billing invoice; report measured tokens instead"},
        "source_integrity":{phase:read(phase+"-source-check.json") for phase in ("ranking","execution","planning") if (output/(phase+"-source-check.json")).exists()},
        "offline_postprocessing":{"meaning":"Computed after frozen experiment; original predictions, outcomes and grading retained",
            "sha256":{name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                      for name in ("quantify.py","planning.py","analyze.py","audit.py")}},
        "limits":["Synthetic oracle pending human review, not human Gold","12 queries share JD templates; not independent unseen-domain generalization",
                  "24 browser scenarios repeated twice, no full upload/search E2E or real sites","Current data and two repetitions cannot establish long-running service or concurrency reliability"]}
    write(output/"quantified-results.json",result)
    return result


if __name__=="__main__":
    parser=argparse.ArgumentParser(); parser.add_argument("output",type=Path); args=parser.parse_args()
    result=quantify(args.output)
    print(json.dumps(result,ensure_ascii=False,indent=2))
