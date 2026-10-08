import pytest
from fastapi import HTTPException

from app.core.security import assert_public_memory, redact_text


def test_redaction_removes_email_phone_and_key() -> None:
    value = redact_text("me@example.com +61 412 345 678 sk-abcdefghijklmnop")
    assert "example.com" not in value
    assert "412" not in value
    assert "sk-" not in value


def test_public_memory_rejects_email() -> None:
    with pytest.raises(HTTPException):
        assert_public_memory("contact me@example.com")
