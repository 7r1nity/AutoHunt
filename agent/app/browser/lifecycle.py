"""Ordered metadata only: no cookies, message contents, URL queries or credentials."""
from __future__ import annotations

import json
import time
import weakref
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

class BrowserLifecycle:
    def __init__(self, path: Path):
        self.path = path
        self.sequence = 0
        self.session_id = uuid.uuid4().hex
        self.next_page_id = 0
        self.pages = weakref.WeakKeyDictionary()

    def record(self, event: str, *, page_id: int | None = None, url: str = ""):
        parsed = urlsplit(url)
        self.sequence += 1
        payload = {
            "at": datetime.now(UTC).isoformat(),
            "monotonic": time.monotonic(),
            "sequence": self.sequence,
            "session_id": self.session_id,
            "event": event,
            "page_id": page_id,
            "origin": f"{parsed.scheme}://{parsed.hostname or ''}" if parsed.scheme else "",
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError:
            pass  # Diagnostics must never break browser operations.

    def watch_page(self, page):
        if page in self.pages:
            return
        self.next_page_id += 1
        page_id = self.next_page_id
        self.pages[page] = page_id
        self.record("page.attached", page_id=page_id, url=page.url)
        page.on("close", lambda *_: self.record("page.close", page_id=page_id, url=page.url))
        page.on("crash", lambda *_: self.record("page.crash", page_id=page_id, url=page.url))
        page.on("framenavigated", lambda frame: self.record(
            "page.navigation", page_id=page_id, url=page.url
        ) if frame == page.main_frame else None)

    def watch_context(self, context):
        self.record("context.attached")
        context.on("close", lambda *_: self.record("context.close"))
        context.on("page", self.watch_page)
        for page in context.pages:
            self.watch_page(page)
