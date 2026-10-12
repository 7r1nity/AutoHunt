"""Validation decisions, separate from planning and execution.

Only plan defects are repairable. Repair never grants authority, supplies missing
user intent, invokes a tool or replays an effect. Every candidate is revalidated.
"""
from __future__ import annotations

import re
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import Literal

SLOT_LABELS = {"queries": "岗位方向", "query": "岗位方向", "locations": "城市",
               "result_limit": "职位数量", "salary_min": "最低月薪", "max_jobs": "候选读取预算",
               "min_score": "最低匹配分", "max_score": "最高匹配分", "score_scope": "匹配分范围",
               "authorization": "操作授权", "target": "目标对象", "selector": "条目序号或名称",
               "instruction": "修改要求", "focus": "关注方向", "snapshot_id": "所指结果列表"}


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    category: Literal["repair", "clarify", "block"]
    detail: str
    tool: str = ""
    field: str = ""
    #: "block" refuses the plan; "warn" is recorded and the plan proceeds.
    #:
    #: Citation fidelity is a warn: no regex can promise that a generated plan is
    #: faithful to the user's phrasing, and treating it as a refusal produced one
    #: rejection per paraphrase — a double space was enough. Faithfulness is now the
    #: confirmation step's concern (see app.security.risk), where the user sees the
    #: action and can correct it, rather than a parser's veto. A genuine conflict such
    #: as two different counts stays blocking, because the plan is then self-contradictory.
    severity: Literal["block", "warn"] = "block"

    def public(self) -> dict:
        from ..security.decisions import Decision
        decision = Decision.WARN if self.severity == "warn" else {
            "repair": Decision.REPAIR, "clarify": Decision.CLARIFY, "block": Decision.DENY}[self.category]
        if self.blocking and self.category == "repair" and self.code in {
                "invalid_dependency", "dependency_cycle", "evidence_order", "ambiguous_evidence_binding"}:
            decision = Decision.REPLAN
        return {**asdict(self), "decision": decision.value}

    @property
    def blocking(self) -> bool:
        return self.severity == "block"


class PlanValidationError(ValueError):
    def __init__(self, code: str, detail: str, *, category="repair", tool="", field="", issues=None):
        super().__init__(detail)
        self.issue = ValidationIssue(code, category, detail, tool, field)
        self.issues = issues or [self.issue]


def citation_found(quote: str, sources: list[str]) -> bool:
    """Audit whether a copied citation appears in user text (whitespace tolerant).

    A mismatch is a non-blocking trace warning, not authorization or proof of
    intent. Semantic paraphrases and normalized business parameters belong to
    the model; execution authority binds the resulting structured action.
    """
    if not quote.strip():
        return False
    if any(quote in source for source in sources):
        return True
    collapsed = re.sub(r"\s+", " ", quote).strip()
    return any(collapsed in re.sub(r"\s+", " ", source) for source in sources)


def preflight(steps: list[dict], goals: list[dict], sources: list[str], tools: set[str], *,
              check_goal_coverage: bool = True) -> None:
    """Validate shape first; goal coverage belongs to the normalized DAG.

    Goal annotations describe terminal steps and their ancestors for auditing.
    Missing annotations warn; they cannot independently grant or revoke authority.
    """
    _warnings.set([])       # isolated per concurrent planning context
    issues: list[ValidationIssue] = []
    ids = {step["id"] for step in steps}
    if len(ids) != len(steps):
        issues.append(ValidationIssue("duplicate_step_ids", "repair", "规划包含重复步骤 ID"))
    covered = set()
    for goal in goals:
        if not set(goal["step_ids"]) <= ids or goal["disposition"] == "steps" and not goal["step_ids"]:
            issues.append(ValidationIssue("goal_binding", "repair", "目标没有绑定到有效步骤", severity="warn"))
        covered.update(goal["step_ids"])
    citations = [(f"goals[{index}].quote", "", goal["quote"]) for index, goal in enumerate(goals)]
    citations.extend((f"steps[{step['id']}].{field}", step["tool"], step[field])
                     for step in steps for field in ("command_quote", "scope_quote") if step[field])
    for field, tool, quote in citations:
        if not citation_found(quote, sources):
            issues.append(ValidationIssue("invalid_citation", "repair", "引用必须来自有效用户原话",
                                          tool, field, severity="warn"))
    for step in steps:
        if step["tool"] not in tools:
            issues.append(ValidationIssue("unknown_tool", "repair", "工具不在已提供能力中", step["tool"]))
        if step["id"] in step["depends_on"] or not set(step["depends_on"]) <= ids:
            issues.append(ValidationIssue("invalid_dependency", "repair", "步骤依赖无效", step["tool"]))
    if not any(issue.blocking for issue in issues):
        resolved: set[str] = set()
        while len(resolved) < len(ids):
            available = {step["id"] for step in steps if step["id"] not in resolved
                         and set(step["depends_on"]) <= resolved}
            if not available:
                issues.append(ValidationIssue("dependency_cycle", "repair", "规划依赖成环"))
                break
            resolved.update(available)
    if check_goal_coverage and not any(issue.blocking for issue in issues):
        for root in tuple(covered):
            covered.update(dependency_ancestors(root, steps))
        missing = sorted(ids - covered)
        if missing:
            issues.append(ValidationIssue("uncovered_step", "repair", "规划包含未绑定用户目标的步骤",
                                          field=",".join(f"steps.{key}" for key in missing), severity="warn"))
    if issues:
        issues = list(dict.fromkeys(issues))
        _record_warnings([issue for issue in issues if not issue.blocking])
        blocking = [issue for issue in issues if issue.blocking]
        if not blocking:
            # Warnings only: the plan proceeds and the findings stay visible. Refusing
            # here is what turned a paraphrase into a dead end, so the warning is
            # reported through _preflight_warnings instead of raised.
            _record_warnings(issues)
            return
        first = blocking[0]
        raise PlanValidationError(first.code, first.detail, category=first.category,
                                  tool=first.tool, field=first.field, issues=issues)


#: Warnings from the most recent preflight, so a caller can surface what would
#: previously have been a refusal. Bounded: only the latest plan matters.
_warnings: ContextVar[list[dict]] = ContextVar("plan_validation_warnings", default=[])


def _record_warnings(issues: list[ValidationIssue]) -> None:
    _warnings.set([issue.public() for issue in issues])


def preflight_warnings() -> list[dict]:
    """Non-blocking findings from the last preflight, for reporting and measurement."""
    return list(_warnings.get())


def restore_quote_spacing(quote: str, sources: list[str], *, max_length: int) -> str:
    """Restore a uniquely matching raw span; normalize whitespace runs ONLY.

    Never delete negation, change numbers/units/punctuation or concatenate
    disjoint fragments. Authorization subsequently checks the restored source.
    Ambiguous raw spans remain a repair issue, not permission to choose one.
    """
    if not quote.strip() or any(quote in source for source in sources):
        return quote
    pattern = "".join(r"\s+" if part.isspace() else re.escape(part)
                      for part in re.split(r"(\s+)", quote.strip()) if part)
    matches = {match.group() for source in sources for match in re.finditer(pattern, source)
               if len(match.group()) <= max_length}
    return matches.pop() if len(matches) == 1 else quote


def normalize_encoding(arguments: dict, schema: dict) -> dict:
    """Lossless JSON encoding only; booleans/units/fractions are not coerced.

    Writes use this too, but their values MUST subsequently pass source evidence
    checks. This does not make a string a grounded user parameter.
    """
    result = dict(arguments)
    for key, value in arguments.items():
        prop = schema.get("properties", {}).get(key, {})
        kinds = prop.get("type", [])
        kinds = kinds if isinstance(kinds, list) else [kinds]
        if "integer" in kinds and isinstance(value, str) and re.fullmatch(r"-?\d{1,8}", value):
            result[key] = int(value)
    return result


def encoding_schema(schema: dict) -> dict:
    """Check shapes before semantic extraction touches arrays or strings.

    Value bounds and required slots are checked after authoritative normalization;
    their absence here is not permission to execute an incomplete candidate.
    """
    limits = {"required", "minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems"}
    result = {key: value for key, value in schema.items() if key not in limits}
    if "properties" in result:
        result["properties"] = {key: encoding_schema(value) for key, value in result["properties"].items()}
    if "items" in result:
        result["items"] = encoding_schema(result["items"])
    return result


def dependency_ancestors(step_id: str, steps: list[dict]) -> set[str]:
    """Transitive graph view, bounded even for an invalid cyclic candidate."""
    graph = {step["id"]: step.get("depends_on", []) for step in steps}
    seen: set[str] = set()
    todo = list(graph.get(step_id, []))
    while todo:
        key = todo.pop()
        if key not in seen:
            seen.add(key)
            todo.extend(graph.get(key, []))
    return seen


def resolved_slots(step: dict, conditions: dict) -> set[str]:
    """Run after authoritative normalization, never on the model's raw args."""
    args = step["arguments"]
    supplied = {key for key, value in args.items() if value is not None and value != [] and value != ""}
    if step["tool"] in {"job_search_start", "search_state_update"}:
        supplied.update(key for key, value in conditions.items()
                        if value is not None and value != [] and value != "")
        if args.get("query"):
            supplied.add("queries")
    if "min_score" in args or "max_score" in args:
        supplied.add("score_scope")
    # Authorization and arbitrary "target" are deliberately not inferred here.
    return supplied
