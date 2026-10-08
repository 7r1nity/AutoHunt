"""Typed, extractively grounded plans for dependent chat actions.

The planner has no tools. Only quoted user commands authorize downstream work;
JD/resume text is never a source of authority. Simple requests bypass this layer.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid

from pydantic import BaseModel, ConfigDict, Field

from ..core.action_language import positive_action, score_scope
from ..core.search_contract import explicit_search_constraints
from ..core.search_language import parse_search_message
from ..llm.base import LLMRequest


class SearchArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    queries: list[str] = Field(default_factory=list, max_length=5)
    locations: list[str] = Field(default_factory=list, max_length=5)
    result_limit: int | None = Field(default=None, ge=1, le=100)
    salary_min: int | None = Field(default=None, ge=0, le=10_000_000)


class PlanExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    search: SearchArguments
    search_quote: str = Field(min_length=1, max_length=2000)
    delivery_quote: str = Field(default="", max_length=1000)
    delivery_scope_quote: str = Field(default="", max_length=1000)
    learning_quote: str = Field(default="", max_length=1000)
    learning_scope_quote: str = Field(default="", max_length=1000)


def qualitative_salary(message: str) -> bool:
    return bool(re.search(r"(?:薪资|薪酬|工资|月薪)[^，。；;\n]{0,12}(?:中等|适中|较高|高一点|高些|偏高|平均|市场水平)", message))


def needs_plan(message: str) -> bool:
    if not parse_search_message(message).authorized:
        return False
    return qualitative_salary(message) or bool(re.search(
        r"投递|提交申请|(?:生成|制定|更新|创建).{0,20}(?:成长计划|学习计划|劣势分析|差距分析)", message))


def pending_request(dialogue: dict, stamp: str) -> dict | None:
    request = dialogue.get("workflow_request") or {}
    if (request.get("status") == "clarifying" and request.get("resume_stamp") == stamp
            and request.get("expires_at", 0) >= time.time()
            and dialogue.get("turn", 0) - request.get("turn", 0) <= 4):
        return request
    return None


def is_slot_reply(message: str) -> bool:
    """Only answers to the outstanding parameter question inherit authority."""
    return bool(re.search(r"\d|不限薪资|薪资不限|取消投递|不投递|不要投递|取消.*计划|不要.*计划", message)) and not bool(
        re.search(r"停止|暂停|重置|退出|为什么|怎么|如何|查看|详情|第\s*\d+\s*个|[？?]", message))


async def extract_plan(llm, message: str, *, previous: dict | None, conditions: dict,
                       stamp: str, turn: int) -> dict:
    original = previous["original"] if previous else message
    answers = [*(previous.get("answers", []) if previous else []), *([message] if previous else [])][-4:]
    texts = [original, *answers]
    response = await asyncio.wait_for(llm.generate(LLMRequest(
        instructions=(
            "你是无工具的求职工作流规划器。只输出JSON，禁止执行动作。只使用用户当前请求和补充答案，"
            "简历方向由执行器继承，不得编造岗位、城市、数字或匹配阈值。字段必须且只能为："
            "search:{queries:[],locations:[],result_limit:null,salary_min:null},search_quote,"
            "delivery_quote,delivery_scope_quote,learning_quote,learning_scope_quote。"
            "所有quote必须是原请求或某条补充答案的连续逐字片段，不得改写、拼接。"
            "search_quote引用搜索部分，不能包含投递/生成计划部分。"
            "delivery_quote引用明确要求投递/提交申请的指令；learning_quote引用生成/更新学习或成长计划的指令。"
            "没有对应动作则相关quote为空。若用户明确取消某个后续动作也设为空。"
            "scope_quote分别引用该动作对应的匹配分数范围；没有明确数字时为空，不能自行把高匹配解释为90。"
            "补充答案可为两个动作分别指定阈值，如投递90分以上，成长计划90分以下。"
            "中等薪资不能换算数字；如用户未给具体下限salary_min=null；不限薪资可设0。"
            "找15份工作提取result_limit=15；没有指定岗位queries=[]继承当前会话/简历。"
            "本轮及原请求都未指定的城市填[]、数量/薪资填null，由执行器继承会话，不照抄session_conditions。"
            "只是搜索时不新增其他动作。保留未被用户更新的明确参数。"
        ), user_message=json.dumps({"request": original, "answers": answers,
            "session_conditions": conditions}, ensure_ascii=False), context="规划依赖：搜索完成后，独立处理投递和成长计划。",
        fast=True
    )), timeout=35)
    raw = response.text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    try:
        extracted = PlanExtraction.model_validate(json.loads(raw))
    except ValueError as exc:
        raise ValueError("模型未返回有效的结构化规划，请检查真实模型配置或明确分开各项条件") from exc
    for quote in (extracted.search_quote, extracted.delivery_quote, extracted.delivery_scope_quote,
                  extracted.learning_quote, extracted.learning_scope_quote):
        if quote and not any(quote in text for text in texts):
            raise ValueError("规划引用无法在用户原话中核对，没有执行任何动作")
    if not parse_search_message(extracted.search_quote).authorized or re.search(r"投递|成长计划|学习计划", extracted.search_quote):
        raise ValueError("没有独立且明确的搜索指令，没有启动任务")
    for quote, verb in ((extracted.delivery_quote, r"投递|提交申请"),
                        (extracted.learning_quote, r"(?:生成|制定|更新|创建).{0,30}(?:学习计划|成长计划)")):
        if quote and (not positive_action(quote) or not re.search(verb, quote)):
            raise ValueError("后续动作不是明确的用户命令，没有执行")
    # Numeric constraints are checked independently of the model's paraphrase.
    explicit = {}
    for text in texts:
        explicit.update(explicit_search_constraints(text))
    if previous and any(item.startswith("最低月薪") for item in previous.get("missing", [])):
        answer = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kK千万]|元)\s*[。！!]?\s*", message)
        if answer:
            explicit["salary_min"] = int(float(answer[1]) * {"k": 1000, "千": 1000, "万": 10000}.get(answer[2].lower(), 1))
    search = extracted.search.model_dump(exclude_none=True)
    for key in ("salary_min", "result_limit"):
        if key in explicit:
            search[key] = explicit[key]
        elif search.get(key) is not None:
            if key == "salary_min" and search[key] == 0 and any(re.search(r"不限薪资|薪资不限|不限制薪资", text) for text in texts):
                pass
            else:
                raise ValueError(f"{key} 未在用户原话中得到数字依据，请明确数值")
    for key in ("queries", "locations"):
        if not search[key]:
            search.pop(key)
        elif not all(any(value in text for text in texts) for value in search[key]):
            raise ValueError("搜索参数未在用户原话中得到依据，请明确岗位和城市")
    missing, actions = [], []
    if qualitative_salary(original) and "salary_min" not in search:
        missing.append("最低月薪（如12k，或不限薪资）")
    if re.search(r"投递|提交申请", original) and not extracted.delivery_quote and not any(re.search(r"取消投递|不投递|不要投递", text) for text in answers):
        missing.append("投递动作的明确范围")
    if re.search(r"(?:生成|制定|更新|创建).{0,20}(?:学习计划|成长计划)", original) and not extracted.learning_quote and not any(re.search(r"不生成|取消.*计划|不要.*计划", text) for text in answers):
        missing.append("成长计划动作的明确范围")
    for kind, quote, scope_quote in (("delivery", extracted.delivery_quote, extracted.delivery_scope_quote),
                                    ("learning", extracted.learning_quote, extracted.learning_scope_quote)):
        if not quote:
            continue
        if kind == "delivery" and re.search(r"boss|直聘|猎聘|智联|真实网站", original, re.I):
            raise ValueError("自动投递只支持本地模拟站，不支持真实招聘网站")
        if scope_quote and re.search(r"成长计划|学习计划" if kind == "delivery" else r"投递|提交申请", scope_quote):
            raise ValueError("后续动作的分数范围相互混淆，请分别说明投递和成长计划的阈值")
        scope = score_scope(scope_quote) if scope_quote else score_scope(quote)
        if kind == "delivery" and (not scope or "min_score" not in scope or "max_score" in scope):
            missing.append("投递的最低匹配分（如90分以上）")
        elif kind == "learning" and not scope:
            missing.append("成长计划的匹配分范围（如90分以下）")
        else:
            actions.append({"kind": kind, "scope": scope, "instruction": quote})
    return {"id": previous["id"] if previous else uuid.uuid4().hex, "original": original,
        "answers": answers, "search_quote": extracted.search_quote, "search": search, "actions": actions,
        "missing": missing, "resume_stamp": stamp, "turn": turn,
        "expires_at": time.time() + (600 if missing else 1800),
        "status": "clarifying" if missing else "ready"}
