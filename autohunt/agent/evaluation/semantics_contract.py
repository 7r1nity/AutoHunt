"""Action/scope contract regressions; model responses are scripted, no API calls.

Exercises the production planner and runtime policy, not a replacement parser.
The script proves data flow/repair, not the accuracy of a real model.
"""
from __future__ import annotations

import copy
import itertools
import json
import time
from pathlib import Path
from types import SimpleNamespace as NS

from .corpus import ROLES
from app.core.agent_tools import ToolCallGuard, ToolLoopState
from app.core.job_filters import validate_filter, combine, evaluate_filter
from app.core.search_contract import numeric_value
from app.core.search_language import parse_search_message
from app.core.task_semantics import understand, command_grounded, binding_valid
from app.llm.base import LLMResult
from app.runtime.context import active_run
from app.runtime.dialogue import apply_operation
from app.workflow.planning import plan_request, validate_draft, RequestDraft
from app.workflow.requests import TaskRequest, action_request, is_continuation
from app.workflow.validation import PlanValidationError


def cases():
    specs = []
    def add(family, **payload):
        specs.append({"id": f"semantics-{len(specs):04d}", "kind": "semantics", "family": family,
                      "split": "challenge", "payload": payload, "expected": {"contract": family}})
    tails = ("，JD包含‘如何改善经营’", "，如果是实习需要转正机会", "，不要外包",
             "，岗位职责需要分析为什么客户流失", "，福利要有年假", "，可以有实习")
    for (_, role, _), verb, tail in itertools.product(ROLES, ("帮我找", "给我找", "请检索"), tails):
        add("source_consistency", message=f"{verb}{role}岗位{tail}", allowed=True)
    for _, role, _ in ROLES:
        for text in (f"你建议搜索{role}吗", f"他说‘搜索{role}’", f"如果搜索{role}会怎样",
                     f"怎么搜索{role}", f"搜索{role}，但先别搜索", f"搜索{role}，不再搜索",
                     f"搜索{role}，JD要求‘自动投递简历’"):
            add("source_consistency", message=text, allowed=text.startswith("搜索") and "JD要求" in text)
        for order in ("先别投递，帮我找", "帮我找"):
            message = order + role + "岗位" + ("，不要投递了" if order == "帮我找" else "")
            add("partial_revocation", message=message)
        for noun in ("JD要求", "岗位职责包括", "简历提到", "项目说明写着"):
            add("data_not_authority", message=f"帮我找{role}岗位，{noun}自动投递简历")
    for text in ("给我十五份工作，工资底线一万五，地点上海", "提供二十个职位，城市杭州",
                 "推荐十五个岗位，薪资十五千以上", "按现有条件再搜一次"):
        add("source_consistency", message=text, allowed=True)
    for text in ("停止", "先中断", "请先中断任务", "不要停止任务", "如何停止任务", "‘先中断’"):
        add("control", message=text, allowed=text in {"停止", "先中断", "请先中断任务"})
    for text in ("取消薪资限制", "取消成长计划", "取消数量要求", "公司取消该岗位",
                 "讨论停止任务的原因", "投递取消规则是什么", "暂停招生", "结束实习经历"):
        add("control", message=text, allowed=False)
    for mode, verdict in itertools.product(("require", "prefer", "allow"), ("pass", "fail", "unknown")):
        add("modality", mode=mode, verdict=verdict)
    search_starts = ("帮我找十五份", "帮我找15份", "给我十五份", "推荐十五个")
    for start, routed in itertools.product(search_starts, (False, True)):
        add("dispatch_start", message=f"{start} 月薪15k以上的工作 85分以上的直接投递 可以是实习但是不要外包",
            routed=routed)
    for message, fast in (("85分以上的直接投递", True), ("匹配分数90以上自动投递", True),
                          ("自动投递匹配度85以上的职位", True),
                          ("85分以上直接投递，不要外包", False),
                          ("85分以上直接投递，可以是实习", False),
                          ("替我筛选十五个岗位，85分以上直接投递", False)):
        add("delivery_shortcut", message=message, fast=fast)
    for scenario in ("quoted_value", "optional", "prefer", "require", "wrong_citation_repair",
                     "retained_root", "revoked_plan", "tampered_binding", "runtime_no_reparse",
                     "unknown_fallback", "partial_dispatch", "partial_failed_dispatch", "amend_numeric",
                     "ungranted_write", "ungranted_question", "real_site", "soft_any", "soft_not",
                     "hard_unknown", "rank_not_claimed", "unknown_scope", "missing_questions_preserved",
                     "employer_eligibility", "unclosed_quote", "fresh_command_not_retention"):
        add("pipeline", scenario=scenario)
    data = json.loads((Path(__file__).parent / "corpus/numeral_cases.json").read_text(encoding="utf-8"))
    for case in data["cases"]:
        add("numeral", text=case["text"], want=case["want"])
    for case in data["invalid"]:
        add("numeral_invalid", text=case["text"])
    return specs


class ScriptedModel:
    def __init__(self, *drafts):
        self.drafts = list(drafts)
        self.requests = []

    async def generate(self, request):
        self.requests.append(json.loads(request.user_message))
        if not self.drafts:
            raise AssertionError("unexpected additional model call")
        return LLMResult(json.dumps(self.drafts.pop(0), ensure_ascii=False), "scripted", "contract")


def draft(message, steps, *, continuation=False):
    return {"continuation": continuation,
            "goals": [{"quote": message, "disposition": "steps", "step_ids": [step["id"] for step in steps]}],
            "steps": steps, "questions": [], "reply": ""}


def step(tool="job_search_start", quote="帮我找工作", **args):
    return {"id": "search" if tool == "job_search_start" else "learning", "tool": tool,
            "arguments": args, "command_quote": quote, "scope_quote": "", "depends_on": [], "missing_slots": []}


def pending(original, **changes):
    return {"id": "synthetic-root", "original": original, "answers": [], "revoked_tools": [],
            "questions": [], "steps": [], "status": "clarifying", "resume_stamp": "",
            "turn": 1, "expires_at": time.time() + 600, **changes}


async def evaluate(case, registry):
    p = case["payload"]
    family = case["family"]
    if family == "source_consistency":
        message = p["message"]
        values = {"parser": parse_search_message(message).authorized,
                  "grounded": command_grounded("job_search_start", message, [message]),
                  "call_guard": ToolCallGuard().check(registry._tools["job_search_start"], {}, ToolLoopState())[0]}
        assert values["parser"] == values["grounded"] == p["allowed"] and values["call_guard"], values
        return values
    if family in {"partial_revocation", "data_not_authority"}:
        meaning = understand(p["message"])
        assert meaning.commands("job_search_start")
        assert not meaning.commands("mock_delivery_start"), meaning.public()
        return meaning.public()
    if family == "control":
        allowed = command_grounded("browser_task_stop", p["message"], [p["message"]])
        assert allowed == p["allowed"], understand(p["message"]).public()
        if allowed:
            assert parse_search_message(p["message"]).action == "stop"
        return {"allowed": allowed}
    if family == "numeral":
        value = numeric_value(p["text"])
        assert value == p["want"], value
        return {"value": value}
    if family == "numeral_invalid":
        try:
            numeric_value(p["text"])
        except ValueError:
            return {"rejected": True}
        raise AssertionError("invalid numeral accepted")
    if family == "modality":
        quote = {"require": "必须有双休", "prefer": "优先双休", "allow": "可以有实习"}[p["mode"]]
        tree = validate_filter({"op": "condition", "text": quote, "quote": quote, "mode": p["mode"]}, [quote])
        result = combine(tree, iter([p["verdict"]]))
        assert result == (p["verdict"] if p["mode"] == "require" else "pass"), result
        return {"status": result}
    if family == "dispatch_start":
        return await dispatch_start(p["message"], p["routed"], registry)
    if family == "delivery_shortcut":
        from app.core.action_language import delivery_shortcut
        result = delivery_shortcut(p["message"])
        assert bool(result) == p["fast"], result
        return {"shortcut": result}
    return await pipeline(p["scenario"], registry)


async def dispatch_start(message, routed, registry):
    """Exercise the real foreground entry before any account exists.

    Stub only the planner/model and browser side effect: actual validation and
    DAG execution must issue search, not account-binding delivery preflight.
    """
    from app.core.main_agent import AutoHuntAgent
    from app.core.agent_tools import AgentToolOutcome
    from app.workflow.execution import begin, advance
    from app.growth.requests import resume_stamp
    from app.intent.router import IntentRouter
    from app.intent.rules import RuleEngine
    context = active_run()
    context.dialogue = {"turn": 1}
    context.search_state = {"queries": ["金融分析师"], "source": "mock"}
    workspace = NS(read_document=lambda: NS(id="synthetic", version=1))
    candidate = NS(read=lambda: NS(profile=NS(resume_id="synthetic")))
    stamp = resume_stamp(workspace.read_document(), candidate)
    calls, plans, routes = [], [], []
    account = {"account_bound": False}
    async def execute(name, arguments, *, user_message, state):
        allowed, reason = ToolCallGuard().check(registry._tools[name], arguments, state)
        assert allowed, reason
        calls.append(name)
        if name == "job_search_start":
            return AgentToolOutcome(name, "ok", {"id": "synthetic-search", "status": "awaiting_login",
                "query": "金融分析师", "detail": "浏览器已打开，等待登录验证"}, "external")
        if not account["account_bound"]:
            return AgentToolOutcome(name, "error", "请先在模拟站登录求职者账号并完成一次搜索，以绑定账号", "external")
        assert context.action_authority["evidence_task_id"] == "synthetic-search"
        return AgentToolOutcome(name, "ok", {"message": "已加入模拟队列", "queued": 1}, "external")
    local = NS(_tools=registry._tools, schemas=registry.schemas, validate_arguments=registry.validate_arguments,
               resume_workspace=workspace, candidate_state=candidate, mock_workflow=NS(), execute=execute)
    agent = object.__new__(AutoHuntAgent)
    agent.tool_registry, agent.resume_workspace, agent.candidate_state = local, workspace, candidate
    agent.router = NS(rule_engine=RuleEngine()) if routed else None
    agent._conversation_history = []
    filters = {"op": "all", "children": [
        {"op": "condition", "text": "可以是实习", "quote": "可以是实习", "mode": "allow"},
        {"op": "condition", "text": "不要外包", "quote": "不要外包", "mode": "require"}]}
    data = draft(message, [step(quote=message.split("85分以上")[0].strip(),
        result_limit=15, salary_min=15000, filters=filters),
        {**step("mock_delivery_start", "85分以上的直接投递", min_score=85), "id": "delivery"}])
    data["steps"][0]["slot_evidence"] = {"result_limit": message.split(" 月薪")[0]}
    async def handle(message, *, previous=None):
        model = ScriptedModel(data)
        plan = await plan_request(model, local, message, dialogue=context.dialogue,
            conditions=context.search_state, stamp=stamp)
        plans.append(plan)
        return await begin(plan, local)
    async def route(message):
        # Use the actual router's compound decision; no classifier/model API.
        router = object.__new__(IntentRouter)
        router.rule_engine = agent.router.rule_engine
        router._last_decision = {}
        router._scoped_hash = lambda _: "synthetic"
        router._record = lambda *_: None
        outcome = await router.route(message)
        assert outcome.intent == "compound_request" and outcome.decision == "llm", outcome
        routes.append(outcome.layer)
        return await handle(message)
    agent._handle_planned, agent._handle_routed = handle, route
    reply = await agent._dispatch_contextual_message(message, None)
    assert calls == ["job_search_start"], {"calls": calls, "reply": reply.message}
    plan = plans[0]
    assert not plan["questions"] and plan["steps"][0]["status"] == "waiting_external"
    assert plan["steps"][1]["status"] == "pending" and not account["account_bound"]
    # Opening / login verification is not search completion or account binding.
    await advance(plan, local)
    assert calls == ["job_search_start"]
    account["account_bound"] = True
    plan["steps"][0]["status"] = "completed"  # Confirmed synthetic search completion.
    await advance(plan, local)
    await advance(plan, local)
    assert calls == ["job_search_start", "mock_delivery_start"], calls
    assert plan["steps"][1]["status"] == "completed", plan
    return {"calls": calls, "routes": routes, "login_waited": True,
            "result_limit": plan["steps"][0]["arguments"]["result_limit"],
            "salary_min": plan["steps"][0]["arguments"]["salary_min"]}


async def pipeline(scenario, registry):
    conditions = {"queries": ["金融分析师"], "source": "mock"}
    context = active_run()
    if scenario == "missing_questions_preserved":
        from app.workflow.requests import stale_questions
        questions = ["请补充最低月薪", "请补充目标数量"]
        assert stale_questions(questions, None) == questions
        assert stale_questions(questions, None, {"salary_min": 15000}) == [questions[1]]
        return {"preserved": True}
    if scenario == "employer_eligibility":
        from app.core.task_semantics import condition_mode
        for quote in ("公司接受应届生", "职位学历不限", "要求有实习转正机会"):
            assert condition_mode(quote) == "require"
            try:
                validate_filter({"op": "condition", "text": quote, "quote": quote, "mode": "allow"}, [quote])
            except ValueError:
                continue
            raise AssertionError("employer requirement weakened")
        return {"hard_preserved": True}
    if scenario == "unclosed_quote":
        assert not command_grounded("job_search_start", "搜索会计", ['他说“搜索会计'])
        return {"quoted_data": True}
    if scenario == "fresh_command_not_retention":
        assert not is_continuation("帮我找工作，只保留搜索和成长计划")
        assert action_request("帮我找工作，只保留搜索和成长计划")
        return {"new_task": True}
    if scenario in {"soft_any", "soft_not", "hard_unknown", "rank_not_claimed"}:
        hard = {"op": "condition", "text": "必须双休", "quote": "必须双休"}
        soft = {"op": "condition", "text": "优先实习", "quote": "优先实习", "mode": "prefer"}
        if scenario == "soft_any":
            tree = validate_filter({"op": "any", "children": [hard, soft]})
            assert combine(tree, iter(["fail", "pass"])) == "fail"
        elif scenario == "soft_not":
            tree = validate_filter({"op": "not", "children": [soft]})
            assert combine(tree, iter(["pass"])) == "pass"
        elif scenario == "hard_unknown":
            tree = validate_filter({"op": "all", "children": [hard, soft]})
            assert combine(tree, iter(["unknown", "pass"])) == "unknown"
        else:
            model = ScriptedModel({"conditions": [{"index": 0, "status": "unknown", "reason": "未提及", "evidence": []}]})
            result = await evaluate_filter(model.generate, soft, {"description": "金融分析师"})
            assert result["status"] == "pass" and not result["ranking_applied"]
            assert result["preferences"][0]["status"] == "unknown"
        return {"contract": scenario}
    if scenario == "unknown_fallback":
        message = "希望下一份工作更贴近我的职业发展"
        model = ScriptedModel({"continuation": False, "goals": [{"quote": message, "disposition": "answer", "step_ids": []}],
                               "steps": [], "questions": [], "reply": "请说明希望发展的方向。"})
        result = await plan_request(model, registry, message, dialogue={}, conditions=conditions, stamp="")
        assert len(model.requests) == 1 and not result["steps"]
        return {"calls": 1, "status": result["status"]}
    if scenario in {"retained_root", "partial_dispatch", "partial_failed_dispatch", "amend_numeric"}:
        original = "帮我找15份工作，地点深圳 杭州，薪资15k以上，85分以上自动投递，85以下且薪资25k以上生成成长计划"
        message = "不要投递了，只保留搜索和成长计划" if scenario != "amend_numeric" else "只保留搜索和成长计划，数量改为20个"
        q = step(quote="只保留搜索", result_limit=15, salary_min=15000, locations=["深圳", "杭州"])
        learning = step("learning_plan_generate", "只保留搜索和成长计划", max_score=84, salary_min=25000)
        data = draft(original, [q, learning], continuation=True)
        model = ScriptedModel(data)
        prev = pending(original)
        assert is_continuation(message) and not action_request(message)
        if scenario in {"partial_dispatch", "partial_failed_dispatch"}:
            from app.core.main_agent import AutoHuntAgent
            agent = object.__new__(AutoHuntAgent)
            agent.resume_workspace = None
            agent.candidate_state = None
            agent.tool_registry = registry
            agent._conversation_history = []
            agent.router = None
            context.dialogue = {"turn": 1, "task_request": TaskRequest(original=original, status="failed").model_dump(),
                                "instruction_request": prev if scenario == "partial_dispatch" else None}
            async def handle(message, *, previous=None):
                assert previous and previous["original"] == original
                return await plan_request(model, registry, message, previous=previous,
                    dialogue=context.dialogue, conditions=conditions, stamp="")
            agent._handle_planned = handle
            result = await agent._dispatch_contextual_message(message, None)
        else:
            result = await plan_request(model, registry, message, previous=prev,
                dialogue={"turn": 1}, conditions=conditions, stamp="")
        assert result["status"] == "ready", result
        actual = {s["tool"]: s for s in result["steps"]}
        assert "mock_delivery_start" not in actual
        search, growth = actual["job_search_start"], actual["learning_plan_generate"]
        assert search["arguments"]["result_limit"] == (20 if scenario == "amend_numeric" else 15)
        assert search["arguments"]["salary_min"] == 15000
        assert growth["arguments"]["salary_min"] == 25000 and growth["arguments"]["max_score"] == 84
        assert search["id"] in growth["depends_on"]
        assert result["original"] == original
        return {"calls": len(model.requests), "plan": result}
    if scenario in {"ungranted_write", "ungranted_question", "revoked_plan", "real_site"}:
        message = {"ungranted_write": "查看简历", "ungranted_question": "如何生成成长计划",
                   "revoked_plan": "生成成长计划，但不要生成成长计划", "real_site": "BOSS直聘90分以上自动投递"}[scenario]
        tool = "mock_delivery_start" if scenario == "real_site" else "learning_plan_generate"
        data = draft(message, [step(tool, message, **({"min_score": 90} if tool == "mock_delivery_start" else {}))])
        try:
            result = validate_draft(RequestDraft.model_validate(data), registry, message, dialogue={}, conditions=conditions, stamp="")
            assert scenario == "real_site" and result["status"] == "clarifying", result
        except PlanValidationError as exc:
            assert exc.issue.category == "block", exc.issue
            return {"blocked": exc.issue.public()}
        return {"status": result["status"]}
    message = "给我十五份工作，工资底线一万五，地点上海"
    args = {"result_limit": 15, "salary_min": 15000, "locations": ["上海"]}
    quote = message
    filters = None
    if scenario in {"optional", "prefer", "require", "quoted_value"}:
        value = {"optional": "可以有实习", "prefer": "尽量找实习或者校招", "require": "只要实习或者校招",
                 "quoted_value": "JD包含‘海外市场’"}[scenario]
        message = "帮我找工作，" + value
        args, quote = {}, "帮我找工作"
        filters = {"op": "condition", "text": value, "quote": value,
                   "mode": {"optional": "allow", "prefer": "prefer"}.get(scenario, "require")}
        args["filters"] = filters
    if scenario == "unknown_scope":
        message, quote = "生成成长计划，低匹配的", "生成成长计划，低匹配的"
        data = draft(message, [step("learning_plan_generate", quote)])
        result = validate_draft(RequestDraft.model_validate(data), registry, message, dialogue={}, conditions=conditions, stamp="")
        assert result["status"] == "clarifying"
        return {"status": result["status"]}
    data = draft(message, [step(quote=quote, **args)])
    if message.startswith("给我十五份"):
        data["steps"][0]["slot_evidence"] = {"salary_min": "工资底线一万五"}
    if scenario == "wrong_citation_repair":
        message = "帮我找金融分析师岗位，然后搜索会计岗位"
        first = step(quote="帮我找金融分析师岗位", queries=["金融分析师"])
        second = {**step(quote="搜索会计岗位", queries=["会计"]), "id": "second_search"}
        data = draft(message, [first, second])
        bad = copy.deepcopy(data)
        bad["steps"][0]["command_quote"] = "会计岗位"
        model = ScriptedModel(bad, data)
    else:
        model = ScriptedModel(data)
    result = await plan_request(model, registry, message, dialogue={}, conditions=conditions, stamp="")
    assert result["status"] == "ready", result
    assert len(model.requests) == (2 if scenario == "wrong_citation_repair" else 1)
    action = result["steps"][0]
    binding = action["authority_binding"]
    assert binding_valid(binding, action["tool"], [result["original"], *result["answers"]])
    if scenario in {"tampered_binding", "runtime_no_reparse"}:
        from app.workflow.execution import persist
        from app.core import agent_tools
        context.dialogue = {}
        persist(result)
        workspace = NS(read_document=lambda: NS(id="synthetic", version=1))
        candidate = NS(read=lambda: NS(profile=NS(resume_id="synthetic")))
        result["resume_stamp"] = "synthetic:synthetic:1"
        persist(result)
        authority = {"tool": action["tool"], "arguments": copy.deepcopy(action["arguments"]),
                     "command": action["command_quote"], "sources": [message], "resume_stamp": result["resume_stamp"], "expires_at": result["expires_at"],
                     "candidate": candidate, "resumes": workspace, "plan_id": result["id"], "step_id": action["id"], "binding": copy.deepcopy(binding)}
        if scenario == "tampered_binding":
            authority["binding"]["source_digest"] = "tampered"
        context.action_authority = authority
        old = getattr(agent_tools, "command_grounded", None)
        def forbidden(*args):
            raise AssertionError("runtime reinterpreted natural language")
        agent_tools.command_grounded = forbidden
        try:
            allowed, reason = ToolCallGuard().check(registry._tools[action["tool"]], action["arguments"], ToolLoopState())
            assert allowed, reason  # Audit binding is no longer an execution gate.
        finally:
            agent_tools.command_grounded = old
            context.action_authority = None
        return {"allowed": allowed}
    return {"calls": len(model.requests), "plan": result}
