"""Export aggregate synthetic benchmark results, never raw runtime data."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from .common import write
from .corpus import PATH, digest


def export(source,target):
    def read(name): return json.loads((source/name).read_text(encoding="utf-8"))
    data=json.loads(PATH.read_text(encoding="utf-8"))
    meta=read("metadata.json"); ranking=read("ranking-audit.json")
    planning=read("planning-regraded.json"); execution=read("execution-summary.json")
    audit=read("execution-audit.json"); diagnosis=read("diagnosis.json")
    calls=[json.loads(line) for line in (source/"model-calls.jsonl").read_text(encoding="utf-8").splitlines()]
    reservations=read("budget.json")["reservations"]
    execution_calls=[r for r in reservations if r["case"].startswith("execution:")]
    official_start=datetime.strptime(Path(execution["artifact_directory"]).name,"%Y%m%dT%H%M%S").replace(tzinfo=UTC)
    official_calls=[r for r in execution_calls if datetime.fromisoformat(r["at"])>=official_start]
    result={"version":data["version"],"production_commit":meta["git_commit"],
        "dataset_sha256":digest(data),"initial_dataset_sha256":meta["dataset_sha256"],
        "counts":{key:len(data[key]) for key in ("resumes","jobs","pairs","queries","planning","execution")},
        "labels":{"source":"synthetic specification oracle","human_gold":False,"human_review":"pending"},
        "model":"deepseek-v4-flash","budget":{"limit":read("budget.json")["limit"],"used":read("budget.json")["used"],
            "logged_physical_requests":len(calls),"phases":diagnosis["phase_requests"],
            "execution_official_calls":len(official_calls),"execution_excluded_harness_attempt_calls":len(execution_calls)-len(official_calls),
            "usage_from_responses":diagnosis["usage"],"model_latency_ms":diagnosis["model_latency_ms"],
            "provider_errors":dict(Counter(c["error_type"] for c in calls if c.get("error_type"))),"actual_money_cost":None},
        "ranking":{"scope":"12 challenge candidate queries / 180 pairs / fixed 15-job pools",
            "tie_policy":ranking["tie_policy"],"challenge":ranking["challenge"],
            "paired_ndcg_gain_bootstrap95":ranking["paired_ndcg_gain_bootstrap95"]},
        "planning":{"original":read("planning-summary.json"),"offline_regraded":planning["outcomes"],
            "regrade_reasons":planning["reasons"],"failed_cases":[r for r in planning["cases"] if r["status"]!="pass"]},
        "execution":{"scope":execution["scope"],"systems":execution["systems"],"audit":audit["systems"],
            "repetitions":execution["repetitions"],"failed_case_ids":[r["id"] for r in audit["cases"] if not r["task_success"]]},
        "source_integrity":{phase:read(phase+"-source-check.json") for phase in ("ranking","planning","execution")},
        "limitations":["No human-labelled ground truth; shared templates between splits",
            "No full UI upload/search-to-submit run or real recruitment website",
            "Planning uses synthetic tool effects; browser execution uses injected isolated session",
            "Two repetitions per browser case do not prove long-run reliability",
            "Initial fixed-script payload digest not independently frozen; content/self-consistency checked. Agent compared to stored authorization",
            "Planning grader changed during browser phase; active browser driver and production source remained unchanged",
            "23 physical requests excluded from valid browser metrics due to mismatched harness snapshot IDs; retained in cost ledger",
            "No production fixes made by this benchmark change"]}
    write(target,result)
    print(json.dumps({"published":str(target),"requests":result["budget"]["used"]},ensure_ascii=False))


if __name__=="__main__":
    parser=argparse.ArgumentParser(); parser.add_argument("source",type=Path); parser.add_argument("target",type=Path)
    args=parser.parse_args(); export(args.source,args.target)
