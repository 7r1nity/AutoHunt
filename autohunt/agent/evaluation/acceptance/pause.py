"""Selected frozen-case rerun plus the production chat handler/runtime path.

Requires continuing an existing explicit consent ledger. All adapters and stores
are isolated/synthetic; this is not a real recruitment site or browser UI test.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import gc
import json
import platform
import tempfile
import traceback
from datetime import UTC, datetime
from pathlib import Path

from evaluation.benchmark.common import CASE, ROOT, append, file_hash, provider, write
from evaluation.pause_contract import ChatHarness
from .contracts import CORPUS
from .grading import grade
from .planning import run as planning_run
from .run import sources


async def chat_run(case, output, model, repetitions):
    rows = []
    for repetition in range(repetitions):
        token = CASE.set(f"pause-chat:{case['id']}:{repetition}")
        row = {"id": case["id"], "repetition": repetition, "status": "error", "checks": {}}
        try:
            with tempfile.TemporaryDirectory(prefix="autohunt-pause-chat-", ignore_cleanup_errors=True) as directory:
                h = ChatHarness(directory, model)
                try:
                    run, response = await h.chat(case["turns"][0])
                    row["pause_response"] = response
                    await h.wait_for(lambda: h.store.get(run["id"])["status"] == "waiting_user")
                    before = copy.deepcopy(h.store.get(run["id"])["checkpoint"]["plan"])
                    row["pause_plan"] = before
                    row["checks"].update({"initial_"+k: v for k, v in grade(before, case["expected"][0]).items()})
                    await asyncio.sleep(1.1)  # Observe a full background polling interval.
                    row["checks"].update(paused_no_tools=not h.calls,
                        paused_no_approvals=not h.store.approvals(run["id"]),
                        paused_no_mutation=not h.store.get(run["id"])["mutation_started"],
                        response_says_paused="暂停" in response["message"])
                    _, response = await h.chat(case["turns"][1])
                    row["continue_response"] = response
                    await h.wait_for(lambda: any(c["tool"] == "job_search_start" for c in h.calls), timeout=10)
                    after = copy.deepcopy(h.store.get(run["id"])["checkpoint"]["plan"])
                    row["continued_plan"] = after
                    row["checks"].update({"continued_"+k: v for k, v in grade(after, case["expected"][1]).items()})
                    row["checks"]["same_plan"] = after["id"] == before["id"]
                    search = next(s for s in before["steps"] if s["tool"] == "job_search_start")
                    row["checks"]["same_search_arguments"] = next(c for c in h.calls if c["tool"] == "job_search_start")["arguments"] == search["arguments"]
                    await h.chat("继续任务")
                    row["checks"]["search_started_once"] = sum(c["tool"] == "job_search_start" for c in h.calls) == 1
                    row["checks"]["no_delivery"] = not any(c["tool"] == "mock_delivery_start" for c in h.calls)
                    row["status"] = "pass" if all(row["checks"].values()) else "fail"
                finally:
                    row["adapter_calls"] = h.calls
                    row["runs"] = h.store.list()
                    row["events"] = {r["id"]: h.store.all_events(r["id"]) for r in h.store.list()}
                    await h.close()
                    h = None
                    gc.collect()
        except Exception as exc:
            row.update(error_type=type(exc).__name__, error=str(exc), traceback=traceback.format_exc())
        finally:
            rows.append(row)
            append(output/"chat-cases.jsonl", row)
            CASE.reset(token)
        print(f"pause HTTP chat [{repetition+1}]: {row['status']}", flush=True)
        if model.budget.state()["used"] >= model.budget.limit:
            break
    summary = {"trials": len(rows), "planned_trials": repetitions,
               **{status: sum(r["status"] == status for r in rows) for status in ("pass", "fail", "error")},
               "not_run": repetitions-len(rows),
               "scope": "production HTTP handler, router, agent, durable runtime and watcher; synthetic adapters; no app startup/middleware/UI"}
    write(output/"chat-summary.json", summary)
    return summary


async def main(args):
    if not args.models_consented or not 1 <= args.call_limit <= 200:
        raise ValueError("Explicit existing model consent and limit in 1..200 required")
    output, budget_path = args.output.resolve(), args.budget.resolve()
    if (ROOT/"evaluation/results").resolve() not in output.parents or output.exists():
        raise ValueError("Use a new directory under evaluation/results")
    if not budget_path.is_file():
        raise ValueError("Existing consent ledger required; never reset an exhausted budget")
    budget_before = json.loads(budget_path.read_text(encoding="utf-8"))
    if budget_before["limit"] != args.call_limit:
        raise ValueError("Cannot change existing consent limit")
    if file_hash(CORPUS) != CORPUS.with_suffix(".sha256").read_text().strip():
        raise ValueError("Frozen corpus changed")
    data = json.loads(CORPUS.read_text(encoding="utf-8"))
    case = next(c for c in data["planning"] if c["id"] == "resume_search")
    selected = {**data, "planning": [case]}
    output.mkdir(parents=True)
    before = sources()
    write(output/"source-manifest.json", before)
    write(output/"dataset.json", selected)
    write(output/"manifest.json", {"created_at": datetime.now(UTC).isoformat(), "python": platform.python_version(),
        "dataset_sha256": file_hash(CORPUS), "selection": [case["id"]], "repetitions": data["repetitions"],
        "budget_path": str(budget_path), "budget_before": budget_before["used"], "limit": args.call_limit,
        "endpoint": "https://api.deepseek.com", "data_scope": "synthetic tasks/tool structures only"})
    model = provider(output, args.call_limit, budget_path=budget_path)
    try:
        planning = await planning_run(selected, output, model)
        chat = await chat_run(case, output, model, data["repetitions"])
        budget_after = model.budget.state()
        changed = before != sources()
        summary = {"planning": planning, "chat": chat, "source_changed": changed,
            "physical_calls_this_run": budget_after["used"]-budget_before["used"],
            "physical_calls_total": budget_after["used"], "consented_limit": args.call_limit,
            "passed": not changed and all(not any(s[k] for k in ("fail", "error", "not_run")) for s in (planning, chat))}
        write(output/"summary.json", summary)
        print(json.dumps(summary), flush=True)
        return int(not summary["passed"])
    finally:
        await model.client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models-consented", action="store_true")
    parser.add_argument("--call-limit", type=int, required=True)
    parser.add_argument("--budget", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    raise SystemExit(asyncio.run(main(parser.parse_args())))
