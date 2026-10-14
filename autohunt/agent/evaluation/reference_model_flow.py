"""Consented real-model planning -> isolated execution -> final-state oracle.

Reads only model configuration/credentials. All prompts/jobs/states are synthetic.
No recruitment browser exists; queued effects are recorded in temporary SQLite.
One first-pass physical call per case, no semantic/synthesis/network retry calls.
"""
from __future__ import annotations

import argparse
import asyncio
import gc
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS

from app.workflow.execution import begin, completion_steps
from app.workflow.planning import plan_request
from app.runtime.context import active_run
from app.llm.base import LLMResult
from .reference_flow_contract import References
from .goal_graph_contract import STAMP
from .run import consented_provider, ModelBudgetExceeded


CASES = [
    {"id": "mixed_bare", "message": "移除第九个天工开物 投递10星云科技 并且给我13的链接", "remove": [9], "queue": [10], "links": [13]},
    {"id": "mixed_synonyms", "message": "删除9天工开物；自动投递第十个星云科技；发我13的网址", "remove": [9], "queue": [10], "links": [13]},
    {"id": "bare_link", "message": "给我13的链接", "remove": [], "queue": [], "links": [13]},
    {"id": "named_link", "message": "查看13合成远景的链接", "remove": [], "queue": [], "links": [13]},
    {"id": "remove_and_link", "message": "移除第9个天工开物，然后给我13的链接", "remove": [9], "queue": [], "links": [13]},
    {"id": "conflicting_name", "message": "投递9星云科技", "remove": [], "queue": [], "links": [], "clarify": True},
    {"id": "two_links", "message": "给我10和13的链接，分别列出，不投递", "remove": [], "queue": [], "links": [10, 13]},
    {"id": "negative_delivery", "message": "移除9天工开物，10星云科技只给链接，不要投递", "remove": [9], "queue": [], "links": [10]},
]


async def evaluate(case, provider):
    fixture = References("test_mixed_three_branch_replay_checks_final_database_and_reply")
    await fixture.asyncSetUp()
    try:
        context = active_run()
        context.run_id, context.user_message = "synthetic-"+case["id"], case["message"]
        if case.get("replay_dialogue"):
            fixture.state = context.dialogue = case["replay_dialogue"]
        start_calls = provider.calls
        async def once(request):
            if provider.calls != start_calls:
                raise ModelBudgetExceeded("First-pass case budget exhausted; no repair call")
            return await provider.generate(request)
        plan = await plan_request(NS(generate=once), fixture.registry, case["message"], dialogue=context.dialogue,
            conditions={"source": "mock"}, stamp=STAMP)
        context.plan_reader = lambda: plan
        # No auxiliary external models, even when the plan chooses a semantic tool.
        fixture.registry.response_llm = None
        response = await begin(plan, fixture.registry)
        removed = [i for i,ident in enumerate(fixture.ids, 1) if fixture.jobs.get_job(ident).get("removed_at")]
        queued_ids = [ident for call in fixture.calls for ident in call.get("job_ids", [])]
        queued = [fixture.ids.index(ident)+1 for ident in queued_ids]
        link_steps = [s for s in plan["steps"] if s["tool"] == "job_detail_read" and s["status"] == "completed"]
        linked = [fixture.ids.index(s["result"]["job_id"])+1 for s in link_steps]
        unexpected = [s["tool"] for s in plan["steps"] if s["tool"] not in {"job_target_resolve", "job_detail_read", "job_remove_selection", "mock_delivery_start"}]
        checks = {"removed_exactly": sorted(removed) == case["remove"], "queued_exactly": sorted(queued) == case["queue"],
            "linked_exactly": sorted(linked) == case["links"], "no_extra_operations": not unexpected,
            "links_in_final_answer": all(fixture.jobs.get_job(fixture.ids[i-1])["source_url"] in response.message for i in case["links"]),
            "no_duplicate_queue": len(queued) == len(set(queued))}
        if case.get("clarify"):
            checks["asks_without_effect"] = bool(plan.get("questions") or any(s["status"] == "waiting_input" for s in plan["steps"]))
        else:
            checks["all_steps_completed"] = all(s["status"] == "completed" for s in plan["steps"])
            selection = next(e["data"] for e in fixture.events if e["kind"] == "plan_response_selection")
            checks["no_background_duplicate"] = completion_steps(plan, [{"kind": "response", "data": selection}]) == []
        return {"passed": all(checks.values()), "checks": checks, "plan": plan, "reply": response.message,
                "final": {"removed_positions": removed, "queued_positions": queued, "linked_positions": linked}, "events": fixture.events}
    except Exception as exc:
        return {"passed": False, "error_type": type(exc).__name__, "error": str(exc), "events": fixture.events}
    finally:
        await fixture.asyncTearDown()
        fixture = None
        gc.collect()


async def main(args):
    if not (args.models_consented or args.replay) or not 1 <= args.model_call_limit <= 10:
        raise ValueError("Synthetic-model consent and physical request budget 1..10 required")
    traces, rows = [], []
    if args.replay:
        recorded = [json.loads(line) for line in (args.replay/"model-calls.jsonl").read_text(encoding="utf-8").splitlines()]
        replies = {r["case_id"]: r["response"]["choices"][0]["message"]["content"] for r in recorded if r.get("response")}
        class Replay:
            calls, model = 0, "saved-synthetic-response"
            async def generate(self, request):
                self.calls += 1
                return LLMResult(replies[active_run().run_id], "replay", self.model)
        provider = Replay()
    else:
        provider = consented_provider(args.model_call_limit, traces)
    cases = [case for case in CASES if not args.case or case["id"] in args.case][:args.model_call_limit]
    if args.replay:
        recorded_inputs = {}
        for entry in recorded:
            for message in entry["request"]["messages"]:
                if message["role"] == "user":
                    try:
                        wrapper = re.search(r"<user_request>\s*([\s\S]*?)\s*</user_request>", message["content"])
                        recorded_inputs[entry["case_id"]] = json.loads(wrapper[1] if wrapper else message["content"])["dialogue"]
                    except (ValueError, KeyError):
                        pass
        cases = [{**case, "replay_dialogue": recorded_inputs["synthetic-"+case["id"]]} for case in cases]
    output = Path(__file__).parent / "results" / "reference-model-flow" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    try:
        for case in cases:
            started = provider.calls
            row = {"id": case["id"], "input": case["message"], **await evaluate(case, provider),
                   "physical_calls": 0 if args.replay else provider.calls-started,
                   "replayed_calls": provider.calls-started if args.replay else 0}
            rows.append(row)
            (output/"cases.jsonl").write_text("".join(json.dumps(r,ensure_ascii=False,default=str)+"\n" for r in rows),encoding="utf-8")
            (output/"model-calls.jsonl").write_text("".join(json.dumps(r,ensure_ascii=False,default=str)+"\n" for r in traces),encoding="utf-8")
            print(json.dumps({k:row[k] for k in ("id","passed","physical_calls")},ensure_ascii=False),flush=True)
    finally:
        if not args.replay:
            await provider.client.close()
    summary = {"cases": len(rows), "passed": sum(r["passed"] for r in rows), "physical_calls": 0 if args.replay else provider.calls,
        "replayed_calls": provider.calls if args.replay else 0,
        "model": provider.model, "real_browser_actions": 0,
        "input_tokens": sum(t.get("response",{}).get("usage",{}).get("prompt_tokens",0) for t in traces),
        "output_tokens": sum(t.get("response",{}).get("usage",{}).get("completion_tokens",0) for t in traces),
        "coverage": "saved-response replay + final execution state, no new model understanding measured" if args.replay else
            "first-pass real planning model + real registry/DAG/temp SQLite/final reply oracle; not recruitment-browser E2E"}
    (output/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"output":str(output),**summary},ensure_ascii=False),flush=True)
    return int(summary["passed"] != summary["cases"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--models-consented", action="store_true")
    parser.add_argument("--model-call-limit", type=int, default=8)
    parser.add_argument("--case", nargs="+")
    parser.add_argument("--replay", type=Path)
    raise SystemExit(asyncio.run(main(parser.parse_args())))
