import uuid

import pytest
from pydantic import ValidationError

from app.sandbox_backend.protocol import SandboxNetworkPolicy, SandboxRequest


def test_sandbox_protocol_defaults_to_no_network() -> None:
    request = SandboxRequest(
        request_id=uuid.uuid4().hex,
        task_id="market-1",
        agent_id="main",
        tool_name="job_analyze",
        capability_token="capability-token-123",
        idempotency_key="market-1-analysis",
    )
    assert request.network.mode == "deny"
    assert request.timeout_seconds == 30
    assert request.memory_mb == 256


def test_sandbox_protocol_rejects_urls_in_host_allowlist() -> None:
    with pytest.raises(ValidationError):
        SandboxNetworkPolicy(mode="allowlist", allowed_hosts=["https://example.com/path"])
