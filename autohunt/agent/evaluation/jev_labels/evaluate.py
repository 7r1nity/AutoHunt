"""Freeze blind AutoHunt predictions, then measure against independent Jev silver.

Prediction does not read annotations. Scoring does not call models or tools.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import hashlib
import json
from pathlib import Path
import random
import time

from app.browser.screening import match_job
from app.core.models import JobAnalysisRequest,JobAnalysis
from app.jobs.analysis import analyze_job
from evaluation.benchmark.common import CASE,append,write,provider,aggregate,percentile
from evaluation.benchmark.ranking import tokens
from evaluation.benchmark.analyze import tie_metrics
from .corpus import ROOT,load,digest
from .runner import result_path,lease,restore


def matcher_input(candidate,job):
    source=candidate["lines"]
    documents={"education":source["c1"],"years_experience":source["c2"],"skills":source["c3"],
               "project:delivery":"\n".join(source[k] for k in ("c4","c5","c6"))}
    if not all(text in candidate["text"] for text in documents.values()): raise ValueError("Candidate fact lost its source")
    return JobAnalysisRequest(title=job["title"],company="虚构评测公司",description=job["text"],
        candidate_skills=candidate["skills"],target_roles=[candidate["role"]],search_queries=[candidate["role"]],
        candidate_facts={"document_id":candidate["id"],"digest":digest(candidate),"documents":documents})


def predictions(output):
    path=output/"predictions.jsonl"
    rows=[json.loads(s) for s in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
    result={r["pair_id"]:r for r in rows}
    if len(result)!=len(rows): raise ValueError("Duplicate prediction pair")
    return result


async def predict(output,*,consented=False,limit=300):
    if not consented: raise ValueError("Fresh DeepSeek destination/data/budget consent is required")
    if not 1<=limit<=300: raise ValueError("This DeepSeek run has an independent 300-request ceiling")
    output=result_path(output); data=load()
    with lease(output):
        app={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/"app").rglob("*.py")}
        manifest={"dataset_sha256":digest(data),"production_sha256":app,"limit":limit,
                  "input_builder_sha256":digest(matcher_input(data["candidates"][0],data["jobs"][0]).model_dump()),
                  "scope":"synthetic candidate facts/projects/JD only; no Jev labels in predictor input"}
        path=output/"prediction-manifest.json"
        if path.exists() and json.loads(path.read_text(encoding="utf-8"))!=manifest: raise ValueError("Prediction run input/source/limit changed")
        write(path,manifest)
        rows=predictions(output); model=provider(output,limit)
        candidates={c["id"]:c for c in data["candidates"]}; jobs={j["id"]:j for j in data["jobs"]}
        semaphore=asyncio.Semaphore(3)
        async def one(pair):
            if pair["id"] in rows: return
            async with semaphore:
                request=matcher_input(candidates[pair["candidate_id"]],jobs[pair["job_id"]])
                token=CASE.set("jev_silver_match:"+pair["id"]); start=time.perf_counter(); observed=[]
                async def generate(req):
                    response=await model.generate(req); observed.append(response.model); return response
                try:
                    result=await match_job(generate,request)
                    row={"pair_id":pair["id"],"input_digest":digest(request.model_dump()),"analysis":result.model_dump(),
                         "response_models":observed,"latency_ms":round((time.perf_counter()-start)*1000,2),
                         "model_response_observed":bool(observed)}
                    append(output/"predictions.jsonl",row); rows[pair["id"]]=row
                finally: CASE.reset(token)
        try:
            for start in range(0,len(data["pairs"]),20):
                await asyncio.gather(*(one(p) for p in data["pairs"][start:start+20]))
                print(json.dumps({"prediction_pairs":len(rows),"physical_requests":model.budget.state()["used"]}),flush=True)
            changed=[name for name,sha in app.items() if hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=sha]
            summary={"pairs":len(rows),"physical_requests":model.budget.state()["used"],"source_changed":changed,
                     "model_responses":sum(bool(r["model_response_observed"]) for r in rows.values()),
                     "latency_ms":{"mean":sum(r["latency_ms"] for r in rows.values())/len(rows),"p95":percentile([r["latency_ms"] for r in rows.values()],.95)}}
            write(output/"prediction-summary.json",summary)
            return summary
        finally: await model.client.close()


def checked_labels(data,annotation_dir):
    document=json.loads((annotation_dir/"annotations.json").read_text(encoding="utf-8"))
    manifest=json.loads((annotation_dir/"manifest.json").read_text(encoding="utf-8"))
    if document["dataset_sha256"]!=digest(data) or manifest["dataset_sha256"]!=digest(data):
        raise ValueError("Reference data digest changed")
    if document.get("label_source")!="jev_model_annotation": raise ValueError("No actual Jev reference labels")
    config={"model":manifest["model"],"endpoint":manifest["endpoint"]}
    summary=json.loads((annotation_dir/"summary.json").read_text(encoding="utf-8"))
    verified=restore(data,annotation_dir,config,summary["review_threshold_value"])
    result={row["pair_id"]:row for row in document["items"]}
    if len(result)!=len(document["items"]): raise ValueError("Duplicate reference pair")
    for ident,row in result.items():
        trusted=verified.get(ident)
        if not trusted or any(row.get(k)!=trusted.get(k) for k in ("relevance","eligibility","model","input_digest","response_digest","review_flags")):
            raise ValueError("Reference label does not match frozen typed response")
    return result


def score(annotation_dir,prediction_dir,output):
    data=load(); annotation_dir=result_path(annotation_dir); prediction_dir=result_path(prediction_dir); output=result_path(output)
    output.mkdir(parents=True,exist_ok=True)
    labels=checked_labels(data,annotation_dir); forecast=predictions(prediction_dir)
    prediction_manifest=json.loads((prediction_dir/"prediction-manifest.json").read_text(encoding="utf-8"))
    if prediction_manifest["dataset_sha256"]!=digest(data): raise ValueError("Predictions belong to a different corpus")
    if set(labels)!={p["id"] for p in data["pairs"]} or set(forecast)!=set(labels):
        raise ValueError("Full-pool evaluation requires all 300 labels and predictions; do not drop difficult cases")
    candidates={c["id"]:c for c in data["candidates"]}; jobs={j["id"]:j for j in data["jobs"]}
    pairs={(p["candidate_id"],p["job_id"]):p for p in data["pairs"]}; rows=[]
    for pair in data["pairs"]:
        row=forecast[pair["id"]]
        expected=matcher_input(candidates[pair["candidate_id"]],jobs[pair["job_id"]])
        if row["input_digest"]!=digest(expected.model_dump()): raise ValueError("Prediction input digest changed")
        JobAnalysis.model_validate(row["analysis"])
    for query in data["queries"]:
        candidate=candidates[query["candidate_id"]]; pool=[jobs[j] for j in query["pool"]]
        ids=[pairs[(candidate["id"],job["id"])]["id"] for job in pool]
        reference=[labels[i]["relevance"] for i in ids]; eligibility=[labels[i]["eligibility"] for i in ids]
        systems={"keyword_overlap":[len(tokens(candidate["text"])&tokens(job["text"])) for job in pool],
                 "production_rule":[analyze_job(matcher_input(candidate,job)).score for job in pool],
                 "production_llm_with_fallback":[forecast[i]["analysis"]["score"] for i in ids]}
        for name,scores in systems.items():
            row={"query_id":query["id"],"split":query["split"],"family":candidate["family"],"system":name,
                 "scores":scores,"labels":reference,"eligibility":eligibility,"pair_ids":ids,
                 **tie_metrics({"scores":scores,"labels":reference,"eligibility":eligibility})}
            rows.append(row)
    write(output/"query-metrics.json",rows)
    groups={split:{method:{"queries":len(selected),"metrics":aggregate(selected),
                          "ndcg_defined":sum(r["ndcg_at_5"] is not None for r in selected)}
        for method in systems if (selected:=[r for r in rows if r["system"]==method and (split=="all" or r["split"]==split)])}
        for split in ("development","test","all")}
    test=[r for r in rows if r["split"]=="test"]
    by={(r["query_id"],r["system"]):r for r in test}; query_ids=sorted({r["query_id"] for r in test}); rng=random.Random(20261026); gains=[]
    for _ in range(1000):
        vals=[by[(qid,"production_llm_with_fallback")]["ndcg_at_5"]-by[(qid,"production_rule")]["ndcg_at_5"]
              for qid in rng.choices(query_ids,k=len(query_ids)) if by[(qid,"production_llm_with_fallback")]["ndcg_at_5"] is not None]
        if vals: gains.append(sum(vals)/len(vals))
    confusion=Counter((labels[i]["eligibility"],(forecast[i]["analysis"].get("qualification") or {}).get("status","unknown")) for i in labels)
    result={"dataset_sha256":digest(data),"label_source":"independent Jev model annotations, unreviewed silver","human_gold":False,
            "reference_endpoint":json.loads((annotation_dir/"manifest.json").read_text(encoding="utf-8"))["endpoint"],
            "pairs":len(labels),"flagged_reference_pairs":sum(bool(r["review_flags"]) for r in labels.values()),
            "actual_reference_counts":dict(Counter(str(r["relevance"]) for r in labels.values())),"splits":groups,
            "test_ndcg_gain_vs_rule_bootstrap95":[percentile(gains,.025),percentile(gains,.975)],
            "qualification_agreement_with_jev":sum(n for (a,b),n in confusion.items() if a==b)/len(labels),
            "qualification_confusion":[{"jev":a,"autohunt":b,"count":n} for (a,b),n in sorted(confusion.items())],
            "limits":["Reference is Jev silver, not human ground truth","Includes all review-flagged labels; not high-confidence cherry picking",
                      "Fixed 20-job pools; Recall is candidate-pool Recall@5","Small heldout candidate set with shared scenario patterns",
                      "No browser, planning or application task success measured in this run"]}
    write(output/"evaluation-summary.json",result)
    return result


if __name__=="__main__":
    parser=argparse.ArgumentParser(); parser.add_argument("command",choices=["predict","score"])
    parser.add_argument("--output",required=True,type=Path); parser.add_argument("--annotations",type=Path)
    parser.add_argument("--predictions",type=Path); parser.add_argument("--models-consented",action="store_true")
    parser.add_argument("--call-limit",type=int,default=300); args=parser.parse_args()
    result=(asyncio.run(predict(args.output,consented=args.models_consented,limit=args.call_limit)) if args.command=="predict"
            else score(args.annotations,args.predictions,args.output))
    print(json.dumps(result,ensure_ascii=False,indent=2))
