"""Application-issued action bindings; language interpretation belongs to models.

Bindings are persisted with a validated plan. They are not credentials and cannot
be supplied through tool arguments. Execution compares the saved action, session,
resume and lifetime, without re-parsing a quote from the user's message.
"""
from __future__ import annotations

import hashlib
import json

VERSION = 1


def argument_digest(tool: str, arguments: dict) -> str:
    return hashlib.sha256(json.dumps([tool, arguments], ensure_ascii=False,
        sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def action_binding(tool: str, arguments: dict, *, session_id: str,
                   requested: bool = False) -> dict:
    return {"version": VERSION, "kind": "action", "tool": tool,
        "arguments_digest": argument_digest(tool, arguments),
        "session_id": session_id, "user_requested": requested}


def binding_matches(binding: dict | None, tool: str, arguments: dict,
                    session_id: str) -> bool:
    return bool(binding and binding.get("kind") == "action"
        and binding.get("version") == VERSION and binding.get("tool") == tool
        and binding.get("session_id") == session_id
        and binding.get("arguments_digest") == argument_digest(tool, arguments))
