from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Awaitable, Callable

from ..core.models import AgentLane, TaskRecord, TaskStatus


TaskHandler = Callable[[dict], Awaitable[dict]]


class TaskBroker:
    """Bounded async broker. Agent lanes are logical boundaries, not free-running agents."""

    def __init__(self, *, worker_count: int = 2, queue_size: int = 100) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=queue_size)
        self.records: dict[str, TaskRecord] = {}
        self.handlers: dict[AgentLane, TaskHandler] = {}
        self.workers: list[asyncio.Task] = []
        self.worker_count = worker_count

    def register(self, lane: AgentLane, handler: TaskHandler) -> None:
        self.handlers[lane] = handler

    async def start(self) -> None:
        if not self.workers:
            self.workers = [asyncio.create_task(self._worker(), name=f"autohunt-worker-{i}") for i in range(self.worker_count)]

    async def stop(self) -> None:
        for worker in self.workers:
            worker.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        self.workers.clear()

    async def submit(self, lane: AgentLane, payload: dict) -> TaskRecord:
        if lane not in self.handlers:
            raise ValueError(f"No handler registered for lane: {lane}")
        task_id = uuid.uuid4().hex
        record = TaskRecord(id=task_id, lane=lane, status=TaskStatus.QUEUED, payload=payload)
        self.records[task_id] = record
        await self.queue.put(task_id)
        return record

    def get(self, task_id: str) -> TaskRecord | None:
        return self.records.get(task_id)

    async def _worker(self) -> None:
        while True:
            task_id = await self.queue.get()
            record = self.records[task_id]
            record.status = TaskStatus.RUNNING
            record.updated_at = datetime.now(UTC)
            try:
                record.result = await self.handlers[record.lane](record.payload)
                record.status = TaskStatus.COMPLETED
            except Exception as exc:  # the audit layer records only a redacted message
                record.error = type(exc).__name__
                record.status = TaskStatus.FAILED
            finally:
                record.updated_at = datetime.now(UTC)
                self.queue.task_done()
