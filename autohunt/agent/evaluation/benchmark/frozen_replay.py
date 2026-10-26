"""Replay saved model outputs against repaired production code: zero API calls.

Scope-review judgments are explicit scripted oracles. This measures contract and
execution compatibility, NOT the new model's semantic accuracy.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
from collections import defaultdict
from pathlib import Path

from app.llm.base import LLMResult
from .common import CASE, ROOT, write
from .corpus import PATH
from .planning import run as planning_run
from .execution import run as execution_run


def decode(text):
    if text.lstrip().startswith("```"):
        text=text.split("\n",1)[1].rsplit("```",1)[0]
    return json.loads(text)


class ReplayModel:
    model="frozen-output-no-external-model"

    def __init__(self,source):
        calls=[json.loads(s) for s in (source/"model-calls.jsonl").read_text(encoding="utf-8").splitlines()]
        self.planning=defaultdict(list); self.execution={}; self.requests=[]
        for call in calls:
            if call["case"].startswith("planning:"):
                self.planning[call["case"]].append(decode(call["response"]["choices"][0]["message"]["content"]))
            elif call["case"].startswith("execution:"):
                # Last observation is the official valid attempt, not the old
                # mismatched-snapshot harness attempt.
                self.execution[call["case"]]=decode(call["response"]["choices"][0]["message"]["content"])

    async def generate(self,request):
        payload=json.loads(request.user_message); case=CASE.get()
        self.requests.append({"case":case,"kind":"scripted_scope" if request.context=="条件语义核对，不产生执行授权" else "frozen_planner"})
        if request.context=="条件语义核对，不产生执行授权":
            scopes=[]
            for item in payload["scopes"]:
                changes={}; status="pass"
                # Explicit oracle for the three diagnosed unrequested exceptions.
                if case in {"planning:count_inheritance","planning:city_replace","planning:append_growth"} and item["tool"]=="job_search_start":
                    status="correct"; changes={"filters":{"op":"condition","text":"月薪至少15000元","salary":{"unit":"monthly","minimum":15000}}}
                scopes.append({"step_id":item["step_id"],"status":status,"changes":changes,"reason":"冻结场景规格的脚本判定，不代表真实模型效果"})
            result={"scopes":scopes}
        elif case.startswith("planning:"):
            if not self.planning[case]:
                raise RuntimeError("Frozen responses exhausted; never call a real model")
            result=copy.deepcopy(self.planning[case].pop(0))
            if result.get("revision") and payload.get("active_plan"):
                result["revision"].update(plan_id=payload["active_plan"]["id"],expected_revision=payload["active_plan"]["revision"])
        else:
            result=copy.deepcopy(self.execution[case])
        snapshots=(payload.get("dialogue") or {}).get("snapshots",[])
        snapshot=snapshots[-1]["id"] if snapshots else None
        def align(value):
            if isinstance(value,dict):
                for key,child in value.items():
                    if key=="snapshot_id" and snapshot: value[key]=snapshot
                    else: align(child)
            elif isinstance(value,list):
                for child in value: align(child)
        align(result)
        return LLMResult(json.dumps(result,ensure_ascii=False),"frozen","scripted-replay")


async def main(args):
    output=args.output.resolve()
    if (ROOT/"evaluation/results").resolve() not in output.parents or output.exists():
        raise ValueError("Use a new isolated evaluation/results folder; preserve earlier failures")
    output.mkdir(parents=True)
    model=ReplayModel(args.source)
    data=json.loads(PATH.read_text(encoding="utf-8"))
    if args.phase=="planning":
        summary=await planning_run(data,output,model)
    else:
        summary=await execution_run(data,output,model,repetitions=args.repetitions,cases=args.cases)
    summary["external_model_calls"]=0
    summary["meaning"]="Saved response compatibility with scripted scope review; not a fresh LLM evaluation"
    write(output/"replay-summary.json",summary)
    write(output/"scripted-requests.json",model.requests)
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    if args.phase=="planning": return int(bool(summary.get("fail") or summary.get("error")))
    return int(any(row["task_success"]<1 for row in summary["systems"].values()))


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--phase",choices=["planning","execution"],required=True)
    parser.add_argument("--repetitions",type=int,default=2)
    parser.add_argument("--cases",nargs="*")
    raise SystemExit(asyncio.run(main(parser.parse_args())))
