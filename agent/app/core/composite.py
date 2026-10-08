"""Shared composite-request guard.

A message that both authorizes a search and asks for downstream actions
(delivery / growth plan) is a *composite* request. It must be decomposed by the
workflow planner; no single-action branch (least of all the growth-plan branch)
may consume it whole, because doing so silently drops the search the user asked
for first.
"""
from __future__ import annotations

import re

from .action_language import positive_action
from .search_contract import needs_search_reasoning
from .search_language import parse_search_message

# Downstream action cues. Deliberately broader than the growth module's
# generation-verb set: "生成一段劣势分析然后更新成长计划" is a learning action
# whose verb sits outside that set, and this guard must still see it.
_LEARNING_ACTION = re.compile(
    r"(?:生成|制定|创建|规划|更新|补充|加入|写进|记录到|做一份|给我一份|帮我做|帮我写|分析)"
    r"[^。！？\n]{0,20}(?:学习计划|成长计划|提升计划|行动方案|学习方案|劣势分析|差距分析)"
)
_DELIVERY_ACTION = re.compile(r"投递|提交申请")


def composite_request(message: str) -> bool:
    """True when the message combines a search request with downstream actions."""
    parsed = parse_search_message(message)
    if not parsed.authorized or not needs_search_reasoning(message):
        return False
    if _DELIVERY_ACTION.search(message):
        return True
    learning = _LEARNING_ACTION.search(message)
    return bool(learning and positive_action(learning.group()))
