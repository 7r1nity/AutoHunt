"""Budgeted, resumable Jev labeling. No Agent tools, user data or recruitment IO."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import contextmanager
from datetime import UTC,datetime
import json
import os
from pathlib import Path
import time

import httpx
from dotenv import dotenv_values

from evaluation.benchmark.common import PhysicalBudget,CASE,append,write
from .corpus import ROOT,load,digest
from .protocol import RUBRIC_VERSION,request_for,annotation

ENDPOINT="https://api.typesafe.ai/v1/systemone"
PROXY_ENDPOINT="https://jevmodel.org/v1/systemone"
CEILING=330


def configuration():
    values=dotenv_values(ROOT/".env")
    get=lambda key,default="":os.environ.get(key,values.get(key) or default)
    key=get("JEV_API_KEY") or get("TYPESAFE_API_KEY")
    model=get("JEV_MODEL","jev-1.14.0")
    if model not in {"jev-1.14.0","jev-1.13.0"}: raise ValueError("Pin a documented versioned Jev model, not a moving alias")
    timeout=float(get("JEV_TIMEOUT_SECONDS","45"))
    if not 1<=timeout<=120: raise ValueError("Jev timeout must be 1..120 seconds")
    endpoint=get("JEV_ENDPOINT",ENDPOINT)
    if endpoint not in {ENDPOINT,PROXY_ENDPOINT}: raise ValueError("Endpoint is not one of the explicitly supported destinations")
    return {"api_key":key,"model":model,"timeout":timeout,"endpoint":endpoint}


def label_request(data,pair,config):
    return request_for(data,pair,config["model"],compact=config.get("endpoint",ENDPOINT)==PROXY_ENDPOINT)


def manifest_for(data,config,limit):
    endpoint=config.get("endpoint",ENDPOINT)
    if endpoint not in {ENDPOINT,PROXY_ENDPOINT}: raise ValueError("Unsupported endpoint")
    return {"dataset_sha256":digest(data),"rubric_version":RUBRIC_VERSION,"endpoint":endpoint,
            "model":config["model"],"physical_limit":limit,
            "question_digest":digest(label_request(data,data["pairs"][0],config)["questions"]),
            "human_gold":False,"data_scope":"frozen AI-authored synthetic corpus only"}


def migrate_endpoint(output,config,limit):
    """Explicit operator migration before any labels; never reset the ledger."""
    path=output/"manifest.json"
    if not path.exists() or not (output/"budget.json").exists(): raise ValueError("Existing manifest and budget are required")
    old=json.loads(path.read_text(encoding="utf-8")); data=load(); new=manifest_for(data,config,limit)
    if old==new: return
    if {k:v for k,v in old.items() if k not in {"endpoint","question_digest"}}!={k:v for k,v in new.items() if k not in {"endpoint","question_digest"}}:
        raise ValueError("Endpoint migration cannot change model, data, rubric or request limit")
    budget=PhysicalBudget(output/"budget.json",limit,ceiling=CEILING)
    if any(c.get("response") and c.get("http_status")==200 for c in saved_calls(output)):
        raise ValueError("Cannot mix endpoints after annotation responses; preserve the reference run")
    append(output/"manifest-revisions.jsonl",{"at":datetime.now(UTC).isoformat(),"reason":"explicit_user_endpoint_migration",
        "physical_used_preserved":budget.state()["used"],"old":old,"new":new})
    write(path,new)


def result_path(output):
    output=Path(output).resolve()
    if (ROOT/"evaluation/results").resolve() not in output.parents:
        raise ValueError("Jev raw outputs must stay in ignored evaluation/results")
    return output


@contextmanager
def lease(output):
    """One process owns the run; OS unlocks automatically on crashes."""
    output.mkdir(parents=True,exist_ok=True)
    with (output/"run.lock").open("a+b") as f:
        f.seek(0); f.write(b"0"); f.flush(); f.seek(0)
        if os.name=="nt":
            import msvcrt
            try: msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
            except OSError as exc: raise RuntimeError("This annotation run is already active") from exc
        else:
            import fcntl
            fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        try: yield
        finally:
            f.seek(0)
            if os.name=="nt": msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
            else: fcntl.flock(f.fileno(),fcntl.LOCK_UN)


def prepare(output,config,limit=330):
    data=load(); output=result_path(output); output.mkdir(parents=True,exist_ok=True)
    if not 1<=limit<=CEILING: raise ValueError("Call limit must be within separately approved ceiling 330")
    manifest=manifest_for(data,config,limit)
    path=output/"manifest.json"
    if path.exists() and json.loads(path.read_text(encoding="utf-8"))!=manifest:
        raise ValueError("Cannot change dataset, model, rubric or request budget inside an existing run")
    write(path,manifest)
    budget=PhysicalBudget(output/"budget.json",limit,ceiling=CEILING)
    # Blinded development-only calibration queue: eight varied cases per person.
    task_path=output/"human-calibration.jsonl"
    if not task_path.exists():
        intended={p["pair_id"]:p for p in data["design_intents"]}
        chosen=[]
        for candidate in data["candidates"]:
            pool=[p for p in data["pairs"] if p["candidate_id"]==candidate["id"] and p["split"]=="development"]
            for kind,n in (("close",2),("transfer",1),("depth",1),("ownership",1),("tenure",1),("missing",1),("untrusted",1)):
                for pair in sorted([p for p in pool if intended[p["id"]]["case_family"]==kind],key=lambda x:x["id"])[:n]:
                    request=label_request(data,pair,config)
                    chosen.append({"pair_id":pair["id"],"split":"development",**request["state"],
                        "reviewer_a":{"relevance":None,"eligibility":None,"evidence":[],"reviewer":None},
                        "reviewer_b":{"relevance":None,"eligibility":None,"evidence":[],"reviewer":None},
                        "adjudicated":{"relevance":None,"eligibility":None,"reason":None}})
        task_path.write_text("".join(json.dumps(r,ensure_ascii=False)+"\n" for r in chosen),encoding="utf-8")
    return data,budget


def saved_calls(output):
    p=output/"model-calls.jsonl"
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def restore(data,output,config,threshold):
    """Rebuild from durable response journal after a crash; never pay twice."""
    previous={}
    for call in saved_calls(output):
        if call.get("response"): previous[call["pair_id"]]=call
    annotations={}; pairs={p["id"]:p for p in data["pairs"]}
    for ident,call in previous.items():
        if ident not in pairs: raise ValueError("Saved response is outside frozen corpus")
        request=label_request(data,pairs[ident],config)
        if call["input_digest"]!=digest(request): raise ValueError("Saved response belongs to a different input")
        if call.get("endpoint")!=config.get("endpoint",ENDPOINT): raise ValueError("Saved response belongs to a different endpoint")
        if not isinstance(call["response"],dict) or call["response"].get("model")!=config["model"]: continue
        try: annotations[ident]=annotation(call["response"],request,pairs[ident],threshold=threshold)
        except ValueError: pass
    return annotations


def save_summary(data,output,budget,annotations,*,status,errors=None,review_threshold=.7):
    pairs={p["id"]:p for p in data["pairs"]}
    pools=[]
    for q in data["queries"]:
        labels=[annotations[p["id"]]["relevance"] for p in data["pairs"] if p["candidate_id"]==q["candidate_id"] and p["id"] in annotations]
        pools.append({"query_id":q["id"],"split":q["split"],"annotated":len(labels),"pool_size":len(q["pool"]),
                      "actual_grades":dict(Counter(map(str,labels))),"all_three_grades_observed":set(labels)=={0,1,2}})
    rows=[{**row,"candidate_id":pairs[key]["candidate_id"],"job_id":pairs[key]["job_id"],"split":pairs[key]["split"]}
          for key,row in sorted(annotations.items())]
    write(output/"annotations.json",{"dataset_sha256":digest(data),"human_gold":False,
          "label_source":"jev_model_annotation" if rows else "unannotated_waiting_jev", "items":rows})
    calls=saved_calls(output); usage=Counter()
    for c in calls:
        raw=c.get("response")
        for k,v in ((raw.get("usage") or {}) if isinstance(raw,dict) else {}).items():
            if isinstance(v,int) and not isinstance(v,bool): usage[k]+=v
    result={"status":status,"dataset_sha256":digest(data),"expected_pairs":len(pairs),"annotated_pairs":len(rows),
            "pending_pairs":len(pairs)-len(rows),"physical_requests":budget.state()["used"],"physical_limit":budget.limit,
            "actual_label_counts":dict(Counter(str(r["relevance"]) for r in rows)),"human_review_pending":len(rows),
            "flagged_pairs":sum(bool(r["review_flags"]) for r in rows),"actual_pools":pools,"usage":dict(usage),
            "errors":errors or [],"human_gold":False,"label_status":"model-labelled silver, not human gold" if rows else "unannotated_waiting_jev",
            "confidence_threshold": "provisional review routing, not a calibrated correctness guarantee"}
    result["review_threshold_value"]=review_threshold
    write(output/"summary.json",result)
    return result


async def annotate(output,config,*,consented=False,limit=330,threshold=.7,transport=None,sleep=asyncio.sleep,endpoint_migration=False):
    output=result_path(output)
    if not 0<=threshold<=1: raise ValueError("Review threshold must be 0..1")
    with lease(output):
        if endpoint_migration:
            if not consented: raise ValueError("Endpoint migration requires explicit destination consent")
            migrate_endpoint(output,config,limit)
        data,budget=prepare(output,config,limit)
        labels=restore(data,output,config,threshold)
        if not consented: return save_summary(data,output,budget,labels,status="awaiting_model_consent",review_threshold=threshold)
        if not config["api_key"]: return save_summary(data,output,budget,labels,status="awaiting_api_key",review_threshold=threshold)
        errors=[]; fatal=None; consecutive_failures=0
        async with httpx.AsyncClient(transport=transport or httpx.AsyncHTTPTransport(retries=0),
                timeout=config["timeout"],follow_redirects=False,trust_env=False) as client:
            for pair in data["pairs"]:
                if pair["id"] in labels: continue
                request=label_request(data,pair,config)
                token=CASE.set("jev_annotation:"+pair["id"])
                try:
                    for attempt in range(3):
                        if budget.state()["used"]>=limit: break
                        number=budget.reserve(); start=time.perf_counter(); retry=False
                        entry={"call":number,"pair_id":pair["id"],"attempt":attempt+1,"input_digest":digest(request),
                               "request":request,"endpoint":config.get("endpoint",ENDPOINT),"at":datetime.now(UTC).isoformat()}
                        try:
                            headers={"Authorization":"Bearer "+config["api_key"]}
                            if config.get("endpoint")==PROXY_ENDPOINT:
                                headers["Idempotency-Key"]="autohunt-"+digest({"request":request,"pair":pair["id"]})
                            response=await client.post(config.get("endpoint",ENDPOINT),json=request,headers=headers)
                            entry["http_status"]=response.status_code
                            if response.status_code in {401,403}:
                                fatal="authentication"; entry["error_type"]="AuthenticationOrPermissionError"
                            elif response.status_code==402:
                                fatal="insufficient_credits"; entry["error_type"]="InsufficientCredits"
                            elif response.status_code==429 or response.status_code==529 or 500<=response.status_code<600:
                                retry=True; entry["error_type"]="TransientHTTPError"
                            elif response.status_code!=200:
                                entry["error_type"]="NonRetryableHTTPError"
                                if response.status_code in {400,404,405,413,415,422}: fatal="request_configuration"
                            else:
                                raw=response.json()
                                entry["response"]=raw
                                if not isinstance(raw,dict): raise ValueError("Response must be an object")
                                if raw.get("model")!=config["model"]:
                                    fatal="model_binding"; entry["error_type"]="ResponseModelChanged"
                                else:
                                    # Persist raw typed result before deriving annotation.
                                    labels[pair["id"]]=annotation(raw,request,pair,threshold=threshold)
                        except (httpx.TimeoutException,httpx.TransportError) as exc:
                            entry["error_type"]=type(exc).__name__; retry=True
                        except (ValueError,TypeError,AttributeError) as exc:
                            entry["error_type"]="InvalidModelResponse"
                        except BaseException as exc:
                            entry["error_type"]=type(exc).__name__; raise
                        finally:
                            entry["latency_ms"]=round((time.perf_counter()-start)*1000,2)
                            # Never write headers, key or exception text to disk.
                            encoded=json.dumps(entry,ensure_ascii=False,default=str).replace(config["api_key"],"[API_KEY]")
                            append(output/"model-calls.jsonl",json.loads(encoded))
                        if entry.get("error_type"): errors.append({"pair_id":pair["id"],"call":number,"type":entry["error_type"]})
                        if fatal or not retry or attempt==2: break
                        await sleep(min(2**attempt,5))
                finally: CASE.reset(token)
                consecutive_failures=0 if pair["id"] in labels else consecutive_failures+1
                if consecutive_failures>=3: fatal=fatal or "consecutive_failures"
                if fatal or budget.state()["used"]>=limit: break
        status="completed" if len(labels)==len(data["pairs"]) else "stopped_"+fatal if fatal else "budget_exhausted" if budget.state()["used"]>=limit else "partial"
        return save_summary(data,output,budget,labels,status=status,errors=errors,review_threshold=threshold)


def main():
    p=argparse.ArgumentParser(); p.add_argument("command",choices=["prepare","annotate"])
    p.add_argument("--output",type=Path,required=True); p.add_argument("--models-consented",action="store_true")
    p.add_argument("--call-limit",type=int,default=330); p.add_argument("--review-threshold",type=float,default=.7)
    p.add_argument("--migrate-endpoint",action="store_true")
    args=p.parse_args(); config=configuration(); output=result_path(args.output)
    if args.command=="prepare":
        with lease(output):
            data,budget=prepare(output,config,args.call_limit)
            result=save_summary(data,output,budget,restore(data,output,config,args.review_threshold),status="prepared_awaiting_jev",review_threshold=args.review_threshold)
    else:
        result=asyncio.run(annotate(output,config,consented=args.models_consented,limit=args.call_limit,threshold=args.review_threshold,endpoint_migration=args.migrate_endpoint))
    print(json.dumps({k:v for k,v in result.items() if k!="actual_pools"},ensure_ascii=False,indent=2))
    return 0 if result["status"] in {"completed","prepared_awaiting_jev"} else 2


if __name__=="__main__": raise SystemExit(main())
