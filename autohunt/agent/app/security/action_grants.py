"""Legacy plan audit fingerprints, retained for stored-plan compatibility.

These are NOT execution credentials. The generic tool authorization gate has been
removed; delivery uses its own job preview and one-use token, not these hashes.
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
