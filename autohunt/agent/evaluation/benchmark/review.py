"""Create a blinded human-review work queue; no model/network access."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .common import ROOT, append, write
from .corpus import PATH, digest


def prepare(output):
    output=Path(output).resolve()
    if (ROOT/"evaluation/results").resolve() not in output.parents:
        raise ValueError("Review annotations stay in ignored evaluation/results")
    output.mkdir(parents=True,exist_ok=True)
    target=output/"review-tasks.jsonl"
    if target.exists(): raise ValueError("Never overwrite existing human annotations")
    data=json.loads(PATH.read_text(encoding="utf-8"))
    resumes={r["id"]:r for r in data["resumes"]}; jobs={j["id"]:j for j in data["jobs"]}
    for pair in data["pairs"]:
        append(target,{"id":pair["id"],"candidate":resumes[pair["candidate_id"]],"job":jobs[pair["job_id"]],
            "reviewer_a":{"relevance":None,"eligibility":None,"evidence":[],"reviewer":None},
            "reviewer_b":{"relevance":None,"eligibility":None,"evidence":[],"reviewer":None},
            "adjudicated":{"relevance":None,"eligibility":None,"reason":None}})
    write(output/"review-reference.json",{"dataset_sha256":digest(data),"human_gold":False,
        "status":"pending human annotation; model predictions excluded from review queue",
        "synthetic_reference":data["pairs"]})
    return len(data["pairs"])


if __name__=="__main__":
    parser=argparse.ArgumentParser(); parser.add_argument("output")
    print(json.dumps({"review_tasks":prepare(parser.parse_args().output),"status":"pending"}))
