import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.browser.lock import BrowserExecutionLock, BrowserLockBusy


def test_same_process_contention_never_sends_process_signals(tmp_path, monkeypatch):
    def forbidden(*args):
        raise AssertionError("Lock must never signal any process")
    monkeypatch.setattr(os, "kill", forbidden)
    path = tmp_path / "browser.lock"
    with BrowserExecutionLock(path, "search"):
        with pytest.raises(BrowserLockBusy):
            BrowserExecutionLock(path, "delivery").acquire()
    with BrowserExecutionLock(path, "delivery"):
        assert path.exists()


def run_child(path: Path, script: str):
    return subprocess.run(
        [sys.executable, "-c", script, str(path)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=10,
    )


def test_cross_process_contention_and_exit_release(tmp_path):
    path = tmp_path / "browser.lock"
    contender = """
import sys
from pathlib import Path
from app.browser.lock import BrowserExecutionLock, BrowserLockBusy
try:
    with BrowserExecutionLock(Path(sys.argv[1]), 'child'):
        sys.exit(7)
except BrowserLockBusy:
    sys.exit(0)
"""
    with BrowserExecutionLock(path, "parent"):
        result = run_child(path, contender)
        assert result.returncode == 0, result.stderr
    crashed = run_child(path, """
import os, sys
from pathlib import Path
from app.browser.lock import BrowserExecutionLock
lock = BrowserExecutionLock(Path(sys.argv[1]), 'crashed')
lock.acquire()
os._exit(23)
""")
    assert crashed.returncode == 23, crashed.stderr
    with BrowserExecutionLock(path, "recovered"):
        pass


def test_stale_or_partial_lock_metadata_does_not_block_recovery(tmp_path):
    path = tmp_path / "browser.lock"
    path.write_text('{"pid":', encoding="utf-8")
    with BrowserExecutionLock(path, "new"):
        pass
