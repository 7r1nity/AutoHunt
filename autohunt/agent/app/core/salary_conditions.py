"""Typed salary facts and predicates. No user-message parsing or model authority."""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SalaryPredicate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    unit: Literal["monthly", "daily", "negotiable"]
    minimum: int | None = Field(default=None, ge=0, le=10_000_000)
    maximum: int | None = Field(default=None, ge=0, le=10_000_000)
    range_mode: Literal["contained", "overlap"] = "contained"
    applies_to: Literal["all", "internship"] = "all"
    origin: Literal["model", "legacy"] = "model"

    @classmethod
    def __get_pydantic_json_schema__(cls,core_schema,handler):
        schema=handler.resolve_ref_schema(handler(core_schema))
        schema["oneOf"]=[
            {"properties":{"unit":{"const":"negotiable"},"minimum":{"type":"null"},"maximum":{"type":"null"}}},
            {"properties":{"unit":{"enum":["monthly","daily"]}},"anyOf":[
                {"required":["minimum"],"properties":{"minimum":{"type":"integer"}}},
                {"required":["maximum"],"properties":{"maximum":{"type":"integer"}}}]}]
        return schema

    @model_validator(mode="after")
    def valid_bounds(self):
        if self.unit == "negotiable" and (self.minimum is not None or self.maximum is not None):
            raise ValueError("面议条件不能携带数值边界")
        if self.unit != "negotiable" and self.minimum is None and self.maximum is None:
            raise ValueError("数值薪资条件至少需要一个边界")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("薪资下界不能超过上界")
        # Current policy retires employment-type restrictions. Keep legacy
        # input readable while amount/unit remain ordinary salary predicates.
        self.applies_to = "all"
        return self


def salary_summary(predicate: SalaryPredicate | dict) -> str:
    """Describe an already typed predicate; never infer a constraint from text.

    The evaluator and persisted predicate remain authoritative. This label is
    suitable for display or filling a missing redundant filter description.
    """
    predicate = SalaryPredicate.model_validate(predicate)
    if predicate.unit == "negotiable":
        text = "薪资面议"
    else:
        bounds = "、".join(part for part in (
            f"下限≥{predicate.minimum:,}元" if predicate.minimum is not None else "",
            f"上限≤{predicate.maximum:,}元" if predicate.maximum is not None else "") if part)
        unit = "日薪" if predicate.unit == "daily" else "月薪"
        text = unit + "区间" + bounds
        if predicate.range_mode == "overlap":
            text = unit + "区间与要求有交集（" + bounds + "）"
    if predicate.applies_to == "internship":
        text = "仅限制实习岗位：" + text
    return text


def salary_facts(value: str) -> dict:
    text = re.sub(r"\s+", "", str(value or "")).lower()
    text = re.sub(r"(?<=\d)[,，](?=\d{3}(?:\D|$))", "", text)
    unknown = {"unit": "unknown", "minimum": None, "maximum": None}
    if not text or re.search(r"美元|美金|usd|eur|欧元|港币|hkd|年薪|/年|每年|时薪|/小时|每小时", text):
        return unknown
    if "面议" in text:
        return {**unknown, "unit": "negotiable"}
    daily = bool(re.search(r"日薪|/天|/日|每天|每日|元一天", text))
    match = re.search(r"(\d+(?:\.\d+)?)([k千万]?)(?:[-~～—–至](\d+(?:\.\d+)?)([k千万]?))?", text)
    if not match:
        return unknown
    a, ua, b, ub = match.groups()
    unit_a, unit_b = ua or ub or "", ub or ua or ""
    if not daily and not unit_a and not re.search(r"元|/月|月薪", text):
        return unknown
    scales = {"k": 1000, "千": 1000, "万": 10000, "": 1}
    low = int(Decimal(a) * scales[unit_a])
    high = int(Decimal(b or a) * scales[unit_b or unit_a])
    if low > high:
        return unknown
    return {"unit": "daily" if daily else "monthly", "minimum": low, "maximum": high}


def judge_salary(predicate: SalaryPredicate, facts: dict) -> dict:
    salary = str(facts.get("salary") or "")
    observed = salary_facts(salary)
    if observed["unit"] == "unknown":
        status, reason = "unknown", "薪资单位或区间未披露，不能猜测或折算"
    elif observed["unit"] == "negotiable" and predicate.unit != "negotiable":
        status, reason = "unknown", "薪资面议，无法核实数值门槛"
    elif observed["unit"] != predicate.unit:
        status, reason = "fail", "薪资类型不符合该条件"
    elif predicate.unit == "negotiable":
        status, reason = "pass", "明确披露薪资面议"
    else:
        low, high = observed["minimum"], observed["maximum"]
        if predicate.range_mode == "contained":
            ok = (predicate.minimum is None or low >= predicate.minimum) and (predicate.maximum is None or high <= predicate.maximum)
        else:
            ok = (predicate.minimum is None or high >= predicate.minimum) and (predicate.maximum is None or low <= predicate.maximum)
        status, reason = ("pass", "薪资区间符合条件") if ok else ("fail", "薪资区间不符合条件")
    return {"status": status, "reason": reason,
            "evidence": [{"field": "salary", "quote": salary}] if salary else [], "facts": observed}


def normalized_search_filters(filters: dict | None, salary_min: int | None) -> dict | None:
    """Legacy lower bound is a predicate, never an independent early veto.

    A supplied hard monthly/negotiable tree owns salary scope (including OR).
    Daily-only restrictions can coexist with an inherited monthly lower bound.
    """
    from .job_filters import compose_filters, validate_filter
    expression = validate_filter(filters) if filters is not None else None
    def remove_legacy(node):
        if node.op == "condition":
            return None if node.salary and node.salary.origin == "legacy" else node
        children = [value for child in node.children if (value := remove_legacy(child)) is not None]
        if not children:
            return None
        return node.model_copy(update={"children": children})
    expression = remove_legacy(expression) if expression else None
    owns_monthly = expression and any(leaf.mode == "require" and leaf.salary and
        leaf.salary.unit in {"monthly", "negotiable"} for leaf in expression.leaves())
    if not salary_min or owns_monthly:
        return expression.model_dump(mode="json") if expression else None
    salary = {"op": "condition", "text": f"月薪区间下限至少{salary_min}元",
              "salary": {"unit": "monthly", "minimum": salary_min, "origin": "legacy"}}
    return compose_filters("all", salary, expression.model_dump(mode="json") if expression else None)
