from __future__ import annotations

import os
import tempfile
from pathlib import Path


class SandboxViolation(ValueError):
    pass


class WorkspaceSandbox:
    """A file-only sandbox. It intentionally exposes no shell or subprocess tool."""

    def __init__(self, root: Path, *, max_bytes: int = 50_000) -> None:
        self.root = root.resolve()
        self.max_bytes = max_bytes
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, relative_path: str, *, suffixes: set[str]) -> Path:
        candidate = (self.root / relative_path).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise SandboxViolation("Path escapes the sandbox root")
        if candidate.suffix.lower() not in suffixes:
            raise SandboxViolation("File type is not allowed")
        return candidate

    def read_text(self, relative_path: str, *, suffixes: set[str]) -> str:
        path = self.resolve(relative_path, suffixes=suffixes)
        if not path.exists():
            raise FileNotFoundError(relative_path)
        if path.stat().st_size > self.max_bytes:
            raise SandboxViolation("File exceeds sandbox size limit")
        return path.read_text(encoding="utf-8")

    def atomic_write(self, relative_path: str, content: str, *, suffixes: set[str]) -> Path:
        encoded = content.encode("utf-8")
        if len(encoded) > self.max_bytes:
            raise SandboxViolation("Content exceeds sandbox size limit")
        path = self.resolve(relative_path, suffixes=suffixes)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return path

