"""Reproducible scope/outreach/interview engineering acceptance, no model calls.

Run from agent/: python -m evaluation.workflow_upgrade_acceptance --browser
Use --ui to also start a temporary frontend with intercepted synthetic APIs.
No .env, personal data or live recruitment service is read by this runner.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
MODULES = (
    "delivery_evidence_contract", "approval_contract", "goal_graph_contract",
    "workflow_execution_contract", "presentation_contract", "recovery_contract",
    "search_delivery_contract", "systemic_repair_contract", "outreach_contract", "interview_contract",
    "daily_workflow_contract", "daily_scheduler_contract",
    "search_evidence_contract", "provider_json_contract",
    "condition_refresh_contract",
    "application_export_contract", "application_export_api_contract", "removed_growth_contract", "retired_feature_migration_contract",
    "task_results_contract", "task_control_contract", "planning_budget_contract",
    "typed_filter_contract", "predicate_repair_contract", "predicate_revision_contract",
    "delivery_limit_contract", "delivery_round_contract", "workflow_wait_delivery_contract", "delivery_request_contract",
    "qualification_advisory_contract",
    "global_filter_policy_contract", "global_filter_state_contract", "global_filter_planning_contract", "global_filter_results_contract",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", action="store_true")
    parser.add_argument("--ui", action="store_true")
    args = parser.parse_args()
    output = ROOT / "evaluation/results/workflow-upgrade" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    commands = [("engineering", ["-m", "unittest", *("evaluation." + name for name in MODULES), "-q"]),
                ("stateful_scopes", ["-m", "evaluation.scope_chain_contract"])]
    if args.browser:
        commands.append(("browser_messages", ["-m", "evaluation.outreach_browser_contract"]))
        commands.append(("browser_gated_queue", ["-m", "evaluation.gated_delivery_browser_contract"]))
    if args.ui:
        commands.append(("interview_ui", ["-m", "evaluation.interview_ui_browser"]))
    # Source hashes are reproducibility metadata, never authorization tokens.
    sources = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
               for path in (ROOT / "app").rglob("*.py")}
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("AUTOHUNT_") and not key.endswith(("_API_KEY", "_TOKEN"))}
    environment.update(PYTHONUTF8="1", PYTHONUNBUFFERED="1")
    results = []
    for name, command in commands:
        started = time.monotonic()
        log = output / (name + ".log")
        timed_out = False
        with log.open("w", encoding="utf-8") as stream:
            try:
                process = subprocess.run([sys.executable, *command], cwd=ROOT, env=environment,
                                         stdout=stream, stderr=subprocess.STDOUT, timeout=600, check=False)
                code = process.returncode
            except subprocess.TimeoutExpired:
                code, timed_out = -1, True
        contents = log.read_text(encoding="utf-8", errors="replace")
        count = re.search(r"Ran (\d+) tests?", contents)
        results.append({"suite": name, "passed": code == 0, "exit_code": code, "timed_out": timed_out,
                        "tests": int(count.group(1)) if count else None,
                        "elapsed_seconds": round(time.monotonic() - started, 3), "log": log.name})
        print(json.dumps(results[-1]), flush=True)
    report = {"schema_version": 1, "evaluation_type": "isolated_engineering_acceptance",
              "external_model_requests": 0, "real_recruitment_actions": 0,
              "passed": all(row["passed"] for row in results), "suites": results,
              "source_sha256": sources,
              "source_changed_during_run": [name for name, value in sources.items()
                                            if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != value],
              "limits": "Scripted planning and isolated HTML/UI prove contracts, not natural-language accuracy or live website reliability."}
    (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "passed": report["passed"]}), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
