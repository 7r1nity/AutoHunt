"""Consented real-model multi-turn planning against isolated scope contracts.

Only synthetic instructions/state/job facts leave this process. Model configuration
is read by consented_provider; no user content or recruitment service is loaded.
Physical planner, semantic-review and repair calls share one explicit budget.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import gc
import hashlib
import json
import re
import tempfile
import time
import traceback
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS

from app.core.models import BrowserTaskView
from app.llm.base import LLMResult
from app.workflow.planning import plan_request
from app.workflow.revision import RevisionDraft, edited_plan
from evaluation.run import ModelBudgetExceeded, consented_provider
from evaluation.scope_chain_contract import CORPUS, ROOT, ScopeHarness, canonical_filter
from evaluation.stateful_scenarios import STAMP, check, step


CASES = [
    {"id": "search-expansion", "corpus_id": "search-cities", "seed": "search",
     "turns": ["先暂停，把正在进行的搜索地点扩大为深圳、上海、北京，原来的岗位、薪资和投递条件保持。",
               "保持暂停，再把本轮目标从15个改为25个，其余按刚才更新后的条件。"]},
    {"id": "delivery-threshold-update", "corpus_id": "delivery-lower-score", "seed": "delivery",
     "turns": ["搜索结果和搜索条件都不变，把原批次自动投递的85分门槛降到80分，其他投递条件保持，执行这个更新。",
               "投递仍按80分以上；正式岗、深圳、月薪15k以上这些条件都保留。等我在确认卡上确认，不用重新搜索。"]},
    {"id": "delivery-city-update", "corpus_id": "delivery-expand-city", "seed": "delivery",
     "turns": ["搜索结果不变，原来只投深圳，现在投递范围增加上海，仍然只投正式岗、月薪15k以上、85分以上。",
               "再把杭州也纳入投递城市，仍然是深圳或上海或杭州，不要改搜索条件，也不要降低分数或薪资门槛。"]},
    {"id": "outside-single-exception", "corpus_id": "exception-outside-city", "seed": "completed",
     "turns": ["第六个在北京，不符合原搜索地点。这次仅对第六个做例外投递，其他岗位继续用原筛选条件；不要改搜索条件。",
               "这个业务条件例外仍然只限第六个岗位，等待我在确认卡核对后执行，不能扩散到其他岗位。"]},
    {"id": "search-to-interview-chain", "corpus_id": "chain-standard", "seed": "empty",
     "turns": ["帮我找15份数据分析师工作，地点深圳上海杭州，月薪15k以上，不要外包和第三方。85分以上自动投递，85分以下但月薪25k以上分析不足并生成成长计划。",
               "搜索完成后还要给第一份符合条件的岗位生成面试准备，列可能问题、追问和准备要点，原有搜索、投递和成长计划都继续。"]},
    {"id": "new-search-old-delivery", "corpus_id": "source-old-receipt", "seed": "completed",
     "turns": ["搜索城市扩展到北京，按新范围另搜15个；但只投上一轮已完成的岗位，仍按原批次深圳正式岗月薪15k以上且85分以上，新搜索结果不要投。",
               "新搜索继续；旧搜索结果的投递分数改为80分以上，其他投递条件不变，先让我核对新投递范围。"]},
]


def relevant(plan, tool):
    return [s for s in plan["steps"] if s["tool"] == tool and not s.get("withdrawn") and s["status"] != "skipped"]


def seed(h, case):
    initial = "找15份数据分析师岗位，搜索深圳上海杭州，排除外包和第三方；原批次只投深圳正式岗、月薪15k以上、85分以上。"
    if case["seed"] == "empty":
        h.plan = None
        return
    h.remember_scope()
    args = {**copy.deepcopy(h.defaults["search"]), "salary_min": 15000}
    if case["seed"] == "search":
        h.install([step("search", "job_search_start", args, status="waiting_external"),
            step("summary", "job_results_read", {"summary": True}, ["search"]),
            step("delivery", "mock_delivery_start", {"min_score": 85}, ["search"])], original=initial)
        h.searches["synthetic-search"]["status"] = "running"
    elif case["seed"] == "delivery":
        h.install([step("search", "job_search_start", args, status="completed"),
            step("summary", "job_results_read", {"summary": True}, ["search"], status="completed"),
            step("delivery", "mock_delivery_start", {"min_score": 85, "filters": h.scope["filters"],
                "scope_mode": "new", "source_task_id": "synthetic-search"}, ["search"])], original=initial)
    else:
        h.install([step("search", "job_search_start", args, status="completed"),
            step("summary", "job_results_read", {"summary": True}, ["search"], status="completed")], original=initial)


async def model_turn(h, provider, message, row, *, max_turn_calls=3):
    started_calls = provider.calls
    async def limited(request):
        if provider.calls - started_calls >= max_turn_calls:
            raise ModelBudgetExceeded("Per-turn synthetic physical request limit exhausted")
        return await provider.generate(request)
    before = copy.deepcopy(h.plan)
    with h.context(message) as context:
        context.dialogue = h.store.mutate_dialogue("default", {"op": "begin", "message": message})
        candidate = await plan_request(NS(generate=limited), h.registry, message, dialogue=context.dialogue,
            conditions=context.search_state, stamp=STAMP)
        row["model_plans"].append({"message": message, "candidate": copy.deepcopy(candidate),
            "physical_requests": provider.calls - started_calls})
        if candidate.get("operation") == "revise":
            h.plan = edited_plan(h.plan, RevisionDraft.model_validate(candidate["revision"]), h.registry,
                stamp=STAMP, session_id="default", message=message)
        else:
            h.plan = candidate
    if before:
        completed = {s["id"]: s for s in before["steps"] if s["status"] == "completed"}
        current = {s["id"]: s for s in h.plan["steps"]}
        for ident, prior in completed.items():
            if ident in current:
                check(current[ident]["status"] == "completed" and current[ident]["result"] == prior["result"],
                    "completed_receipt_changed", prior, current[ident])
    check(h.store.save_plan(h.run["id"], h.plan), "plan_not_saved", True, h.store.get(h.run["id"]))
    h.record("real_model_plan_committed")
    return candidate


def semantic_oracle(h, case):
    plan = h.plan
    ident = case["id"]
    deliveries = relevant(plan, "mock_delivery_start")
    searches = relevant(plan, "job_search_start")
    if ident == "search-expansion":
        node = searches[-1]
        args = node.get("staged_arguments", node["arguments"])
        check(set(args.get("locations", [])) == {"深圳", "上海", "北京"}, "expanded_search_cities_wrong", ["深圳", "上海", "北京"], args)
        check(args.get("result_limit") == 25 and plan.get("paused"), "quantity_or_pause_lost", "paused and 25", plan)
        check(any(d["arguments"].get("min_score") == 85 for d in deliveries), "search_expansion_changed_delivery", 85, deliveries)
    elif ident.startswith("delivery-"):
        node = deliveries[-1]
        args = node["arguments"]
        check(not any(s["status"] not in {"completed", "skipped"} for s in searches), "delivery_update_started_search", "no new search", searches)
        check(not relevant(plan, "search_state_update"), "delivery_update_changed_search_state", [], plan)
        check(not plan.get("paused"), "approval_wait_misread_as_global_pause", "node approval wait, not whole-plan pause", plan)
        expected_score = 80 if ident == "delivery-threshold-update" else 85
        check(args.get("min_score") == expected_score, "model_threshold_wrong", expected_score, args)
        tree = json.dumps(args.get("filters"), ensure_ascii=False)
        if ident == "delivery-threshold-update":
            check(canonical_filter(args.get("filters")) == h.scope["filters"], "model_lost_old_delivery_conditions", h.scope, args)
        else:
            check(all(city in tree for city in ("深圳", "上海", "杭州")) and ('"any"' in tree or "或" in tree),
                "expanded_delivery_cities_not_or", "深圳 OR 上海 OR 杭州", tree)
            check("正式" in tree and ("15000" in tree or "15k" in tree.lower() or "15K" in tree), "delivery_hard_conditions_lost", "formal and 15000", tree)
    elif ident == "outside-single-exception":
        exceptions = [s for s in deliveries if s["arguments"].get("allow_outside_search")]
        check(len(exceptions) == 1 and (exceptions[0]["arguments"].get("selector") or exceptions[0]["arguments"].get("target_step_id")),
            "model_did_not_plan_bounded_exception", "one selector/target_step_id + allow_outside_search", deliveries)
        for node in deliveries:
            if node in exceptions:
                continue
            check(node["arguments"].get("min_score", 0) >= 85 and canonical_filter(node["arguments"].get("filters")) == h.scope["filters"],
                "selector_inherit_dropped_batch_constraints", "original85 + formal/SZ/monthly15000", node)
        check(not plan.get("paused"), "approval_wait_misread_as_global_pause", "node approval wait, not whole-plan pause", plan)
        check(not relevant(plan, "search_state_update") and not any(s["status"] == "pending" for s in searches),
            "exception_changed_search", "unchanged search", plan)
    elif ident == "search-to-interview-chain":
        tools = {s["tool"] for s in plan["steps"] if not s.get("withdrawn")}
        expected = {"job_search_start", "job_results_read", "mock_delivery_start", "learning_plan_generate", "interview_preparation_generate"}
        check(expected <= tools, "model_omitted_chain_goal", sorted(expected), sorted(tools))
        check(len(searches) == 1, "interview_append_restarted_search", 1, searches)
        check(deliveries[-1]["arguments"].get("min_score") == 85, "chain_delivery_threshold", 85, deliveries)
        growth = relevant(plan, "learning_plan_generate")[-1]
        check(growth["arguments"].get("max_score") == 84 and growth["arguments"].get("salary_min") == 25000,
            "chain_growth_scope_wrong", {"max_score": 84, "salary_min": 25000}, growth)
    else:
        node = deliveries[-1]
        check(node["arguments"].get("source_task_id") == "synthetic-search" and node["arguments"].get("min_score") == 80,
            "old_source_or_threshold_lost", "old source with score80", node)
        producers = {s["id"] for s in searches if s["status"] != "completed"}
        check(not producers.intersection(node["depends_on"]) and node.get("evidence_step") not in producers,
            "old_delivery_depends_on_new_search", "independent old source", node)


async def evaluate(h, provider, case, replay_dialogue=None):
    row = {"id": case["id"], "corpus_id": case["corpus_id"], "turns": case["turns"], "model_plans": [],
        "semantic_pass": False, "contract_pass": False, "execution_pass": False}
    seed(h, case)
    if replay_dialogue is not None:
        # Restore only this benchmark's saved synthetic first-turn state. Keep
        # displayed snapshot identities stable across replay; never rewrite model
        # references to make an otherwise invalid target pass.
        with h.store.connect() as db:
            db.execute("UPDATE session_dialogue_state SET state=? WHERE session_id='default'", (h.store._encode(replay_dialogue),))
        h.snapshot_id = replay_dialogue["snapshots"][-1]["id"]
    before_search = h.store.search_state("default")
    stage = "planning_or_contract"
    try:
        for index, message in enumerate(case["turns"]):
            await model_turn(h, provider, message, row)
            row["contract_pass"] = True
            if case["id"] == "search-to-interview-chain" and index == 0:
                await h.tick()
                check([c["tool"] for c in h.calls] == ["job_search_start"], "first_chain_ignored_search_wait", "search only", h.calls)
            elif case["id"] in {"delivery-threshold-update", "delivery-city-update", "outside-single-exception"}:
                await h.tick()
                if case["id"] == "outside-single-exception":
                    # Independently authorized old-scope work may continue. The
                    # *exception* must wait; unrelated branches must not be blocked.
                    check(not any(q.get("selection_evidence", {}).get("allow_outside_search") or 6 in q.get("job_ids", []) for q in h.queues),
                        "model_self_approved_exception", "no exception queue before human decision", h.queues)
                else:
                    check(not h.queues, "model_self_approved_expansion", "no queue before human decision", h.queues)
        stage = "semantic_oracle"
        semantic_oracle(h, case)
        row["semantic_pass"] = True
        stage = "execution_oracle"
        if case["id"] == "search-expansion":
            await h.tick()
            check(not h.calls, "pause_ignored", "no tools", h.calls)
        elif case["id"] in {"delivery-threshold-update", "delivery-city-update", "outside-single-exception"}:
            await h.tick()
            check(any(s["status"] == "waiting_approval" for s in h.plan["steps"]), "missing_approval_wait", "waiting_approval", h.plan)
            await h.decision(True)
            if case["id"] == "outside-single-exception":
                exceptions = [q for q in h.queues if q.get("selection_evidence", {}).get("allow_outside_search")]
                check(len(exceptions) == 1 and exceptions[0].get("job_ids") == [6], "exception_wrong_job", [6], h.queues)
                remembered = h.store.dialogue_state("default").get("delivery_scope") or {}
                fields = ("session_id", "resume_stamp", "task_id", "filters", "min_score")
                check({k: remembered.get(k) for k in fields} == {k: h.scope.get(k) for k in fields},
                    "exception_leaked_scope", h.scope, remembered)
            else:
                check(len(h.queues) == 1, "approved_action_not_queued_once", 1, h.queues)
                expected = 80 if case["id"] == "delivery-threshold-update" else 85
                check(h.queues[0]["min_score"] == expected, "queue_score_wrong", expected, h.queues)
            check(h.store.search_state("default") == before_search, "delivery_mutated_search_state", before_search, h.trace())
        elif case["id"] == "search-to-interview-chain":
            search = relevant(h.plan, "job_search_start")[0]
            task_id = search["task_id"]
            h.searches[task_id]["status"] = "completed"
            h.browser_views[task_id] = BrowserTaskView(id=task_id, status="completed", source="mock", detail="合成搜索完成")
            await h.observe()
            check(all(s["status"] == "completed" for s in h.plan["steps"]), "model_chain_not_completed", "all completed", h.plan)
            check(Counter(c["tool"] for c in h.calls) == Counter({tool: 1 for tool in
                ("job_search_start", "job_results_read", "mock_delivery_start", "learning_plan_generate", "interview_preparation_generate")}),
                "model_chain_tool_replay_or_omission", "exactly one of each tool", h.calls)
        else:
            await h.tick()
            check(not h.queues, "old_scope_update_skipped_approval", "no queue", h.queues)
            check(any(s["status"] == "waiting_approval" for s in h.plan["steps"]), "old_scope_missing_approval", "waiting_approval", h.plan)
        row["execution_pass"] = True
    except Exception as exc:
        row["failure"] = {"stage": stage, "code": getattr(exc, "code", type(exc).__name__), "message": str(exc),
            "expected": getattr(exc, "expected", None), "actual": getattr(exc, "actual", None), "traceback": traceback.format_exc()}
    row["passed"] = all(row[k] for k in ("semantic_pass", "contract_pass", "execution_pass"))
    row["trace"] = h.trace()
    return row


async def main_async(args):
    if not (args.models_consented or args.replay) or not 1 <= args.physical_limit <= 24:
        raise ValueError("Explicit synthetic-data model consent and physical budget 1..24 required")
    if args.replay and args.warm_start:
        raise ValueError("Choose replay or a partially recorded first-turn warm start")
    traces, rows = [], []
    replay_states = {}
    if args.replay or args.warm_start:
        source = args.replay or args.warm_start
        recorded_rows = [json.loads(line) for line in (source / "cases.jsonl").read_text(encoding="utf-8").splitlines()]
        recorded_calls = [json.loads(line) for line in (source / "model-calls.jsonl").read_text(encoding="utf-8").splitlines()]
        requests_by_case = {}
        for row in recorded_rows:
            requests_by_case[row["id"]] = [t for t in recorded_calls if t.get("case_id") == row["trace"]["final_run"]["id"]]
            first = requests_by_case[row["id"]][0]
            for message in first["request"]["messages"]:
                if message["role"] == "user":
                    wrapper = re.search(r"<user_request>\s*([\s\S]*?)\s*</user_request>", message["content"])
                    try:
                        replay_states[row["id"]] = json.loads(wrapper[1] if wrapper else message["content"])["dialogue"]
                    except (ValueError, KeyError):
                        continue
    if args.replay:
        class Replay:
            calls, model = 0, "saved-synthetic-deepseek-response"
            def select(self, ident):
                self.pending = iter(requests_by_case[ident])
            async def generate(self, _request):
                self.calls += 1
                entry = next(self.pending)
                return LLMResult(entry["response"]["choices"][0]["message"]["content"], "replay", self.model)
        provider = Replay()
    else:
        provider = consented_provider(args.physical_limit, traces)
        if args.warm_start:
            live = provider
            class WarmStart:
                replayed = 0
                @property
                def calls(self):
                    return live.calls
                model, client = live.model, live.client
                def select(self, ident):
                    previous = next(r for r in recorded_rows if r["id"] == ident)
                    if not previous["model_plans"]:
                        raise ValueError("Warm start requires a completed recorded first model turn")
                    count = previous["model_plans"][0]["physical_requests"]
                    self.cached = iter(requests_by_case[ident][:count])
                async def generate(self, request):
                    entry = next(self.cached, None)
                    if entry is not None:
                        self.replayed += 1
                        return LLMResult(entry["response"]["choices"][0]["message"]["content"], "replay", self.model)
                    return await live.generate(request)
            provider = WarmStart()
    output = ROOT / "evaluation/results/scope-chain-model" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    cases = [case for case in CASES if not args.case or case["id"] in args.case]
    try:
        for case in cases:
            if provider.calls >= args.physical_limit:
                break
            started, initial_calls = time.monotonic(), provider.calls
            if args.replay or args.warm_start:
                provider.select(case["id"])
            with tempfile.TemporaryDirectory(prefix="autohunt-scope-model-") as directory:
                h = ScopeHarness(directory, corpus["defaults"])
                row = await evaluate(h, provider, case, replay_states.get(case["id"]))
                await h.close()
                h = None
                gc.collect()
            row.update(physical_requests=0 if args.replay else provider.calls - initial_calls,
                replayed_responses=provider.calls - initial_calls if args.replay else 0, elapsed_ms=round((time.monotonic() - started) * 1000))
            rows.append(row)
            (output / "cases.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in rows), encoding="utf-8")
            (output / "model-calls.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in traces), encoding="utf-8")
            print(json.dumps({k: row[k] for k in ("id", "passed", "semantic_pass", "contract_pass", "execution_pass", "physical_requests", "elapsed_ms")} |
                {"failure": row.get("failure", {}).get("message")}, ensure_ascii=False), flush=True)
    finally:
        if not args.replay:
            await provider.client.close()
        (output / "model-calls.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in traces), encoding="utf-8")
    summary = {"cases": len(rows), "passed": sum(r["passed"] for r in rows), "physical_requests": 0 if args.replay else provider.calls,
        "replayed_responses": provider.calls if args.replay else getattr(provider, "replayed", 0),
        "replay_source": str(args.replay) if args.replay else None, "warm_start_source": str(args.warm_start) if args.warm_start else None,
        "physical_limit": args.physical_limit, "model": provider.model, "real_recruitment_actions": 0,
        "layers": {key: {"passed": sum(r[key] for r in rows), "total": len(rows)} for key in ("contract_pass", "semantic_pass", "execution_pass")},
        "failures": [{"id": r["id"], **r["failure"]} for r in rows if r.get("failure")],
        "input_tokens": sum(t.get("response", {}).get("usage", {}).get("prompt_tokens", 0) for t in traces),
        "output_tokens": sum(t.get("response", {}).get("usage", {}).get("completion_tokens", 0) for t in traces),
        "corpus_sha256": hashlib.sha256(CORPUS.read_bytes()).hexdigest(),
        "scope": "Small consented synthetic real-model multi-turn planning sample + production contracts/DAG/approval/temp SQLite; synthetic recruitment and analysis adapters, not live browser submission E2E.",
        "output": str(output)}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str), flush=True)
    return 0 if len(rows) == len(cases) and all(r["passed"] for r in rows) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models-consented", action="store_true")
    parser.add_argument("--physical-limit", type=int, default=24)
    parser.add_argument("--case", nargs="+")
    parser.add_argument("--replay", type=Path, help="Replay saved synthetic responses without any external call")
    parser.add_argument("--warm-start", type=Path, help="Reuse one saved first turn, then model only its unfinished follow-up under a fresh physical budget")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
