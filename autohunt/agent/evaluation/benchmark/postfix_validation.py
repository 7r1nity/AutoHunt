"""Consent-budgeted post-fix planning and factual matching; no browser effects."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from app.browser.screening import match_job
from app.core.models import JobAnalysisRequest
from .common import ROOT,CASE,provider,write,append
from .corpus import PATH,digest
from .planning import run

CASES={"count_inheritance","city_replace","append_delivery","append_growth","salary_or","quota",
       "pause_and_patch","mixed_references","scope_inherit","formal_only_apply"}


async def main(args):
    output=args.output.resolve()
    if not args.models_consented or (ROOT/"evaluation/results").resolve() not in output.parents or output.exists():
        raise ValueError("Requires explicit model consent and a new isolated results folder")
    output.mkdir(parents=True)
    data=json.loads(PATH.read_text(encoding="utf-8"))
    manifest={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/"app").rglob("*.py")}
    selected=set(args.cases or CASES)
    write(output/"metadata.json",{"dataset_sha256":digest(data),"case_ids":sorted(selected),"physical_limit":args.limit,
        "data":"synthetic requests/state/candidate facts only","scope":"real planning, synthetic effects; not new browser E2E"})
    write(output/"source-manifest.json",manifest)
    model=provider(output,args.limit)
    try:
        plan_summary=await run({**data,"planning":[c for c in data["planning"] if c["id"] in selected]},output,model)
        matching=[]
        for ident,role,description,documents,expected in (
            ("education_fail","Python后端工程师","【虚构JD】硬性要求：硕士学历，至少5年相关工作经验。负责后端服务开发、质量检查与团队协作。",{"education":"最高学历为本科","years_experience":"相关工作经验6年"},"fail"),
            ("facts_pass","金融分析师","【虚构JD】硬性要求：本科学历，至少3年相关工作经验。负责财务建模、风险分析与业务报告。",{"education":"最高学历为硕士","years_experience":"相关工作经验6年"},"pass"),
            ("facts_unknown","外贸业务员","【虚构JD】硬性要求：本科学历，至少3年相关工作经验。负责开发客户、报价与国际销售沟通。",{},"unknown"),
            ("years_fail","机械设计工程师","【虚构JD】硬性要求：本科学历，至少5年相关工作经验。负责机械制图、公差分析与设计验证。",{"education":"最高学历为本科","years_experience":"相关工作经验1年"},"fail")):
            if model.budget.state()["used"]>=args.limit:
                matching.append({"id":ident,"status":"skipped","reason":"physical_budget_exhausted"}); continue
            token=CASE.set("matching:"+ident)
            try:
                request=JobAnalysisRequest(title=role,company="虚构评测公司",description=description,candidate_skills=["Python","Excel"],target_roles=[role],search_queries=[role],
                    candidate_facts={"document_id":"synthetic","digest":"synthetic-digest","documents":documents})
                result=await match_job(model.generate,request)
                actual=(result.qualification or {}).get("status")
                matching.append({"id":ident,"status":"pass" if actual==expected else "fail","expected":expected,"actual":actual,"analysis":result.model_dump()})
            finally: CASE.reset(token)
        write(output/"matching-cases.json",matching)
        # Parameter presence alone is insufficient if a clear task is still
        # waiting for an unavailable/ambiguous scope review.
        pending=[]
        for line in (output/"planning-cases.jsonl").read_text(encoding="utf-8").splitlines():
            case=json.loads(line)
            plan=(case.get("trace") or {}).get("final_run",{}).get("checkpoint",{}).get("plan",{})
            if (plan.get("scope_review") or {}).get("waits"):
                pending.append(case["id"])
        changed=[name for name,sha in manifest.items() if hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=sha]
        summary={"planning":plan_summary,"matching":{"pass":sum(r["status"]=="pass" for r in matching),"fail":sum(r["status"]=="fail" for r in matching),"skipped":sum(r["status"]=="skipped" for r in matching)},
            "physical_requests":model.budget.state()["used"],"source_changed":changed,"unexpected_scope_waits":pending}
        write(output/"validation-summary.json",summary)
        print(json.dumps(summary,ensure_ascii=False,indent=2))
        return int(bool(plan_summary["fail"] or plan_summary["error"] or summary["matching"]["fail"] or changed or pending))
    finally: await model.client.close()


if __name__=="__main__":
    parser=argparse.ArgumentParser(); parser.add_argument("--output",required=True,type=Path)
    parser.add_argument("--models-consented",action="store_true")
    parser.add_argument("--limit",type=int,choices=range(1,41),default=40)
    parser.add_argument("--cases",nargs="*",choices=sorted(CASES))
    raise SystemExit(asyncio.run(main(parser.parse_args())))
