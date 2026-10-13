"""Passive model-call diagnostics; no retries, authority or request mutation."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import replace

from ..runtime.context import emit

PRIVATE_MODEL_EVENTS = {"model_request", "model_response", "plan_draft"}
PRIVATE_FIELDS = {"request", "response", "draft"}


def public_model_data(data):
    return {key: value for key, value in data.items() if key not in PRIVATE_FIELDS}


async def traced_call(provider, operation, request, invoke):
    call_id = uuid.uuid4().hex
    metadata = dict(call_id=call_id, operation=operation, provider=provider.name, model=provider.model)
    encoded = json.dumps(request, ensure_ascii=False, sort_keys=True, default=str)
    emit("model_request", **metadata, request=request, request_chars=len(encoded),
         request_digest=hashlib.sha256(encoded.encode()).hexdigest())
    started = time.monotonic()
    try:
        result = await invoke()
    except BaseException as exc:
        emit("model_response", **metadata, status="cancelled" if type(exc).__name__ == "CancelledError" else "error",
             error_type=type(exc).__name__, latency_ms=round((time.monotonic()-started)*1000, 2))
        raise
    response = dict(text=result.text)
    if hasattr(result, "tool_calls"):
        response["tool_calls"] = [dict(id=call.id, name=call.name, arguments=call.arguments) for call in result.tool_calls]
    emit("model_response", **metadata, status="ok", response=response, response_chars=len(result.text),
         response_id=result.response_id, usage=result.usage,
         response_provider=result.provider, response_model=result.model,
         latency_ms=round((time.monotonic()-started)*1000, 2))
    return replace(result, call_id=call_id)
