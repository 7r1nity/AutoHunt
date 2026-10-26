"""Consent-budgeted model access and append-only synthetic evaluation records."""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import math
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from types import MethodType

CASE = contextvars.ContextVar("benchmark_case", default="unscoped")
ROOT = Path(__file__).parents[2]


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str),encoding="utf-8")


def append(path, value):
    with path.open("a",encoding="utf-8") as stream:
        stream.write(json.dumps(value,ensure_ascii=False,default=str)+"\n"); stream.flush()


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class BudgetExceeded(PermissionError):
    pass


class PhysicalBudget:
    def __init__(self, path, limit, *, ceiling=300):
        if not 1 <= limit <= ceiling <= 10000: raise ValueError("Physical request limit exceeds this runner's consent ceiling")
        self.path, self.limit = path, limit
        if not path.exists(): write(path,{"limit":limit,"used":0,"reservations":[]})
        if self.state()["limit"] != limit: raise ValueError("Cannot silently change persisted budget")

    def state(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def reserve(self):
        # A sidecar OS lease serializes different evaluation processes too.
        lockpath = self.path.with_suffix(".lock")
        with lockpath.open("a+b") as stream:
            stream.seek(0); stream.write(b"0"); stream.flush(); stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_LOCK,1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_EX)
            try:
                state=self.state()
                if state["used"] >= self.limit: raise BudgetExceeded("Physical request budget exhausted")
                state["used"] += 1
                state["reservations"].append({"call":state["used"],"case":CASE.get(),"at":datetime.now(UTC).isoformat()})
                write(self.path,state)
                return state["used"]
            finally:
                stream.seek(0)
                if os.name == "nt": msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
                else: fcntl.flock(stream.fileno(),fcntl.LOCK_UN)


def provider(output, limit=300):
    from evaluation.run import consented_provider
    model = consented_provider(limit,[])
    budget = PhysicalBudget(output/"budget.json",limit)
    async def completion(self, **kwargs):
        number=budget.reserve(); start=time.perf_counter()
        entry={"call":number,"case":CASE.get(),"request":kwargs,"endpoint":"https://api.deepseek.com"}
        try:
            async with asyncio.timeout(65): result=await self.client.chat.completions.create(**kwargs)
            entry["response"]=result.model_dump(mode="json")
            return result
        except BaseException as exc:
            entry["error_type"]=type(exc).__name__
            raise
        finally:
            entry["latency_ms"]=round((time.perf_counter()-start)*1000,2)
            append(output/"model-calls.jsonl",entry)
    model._completion=MethodType(completion,model)
    model.budget=budget
    return model


def ranking_metrics(labels, scores, eligible):
    order=sorted(range(len(scores)),key=lambda i:(-scores[i],i))
    k=min(5,len(order)); top=order[:k]
    dcg=lambda values:sum((2**v-1)/math.log2(i+2) for i,v in enumerate(values))
    ideal=dcg(sorted(labels,reverse=True)[:k])
    relevant=sum(v>=1 for v in labels)
    return {"ndcg_at_5":dcg([labels[i] for i in top])/ideal if ideal else None,
        "precision_at_5":sum(labels[i]>=1 for i in top)/5,
        "high_precision_at_5":sum(labels[i]==2 for i in top)/5,
        "recall_at_5_pool":sum(labels[i]>=1 for i in top)/relevant if relevant else None,
        "hard_fail_at_5":sum(eligible[i]=="fail" for i in top)/5,
        "unknown_at_5":sum(eligible[i]=="unknown" for i in top)/5,
        "top_indices":top,"relevant_in_pool":relevant}


def aggregate(rows):
    keys={key for row in rows for key,value in row.items() if isinstance(value,(int,float)) and not isinstance(value,bool)}
    return {key:sum(row[key] for row in rows if isinstance(row.get(key),(int,float)))/sum(isinstance(row.get(key),(int,float)) for row in rows) for key in sorted(keys)}


def percentile(values, p):
    return sorted(values)[max(0,math.ceil(len(values)*p)-1)] if values else None
