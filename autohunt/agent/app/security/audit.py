"""Measure what the authorization layer would decide, without changing what runs.

Stage (c) of the agreed plan: call the risk grader and the scope boundary on the real
delivery path, record the verdicts, and act on none of them. That produces the numbers
needed to choose between the two behaviour changes — gating on confirmation, or filtering
by the instruction's own scope — instead of discovering the consequences after wiring one.

Nothing here may block, filter or delay delivery. If a measurement is wrong the cost is a
misleading row, not a missing application, which is exactly why the measurement comes
first: the current behaviour is known and the numbers are not.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from ..security.risk import (
    ConfirmationPolicy,
    ConfirmRisky,
    ScopedInstruction,
    SecurityRisk,
    grade_action,
)


def salary_floor(salary: str) -> tuple[int | None, str]:
    """The lower bound of a salary string, or ``(None, why)`` when it cannot be read.

    Unknown is deliberately distinct from below-threshold. Collapsing them is how a
    condition silently disappears: "JD 没写薪资" would count as satisfying "薪资 >= 15k",
    and the user's own criterion would stop filtering anything.
    """
    text = str(salary or "").strip()
    if not text:
        return None, "JD 未标注薪资"
    numbers = re.findall(r"\d+(?:\.\d+)?", text)
    if not numbers:
        return None, "薪资字段没有数字"
    if "万" in text:
        return int(float(min(numbers)) * 10000), ""
    if "k" in text.lower():
        return int(float(min(numbers)) * 1000), ""
    if "元" in text:
        return int(float(min(numbers))), ""
    return None, "无法识别的薪资单位"


@dataclass
class CandidateVerdict:
    job_id: int
    title: str = ""
    score: int | None = None
    salary: str = ""
    in_scope: bool = False
    reason: str = ""


@dataclass
class AuthorizationAudit:
    """What the policy and the boundary would have decided for one delivery request."""

    tool: str
    risk: SecurityRisk
    risk_reason: str
    would_confirm: bool
    policy: str
    threshold: str
    candidates: int
    in_scope: int
    out_of_scope: int
    scope_quote: str
    verdicts: list[CandidateVerdict] = field(default_factory=list)

    def as_row(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "risk": str(self.risk),
            "would_confirm": 1 if self.would_confirm else 0,
            "policy": self.policy,
            "threshold": self.threshold,
            "candidates": self.candidates,
            "in_scope": self.in_scope,
            "out_of_scope": self.out_of_scope,
            "detail": json.dumps(
                {"risk_reason": self.risk_reason, "scope_quote": self.scope_quote,
                 "verdicts": [asdict(v) for v in self.verdicts[:20]]},
                ensure_ascii=False)[:4000],
        }


def audit_delivery(*, candidates: list[dict], scope: ScopedInstruction | None,
                   policy: ConfirmationPolicy | None = None) -> AuthorizationAudit:
    """Grade a delivery batch and test each candidate against the instruction's scope.

    ``scope`` is None when no instruction stated the boundaries. That is not "everything
    is authorised": with no stated criteria there is nothing to be inside of, so the
    candidates are reported as unauthorised for the purposes of measurement while
    delivery continues unchanged.
    """
    policy = policy or ConfirmRisky()
    risk = grade_action("mock_delivery_start", {"expected_count": len(candidates)},
                        effect="external", batch_size=len(candidates))
    would_confirm = policy.should_confirm(risk.risk)

    verdicts: list[CandidateVerdict] = []
    for job in candidates:
        job_id = int(job.get("id", 0))
        title = str(job.get("title", ""))[:80]
        raw_score = job.get("score")
        score = int(raw_score) if isinstance(raw_score, (int, float)) else None
        salary_text = str(job.get("salary", ""))
        floor, salary_why = salary_floor(salary_text)
        if scope is None:
            verdicts.append(CandidateVerdict(job_id, title, score, salary_text, False,
                                             "本轮没有说明投递条件，没有可依据的授权范围"))
            continue
        in_scope, why = scope.covers(score=score, salary_min=floor, count=len(verdicts) + 1)
        if not in_scope and salary_why and "薪资" in why:
            why = f"{why}（{salary_why}）"
        verdicts.append(CandidateVerdict(job_id, title, score, salary_text, in_scope, why))

    inside = sum(1 for v in verdicts if v.in_scope)
    return AuthorizationAudit(
        tool="mock_delivery_start", risk=risk.risk, risk_reason=risk.reason,
        would_confirm=would_confirm, policy=policy.describe(), threshold=str(getattr(policy, "threshold", "")),
        candidates=len(candidates), in_scope=inside, out_of_scope=len(verdicts) - inside,
        scope_quote=scope.quote if scope else "", verdicts=verdicts)
