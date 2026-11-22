"""Lossless graph normalization shared by initial planning and revisions.

Only a unique producer or explicit dependency identifies an evidence source.
Direct reversed edges are repairable; indirect contradictory paths need replanning.
Historical execution receipts never participate in mutation deduplication.
"""
from __future__ import annotations

import json

from .validation import PlanValidationError, dependency_ancestors


def wait_for_scope(step, question, *, field=None, value=None):
    step.setdefault("questions", []).append(question)
    if field:
        step.setdefault("unresolved_arguments", {})[field] = value
        step.setdefault("missing_slots", []).append(field)


def depend_on(steps, consumer, producer):
    """Ensure producer finishes first, repairing only a direct reversed edge."""
    if consumer["id"] == producer["id"]:
        raise PlanValidationError("invalid_dependency", "步骤不能依赖自己")
    if consumer["id"] in dependency_ancestors(producer["id"], steps):
        if producer.get("status", "pending") not in {"pending", "waiting_input", "waiting_approval"}:
            raise PlanValidationError("evidence_order", "已执行步骤的依赖记录不能改写，请派生新的后续步骤")
        remaining = [key for key in producer["depends_on"] if key != consumer["id"]]
        candidate = [item if item is not producer else {**producer, "depends_on": remaining} for item in steps]
        if consumer["id"] in dependency_ancestors(producer["id"], candidate):
            raise PlanValidationError("evidence_order", "证据依赖存在间接反向路径，需要重新规划")
        producer["depends_on"] = remaining
    consumer["depends_on"] = list(dict.fromkeys([*consumer["depends_on"], producer["id"]]))


def bind_evidence(steps, consumer, producers, *, known_sources=()):
    # Cancelled/failed producers are not implicit evidence for newly appended
    # actions. Keep explicit dependencies intact, but never invent a new edge to
    # a withdrawn node when a revision has replaced the old work.
    producers = [item for item in producers if not item.get("withdrawn") and item.get("status") not in
                 {"skipped", "cancelled", "failed", "blocked", "outcome_indeterminate"}]
    bound = [item for item in producers if item["id"] in dependency_ancestors(consumer["id"], steps)]
    bound = [item for item in bound if not any(item["id"] in dependency_ancestors(other["id"], steps)
             for other in bound if other is not item)]
    explicit = consumer.get("arguments", {}).get("source_task_id") or consumer.get("arguments", {}).get("task_id")
    if not bound and explicit:
        matching = [item for item in producers if item.get("task_id") == explicit]
        if len(matching) == 1:
            depend_on(steps, consumer, matching[0])
            bound = matching
        elif explicit in set(known_sources):
            # A trusted prior receipt is independent of newly added searches.
            # The executor still checks completion, resume and session ownership.
            consumer.pop("evidence_step", None)
            return None
    if not bound and len(producers) == 1:
        depend_on(steps, consumer, producers[0])
        bound = producers
    if len(bound) == 1:
        consumer["evidence_step"] = bound[0]["id"]
        return bound[0]
    if producers:
        consumer.pop("evidence_step", None)
        wait_for_scope(consumer, "请明确本步骤使用哪一轮搜索结果：" + "、".join(item["id"] for item in producers) + "。")
    return None


def deduplicate_mutations(steps, goals):
    """Merge identical pending mutations with identical execution preconditions.

    Same arguments alone do not make writes interchangeable across a read/write
    barrier. Including dependencies and evidence avoids creating cycles or
    erasing intentional repeated state updates.
    """
    canonical, aliases, retained = {}, {}, []
    for step in steps:
        if (step.get("effect") != "read" and step.get("status") == "pending"
                and step["tool"] not in {"job_search_start", "job_results_refilter"}):
            # Result producers have distinct snapshot identities even when their
            # arguments coincide. Explicit consumers must not be rebound by dedup.
            key = json.dumps([step["tool"], step["arguments"], sorted(step["depends_on"]),
                              step.get("evidence_step"), step.get("questions"),
                              step.get("missing_slots"), step.get("user_requested")], sort_keys=True, ensure_ascii=False)
            if key in canonical:
                aliases[step["id"]] = canonical[key]
                continue
            canonical[key] = step["id"]
        retained.append(step)
    for step in retained:
        step["depends_on"] = list(dict.fromkeys(aliases.get(key, key) for key in step["depends_on"]))
        if step.get("evidence_step") in aliases:
            step["evidence_step"] = aliases[step["evidence_step"]]
    for goal in goals:
        goal["step_ids"] = list(dict.fromkeys(aliases.get(key, key) for key in goal["step_ids"]))
    return retained
