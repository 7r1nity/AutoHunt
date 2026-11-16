"""Reproducible current-version acceptance using the existing evaluation harness."""
from __future__ import annotations
import argparse
import asyncio
import importlib.metadata
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from evaluation.benchmark.common import ROOT, file_hash, write
from .contracts import CORPUS


def sources():
    paths = list((ROOT/"app").rglob("*.py")) + list((ROOT/"evaluation").rglob("*"))
    return {str(p.relative_to(ROOT)):file_hash(p) for p in paths if p.is_file()
            and "results" not in p.relative_to(ROOT).parts and "__pycache__" not in p.parts
            and p.suffix in {".py", ".html", ".js", ".css", ".json", ".sha256"}}


async def main(args):
    output = args.output.resolve()
    if (ROOT/"evaluation/results").resolve() not in output.parents or output.exists():
        raise ValueError("Use a NEW folder under evaluation/results; existing attempts are never overwritten")
    if args.models_consented and not 1 <= args.call_limit <= 200:
        raise ValueError("Explicit model request budget must be in 1..200")
    data = json.loads(CORPUS.read_text(encoding="utf-8"))
    if file_hash(CORPUS) != CORPUS.with_suffix(".sha256").read_text().strip():
        raise ValueError("Frozen dataset changed; create an explicit new version")
    output.mkdir(parents=True)
    before = sources()
    write(output/"source-manifest.json", before)
    write(output/"dataset.json", data)
    commit = subprocess.run(["git", "rev-parse", "HEAD"],cwd=ROOT,capture_output=True,text=True)
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"],cwd=ROOT,capture_output=True,text=True)
    own_checkout = top.returncode == 0 and Path(top.stdout.strip()).resolve() == ROOT.parents[1].resolve()
    write(output/"manifest.json", {"created_at":datetime.now(UTC).isoformat(),
        "git_commit":commit.stdout.strip() if commit.returncode == 0 and own_checkout else "source-export",
        "dataset_sha256":file_hash(CORPUS), "python":sys.version,
        "dependencies":{d.metadata["Name"]:d.version for d in importlib.metadata.distributions()},
        "models_consented":args.models_consented,"call_limit":args.call_limit if args.models_consented else 0,
        "endpoint":"https://api.deepseek.com" if args.models_consented else None,
        "scope":"synthetic tasks and bundled simulator; no real recruitment site/user data"})
    results = {}
    model = None
    try:
        # Separate process: offline contracts cannot inherit the provider object.
        command = [sys.executable,"-m","unittest","evaluation.acceptance.contracts",
                   "evaluation.benchmark.contracts","evaluation.daily_scheduler_contract",
                   "evaluation.daily_workflow_contract"]
        with (output/"contracts.log").open("w",encoding="utf-8") as log:
            child = await asyncio.create_subprocess_exec(*command,cwd=ROOT,stdout=log,stderr=log)
            code = await child.wait()
        results["contracts"] = {"exit_code":code,"passed":code == 0}
        count = re.search(r"Ran (\d+) tests?", (output/"contracts.log").read_text(encoding="utf-8", errors="replace"))
        results["contracts"]["tests"] = int(count.group(1)) if count else None
        if code: raise RuntimeError("Offline contracts failed; inspect contracts.log")
        if args.models_consented:
            from evaluation.benchmark.common import provider
            model = provider(output,args.call_limit)
        from evaluation.benchmark.execution import run as browser_run
        from evaluation.benchmark.corpus import build
        from evaluation.benchmark.audit import execution_audit
        from app.llm.base import LLMResult
        async def scripted(request):
            return LLMResult(json.dumps({"steps":[{"id":"apply","tool":"mock_delivery_start",
                "arguments":{"selector":"第1个"},"user_requested":True}],"goals":[],"questions":[]}),"scripted","acceptance-fixture")
        browser_dir=output/"browser"; browser_dir.mkdir()
        browser_summary=await browser_run(build(),browser_dir,model or SimpleNamespace(generate=scripted),
            repetitions=3 if model else 1,cases=data["browser_regression"])
        rows=[json.loads(line) for line in (browser_dir/"execution-cases.jsonl").read_text(encoding="utf-8").splitlines()]
        audit=execution_audit(output,{"artifact_directory":"browser"},rows)
        write(output/"browser-audit.json",audit)
        selected=[r for r in audit["cases"] if r["method"]=="agent"]
        results["browser"]={"passed":bool(selected) and all(r["task_success"] and r["post_attempts_above_one"] == 0 for r in selected),
            "mode":"real_model" if model else "scripted_model", "systems":audit["systems"],
            "scope":"existing browser regression scenarios, not new task set or full UI E2E"}
        if model:
            from .planning import run as planning_run
            results["planning"]=await planning_run(data,output,model)
            results["planning"]["passed"]=not any(results["planning"][k] for k in ("fail","error","not_run"))
        else:
            results["planning"]={"status":"skipped","reason":"No external-model consent; scripted answers are not semantic acceptance"}
        if args.ui:
            ui_output = ROOT/"evaluation/results/daily-site-e2e"/datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
            with (output/"daily-ui.log").open("w",encoding="utf-8") as log:
                child=await asyncio.create_subprocess_exec(sys.executable,"-m","evaluation.daily_site_e2e",cwd=ROOT,
                    env={**os.environ,"AUTOHUNT_ACCEPTANCE_UI_OUTPUT":str(ui_output)},stdout=log,stderr=log)
                code=await child.wait()
            results["daily_ui"]={"exit_code":code,"passed":code==0,"mode":"mock_model",
                                  "artifact_directory":str(ui_output.relative_to(ROOT))}
        else: results["daily_ui"]={"status":"skipped","reason":"Use --ui after installing frontend dependencies"}
    except Exception as exc:
        import traceback
        results["error"]={"type":type(exc).__name__,"traceback":traceback.format_exc()}
    finally:
        if model:
            results["model_calls"]=model.budget.state()["used"]
            await model.client.close()
        else: results["model_calls"]=0
        after=sources()
        changed=sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
        results["changed_sources"]=changed
        results["executed_checks_passed"]=not changed and "error" not in results and all(
            value.get("passed", True) for value in results.values() if isinstance(value,dict))
        results["semantic_acceptance_complete"]=bool(args.models_consented and results.get("planning",{}).get("not_run")==0)
        write(output/"summary.json",results)
        print(json.dumps({"output":str(output),**results},ensure_ascii=False,indent=2),flush=True)
    return int(not results["executed_checks_passed"])


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--ui",action="store_true",help="Also run existing real frontend + daily workflow with Mock model")
    parser.add_argument("--models-consented",action="store_true")
    parser.add_argument("--call-limit",type=int,default=0)
    raise SystemExit(asyncio.run(main(parser.parse_args())))
