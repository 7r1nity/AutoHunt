"""Explicit salary labels for synthetic simulator listings; never convert daily pay."""
from __future__ import annotations

import re


def salary_info(description: str, minimum: int, maximum: int) -> tuple[str, str]:
    section = re.search(r"【薪资福利】([^\n]*)", description or "")
    text = section.group(1) if section else ""
    if "薪资面议" in text:
        return "negotiable", "面议"
    daily = re.search(r"实习日薪\s*(\d+)\s*-\s*(\d+)\s*元/天", text)
    if daily:
        low, high = map(int, daily.groups())
        if 0 < low <= high:
            return "daily", f"{low}-{high}元/天"
    if minimum == maximum == 0:
        return "unknown", "薪资未注明"
    return "monthly", f"{minimum}-{maximum}K"
