"""Shared action semantics. User text supplies authority; model arguments do not."""
from __future__ import annotations

import re


def positive_action(message: str) -> bool:
    text = re.sub(r"\s+", "", message)
    return not bool(re.search(r"不要|先别|暂不|不需要|不用|别(?:生成|制定|投递)|不能|如何|怎么|为什么|是否|能不能|可以吗|[？?]|[“”\"『』]", text))


def score_scope(message: str) -> dict | None:
    """Return a closed integer score range; linguistic below/above are strict."""
    text = re.sub(r"\s+", "", message)
    patterns = (
        (r"(?:匹配(?:分数|分|度)?|分数|评分)?(?:在|为)?(\d{1,3})(?:分)?(?:以下|之下)", -1, "max_score"),
        (r"(?:(?<!不)低于|小于|不足|(?<![<=])<(?![=]))(\d{1,3})(?:分)?", -1, "max_score"),
        (r"(?:匹配(?:分数|分|度)?|分数|评分)?(?:在|为)?(\d{1,3})(?:分)?(?:以上|之上)", 0, "min_score"),
        (r"(?:不低于|至少|>=|≥)(\d{1,3})(?:分)?", 0, "min_score"),
        (r"(?:高于|大于|>)(\d{1,3})(?:分)?", 1, "min_score"),
    )
    result = {}
    for pattern, offset, key in patterns:
        for match in re.finditer(pattern, text):
            value = int(match[1]) + offset
            if not 0 <= int(match[1]) <= 100 or key in result and result[key] != value:
                raise ValueError("匹配分数范围无效或相互冲突，请明确 0–100 分之间的范围")
            result[key] = value
    if result and result.get("min_score", 0) > result.get("max_score", 100):
        raise ValueError("匹配分数范围为空，请调整范围")
    return result or None


def explicit_mock_delivery(message: str) -> dict | None:
    if not positive_action(message) or not re.search(r"(?:自动|批量)投递|自动(?:发送|提交)简历", message):
        return None
    scope = score_scope(message)
    if not scope or "min_score" not in scope or "max_score" in scope:
        return None
    return scope


def plan_scope_answer(message: str) -> dict | None:
    """Only an elliptical range answer, not a new topic or a quoted instruction."""
    if not positive_action(message):
        return None
    if not re.fullmatch(r"(?:那就|改成|改为|换成|就|用|按)?(?:匹配(?:分数|分|度)?|分数|评分)?(?:在|为)?(?:低于|小于|不足|不低于|至少|高于|大于)?\d{1,3}(?:分)?(?:以下|以上|之下|之上)?(?:的|那些|这些|职位|岗位|的职位|的岗位)?[。！!]*", re.sub(r"\s+", "", message)):
        return None
    return score_scope(message)
