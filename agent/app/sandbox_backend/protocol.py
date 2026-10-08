from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class SandboxStatus(StrEnum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    NEEDS_HUMAN = "needs_human"
    COMPLETED = "completed"
    FAILED = "failed"
    OUTCOME_INDETERMINATE = "outcome_indeterminate"


class SandboxNetworkPolicy(BaseModel):
    mode: Literal["deny", "allowlist"] = "deny"
    allowed_hosts: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("allowed_hosts")
    @classmethod
    def validate_hosts(cls, hosts: list[str]) -> list[str]:
        if any("/" in host or "://" in host for host in hosts):
            raise ValueError("Sandbox network allowlist accepts hostnames only")
        return sorted({host.lower().strip() for host in hosts if host.strip()})


class SandboxRequest(BaseModel):
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    task_id: str = Field(min_length=1, max_length=100)
    agent_id: str = Field(min_length=1, max_length=100)
    tool_name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    typed_arguments: dict[str, Any] = Field(default_factory=dict)
    capability_token: str = Field(min_length=16, max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=200)
    timeout_seconds: int = Field(default=30, ge=1, le=300)
    cpu_limit: float = Field(default=1, gt=0, le=8)
    memory_mb: int = Field(default=256, ge=64, le=4096)
    network: SandboxNetworkPolicy = Field(default_factory=SandboxNetworkPolicy)


class SandboxEvent(BaseModel):
    request_id: str
    status: SandboxStatus
    detail: str = Field(max_length=500)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SandboxResponse(BaseModel):
    request_id: str
    status: SandboxStatus
    structured_result: dict[str, Any] | None = None
    artifact_refs: list[str] = Field(default_factory=list, max_length=100)
    evidence_refs: list[str] = Field(default_factory=list, max_length=100)
    redacted_logs: list[str] = Field(default_factory=list, max_length=500)
    resource_usage: dict[str, float] = Field(default_factory=dict)
    error_code: str | None = None
    retryable: bool = False
