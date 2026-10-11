from __future__ import annotations

import hmac
import re
from typing import Any

from fastapi import HTTPException, status


EMAIL_RE = re.compile(r"(?<![A-Z0-9._%+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![A-Z0-9_.-])", re.IGNORECASE)
# Only recognizable standalone telephone formats, never arbitrary digit runs.
# Bare international/local numbers require a contact label or a typed phone field.
PHONE_RE = re.compile(
    r"(?<![\w.+])(?:\+\d(?:[ ()-]*\d){7,14}|(?:\+?86[ -]?)?1[3-9]\d{9}|"
    r"0\d{2,3}[- ]\d{7,8})(?![\w.-])"
)
CONTACT_PHONE_RE = re.compile(
    r"((?:电话|手机|联系电话|联系方式|phone|mobile|tel(?:ephone)?)\s*[:：=]?\s*)"
    r"(\+?\d(?:[ ()-]*\d){5,14})(?![\w.-])", re.IGNORECASE
)
CONTACT_NAME_RE = re.compile(
    r"((?:姓名|联系人|contact[_ ]?name|full[_ ]?name)\s*[:：=]\s*)"
    r"([^\n,，;；<>]{1,60})", re.IGNORECASE
)
CONTACT_ADDRESS_RE = re.compile(
    r"((?:家庭住址|居住地址|通讯地址|home[_ ]?address)\s*[:：=]\s*)"
    r"([^\n;；<>]{1,160})", re.IGNORECASE
)
CONTACT_FIELDS = {
    "phone", "phone_number", "mobile", "mobile_number", "telephone", "tel",
    "email", "email_address", "contact_email", "contact_phone", "full_name",
    "contact_name", "candidate_name", "applicant_name", "home_address",
    "姓名", "手机号", "手机", "电话", "邮箱", "家庭住址",
}
SECRET_FIELDS = {
    "api_key", "apikey", "secret", "password", "access_token", "refresh_token",
    "authorization", "authorization_token", "auth_token", "token", "cookie",
    "cookies", "private_key", "data_key", "auto_hunt_key", "autohunt_api_key",
}
SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(?:api[_-]?key|secret|token)\s*[:=]\s*[^\s,;]{8,}", re.IGNORECASE),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]


def redact_text(value: str) -> str:
    """A boundary text copy. Never use this to serialize canonical execution state."""
    value = EMAIL_RE.sub("[REDACTED_EMAIL]", value)
    value = CONTACT_PHONE_RE.sub(r"\1[REDACTED_PHONE]", value)
    value = PHONE_RE.sub("[REDACTED_PHONE]", value)
    value = CONTACT_NAME_RE.sub(r"\1[REDACTED_NAME]", value)
    value = CONTACT_ADDRESS_RE.sub(r"\1[REDACTED_ADDRESS]", value)
    for pattern in SECRET_PATTERNS:
        value = pattern.sub("[REDACTED_SECRET]", value)
    return value


def redact(value: Any) -> Any:
    """Project data across a log/model/third-party boundary without mutating it.

    Typed contact/credential fields are masked; other fields retain business data
    and opaque identifiers. JSON strings are traversed so native tool arguments
    cannot bypass field-level masking. This is minimization, not universal PII DLP.
    """
    if isinstance(value, str):
        import json
        try:
            parsed = json.loads(value) if value.lstrip().startswith(("{", "[")) else None
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, (dict, list)):
            return json.dumps(redact(parsed), ensure_ascii=False)
        return redact_text(value)
    if isinstance(value, dict):
        def field(key, item):
            normalized = str(key).casefold().replace("-", "_")
            if normalized in SECRET_FIELDS and item not in (None, "", [], {}):
                return "[REDACTED_SECRET]"
            if normalized in CONTACT_FIELDS and item not in (None, "", [], {}):
                return "[REDACTED_CONTACT]"
            # Protocol references are data types, not free-form text. This only
            # applies to scalar references (or lists of them), never a nested
            # object hidden under an ID key. Canonical storage needs no exceptions.
            reference = normalized == "id" or normalized.endswith(("_id", "_ids", "_hash", "_digest"))
            def opaque(value):
                return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9_:-]{1,128}", value)) and not any(
                    pattern.search(value) for pattern in SECRET_PATTERNS)
            if reference and opaque(item):
                return item
            if reference and isinstance(item, list) and all(opaque(value) for value in item):
                return list(item)
            return redact(item)
        return {key: field(key, item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
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
