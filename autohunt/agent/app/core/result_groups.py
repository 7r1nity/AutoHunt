"""Model-defined result quotas; admission uses evidence and stable group indexes."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .job_filters import combine, validate_filter


class ResultGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=80)
    count: int = Field(ge=1, le=100)
    filters: dict

    @field_validator("filters")
    @classmethod
    def hard_filter(cls, value):
        expression = validate_filter(value)
        if expression is None:
            raise ValueError("分组必须提供类别条件")
        if any(leaf.mode != "require" for leaf in expression.leaves()):
            raise ValueError("分组配额必须由岗位事实判断，不能用allow/prefer代替类别")
        return expression.model_dump(mode="json")


def normalize_groups(groups):
    values = [ResultGroup.model_validate(item).model_dump(mode="json") for item in (groups or [])]
    if len(values) > 10 or sum(item["count"] for item in values) > 100:
        raise ValueError("分组最多10组，总数最多100个")
    if len({item["label"] for item in values}) != len(values):
        raise ValueError("分组名称不能重复")
    return values


def groups_schema():
    return {"type": ["array", "null"], "maxItems": 10,
            "description": "分组数量配额，例如正式5份、实习5份；每组filters是必须核实的类别事实，不是allow。省略继承，null或[]清除。",
            "items": ResultGroup.model_json_schema()}


def grouped_filter(base, groups):
    if not groups:
        return base
    alternatives = {"op": "any", "children": [item["filters"] for item in groups]}
    return {"op": "all", "children": [base, alternatives]} if base else alternatives


def group_verdicts(base, groups, verdict):
    offset = len(validate_filter(base).leaves()) if base else 0
    judgments = verdict.get("conditions", [])
    result = []
    for index, group in enumerate(groups):
        expression = validate_filter(group["filters"])
        size = len(expression.leaves())
        values = judgments[offset:offset + size]
        status = combine(expression, iter(item["status"] for item in values)) if len(values) == size else "unknown"
        result.append({"index": index, "label": group["label"], "count": group["count"], "status": status})
        offset += size
    return result
