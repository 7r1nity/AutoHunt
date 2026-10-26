"""Minimal, redacted and sourced candidate facts; never export a full resume."""
from __future__ import annotations

from ..core.security import redact_text
from ..memory.resume_workspace import resume_document_digest
import json


def match_facts(workspace, candidate=None):
    if workspace is None:
        return {}
    try:
        document=workspace.read_document()
        documents={}
        # Local text is used only to validate stored quotes, never exported.
        original="\n".join([document.raw_content,document.summary,*[entry.summary for entry in document.entries]])
        profile=candidate.read().profile if candidate else None
        facts=[]
        for fact in getattr(profile,"qualification_facts",[]):
            quote=fact.get("quote","")
            if fact.get("field") in {"education","years_experience"} and isinstance(quote,str) and quote and quote in original:
                documents[fact["field"]]=redact_text(quote)[:200]
                facts.append({"field":fact["field"],"source":fact["field"]})
        # Confirmed main-resume project entries only, with a small fixed budget.
        for entry in [item for item in document.entries if item.kind in {"project","course","code"}][:4]:
            text="\n".join([entry.title,entry.summary,*entry.highlights[:2]])
            documents["project:"+entry.id]=redact_text(text)[:700]
        return {"document_id":document.id,"version":document.version,"digest":resume_document_digest(document),
                "facts":facts,"documents":documents,"authority":"candidate_evidence_not_execution_permission"}
    except (RuntimeError,ValueError,AttributeError):
        return {}


def verify_requirements(analysis, request, *, assessed):
    documents=request.candidate_facts.get("documents",{})
    checked=[]
    for item in analysis.requirements:
        status=item.get("status","unknown")
        if status not in {"pass","fail","unknown"}:
            status="unknown"
        job_quote=item.get("job_quote","")
        evidence=item.get("candidate_evidence",[])
        valid_job=isinstance(job_quote,str) and len(job_quote.strip())>=2 and job_quote in request.description
        valid_candidate=bool(evidence) and all(isinstance(e,dict) and isinstance(e.get("quote"),str)
            and len(e["quote"].strip())>=2 and e.get("field") in documents and e["quote"] in documents[e["field"]] for e in evidence)
        if not valid_job or status!="unknown" and not valid_candidate:
            status="unknown"
        checked.append({"name":str(item.get("name","") or "未说明的岗位硬条件")[:300],"status":status,
                        "job_quote":job_quote if valid_job else "","candidate_evidence":evidence if valid_candidate else [],
                        "reason":str(item.get("reason","") or "")[:500]})
    status="fail" if any(r["status"]=="fail" for r in checked) else "unknown" if not assessed or any(r["status"]=="unknown" for r in checked) else "pass"
    qualification={"status":status,"assessed":assessed,"requirements":checked,
                   "resume_digest":request.candidate_facts.get("digest"),"document_id":request.candidate_facts.get("document_id")}
    updates={"requirements":checked,"qualification":qualification}
    if status!="pass" and analysis.decision=="APPLY":
        updates.update(decision="REVIEW",reasoning="岗位资历条件"+("不满足" if status=="fail" else "待核实")+"；"+analysis.reasoning)
    return analysis.model_copy(update=updates)


def qualification_status(job,digest):
    value=job.get("qualification_evidence")
    if value is None or value=="null":
        return "legacy"  # Do not silently revoke previously authorized queues.
    try:
        data=json.loads(value) if isinstance(value,str) else value
        if not isinstance(data,dict) or data.get("resume_digest")!=digest:
            return "unknown"
        return data["status"] if data.get("status") in {"pass","fail","unknown"} else "unknown"
    except (ValueError,TypeError):
        return "unknown"
