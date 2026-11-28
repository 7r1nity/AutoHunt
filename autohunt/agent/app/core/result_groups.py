"""Model-defined result quotas; admission uses evidence and stable group indexes."""
from __future__ import annotations

import asyncio
import copy
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .job_filters import combine, compose_filters, evaluate_filter, filter_schema, validate_filter
from .filter_policy import project_filter, project_groups


class ResultGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=80)
    count: int = Field(ge=1, le=100)
    filters: dict | None

    @field_validator("filters")
    @classmethod
    def hard_filter(cls, value):
        value = project_filter(value)
        if value is None:
            return None  # A retired category quota becomes an unrestricted slot.
        expression = validate_filter(value)
        if expression is None:
            raise ValueError("分组必须提供类别条件")
        if any(leaf.mode != "require" for leaf in expression.leaves()):
            raise ValueError("数量分组需要明确的类别事实，不能把允许或偏好自动升级为硬条件")
        return expression.model_dump(mode="json")


def normalize_groups(groups):
    projected, _ = project_groups(groups)
    values = [ResultGroup.model_validate(item).model_dump(mode="json") for item in (projected or [])]
    if len(values) > 10 or sum(item["count"] for item in values) > 100:
        raise ValueError("分组最多10组，总数最多100个")
    if len({item["label"] for item in values}) != len(values):
        raise ValueError("分组名称不能重复")
    return values


def groups_schema():
    item = ResultGroup.model_json_schema()
    item["properties"]["filters"] = {"anyOf": [filter_schema(), {"type": "null"}]}
    return {"type": ["array", "null"], "maxItems": 10,
            "description": "分组数量配额，例如深圳5份、杭州5份；每组filters是必须核实的非禁用条件，null表示不再限制类别的配额。省略继承，null或[]清除。",
            "items": item}


def grouped_filter(base, groups):
    if not groups:
        return base
    if any(item.get("filters") is None for item in groups):
        return base  # An unrestricted quota cannot add another group's gate.
    alternatives = compose_filters("any", *[item["filters"] for item in groups])
    return compose_filters("all", base, alternatives)


def group_verdicts(base, groups, verdict):
    offset = len(validate_filter(base).leaves()) if base else 0
    judgments = verdict.get("conditions", [])
    result = []
    for index, group in enumerate(groups):
        if group.get("filters") is None:
            result.append({"index": index, "label": group["label"], "count": group["count"], "status": "pass"})
            continue
        expression = validate_filter(group["filters"])
        size = len(expression.leaves())
        values = judgments[offset:offset + size]
        status = combine(expression, iter(item["status"] for item in values)) if len(values) == size else "unknown"
        result.append({"index": index, "label": group["label"], "count": group["count"], "status": status})
        offset += size
    return result


async def evaluate_grouped_filter(generate, base, groups, facts, *, prior_evidence=None):
    """Judge admission and quota membership without inventing a true predicate.

    A removed category may leave an unrestricted quota next to a city/skill
    quota. In that case group facts must still be collected, while they must not
    become an extra admission gate or ranking preference for the open slots.
    """
    base = project_filter(base)
    groups = normalize_groups(groups)
    if not groups or not any(item.get("filters") is None for item in groups):
        verdict = await evaluate_filter(generate, grouped_filter(base, groups), facts,
                                        prior_evidence=prior_evidence)
        verdict["result_group_evidence"] = group_verdicts(base, groups or [], verdict)
        return verdict
    base_verdict = await evaluate_filter(generate, base, facts, prior_evidence=prior_evidence)
    slots = asyncio.Semaphore(3)
    async def judge(group):
        if group.get("filters") is None:
            return None
        async with slots:
            return await evaluate_filter(generate, group["filters"], facts, prior_evidence=prior_evidence)
    verdicts = await asyncio.gather(*(judge(group) for group in groups))
    result = copy.deepcopy(base_verdict)
    evidence = []
    for index, (group, verdict) in enumerate(zip(groups, verdicts)):
        evidence.append({"index": index, "label": group["label"], "count": group["count"],
                         "status": verdict["status"] if verdict is not None else "pass"})
        if verdict is not None:
            for condition in verdict.get("conditions", []):
                result["conditions"].append({**condition, "index": len(result["conditions"])})
    result["result_group_evidence"] = evidence
    return result
