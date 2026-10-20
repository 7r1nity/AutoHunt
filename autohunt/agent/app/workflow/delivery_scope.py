"""Materialize a batch's remembered constraints before action authorization.

This is data inheritance, not natural-language parsing or a reusable grant.
Selection of an individual job remains independent. New searches/new scopes do
not accidentally inherit an unrelated batch, and historical state never sends.
"""
from __future__ import annotations

import copy

from ..core.job_filters import compose_filters, validate_filter
from .result_sources import ResultSourceClarification


def materialize_delivery_scope(arguments, previous, *, session_id, stamp, task_id=None, new_search=False):
    args = copy.deepcopy(arguments)
    selected = args.get("selector") or args.get("target_step_id")
    if selected or new_search or args.get("scope_mode") == "new" or not previous:
        return args
    source = args.get("source_task_id") or task_id
    if source and previous.get("task_id") and source != previous["task_id"]:
        return args  # Another explicitly bound search is not a retry of this batch.
    if previous.get("session_id") != session_id or previous.get("resume_stamp") != stamp:
        raise ResultSourceClarification("上一批投递绑定的会话或简历已变化，请核对本次投递范围；没有沿用旧授权。")
    old, extra = previous.get("filters"), args.get("filters")
    if old is not None:
        old = validate_filter(old).model_dump(mode="json")
        extra = validate_filter(extra).model_dump(mode="json") if extra is not None else None
        # Idempotent even when the model already copied the remembered tree.
        present = extra == old or bool(extra and extra.get("op") == "all" and old in extra["children"])
        args["filters"] = extra if present else compose_filters("all", old, extra)
    args["min_score"] = max(previous.get("min_score", 0), args.get("min_score", 0))
    if previous.get("task_id") and not args.get("source_task_id"):
        args["source_task_id"] = source or previous["task_id"]
    # Freeze the expanded parameters. A later dialogue update may not rebind
    # this already planned/approved action to another remembered scope.
    args["scope_mode"] = "new"
    return args
