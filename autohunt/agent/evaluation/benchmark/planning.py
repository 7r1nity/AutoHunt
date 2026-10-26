"""Real model -> production planner -> revision -> executor, synthetic tool effects."""
from __future__ import annotations

import copy
import gc
import json
import tempfile
import time
import traceback
from unittest.mock import patch

from app.runtime.dialogue import apply_operation
from app.workflow.planning import plan_request
from app.workflow.revision import RevisionDraft, edited_plan
from app.workflow.execution import advance
from evaluation.stateful_scenarios import Harness, STAMP
from .common import CASE, append, write


def salary_accepts(arguments, amount=None, *, negotiable=False,salary=None):
    from app.core.job_filters import validate_filter, combine
    from app.core.salary_conditions import normalized_search_filters, judge_salary
    tree=normalized_search_filters(arguments.get("filters"),arguments.get("salary_min"))
    if not tree: return True
    expression=validate_filter(tree)
    facts={"salary":salary if salary is not None else "面议" if negotiable else str(amount)+"元/月","employment_kind":"regular"}
    values=[]
    for leaf in expression.leaves():
        if leaf.salary: values.append(judge_salary(leaf.salary,facts)["status"])
        else: values.append("fail" if "实习" in leaf.text and "非实习" not in leaf.text else "pass")
    return combine(expression,iter(values))=="pass"


def salary_scope_exact(arguments,floor):
    return (salary_accepts(arguments,floor) and not salary_accepts(arguments,floor-1)
            and not salary_accepts(arguments,negotiable=True)
            and not salary_accepts(arguments,salary=f"{max(0,floor-1000)}-{floor+1000}元/月"))


def inherited_search_scope(plan, consumer, old=None):
    """Search provenance can enforce scope without copying its filter to delivery."""
    from app.workflow.validation import dependency_ancestors
    args=consumer.get("arguments",{})
    if args.get("selector") or args.get("target_step_id"):
        return False  # Explicit targets are a different contract.
    source=next((s for s in plan.get("steps",[]) if s["id"]==consumer.get("evidence_step")),None)
    if not source or source["tool"]!="job_search_start" or source["id"] not in dependency_ancestors(consumer["id"],plan["steps"]):
        return False
    current=source.get("staged_arguments") or source["arguments"]
    if not current.get("filters"):
        return False
    if args.get("source_task_id") and args["source_task_id"]!=source.get("task_id"):
        return False
    if old:
        previous=next((s for s in old.get("steps",[]) if s["id"]==source["id"]),None)
        previous=(previous.get("staged_arguments") or previous["arguments"]) if previous else {}
    else:
        previous=plan.get("verified_conditions",{})
    return all(current.get(key)==previous.get(key) for key in ("filters","locations","queries"))


def check_plan(plan,expected,conditions,old):
    pending=[s for s in plan.get("steps",[]) if not s.get("withdrawn") and s.get("status") not in {"skipped","blocked"}]
    tools={s["tool"]:s for s in pending}; checks={}
    if expected.get("clarify"):
        return {"asks_before_send":bool(plan.get("questions") or any(s.get("questions") or s.get("missing_slots") or s.get("user_requested") is False and s["tool"]=="mock_delivery_start" for s in pending))}
    if expected.get("read_only"): checks["read_only"]=all(s.get("effect")=="read" for s in pending if s.get("status") in {"pending","waiting_approval"})
    for name in expected.get("tools",[]): checks["has_"+name]=name in tools
    if "tool" in expected: checks["target_tool"]=expected["tool"] in tools
    args=tools.get(expected.get("tool","job_search_start"),{}).get("staged_arguments") or tools.get(expected.get("tool","job_search_start"),{}).get("arguments",{})
    search=tools.get("job_search_start",{}); search_args=search.get("staged_arguments",search.get("arguments",{}))
    for key in ("result_limit","min_score","max_score"):
        if key in expected: checks[key]=args.get(key)==expected[key]
    if "salary_min" in expected:
        checks["salary_min"]=args.get("salary_min")==expected["salary_min"] if expected.get("tool")=="learning_plan_generate" else salary_scope_exact(args,expected["salary_min"])
    if "locations" in expected: checks["locations"]=search_args.get("locations",conditions.get("locations"))==expected["locations"]
    if "groups" in expected: checks["groups"]=[g["count"] for g in search_args.get("result_groups",[])]==expected["groups"]
    if expected.get("no_groups"): checks["no_groups"]=not search_args.get("result_groups")
    if expected.get("filters"): checks["filter_retained"]=bool(args.get("filters"))
    if expected.get("selector"): checks["ordinal_not_database_id"]=expected["selector"] in str(args.get("selector", "")) or "八" in str(args.get("selector",""))
    if expected.get("pause"): checks["paused"]=plan.get("paused") is True
    if expected.get("no_pending_delivery"): checks["delivery_withdrawn"]=not any(s["tool"]=="mock_delivery_start" and not s.get("withdrawn") and s["status"] not in {"skipped","blocked"} for s in plan["steps"])
    if expected.get("preserve_search"):
        checks["search_not_replayed"]=len([s for s in pending if s["tool"]=="job_search_start"])==1
        checks["search_parameters_retained"]=search_args.get("result_limit")==15 and salary_scope_exact(search_args,15000)
        from app.workflow.validation import dependency_ancestors
        consumer=tools.get(expected["tool"],{})
        checks["delivery_or_growth_depends_on_search"]=search.get("id") in dependency_ancestors(consumer.get("id",""),pending)
    if expected.get("inherited_scope"):
        checks["scope_retained"]=bool(args.get("filters")) or inherited_search_scope(plan,tools.get(expected["tool"],{}),old)
    if expected.get("not_stop"): checks["no_stop"]=not any("stop" in name or "cancel" in name for name in tools)
    if expected.get("no_search"): checks["no_new_search"]=not any(s["tool"]=="job_search_start" and s["status"]=="pending" for s in pending)
    if expected.get("no_search") and expected.get("tool")=="search_state_update" and search.get("staged_arguments"):
        checks["target_tool"]=plan.get("paused") is True and any("金融" in term for term in search["staged_arguments"].get("queries",[]))
    if expected.get("salary_tree"):
        tree=json.dumps(search_args.get("filters") or args.get("filters") or {},ensure_ascii=False)
        checks["or_branches"]=all(term in tree for term in ('"any"', '"daily"', '"monthly"', '"negotiable"', '300','15000'))
    return checks or {"nonempty_plan":bool(pending)}


async def run(data,output,model):
    rows=[]
    for case in data["planning"]:
        token=CASE.set("planning:"+case["id"]); started=time.perf_counter()
        row={"id":case["id"],"turns":case["turns"],"expected":case["expected"],"status":"error","plans":[]}
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
                h=Harness(directory); conditions={"queries":["Python后端工程师"],"source":"mock"}
                with patch("app.workflow.execution.resume_stamp",return_value=STAMP):
                    for index,message in enumerate(case["turns"]):
                        with h.context(message) as ctx:
                            ctx.search_state=copy.deepcopy(conditions)
                            h.store.mutate_dialogue("default",{"op":"begin","message":message})
                            ctx.dialogue=h.store.dialogue_state("default")
                            if index:
                                ctx.dialogue=h.store.mutate_dialogue("default",{"op":"snapshot","topic":"applications","kind":"job","source":"synthetic-result",
                                    "items":[{"id":1000+i,"title":f"虚构Python后端工程师{i}","company":f"虚构企业{i}","score":88} for i in range(1,16)]})
                            candidate=await plan_request(model,h.registry,message,dialogue=ctx.dialogue,conditions=conditions,stamp=STAMP)
                            row["plans"].append(copy.deepcopy(candidate))
                            old=copy.deepcopy(h.plan)
                            if candidate.get("operation")=="revise":
                                h.plan=edited_plan(h.plan,RevisionDraft.model_validate(candidate["revision"]),h.registry,stamp=STAMP,session_id="default",message=message)
                            else: h.plan=candidate
                            h.store.save_plan(h.run["id"],h.plan)
                            if index == 0:
                                for step in h.plan["steps"]:
                                    if step["tool"]=="job_search_start": conditions.update({k:v for k,v in step["arguments"].items() if k in {"queries","locations","result_limit","salary_min","filters","result_groups","source"}})
                                # Production runner establishes waiting_external; adapters have synthetic effects.
                                await advance(h.plan,h.registry)
                                h.store.save_plan(h.run["id"],h.plan)
                            else:
                                row["checks"]=check_plan(h.plan,case["expected"],conditions,old)
                                await advance(h.plan,h.registry)
                    row["status"]="pass" if all(row["checks"].values()) else "fail"
                    row["trace"]=h.trace()
                    await h.close(); h=None; gc.collect()
        except Exception as exc:
            row.update(error_type=type(exc).__name__,error=str(exc),traceback=traceback.format_exc())
        finally: CASE.reset(token)
        row["latency_ms"]=round((time.perf_counter()-started)*1000,2)
        append(output/"planning-cases.jsonl",row); rows.append(row)
        print(json.dumps({"phase":"planning","id":case["id"],"status":row["status"]}),flush=True)
    summary={"cases":len(rows),"pass":sum(r["status"]=="pass" for r in rows),"fail":sum(r["status"]=="fail" for r in rows),
        "error":sum(r["status"]=="error" for r in rows),"scope":"production model/planner/revision/runner with synthetic tool effects; not browser E2E"}
    write(output/"planning-summary.json",summary)
    return summary
