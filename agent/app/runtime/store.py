from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from ..core.security import redact, redact_text


TERMINAL = {"completed", "failed", "cancelled", "timed_out", "interrupted", "outcome_indeterminate"}
STATUSES = TERMINAL | {"queued", "running", "waiting_user", "waiting_reply", "waiting_confirmation", "cancelling"}


def now() -> str:
    return datetime.now(UTC).isoformat()


def safe_json(value) -> str:
    normalized = json.loads(json.dumps(value, ensure_ascii=False, default=lambda item:
                                      item.model_dump(mode="json") if hasattr(item, "model_dump") else str(item)))
    def sanitize(item, key=""):
        if isinstance(item, dict):
            return {name: sanitize(content, name) for name, content in item.items()}
        if isinstance(item, list):
            return [sanitize(content) for content in item]
        if isinstance(item, str):
            if key in {"id", "run_id", "browser_id", "delivery_id", "tool_call_id", "proposal_id", "plan_id", "resume_stamp", "action_id", "snapshot_id", "source"} and re.fullmatch(r"[A-Za-z0-9_:-]{1,128}", item):
                return item
            return redact_text(item)
        return item
    return json.dumps(sanitize(normalized), ensure_ascii=False)


class RunStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS agent_sessions (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_runs (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, request_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
                    status TEXT NOT NULL, result TEXT, error TEXT, checkpoint TEXT,
                    error_type TEXT,
                    mutation_started INTEGER NOT NULL DEFAULT 0, cancel_requested INTEGER NOT NULL DEFAULT 0,
                    parent_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(session_id, request_key)
                );
                CREATE INDEX IF NOT EXISTS agent_runs_session ON agent_runs(session_id, created_at);
                CREATE TABLE IF NOT EXISTS agent_run_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    kind TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS agent_events_run ON agent_run_events(run_id, seq);
                CREATE TABLE IF NOT EXISTS session_search_state (
                    session_id TEXT PRIMARY KEY REFERENCES agent_sessions(id),
                    conditions TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS session_dialogue_state (
                    session_id TEXT PRIMARY KEY REFERENCES agent_sessions(id),
                    state TEXT NOT NULL, updated_at TEXT NOT NULL
                );
            """)
            db.execute("INSERT OR IGNORE INTO agent_sessions VALUES (?,?,?)", ("default", "默认会话", now()))
            if "error_type" not in {row[1] for row in db.execute("PRAGMA table_info(agent_runs)")}:
                db.execute("ALTER TABLE agent_runs ADD COLUMN error_type TEXT")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def has_event(self, run_id: str, kind: str) -> bool:
        with self.connect() as db:
            return db.execute("SELECT 1 FROM agent_run_events WHERE run_id=? AND kind=? LIMIT 1", (run_id, kind)).fetchone() is not None

    def sessions(self) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM agent_sessions ORDER BY created_at DESC")]

    def ensure_default_session(self) -> None:
        with self.lock, self.connect() as db:
            db.execute("INSERT OR IGNORE INTO agent_sessions VALUES (?,?,?)", ("default", "默认会话", now()))

    def create_session(self, title: str) -> dict:
        session = {"id": uuid.uuid4().hex, "title": redact_text(title.strip())[:80] or "新会话", "created_at": now()}
        with self.lock, self.connect() as db:
            db.execute("INSERT INTO agent_sessions VALUES (?,?,?)", tuple(session.values()))
        return session

    def search_state(self, session_id: str) -> dict | None:
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM agent_sessions WHERE id=?", (session_id,)).fetchone():
                raise KeyError("会话不存在")
            row = db.execute("SELECT conditions FROM session_search_state WHERE session_id=?", (session_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def save_search_state(self, session_id: str, conditions: dict) -> dict:
        from .search_state import SearchState
        conditions = SearchState.model_validate(conditions).model_dump(mode="json")
        with self.lock, self.connect() as db:
            previous = self.search_state(session_id)
            conditions["revision"] = (previous or {}).get("revision", 0)
            if previous == conditions:
                return conditions
            conditions["revision"] += 1
            db.execute("INSERT INTO session_search_state VALUES (?,?,?) ON CONFLICT(session_id) DO UPDATE SET conditions=excluded.conditions,updated_at=excluded.updated_at",
                       (session_id, safe_json(conditions), now()))
        return conditions

    def dialogue_state(self, session_id: str) -> dict:
        from .dialogue import DialogueState
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM agent_sessions WHERE id=?", (session_id,)).fetchone():
                raise KeyError("会话不存在")
            row = db.execute("SELECT state FROM session_dialogue_state WHERE session_id=?", (session_id,)).fetchone()
        return DialogueState.model_validate(json.loads(row[0]) if row else {}).model_dump(mode="json")

    def mutate_dialogue(self, session_id: str, operation: dict) -> dict:
        from .dialogue import apply_operation
        # Background watch updates and foreground turns share atomic read-modify-write.
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM agent_sessions WHERE id=?", (session_id,)).fetchone():
                raise KeyError("会话不存在")
            row = db.execute("SELECT state FROM session_dialogue_state WHERE session_id=?", (session_id,)).fetchone()
            value = apply_operation(json.loads(row[0]) if row else None, operation)
            encoded = safe_json(value)
            db.execute("INSERT INTO session_dialogue_state VALUES (?,?,?) ON CONFLICT(session_id) DO UPDATE SET state=excluded.state,updated_at=excluded.updated_at",
                       (session_id, encoded, now()))
            return json.loads(encoded)

    def create(self, session_id: str, request_key: str, kind: str, payload: dict, parent_id: str | None = None, *, control: bool = False) -> tuple[dict, bool]:
        encoded = json.dumps(redact(payload), ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        with self.lock, self.connect() as db:
            if not db.execute("SELECT 1 FROM agent_sessions WHERE id=?", (session_id,)).fetchone():
                raise KeyError("会话不存在")
            previous = db.execute("SELECT * FROM agent_runs WHERE session_id=? AND request_key=?", (session_id, request_key)).fetchone()
            if previous:
                if previous["request_hash"] != digest or previous["kind"] != kind:
                    raise ValueError("同一请求 ID 已用于不同内容")
                return self.decode(previous), False
            active = db.execute("SELECT COUNT(*) FROM agent_runs WHERE status IN ('queued','running','waiting_user','waiting_reply','waiting_confirmation','cancelling')").fetchone()[0]
            if active >= 100 and not control:
                raise ValueError("运行队列已满，请先处理或停止已有任务")
            run_id = uuid.uuid4().hex
            timestamp = now()
            db.execute("""INSERT INTO agent_runs
                (id,session_id,request_key,request_hash,kind,payload,status,parent_id,created_at,updated_at)
                VALUES (?,?,?,?,?,?,'queued',?,?,?)""",
                (run_id, session_id, request_key, digest, kind, encoded, parent_id, timestamp, timestamp))
        self.event(run_id, "status", {"status": "queued", "detail": "任务已进入队列"})
        return self.get(run_id), True

    @staticmethod
    def decode(row) -> dict:
        item = dict(row)
        for name in ("payload", "result", "checkpoint"):
            item[name] = json.loads(item[name]) if item[name] else None
        return item

    def get(self, run_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError("运行记录不存在")
        return self.decode(row)

    def list(self, session_id: str | None = None, *, active: bool = False) -> list[dict]:
        with self.connect() as db:
            condition = " AND status NOT IN ('completed','failed','cancelled','timed_out','interrupted','outcome_indeterminate')" if active else ""
            rows = db.execute("SELECT * FROM agent_runs WHERE (? IS NULL OR session_id=?)" + condition + " ORDER BY created_at DESC LIMIT 500",
                              (session_id, session_id)).fetchall()
        runs = [self.decode(row) for row in rows]
        return runs

    def request_records(self, session_id: str) -> list[dict]:
        """Task facts for reference resolution, not additional model chat context."""
        from ..workflow.requests import is_continuation, proven_idle_resume
        with self.connect() as db:
            rows = db.execute("SELECT id,payload,status,mutation_started,result,created_at,checkpoint FROM agent_runs "
                "WHERE session_id=? AND kind='chat' ORDER BY created_at DESC LIMIT 500", (session_id,)).fetchall()
            output = []
            for row in reversed(rows):
                payload, result = json.loads(row["payload"]), json.loads(row["result"] or "{}")
                checkpoint = json.loads(row["checkpoint"] or "{}")
                started = bool(row["mutation_started"])
                if started and is_continuation(payload.get("message", "")) and not any(checkpoint.get(k) for k in ("browser_id", "delivery_id", "plan")):
                    facts = [{"kind": event["kind"], **json.loads(event["data"])} for event in db.execute(
                        "SELECT kind,data FROM agent_run_events WHERE run_id=? AND kind IN ('tool_started','tool_finished') ORDER BY seq", (row["id"],))]
                    if proven_idle_resume(facts):
                        started = False
                output.append({"id": row["id"], "message": payload.get("message", ""), "status": row["status"],
                    "execution_started": started, "reply": result.get("message", ""),
                    "created_at": row["created_at"], "resume_stamp": (checkpoint.get("plan") or {}).get("resume_stamp", "")})
        return output

    def update(self, run_id: str, **fields) -> dict:
        allowed = {"status", "result", "error", "error_type", "checkpoint", "mutation_started", "cancel_requested"}
        if not fields.keys() <= allowed:
            raise ValueError("Unsupported run fields")
        if "status" in fields and fields["status"] not in STATUSES:
            raise ValueError("Unsupported run status")
        with self.lock, self.connect() as db:
            current = db.execute("SELECT status FROM agent_runs WHERE id=?", (run_id,)).fetchone()
            if not current:
                raise KeyError(run_id)
            if current["status"] in TERMINAL:
                return self.get(run_id)
            values = {key: safe_json(value) if key in {"result", "checkpoint"}
                      else redact_text(value) if isinstance(value, str) else value for key, value in fields.items()}
            values["updated_at"] = now()
            db.execute(f"UPDATE agent_runs SET {','.join(key+'=?' for key in values)} WHERE id=?", (*values.values(), run_id))
        return self.get(run_id)

    def event(self, run_id: str, kind: str, data: dict):
        with self.lock, self.connect() as db:
            db.execute("INSERT INTO agent_run_events (run_id,kind,data,created_at) VALUES (?,?,?,?)",
                       (run_id, kind, safe_json(data), now()))

    def events(self, run_id: str, after: int = 0) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM agent_run_events WHERE run_id=? AND seq>? ORDER BY seq LIMIT 200", (run_id, after)).fetchall()
        return [{**dict(row), "data": json.loads(row["data"])} for row in rows]
