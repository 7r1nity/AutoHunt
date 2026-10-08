from __future__ import annotations

import hmac
import re
from typing import Any

from fastapi import HTTPException, status


EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\s()\-]{6,}\d)(?!\d)")
SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(?:api[_-]?key|secret|token)\s*[:=]\s*[^\s,;]{8,}", re.IGNORECASE),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]


def redact_text(value: str) -> str:
    value = EMAIL_RE.sub("[REDACTED_EMAIL]", value)
    value = PHONE_RE.sub("[REDACTED_PHONE]", value)
    for pattern in SECRET_PATTERNS:
        value = pattern.sub("[REDACTED_SECRET]", value)
    return value


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def assert_no_secrets(value: str) -> None:
    if any(pattern.search(value) for pattern in SECRET_PATTERNS):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Secrets and API keys must not be stored in memory or skill files.",
        )


def assert_public_memory(value: str) -> None:
    assert_no_secrets(value)
    if EMAIL_RE.search(value) or PHONE_RE.search(value):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Email and phone data belong in encrypted private profile storage.",
        )


def secure_compare(expected: str, supplied: str | None) -> bool:
    return bool(supplied) and hmac.compare_digest(expected.encode(), supplied.encode())
