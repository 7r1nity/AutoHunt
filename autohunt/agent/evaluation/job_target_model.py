"""Consented synthetic model checks; planning/resolution only, never execution.

python -m evaluation.job_target_model --models-consented --model-call-limit 8
Each case gets at most one physical call. Planner repair cannot use another case's
budget. Configuration/credentials are read through the established eval provider;
no user resume, history, database or browser is loaded.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS

from app.core.agent_tools import AgentToolRegistry
from app.core.job_targets import TargetClarification, resolve_job_targets, is_remaining_selector
from app.jobs.store import JobStore
from app.llm.base import LLMResult
from app.runtime.context import RunContext, current_run
from app.runtime.dialogue import apply_operation
from app.workflow.planning import plan_request
from app.workflow.revision import RevisionDraft, edited_plan
from .goal_graph_contract import STAMP
from .run import ModelBudgetExceeded, consented_provider


def fixtures():
    facts = {
        101: dict(id=101, title="NLP算法工程师", company="合成甲科技", location="杭州", salary="20-30K",
                  tags=["RAG", "评测"], description="正式岗位：开发RAG知识库检索与离线评测，维护问答服务。"),
        102: dict(id=102, title="NLP算法工程师", company="合成乙科技", location="上海", salary="25-35K",
                  tags=["SFT"], description="正式岗位：负责模型微调、训练数据清洗和推理优化。"),
        103: dict(id=103, title="信用风险分析师", company="合成丙金融", location="深圳", salary="18-26K",
                  tags=["SQL", "风险建模"], description="正式岗位：负责贷后预警、逾期迁移率监控和信用风险模型验证。"),
        104: dict(id=104, title="海外销售", company="合成丁贸易", location="武汉", salary="面议",
                  tags=["商务英语", "客户开发"], description="正式岗位：负责海外客户开发、询盘报价与出口交付。"),
    }
    store = NS(get_job=lambda ident: facts[ident], review_fingerprint=JobStore.review_fingerprint)
    entries = [{**{k: row[k] for k in ("id", "title", "company")},
                "content_hash": JobStore.review_fingerprint(row)} for row in facts.values()]
    dialogue = apply_operation({}, dict(op="snapshot", topic="applications", kind="job", source="synthetic",
                                        version="synthetic-search", displayed=True, items=entries))
    return store, dialogue


def old_plan(*, searching=False):
    return dict(id="synthetic-old-plan", session_id="default", resume_stamp=STAMP, revision=0,
                status="running" if searching else "clarifying", original="找工作" if searching else "移除这些，其余投递",
                questions=[] if searching else ["这些具体指哪几个岗位？"], goals=[],
                turn=0, expires_at=time.time()+1800, answers=[], steps=[dict(
                    id="old-search", tool="job_search_start", arguments=dict(queries=["NLP算法工程师"], source="mock"),
                    status="waiting_external", depends_on=[], effect="external", user_requested=True,
                    command_quote="找工作", scope_quote="", slot_evidence={}, missing_slots=[])] if searching else [])


CASES = [
    dict(id="description_city_role", kind="target", selector="杭州做知识库检索和评测的那个", ids=[101]),
    dict(id="cross_industry_description", kind="target", selector="负责贷后预警和逾期监控的岗位", ids=[103]),
    dict(id="ambiguous_single", kind="target", selector="那个NLP算法岗位", clarify=True),
    dict(id="deictic_subset", kind="target", selector="这些", ids=[101, 103], history=[
        dict(role="assistant", content="需核验的两个是：合成甲科技的杭州NLP算法工程师，以及合成丙金融的深圳信用风险分析师。其他两个不在本次范围。"),
        dict(role="user", content="把这些移除")]),
    dict(id="single_delivery_ignores_old_question", kind="plan", message="先不管之前的移除问题，单独投递第2个",
         old=True, tools=["mock_delivery_start"], target="第2个", independent=True),
    dict(id="semantic_delivery_plan", kind="plan", message="投递杭州做RAG检索的那个岗位",
         tools=["mock_delivery_start"], semantic=True),
    dict(id="confirm_remove_then_remainder", kind="plan", message="确认移除第2个，其余自动投递",
         old=True, tools=["job_remove_selection", "mock_delivery_start"], removal=True),
    dict(id="append_semantic_delivery_during_search", kind="plan", message="另外把已展示列表里负责海外客户开发的那个岗位单独投递，不用等这次搜索",
         old=True, searching=True, tools=["mock_delivery_start"], semantic=True, independent=True),
]


async def evaluate(case, provider):
    jobs, dialogue = fixtures()
    events = []
    previous = old_plan(searching=case.get("searching", False)) if case.get("old") else None
    context = RunContext(case["id"], asyncio.current_task(),
        lambda kind, data: events.append(dict(kind=kind, data=copy.deepcopy(data))), lambda _: None,
        dialogue=dialogue, search_state=dict(queries=["NLP算法工程师"], source="mock"),
        plan_reader=(lambda: previous), user_message=case.get("message", case.get("selector", "")))
    token = current_run.set(context)
    try:
        if case["kind"] == "target":
            try:
                result = await resolve_job_targets(case["selector"], dialogue=dialogue, jobs=jobs,
                    generate=provider.generate, history=case.get("history", ()))
                actual = dict(ids=[item["id"] for item in result["items"]], resolution=result["resolution"])
                checks = dict(correct_ids=actual["ids"] == case.get("ids"), no_wrong_single=not case.get("clarify"))
            except TargetClarification as exc:
                actual = dict(clarification=str(exc), candidates=[item["id"] for item in exc.candidates])
                # A timeout/invalid JSON is NOT evidence of correct ambiguity handling.
                checks = dict(correct_ambiguity=bool(case.get("clarify") and set(actual["candidates"]) == {101, 102}))
            return dict(checks=checks, actual=actual, events=events)

        registry = AgentToolRegistry(jobs=jobs, browser=NS(), memory=None, mock_workflow=NS(),
            candidate_state=NS(read=lambda: NS(profile=NS(resume_id="synthetic-resume"))),
            resume_workspace=NS(read_document=lambda: NS(id="synthetic-document", version=1)))
        result = await plan_request(provider, registry, case["message"], dialogue=dialogue,
                                    conditions=context.search_state, stamp=STAMP)
        if result.get("operation") == "revise":
            result = edited_plan(previous, RevisionDraft.model_validate(result["revision"]), registry,
                                 stamp=STAMP, session_id="default", message=case["message"])
        steps = [s for s in result.get("steps", []) if s["id"] != "old-search"]
        by_tool = {s["tool"]: s for s in steps}
        checks = dict(tools_present=set(case["tools"]) <= by_tool.keys(),
                      no_extra_effects=all(s["tool"] in case["tools"] for s in steps),
                      executable=bool(steps) and not result.get("questions") and
                          all(s["status"] == "pending" and not s.get("missing_slots") for s in steps))
        delivery = by_tool.get("mock_delivery_start", {})
        args = delivery.get("arguments", {})
        if case.get("target"):
            checks["correct_target"] = args.get("selector") == case["target"]
        if case.get("semantic"):
            checks["semantic_target_retained"] = bool(args.get("selector")) and args["selector"] not in {"全部已展示", "这些", "剩余岗位"}
            checks["no_old_filters"] = not args.get("filters")
        if case.get("independent"):
            checks["independent_of_old_search"] = "old-search" not in delivery.get("depends_on", [])
        if case.get("removal"):
            removal = by_tool.get("job_remove_selection", {})
            checks["remove_correct_target"] = removal.get("arguments", {}).get("selector") == "第2个"
            checks["remainder_target"] = is_remaining_selector(args.get("selector", ""))
            checks["remove_before_delivery"] = bool(removal.get("id")) and removal["id"] in delivery.get("depends_on", [])
        return dict(checks=checks, actual=result, events=events)
    except Exception as exc:
        return dict(error_type=type(exc).__name__, error=str(exc), checks={"completed": False}, events=events)
    finally:
        current_run.reset(token)


async def main(args):
    if not (args.models_consented or args.replay) or not 1 <= args.model_call_limit <= 8:
        raise ValueError("Explicit synthetic-model consent and budget 1..8 required")
    traces, rows = [], []
    if args.replay:
        saved = [json.loads(line) for line in (args.replay / "model-traces.jsonl").read_text(encoding="utf-8").splitlines()]
        replies = {entry["case_id"]: entry["response"]["choices"][0]["message"]["content"] for entry in saved if entry.get("response")}
        class ReplayProvider:
            calls, model = 0, "saved-synthetic-deepseek-response"
            async def generate(self, request):
                from app.runtime.context import active_run
                ident = active_run().run_id
                self.calls += 1
                traces.append(dict(case_id=ident, replayed=True, response_text=replies[ident]))
                return LLMResult(replies[ident], "replay", self.model)
        provider = ReplayProvider()
    else:
        provider = consented_provider(args.model_call_limit, traces)
    output = Path(__file__).parent / "results" / "job-target-model" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    try:
        for case in CASES[:args.model_call_limit]:
            start_calls = provider.calls
            async def generate(request):
                if provider.calls > start_calls:
                    raise ModelBudgetExceeded("First-pass evaluation: no second physical call for this case")
                return await provider.generate(request)
            started = time.perf_counter()
            try:
                result = await evaluate(case, NS(generate=generate))
                passed = all(result["checks"].values())
            except Exception as exc:
                result = dict(error_type=type(exc).__name__, error=str(exc), checks={"completed": False})
                passed = False
            row = dict(id=case["id"], kind=case["kind"], passed=passed, **result,
                       physical_calls=0 if args.replay else provider.calls-start_calls,
                       replayed_calls=provider.calls-start_calls if args.replay else 0,
                       latency_ms=round((time.perf_counter()-started)*1000, 2))
            rows.append(row)
            (output / "cases.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False, default=str)+"\n" for r in rows), encoding="utf-8")
            (output / "model-traces.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False, default=str)+"\n" for r in traces), encoding="utf-8")
            print(json.dumps({k: row[k] for k in ("id", "passed", "checks", "physical_calls", "latency_ms")}, ensure_ascii=False), flush=True)
    finally:
        if not args.replay:
            await provider.client.close()
    summary = dict(cases=len(rows), passed=sum(r["passed"] for r in rows), failed=sum(not r["passed"] for r in rows),
                   physical_calls=0 if args.replay else provider.calls,
                   replayed_calls=provider.calls if args.replay else 0,
                   real_browser_actions=0, model=provider.model,
                   input_tokens=sum(t.get("response", {}).get("usage", {}).get("prompt_tokens", 0) for t in traces),
                   output_tokens=sum(t.get("response", {}).get("usage", {}).get("completion_tokens", 0) for t in traces),
                   coverage="saved-output replay; does not validate changed prompts" if args.replay else
                       "first-pass real model + production resolver/planner/validator; no tool effects or browser E2E")
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(dict(output=str(output), **summary), ensure_ascii=False), flush=True)
    return 0 if not summary["failed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--models-consented", action="store_true")
    mode.add_argument("--replay", type=Path)
    parser.add_argument("--model-call-limit", type=int, required=True)
    raise SystemExit(asyncio.run(main(parser.parse_args())))
