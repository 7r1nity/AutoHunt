"""Regrade frozen predictions, audit ties, diagnose failures; ZERO model calls."""
from __future__ import annotations

import argparse
import json
import math
import random
from collections import Counter, defaultdict
from pathlib import Path

from .common import aggregate, write, percentile
from .planning import check_plan
from .audit import execution_audit


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def tie_metrics(row):
    labels,scores,eligible=row["labels"],row["scores"],row["eligibility"]
    groups=defaultdict(list)
    for index,score in enumerate(scores): groups[score].append(index)
    dcg=precision=high=hard=unknown=0; position=0
    for score,indices in sorted(groups.items(),reverse=True):
        exposure=min(len(indices),max(0,5-position))
        discounts=sum(1/math.log2(position+j+2) for j in range(exposure))
        dcg+=discounts*sum(2**labels[i]-1 for i in indices)/len(indices)
        precision+=exposure*sum(labels[i]>=1 for i in indices)/len(indices)
        high+=exposure*sum(labels[i]==2 for i in indices)/len(indices)
        hard+=exposure*sum(eligible[i]=="fail" for i in indices)/len(indices)
        unknown+=exposure*sum(eligible[i]=="unknown" for i in indices)/len(indices)
        position+=len(indices)
    ideal=sum((2**v-1)/math.log2(i+2) for i,v in enumerate(sorted(labels,reverse=True)[:5]))
    relevant=sum(v>=1 for v in labels)
    return {"ndcg_at_5":dcg/ideal if ideal else None,"precision_at_5":precision/5,"high_precision_at_5":high/5,
        "hard_fail_at_5":hard/5,"unknown_at_5":unknown/5,"recall_at_5_pool":precision/relevant if relevant else None,
        "tied_score_groups":sum(len(group)>1 for group in groups.values())}


def payload(call):
    content=call["request"]["messages"][1]["content"]
    if "<user_request>" in content: content=content.split("<user_request>",1)[1].split("</user_request>",1)[0].strip()
    try: return json.loads(content)
    except ValueError: return {}


def analyze(output):
    query_rows=rows(output/"ranking-queries.jsonl")
    audited=[{"query_id":r["query_id"],"system":r["system"],"split":r["split"],**tie_metrics(r)} for r in query_rows]
    ranking={system:aggregate([r for r in audited if r["system"]==system and r["split"]=="challenge"])
        for system in {r["system"] for r in audited}}
    # Candidate-query bootstrap, not pair-level IID; small synthetic sample remains a limitation.
    by_key={(r["query_id"],r["system"]):r for r in audited if r["split"]=="challenge"}
    query_ids=sorted({r["query_id"] for r in audited if r["split"]=="challenge"})
    rng=random.Random(20261026); gains=[]
    for _ in range(1000):
        draw=rng.choices(query_ids,k=len(query_ids))
        values=[by_key[(q,"production_llm_with_fallback")]["ndcg_at_5"]-by_key[(q,"production_rule")]["ndcg_at_5"] for q in draw
                if (q,"production_llm_with_fallback") in by_key and by_key[(q,"production_llm_with_fallback")]["ndcg_at_5"] is not None and by_key[(q,"production_rule")]["ndcg_at_5"] is not None]
        if values: gains.append(sum(values)/len(values))
    write(output/"ranking-audit.json",{"tie_policy":"expected metrics under uniform random ordering within equal scores; no extra model calls",
        "challenge":ranking,"paired_ndcg_gain_bootstrap95":[percentile(gains,.025),percentile(gains,.975)],
        "queries":audited,"limitations":"12 candidate queries, shared synthetic templates, provisional labels; not real hiring quality"})
    cases=rows(output/"planning-cases.jsonl"); reassessed=[]
    for row in cases:
        verdict={"id":row["id"],"original_status":row["status"],"status":row["status"],"error":row.get("error")}
        if row.get("trace"):
            plan=row["trace"]["final_run"]["checkpoint"]["plan"]
            checks=check_plan(plan,row["expected"],{},None)
            verdict.update(checks=checks,status="pass" if all(checks.values()) else "fail")
        reassessed.append(verdict)
    write(output/"planning-regraded.json",{"mode":"offline regrading of saved validated plans; NOT new model trials",
        "reasons":["typed salary predicates equivalent to legacy fields","transitive dependency is valid","paused staged search revision is valid condition update"],
        "outcomes":dict(Counter(r["status"] for r in reassessed)),"cases":reassessed})
    calls=rows(output/"model-calls.jsonl")
    feedback=[]; scope_expansions=[]
    for call in calls:
        if not call["case"].startswith("planning:"): continue
        request=payload(call)
        if request.get("validation_feedback"):
            feedback.append({"case":call["case"],"call":call["call"],"issues":request["validation_feedback"].get("issues")})
        response=call.get("response",{}).get("choices",[{}])[0].get("message",{}).get("content","")
        if "negotiable" in response and "面议" not in request.get("message","") and not request.get("active_plan"):
            scope_expansions.append({"case":call["case"],"call":call["call"],"note":"Raw model added negotiable despite no such initial request; inspect typed predicate before concluding actual scope expansion"})
    execution_summary=json.loads((output/"execution-summary.json").read_text(encoding="utf-8")) if (output/"execution-summary.json").exists() else None
    execution=rows(output/execution_summary["artifact_directory"]/"execution-cases.jsonl") if execution_summary else []
    if execution_summary:
        write(output/"execution-audit.json", execution_audit(output, execution_summary, execution))
    latency=[c["latency_ms"] for c in calls]; usage=Counter()
    for call in calls:
        u=call.get("response",{}).get("usage") or {}
        for key in ("prompt_tokens","completion_tokens","total_tokens"): usage[key]+=u.get(key) or 0
    diagnosis={"physical_requests":len(calls),"budget":json.loads((output/"budget.json").read_text(encoding="utf-8")),
        "phase_requests":dict(Counter(c["case"].split(":")[0] for c in calls)),"usage":dict(usage),
        "model_latency_ms":{"p50":percentile(latency,.5),"p95":percentile(latency,.95)},"known_provider_cost":None,
        "planning_validation_feedback":feedback,"potential_unrequested_scope_expansions":scope_expansions,
        "planning_regraded":dict(Counter(r["status"] for r in reassessed)),
        "execution_failed_cases":[{"id":r["id"],"error":r.get("error"),"expected":r.get("effective_expected",r["expected"]),
            "database":r.get("database_checks"),"delivery":r.get("delivery_runs")} for r in execution if not r["task_success"]],
        "execution_consistency":{method:{scenario:all(r["task_success"] for r in execution if r["method"]==method and r["scenario"]==scenario)
            for scenario in {r["scenario"] for r in execution}} for method in ("fixed_script","agent")}}
    write(output/"diagnosis.json",diagnosis)
    print(json.dumps({"tie_aware_challenge":ranking,"planning_regraded":diagnosis["planning_regraded"],"requests":len(calls),"usage":dict(usage)},ensure_ascii=False,indent=2))


if __name__=="__main__":
    parser=argparse.ArgumentParser(); parser.add_argument("output"); args=parser.parse_args()
    analyze(Path(args.output))
