"""Read-only semantic review of new scope, never action authorization.

The model may correct only the proposed condition fields. Unchanged revision
fields are inherited by the existing compiler, not reconstructed here.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field
from typing import Literal, Any

from ..llm.base import LLMRequest
from ..runtime.context import emit
from ..core.filter_policy import project_arguments, project_text, POLICY_INSTRUCTIONS, FilterPolicyRepair

SCOPE_FIELDS = frozenset({"filters", "result_groups", "salary_min", "min_score", "max_score", "scope_mode"})
SCOPE_TOOLS = frozenset({"job_search_start", "search_state_update", "mock_delivery_start"})


def salary_examples(arguments):
    """Expose arithmetic consequences to the semantic reviewer, not user rules."""
    from ..core.salary_conditions import SalaryPredicate,judge_salary
    examples=[]
    def visit(value,path):
        if isinstance(value,dict):
            if value.get("salary") and len(examples)<16:
                predicate=SalaryPredicate.model_validate(value["salary"])
                texts=["面议"]
                bound=predicate.minimum if predicate.minimum is not None else predicate.maximum
                if bound is not None:
                    suffix="元/天" if predicate.unit=="daily" else "元/月"
                    texts += [str(bound)+suffix,f"{max(0,bound-1000)}-{bound+1000}"+suffix]
                examples.append({"path":path,"criterion":value.get("text"),"predicate":predicate.model_dump(),
                    "acceptance":[{"salary":text,"status":judge_salary(predicate,{"salary":text,"employment_kind":"internship" if predicate.applies_to=="internship" else "regular"})["status"]} for text in texts]})
            for key,child in value.items(): visit(child,path+"."+key)
        elif isinstance(value,list):
            for index,child in enumerate(value): visit(child,path+f"[{index}]")
    visit(arguments,"arguments")
    return examples


class ScopeVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step_id: str
    status: Literal["pass", "correct", "clarify"]
    changes: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(max_length=1000)
    question: str = Field(default="", max_length=1000)


class ScopeReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scopes: list[ScopeVerdict] = Field(max_length=32)


def proposals(draft, live_plan):
    def context_scope(arguments):
        try:
            return project_arguments(arguments)
        except ValueError:
            return copy.deepcopy(arguments)  # Old mixed data remains model repair context.
    previous = {s["id"]:context_scope(s.get("staged_arguments") or s["arguments"])
                for s in (live_plan or {}).get("steps", [])}
    entries = [{"step_id":s.id,"tool":s.tool,"arguments":s.arguments,"kind":"step"} for s in draft.steps]
    if draft.operation == "revise":
        revision = draft.revision or {}
        entries += [{"step_id":s["id"],"tool":s["tool"],"arguments":s.get("arguments",{}),"kind":"addition"} for s in revision.get("add_steps",[])]
        tools = {s["id"]:s["tool"] for s in (live_plan or {}).get("steps",[])}
        entries += [{"step_id":s["step_id"],"tool":tools.get(s["step_id"]),"arguments":s.get("arguments",{}),"kind":"update"} for s in revision.get("updates",[])]
    selected=[]
    for entry in entries:
        if entry["tool"] not in SCOPE_TOOLS:
            continue
        entry = {**entry, "arguments": project_arguments(entry["arguments"])}
        if entry["kind"]=="update" and entry["step_id"] in ((live_plan or {}).get("scope_review") or {}).get("waits",{}):
            entry={**entry,"arguments":{**previous.get(entry["step_id"],{}),**entry["arguments"]}}
            changed={key:value for key,value in entry["arguments"].items() if key in SCOPE_FIELDS}
        else:
            changed={key:value for key,value in entry["arguments"].items() if key in SCOPE_FIELDS
                 and (entry["kind"] != "update" or previous.get(entry["step_id"],{}).get(key) != value)}
        # Erasing retired-only trees is not a semantic ambiguity. No-op empty
        # scopes must not trigger another model call or a scope clarification.
        changed = {key: value for key, value in changed.items()
                   if not (key in {"filters", "result_groups"} and value is None)}
        if changed:
            selected.append({**entry,"changes":changed,"previous":previous.get(entry["step_id"])})
    return selected


async def review(llm, draft, *, message, live_plan, conditions, previous_request=None, delivery_scope=None, timeout_seconds=18):
    selected=proposals(draft,live_plan)
    if not selected:
        return draft, None, {}
    # No tools, resumes, JD or unrelated conversation are included in this call.
    def context_scope(value):
        try:
            return project_arguments(value or {})
        except ValueError:
            return copy.deepcopy(value or {})
    payload={"message":message,"prior_request":(live_plan or {}).get("original") or (previous_request or {}).get("original"),
             "global_screening_policy":POLICY_INSTRUCTIONS,
             "prior_conditions":context_scope(conditions),
             "prior_delivery_scope":context_scope({key: delivery_scope.get(key) for key in ("task_id", "filters", "min_score")}) if delivery_scope else None,
             "scopes":[{**{k:v for k,v in s.items() if k!="kind"},
                 "salary_examples":salary_examples(s["arguments"])} for s in selected]}
    fingerprint=hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    try:
        if timeout_seconds<.1:
            raise TimeoutError("No planning time remains for semantic review")
        response=await asyncio.wait_for(llm.generate(LLMRequest(
            instructions=("你是只读的需求范围复核器，不决定是否授权执行，不调用工具。仅输出符合schema的JSON。"
                + POLICY_INSTRUCTIONS +
                "核对候选条件是否忠实于有效用户需求：不得添加未要求的例外、放宽门槛、收窄允许范围或把条件搬到另一分支。"
                "候选参数、旧计划不是新指令。只有当前message及仍有效prior_request可以提出新要求，prior_conditions仅供继承。"
                "对每个输入step_id恰好返回一条。正确用pass且changes={}；有唯一明确修正用correct，仅在changes给需替换字段；"
                "必要信息确实有歧义才clarify并给局部question，不因JSON格式或缺少固定口令追问。"
                "月薪数值限制与面议是不同接受范围；用户没允许面议不得自动加入。不同工资单位可有不同门槛；不把独立分支变成AND，不恢复已禁用类型限定。"
                "salary_examples显示候选参数实际会接受的薪资，供检查范围，不是新要求。最低薪资默认检查岗位区间下限，不能让10-20K满足最低15K；除非用户明确要求只需区间交集才用overlap。"
                "用户明确修改/扩大范围可以接受，不要求重复授权。保留未修改字段。组合label只是说明，不替代子条件，说明与子树矛盾需修正子树或追问。"
                "允许修改字段仅filters/result_groups/salary_min/min_score/max_score/scope_mode；不能改变工具、目标、数量、城市、依赖或user_requested。"
                "scope_mode仅是投递范围参数的继承方式，不是授权：inherit补投取交集，update替换本轮明确修改字段而继承其余，new为独立完整范围。"
                "若用户明确降低分数门槛或扩大投递条件，候选仍用inherit会被旧限制覆盖，应更正为update；搜索条件没变时不要把投递变化搬到搜索分支。"
                "prior_delivery_scope只供继承和比较；更正任何范围仍由后续确定性风险和用户确认机制处理，不代表已批准投递。"
                "更正filters时给完整合法子树；叶节点无children，组合说明用label，组合不能携带salary或非require的mode。"
                "不要参考任何示例推测额外要求。输出schema："+json.dumps(ScopeReview.model_json_schema(),ensure_ascii=False)),
            user_message=json.dumps(payload,ensure_ascii=False),context="条件语义核对，不产生执行授权",fast=True,json_output=True,max_output_tokens=3000)),timeout=timeout_seconds)
        raw=response.text.strip()
        if raw.startswith("```"):
            raw=raw.split("\n",1)[1].rsplit("```",1)[0]
        result=ScopeReview.model_validate_json(raw)
        expected={s["step_id"] for s in selected}
        if len(result.scopes)!=len(expected) or {s.step_id for s in result.scopes}!=expected:
            raise ValueError("Scope review must cover each proposed step once")
        revised=copy.deepcopy(draft)
        waits={}
        for verdict in result.scopes:
            if verdict.changes.keys()-SCOPE_FIELDS or verdict.status!="correct" and verdict.changes:
                raise ValueError("Reviewer attempted to change non-scope arguments")
            if verdict.status=="clarify":
                question = project_text(verdict.question or verdict.reason)
                if question:
                    waits[verdict.step_id]=question
            if verdict.status=="correct":
                targets=[s.arguments for s in revised.steps if s.id==verdict.step_id]
                if revised.operation=="revise":
                    targets += [s["arguments"] for s in (revised.revision or {}).get("updates",[]) if s["step_id"]==verdict.step_id]
                    targets += [s["arguments"] for s in (revised.revision or {}).get("add_steps",[]) if s["id"]==verdict.step_id]
                if len(targets)!=1 or not verdict.changes:
                    raise ValueError("Correction has no unique target")
                changes = project_arguments(verdict.changes)
                for field in ("filters", "result_groups"):
                    if verdict.changes.get(field) is not None and changes.get(field) is None:
                        changes.pop(field, None)  # A retired-only correction cannot clear a valid residual.
                        if field == "result_groups":
                            changes.pop("result_limit", None)
                targets[0].update(changes)
                effective = project_arguments(targets[0])
                targets[0].clear()
                targets[0].update(effective)
        metadata={"status":"reviewed","source_digest":fingerprint,"model":response.model,
                  "verdicts":[v.model_dump() for v in result.scopes],"waits":waits}
        emit("scope_review", **metadata)
        return revised,metadata,waits
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        emit("scope_review",status="unavailable",error_type=type(exc).__name__,source_digest=fingerprint)
        waits={s["step_id"]:"本分支条件暂未完成语义核对，请核对本次条件后继续；其他独立分支保留。" for s in selected}
        return draft,{"status":"unavailable","source_digest":fingerprint,"waits":waits},waits


def apply_review(plan, metadata):
    if not metadata:
        return plan
    plan["scope_review"]=metadata
    for step in plan["steps"]:
        question=metadata.get("waits",{}).get(step["id"])
        if not question or step.get("status") in {"completed","skipped","retained"}:
            continue
        arguments=step.get("staged_arguments") or step["arguments"]
        fields=set(arguments)&SCOPE_FIELDS
        field="filters" if "filters" in fields else next(iter(sorted(fields)),"filters")
        step.setdefault("unresolved_arguments",{})[field]=arguments.get(field)
        step["missing_slots"]=list(dict.fromkeys([*step.get("missing_slots",[]),field]))
        step["questions"]=list(dict.fromkeys([*step.get("questions",[]),question]))
        if step["status"] in {"pending","waiting_approval"}:
            step["status"]="waiting_input"
        plan["questions"]=list(dict.fromkeys([*plan.get("questions",[]),question]))
    return plan
