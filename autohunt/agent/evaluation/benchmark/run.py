"""Run isolated benchmark; external calls require explicit consent and one ledger.

Example: python -m evaluation.benchmark.run --output evaluation/results/task-benchmark/session-20261026 --models-consented --phases ranking planning
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from .common import ROOT, file_hash, provider, write
from .corpus import PATH, build, digest


async def main(args):
    output=Path(args.output).resolve()
    allowed=(ROOT/"evaluation/results").resolve()
    if allowed not in output.parents: raise ValueError("Output must stay inside ignored evaluation/results")
    output.mkdir(parents=True,exist_ok=True)
    data=json.loads(PATH.read_text(encoding="utf-8"))
    if data != build(): raise ValueError("Corpus differs from reproducible generator; do not silently change labels")
    meta={"version":data["version"],"created_at":datetime.now(UTC).isoformat(),"dataset_sha256":digest(data),
          "git_commit":subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip(),
          "models_consented":args.models_consented,"model_physical_limit":args.call_limit,"human_gold":False,
          "python":sys.version,"dependencies":{p:importlib.metadata.version(p) for p in ("openai","pydantic","playwright","httpx")}}
    if not (output/"metadata.json").exists(): write(output/"metadata.json",meta)
    else:
        previous=json.loads((output/"metadata.json").read_text(encoding="utf-8"))
        if previous["dataset_sha256"] != meta["dataset_sha256"]:
            original=json.loads((output/"dataset-v1-initial.json").read_text(encoding="utf-8"))
            if args.phases != ["execution"] or any(original[key]!=data[key] for key in ("resumes","jobs","queries","pairs","planning")):
                raise ValueError("Cannot mix changed evaluated inputs/labels in one session")
            write(output/"execution-spec-amendment.json",{"old_sha256":previous["dataset_sha256"],"new_sha256":meta["dataset_sha256"],
                "reason":"Before official execution, solvable button-ID/extra-step changes must require delivery, not count abstention as success. Ranking/planning unchanged; raw pilot results retained."})
    write(output/("dataset-"+data["version"]+".json"),data)
    model=provider(output,args.call_limit) if args.models_consented else None
    print(json.dumps({"output":str(output),"model":model.model if model else None,"phases":args.phases},ensure_ascii=False),flush=True)
    failed=False
    for phase in args.phases:
        if (output/(phase+"-summary.json")).exists(): raise ValueError("Phase already completed; preserve results, choose a new phase/session")
        sources={str(p.relative_to(ROOT)):file_hash(p) for p in (ROOT/"app").rglob("*.py")}
        sources.update({str(p.relative_to(ROOT)):file_hash(p) for p in Path(__file__).parent.glob("*.py")})
        manifest=output/(phase+"-source-manifest.json")
        if manifest.exists(): manifest=output/(phase+"-source-manifest-"+datetime.now(UTC).strftime("%H%M%S")+".json")
        write(manifest,sources)
        if phase=="ranking":
            from .ranking import run
            summary=await run(data,output,model)
        elif phase=="planning":
            if model is None:
                summary={"status":"skipped","reason":"No consented real model; scripted JSON is not a language evaluation"}
                write(output/"planning-summary.json",summary)
            else:
                from .planning import run
                summary=await run(data,output,model)
        else:
            from .execution import run
            attempt=output/"execution-attempts"/datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
            attempt.mkdir(parents=True)
            summary=await run(data,attempt,model,repetitions=args.repetitions,cases=args.cases)
            summary["artifact_directory"]=str(attempt.relative_to(output))
            write(output/"execution-summary.json",summary)
        changed=[path for path,sha in sources.items() if file_hash(ROOT/path)!=sha]
        write(output/(phase+"-source-check.json"),{"changed":changed})
        failed = failed or phase_failed(phase,summary) or bool(changed)
        print(json.dumps({"phase":phase,"done":True,"source_changed":changed,"calls":model.budget.state()["used"] if model else 0},ensure_ascii=False),flush=True)
    if model:
        await model.client.close()
    return 1 if failed else 0


def phase_failed(phase,summary):
    """A completed experiment is not necessarily a passing acceptance test."""
    if summary.get("status")=="skipped": return False
    if phase=="planning": return bool(summary.get("fail") or summary.get("error"))
    if phase=="execution": return any(row["task_success"] < 1 for row in summary["systems"].values())
    # Ranking is descriptive; no post-hoc quality threshold is invented.
    return False


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",required=True)
    parser.add_argument("--models-consented",action="store_true")
    parser.add_argument("--call-limit",type=int,default=300)
    parser.add_argument("--phases",nargs="+",choices=["ranking","planning","execution"],default=["ranking","planning","execution"])
    parser.add_argument("--repetitions",type=int,default=2)
    parser.add_argument("--cases",nargs="*")
    raise SystemExit(asyncio.run(main(parser.parse_args())))
