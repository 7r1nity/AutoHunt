"""Paired fixed-pool ranking; production matcher receives its actual narrow schema."""
from __future__ import annotations

import asyncio
import json
import re
import time

from app.browser.screening import match_job
from app.core.models import JobAnalysisRequest
from app.jobs.analysis import analyze_job
from .common import CASE, aggregate, append, ranking_metrics, write


def tokens(text):
    words=set(re.findall(r"[a-z0-9_+.]+",text.casefold()))
    for han in re.findall(r"[\u4e00-\u9fff]+",text): words.update(han[i:i+2] for i in range(max(1,len(han)-1)))
    return words


async def run(data, output, model=None):
    resumes={item["id"]:item for item in data["resumes"]}; jobs={item["id"]:item for item in data["jobs"]}
    labels={(p["candidate_id"],p["job_id"]):p for p in data["pairs"]}
    rows=[]; semaphore=asyncio.Semaphore(3)
    async def score(candidate,jd):
        async with semaphore:
            token=CASE.set("ranking:"+candidate["id"]+":"+jd["id"]); started=time.perf_counter()
            request=JobAnalysisRequest(title=jd["title"],company=jd["company"],description=jd["description"],
                candidate_skills=candidate["skills"],target_roles=[candidate["role"]],search_queries=[candidate["role"]])
            raw=[]
            async def traced(request):
                result=await model.generate(request); raw.append(result); return result
            try:
                result=await match_job(traced,request)
                row={"candidate_id":candidate["id"],"job_id":jd["id"],"analysis":result.model_dump(),
                    "model_response_observed":bool(raw),"latency_ms":round((time.perf_counter()-started)*1000,2)}
                append(output/"ranking-predictions.jsonl",row)
                return row
            finally: CASE.reset(token)
    for query in data["queries"]:
        candidate=resumes[query["candidate_id"]]; pool=[jobs[ident] for ident in query["pool"]]
        references=[labels[(candidate["id"],j["id"])] for j in pool]
        relevant=[p["relevance"] for p in references]; eligible=[p["eligibility"] for p in references]
        rule_scores=[analyze_job(JobAnalysisRequest(title=j["title"],company=j["company"],description=j["description"],
            candidate_skills=candidate["skills"],target_roles=[candidate["role"]],search_queries=[candidate["role"]])).score for j in pool]
        candidate_terms=tokens(candidate["text"])
        keyword_scores=[len(candidate_terms & tokens(j["title"]+j["description"])) for j in pool]
        systems={"keyword_overlap":keyword_scores,"production_rule":rule_scores}
        if model is not None and query["split"] == "challenge":
            predictions=await asyncio.gather(*(score(candidate,jd) for jd in pool))
            systems["production_llm_with_fallback"]=[row["analysis"]["score"] for row in predictions]
        for name,scores in systems.items():
            row={"query_id":query["id"],"split":query["split"],"system":name,"scores":scores,
                "pool":query["pool"],"labels":relevant,"eligibility":eligible,
                **ranking_metrics(relevant,scores,eligible)}
            rows.append(row); append(output/"ranking-queries.jsonl",row)
        print(json.dumps({"phase":"ranking","query":query["id"],"split":query["split"]}),flush=True)
    groups={}
    for split in ("development","challenge"):
        groups[split]={name:{"queries":len(selected),"metrics":aggregate(selected),
            "ndcg_defined_queries":sum(row["ndcg_at_5"] is not None for row in selected)}
            for name in {r["system"] for r in rows if r["split"]==split}
            if (selected:=[r for r in rows if r["split"]==split and r["system"]==name])}
    summary={"label_status":"synthetic_oracle_pending_human_review","scope":"fixed_pool_reranking_not_full_corpus_recall",
        "candidate_information_gap":"Production matcher receives skills and target roles, NOT structured years/education/full resume. Same production input used for rules and LLM.",
        "tie_policy":"stable corpus order, shared across systems; report ties rather than claim unique rank",
        "splits":groups}
    write(output/"ranking-summary.json",summary)
    return summary
