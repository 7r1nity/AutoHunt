from pathlib import Path

import pytest

from app.sandbox_backend.filesystem import SandboxViolation, WorkspaceSandbox


def test_sandbox_rejects_path_escape(tmp_path: Path) -> None:
    sandbox = WorkspaceSandbox(tmp_path)
    with pytest.raises(SandboxViolation):
        sandbox.atomic_write("../escape.md", "no", suffixes={".md"})


def test_sandbox_writes_atomically(tmp_path: Path) -> None:
    sandbox = WorkspaceSandbox(tmp_path)
    sandbox.atomic_write("memory.md", "hello", suffixes={".md"})
    assert sandbox.read_text("memory.md", suffixes={".md"}) == "hello"
