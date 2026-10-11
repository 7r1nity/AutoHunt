from __future__ import annotations

import json
import os
from pathlib import Path


class BrowserLockBusy(RuntimeError):
    pass


class BrowserExecutionLock:
    """OS-owned lease; crash releases it without probing or killing any PID.

    Keep the file in place: unlinking it would allow two different inodes to be
    locked simultaneously on POSIX. Its JSON is diagnostic, not lock ownership.
    Every browser worker must use the same data root, including delivery workers.
    """

    def __init__(self, path: Path, task_id: str) -> None:
        self.path = path
        self.task_id = task_id
        self.acquired = False
        self._descriptor: int | None = None

    def acquire(self) -> None:
        if self.acquired:
            raise BrowserLockBusy("当前实例已经持有浏览器执行锁")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(descriptor)
            raise BrowserLockBusy("另一个浏览器执行任务正在运行，或锁暂时不可用") from exc
        self._descriptor = descriptor
        self.acquired = True
        try:
            payload = json.dumps({"pid": os.getpid(), "task_id": self.task_id}).encode()
            os.write(descriptor, payload)
            os.ftruncate(descriptor, len(payload))
        except BaseException:
            self.release()
            raise

    def release(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
            self._descriptor = None
            self.acquired = False

    def __enter__(self) -> "BrowserExecutionLock":
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
