"""Capability-confined storage backend and typed sandbox protocol."""

from .filesystem import SandboxViolation, WorkspaceSandbox

__all__ = ["SandboxViolation", "WorkspaceSandbox"]
