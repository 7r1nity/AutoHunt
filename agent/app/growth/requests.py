"""Resume-bound, short-lived continuation of an explicitly requested plan."""
import time

from ..core.action_language import plan_scope_answer
from .plans import is_explicit_plan_request


def resume_stamp(document, candidate) -> str:
    return f"{candidate.read().profile.resume_id}:{document.id}:{document.version}"


def continued_plan_scope(dialogue: dict, message: str, stamp: str) -> dict | None:
    request = dialogue.get("learning_request") or {}
    if (dialogue.get("active_intent") != "learning" or not request
            or request.get("expires_at", 0) < time.time()
            or dialogue.get("turn", 0) - request.get("turn", 0) > 4
            or request.get("resume_stamp") != stamp
            or not is_explicit_plan_request(request.get("message", ""))):
        return None
    try:
        scope = plan_scope_answer(message)
    except ValueError:
        return None
    if scope is None:
        return None
    # A new score answer replaces the range, never intersects the obsolete one.
    return scope
