"""Read-only execution accounting over receipts/events, never a second state store.

Inference calls, tool invocations and business completion are different units.
Missing provider usage/pricing stays unknown. No requests, arguments, resume or
response text is exported by this projection.
"""
from __future__ import annotations

import math
from collections import Counter
from datetime import datetime


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def usage_tokens(usage, *keys):
    if not isinstance(usage, dict):
        return None
    for key in keys:
        value = number(usage.get(key))
        if value is not None:
            return value
    return None


def call_cost(usage: dict, rate: dict | None, prompt, completion):
    usage = usage if isinstance(usage, dict) else {}
    explicit = number(usage.get("cost"))
    currency = usage.get("currency") if isinstance(usage.get("currency"), str) else None
    if explicit is not None:
        return {"amount": explicit, "currency": currency, "source": "provider_reported"}
    rate = rate if isinstance(rate, dict) else {}
    incoming, outgoing = number(rate.get("input_per_million")), number(rate.get("output_per_million"))
    cached = usage_tokens(usage, "prompt_cache_hit_tokens")
    if cached is None:
        details = usage.get("prompt_tokens_details") or {}
        cached = number(details.get("cached_tokens")) if isinstance(details, dict) else None
    cached = cached or 0
    cache_rate = number(rate.get("cached_input_per_million"))
    if (prompt is None or completion is None or incoming is None or outgoing is None
            or not isinstance(rate.get("currency"), str) or not rate.get("currency")
            or cached > prompt or (cached and cache_rate is None)):
        return {"amount": None, "currency": None, "source": "unknown"}
    value = ((prompt - cached) * incoming + cached * (cache_rate or 0) + completion * outgoing) / 1_000_000
    return {"amount": value, "currency": rate["currency"], "source": "configured_estimate_not_invoice"}


def execution_ledger(runs: list[dict], events: list[dict], *, model_rates: dict | None = None) -> dict:
    """Pure aggregation; model_rates keys are 'provider/model', with currency."""
    model_rates = model_rates or {}
    requests, responses, tools = {}, {}, {}
    retries = Counter()
    budgets, decisions = [], Counter()
    external, seen_events = {}, set()
    for event in events:
        kind, data = event.get("kind"), event.get("data") or {}
        run_id = event.get("run_id") or data.get("run_id")
        event_id = (run_id, event.get("seq"))
        if event_id[1] is not None:
            if event_id in seen_events:
                continue
            seen_events.add(event_id)
        key = (run_id, data.get("call_id"))
        if kind == "model_request" and key[1]:
            requests.setdefault(key, data)
        elif kind == "model_response" and key[1]:
            responses[key] = data
        elif kind in {"model_retry", "tool_retry"}:
            retries[kind] += 1
        elif kind == "context_budget":
            budgets.append(data)
        elif kind == "action_decision":
            decisions[str(data.get("decision", "UNKNOWN"))] += 1
        elif kind in {"tool_started", "tool_finished"}:
            # One execution may have several distinct tool calls. Legacy events
            # without an execution id are deliberately separate invocations.
            identity = (run_id, data.get("execution_id") or data.get("call_id"), data.get("tool"))
            if not identity[1]:
                if kind == "tool_started":
                    identity = (run_id, f"legacy:{event.get('seq')}", data.get("tool"))
                else:
                    pending = [k for k, v in tools.items() if k[0] == run_id and k[2] == data.get("tool") and "finish" not in v]
                    identity = pending[-1] if pending else (run_id, f"legacy:{event.get('seq')}", data.get("tool"))
            tools.setdefault(identity, {})["start" if kind == "tool_started" else "finish"] = data
        elif kind in {"browser", "delivery"}:
            task = data.get("task") or {}
            if task.get("id"):
                external[(kind, task["id"])] = {"kind": kind, "id": task["id"], "status": task.get("status"),
                    "checkpoint": task.get("checkpoint"), "submitted": task.get("submitted")}
    calls = []
    for key in dict.fromkeys([*requests, *responses]):
        request, response = requests.get(key, {}), responses.get(key, {})
        metadata = {**request, **response}
        usage = response.get("usage") or {}
        prompt = usage_tokens(usage, "prompt_tokens", "input_tokens")
        completion = usage_tokens(usage, "completion_tokens", "output_tokens")
        provider = metadata.get("response_provider") or metadata.get("provider")
        model = metadata.get("response_model") or metadata.get("model")
        calls.append({"run_id": key[0], "call_id": key[1], **{name: metadata.get(name) for name in
            ("session_id", "plan_id", "revision", "step_id", "execution_id", "operation")},
            "provider": provider, "model": model, "status": response.get("status", "incomplete"),
            "latency_ms": number(response.get("latency_ms")), "prompt_tokens": prompt, "completion_tokens": completion,
            "cost": call_cost(usage, model_rates.get(f"{provider}/{model}"), prompt, completion)})
    invocations = []
    for key, record in tools.items():
        data = {**record.get("start", {}), **record.get("finish", {})}
        invocations.append({"run_id": key[0], "tool": key[2], "execution_id": data.get("execution_id"),
            **{name: data.get(name) for name in ("plan_id", "revision", "step_id")},
            "status": record.get("finish", {}).get("status", "incomplete"),
            "duration_ms": number(data.get("duration_ms")), "attempts": number(data.get("attempts"))})
    known_costs = {}
    for call in calls:
        cost = call["cost"]
        if cost["amount"] is not None and cost["currency"]:
            currency = cost["currency"]
            known_costs[currency] = known_costs.get(currency, 0) + cost["amount"]
    # Transport retries may have consumed tokens before a response was lost;
    # usage from the final response alone cannot account for those attempts.
    token_complete = bool(calls) and not retries["model_retry"] and all(c["prompt_tokens"] is not None and c["completion_tokens"] is not None for c in calls)
    cost_complete = bool(calls) and not retries["model_retry"] and all(c["cost"]["amount"] is not None and c["cost"]["currency"] for c in calls)
    steps = []
    for run in runs:
        plan = (run.get("checkpoint") or {}).get("plan") or {}
        steps.extend({"run_id": run["id"], "plan_id": plan.get("id"), "revision": plan.get("revision"),
                      **{k: step.get(k) for k in ("id", "tool", "status", "execution_id", "execution_phase")}}
                     for step in plan.get("steps", []))
    current = runs[0]
    try:
        span = max(0, (datetime.fromisoformat(current["updated_at"]) - datetime.fromisoformat(current["created_at"])).total_seconds() * 1000)
    except (KeyError, ValueError, TypeError):
        span = None
    return {"version": 1, "run_id": current["id"], "status": current.get("status"),
        "covered_run_ids": [r["id"] for r in runs], "calls": calls, "tools": invocations,
        "steps": steps, "external_tasks": list(external.values()),
        "accounting": {"recorded_model_calls": len(calls), "model_statuses": dict(Counter(c["status"] for c in calls)),
            "model_retries": retries["model_retry"], "tool_retries": retries["tool_retry"],
            "retry_attempts_without_usage": retries["model_retry"],
            "known_prompt_tokens": sum(c["prompt_tokens"] or 0 for c in calls),
            "known_completion_tokens": sum(c["completion_tokens"] or 0 for c in calls),
            "tokens_complete": token_complete, "calls_without_complete_usage": sum(c["prompt_tokens"] is None or c["completion_tokens"] is None for c in calls),
            "known_cost_by_currency": known_costs, "cost_complete": cost_complete,
            "total_cost": next(iter(known_costs.values())) if cost_complete and len(known_costs) == 1 else None,
            "model_time_sum_ms": sum(c["latency_ms"] or 0 for c in calls), "persisted_run_span_ms": span,
            "calls_without_step_binding": sum(not c["execution_id"] for c in calls),
            "step_statuses": dict(Counter(s["status"] for s in steps)), "action_decisions": dict(decisions)},
        "context": {"observations": len(budgets), "exceeded": sum(b.get("status") == "exceeded" for b in budgets),
            "omitted_turns": sum(b.get("omitted_turns", 0) for b in budgets),
            "max_estimated_input_tokens": max((b.get("estimated_input_tokens", 0) for b in budgets), default=0)},
        "notes": ["Read-only receipts; queued/tool success does not mean application submitted.",
                  "Model calls are logical invocations; transport retries are counted separately. Missing usage/pricing is unknown, not zero.",
                  "Model time sum is not wall time; parallel calls and human/HR waiting differ.",
                  "Legacy or out-of-run calls may lack telemetry; this is not a provider billing statement."]}
