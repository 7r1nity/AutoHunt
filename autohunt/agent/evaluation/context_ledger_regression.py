"""Offline cross-module regression for the context/ledger trial branch.

Does not run live model or browser runners. Reused fixture/inherited tests and
parameterized variants are counted as contracts, not independent scenarios.
"""
from __future__ import annotations

import hashlib
import importlib
import io
import json
import platform
import unittest
from datetime import UTC, datetime
from pathlib import Path

MODULES = (
    "metrics_dashboard_contract", "pause_contract",
    "context_ledger_contract", "model_trace_contract", "workflow_execution_contract",
    "loosening_contract", "approval_contract", "goal_graph_contract", "action_contract",
    "command_recovery_contract", "recovery_contract", "salary_revision_contract",
    "condition_refresh_contract", "result_source_contract", "search_delivery_contract",
    "job_target_contract", "job_choice_contract", "reference_flow_contract",
    "presentation_contract", "reporting_contract", "privacy_contract",
    "delivery_scheduling_contract", "daily_workflow_contract", "daily_scheduler_contract", "background_polling_contract", "special_salary_contract",
    "delivery_evidence_contract",
    "recovery_memory_contract",
    "systemic_repair_contract",
    "application_export_contract", "application_export_api_contract", "removed_growth_contract", "retired_feature_migration_contract",
    "task_results_contract", "task_control_contract", "planning_budget_contract",
    "typed_filter_contract", "predicate_repair_contract", "predicate_revision_contract",
)


def main():
    root = Path(__file__).parents[1]
    output = root / "evaluation" / "results" / "context-ledger-regression" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    source = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
              for folder in (root / "app", root / "evaluation") for p in folder.rglob("*.py") if "results" not in p.parts}
    rows, traces = [], []
    for name in MODULES:
        module = importlib.import_module("evaluation." + name)
        # Existing runners intentionally exclude inherited/imported tests when
        # their subclass changes the fixture (e.g. all companies share a name).
        suite = unittest.TestSuite(cls(test) for cls in vars(module).values()
            if isinstance(cls, type) and issubclass(cls, unittest.TestCase) and cls.__module__ == module.__name__
            for test in cls.__dict__ if test.startswith("test_"))
        log = io.StringIO()
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
        (output / (name + ".log")).write_text(log.getvalue(), encoding="utf-8")
        row = {"module": name, "tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors)}
        rows.append(row)
        traces.append({"module": name, "traces": getattr(module, "TRACES", [])})
        print(json.dumps(row), flush=True)
        if not result.wasSuccessful(): print(log.getvalue()[-8000:], flush=True)
    summary = {"modules": rows, "tests": sum(r["tests"] for r in rows), "failures": sum(r["failures"] for r in rows),
        "errors": sum(r["errors"] for r in rows), "python": platform.python_version(), "source_sha256": source,
        "source_changed_during_run": [path for path, digest in source.items() if hashlib.sha256((root / path).read_bytes()).hexdigest() != digest],
        "external_model_calls": 0, "browser_actions": 0, "scope": "Offline contract regression; not live semantic accuracy/browser E2E"}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "traces.jsonl").write_text("".join(json.dumps(t, ensure_ascii=False, default=str) + "\n" for t in traces), encoding="utf-8")
    print(json.dumps({"output": str(output), **{k: summary[k] for k in ("tests", "failures", "errors", "external_model_calls", "source_changed_during_run")}}), flush=True)
    return int(bool(summary["failures"] or summary["errors"] or summary["source_changed_during_run"]))


if __name__ == "__main__": raise SystemExit(main())
