"""New requests through existing planner/revision/runtime; synthetic tool effects."""
from __future__ import annotations
import copy
import gc
import tempfile
import time
import traceback
from unittest.mock import patch
from app.workflow.planning import plan_request
from app.workflow.revision import RevisionDraft, edited_plan
from app.workflow.execution import advance
from evaluation.stateful_scenarios import Harness, STAMP
from evaluation.benchmark.common import CASE, append, write
from .grading import grade


async def run(data, output, model):
    rows = []
    for repetition in range(data["repetitions"]):
        for case in data["planning"]:
            token = CASE.set(f"acceptance:{case['id']}:{repetition}")
            row = {"id": case["id"], "repetition": repetition, "status": "error", "turns": []}
            started = time.monotonic()
            h = None
            try:
                with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
                    h = Harness(directory)
                    conditions = {"queries": ["Python后端工程师"], "source": "mock"}
                    try:
                        with patch("app.workflow.execution.resume_stamp", return_value=STAMP):
                            for index, message in enumerate(case["turns"]):
                                with h.context(message) as ctx:
                                    ctx.search_state = copy.deepcopy(conditions)
                                    h.store.mutate_dialogue("default", {"op": "begin", "message": message})
                                    ctx.dialogue = h.store.dialogue_state("default")
                                    draft = await plan_request(model, h.registry, message, dialogue=ctx.dialogue,
                                                               conditions=conditions, stamp=STAMP)
                                    h.plan = (edited_plan(h.plan, RevisionDraft.model_validate(draft["revision"]),
                                              h.registry, stamp=STAMP, session_id="default", message=message)
                                              if draft.get("operation") == "revise" else draft)
                                    h.store.save_plan(h.run["id"], h.plan)
                                    checks = grade(h.plan, case["expected"][index])
                                    row["turns"].append({"message": message, "draft": copy.deepcopy(draft),
                                                         "plan": copy.deepcopy(h.plan), "checks": checks})
                                    calls_before = len(h.calls)
                                    await advance(h.plan, h.registry)
                                    # A matching plan flag alone is insufficient: audit effects.
                                    if case["expected"][index].get("paused") is True:
                                        checks["pause_no_tool_calls"] = len(h.calls) == calls_before
                                        checks["pause_no_approvals"] = not h.store.approvals(h.run["id"])
                                    if index and case["expected"][index-1].get("paused") is True and case["expected"][index].get("paused") is False:
                                        checks["resume_search_once"] = sum(c["tool"] == "job_search_start" for c in h.calls) == 1
                                    h.store.save_plan(h.run["id"], h.plan)
                                    for step in h.plan.get("steps", []):
                                        if step["tool"] == "job_search_start":
                                            args = step.get("staged_arguments") or step["arguments"]
                                            conditions.update({k: v for k, v in args.items() if k in
                                                {"queries", "locations", "result_limit", "salary_min", "filters", "source"}})
                        row["status"] = "pass" if all(all(t["checks"].values()) for t in row["turns"]) else "fail"
                    finally:
                        row["trace"] = h.trace()
                        await h.close()
                        h = None
                        gc.collect()
            except Exception as exc:
                row.update(error_type=type(exc).__name__, error=str(exc), traceback=traceback.format_exc())
            finally:
                row["duration_seconds"] = round(time.monotonic()-started, 3)
                append(output/"planning-cases.jsonl", row)
                rows.append(row)
                CASE.reset(token)
            print(f"acceptance {case['id']} [{repetition+1}]: {row['status']}", flush=True)
            if model.budget.state()["used"] >= model.budget.limit: break
        if model.budget.state()["used"] >= model.budget.limit: break
    expected_trials = len(data["planning"])*data["repetitions"]
    summary = {"planned_trials": expected_trials, "trials": len(rows),
               "pass": sum(r["status"] == "pass" for r in rows),
               "fail": sum(r["status"] == "fail" for r in rows),
               "error": sum(r["status"] == "error" for r in rows),
               "not_run": expected_trials-len(rows),
               "all_repetitions_pass": sum(len(group := [r for r in rows if r["id"] == c["id"]]) == data["repetitions"]
                   and all(r["status"] == "pass" for r in group) for c in data["planning"]),
               "independent_scenarios": len(data["planning"]),
               "scope": "real model and production planner/revision/runtime; synthetic tools, not browser E2E"}
    write(output/"planning-summary.json", summary)
    return summary
