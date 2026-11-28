from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from ..core.security import redact


TERMINAL = {"completed", "failed", "cancelled", "timed_out", "interrupted", "outcome_indeterminate"}
STATUSES = TERMINAL | {"queued", "running", "waiting_user", "waiting_reply", "waiting_confirmation", "cancelling"}


def now() -> str:
    return datetime.now(UTC).isoformat()


def safe_json(value) -> str:
    """Lossless internal serialization; 'safe' means valid JSON, not redacted.

    Privacy projections belong at output boundaries, not in execution state.
    """
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
        default=lambda item: item.model_dump(mode="json") if hasattr(item, "model_dump") else str(item))


class RunStore:
    def __init__(self, path: Path, data_key: str | None = None):
        self.path = path
        self.cipher = Fernet(data_key.encode()) if data_key else None
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
                CREATE TABLE IF NOT EXISTS action_approvals (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, session_id TEXT NOT NULL,
                    step_id TEXT NOT NULL, snapshot TEXT NOT NULL, status TEXT NOT NULL,
                    source TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS action_approvals_owner ON action_approvals(run_id, step_id);
                CREATE TABLE IF NOT EXISTS session_memories (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES agent_sessions(id),
                    start_seq INTEGER NOT NULL, end_seq INTEGER NOT NULL,
                    payload TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(session_id,end_seq)
                );
            """)
            db.execute("INSERT OR IGNORE INTO agent_sessions VALUES (?,?,?)", ("default", "默认会话", now()))
            if "error_type" not in {row[1] for row in db.execute("PRAGMA table_info(agent_runs)")}:
                db.execute("ALTER TABLE agent_runs ADD COLUMN error_type TEXT")

    def _encode(self, value) -> str:
        encoded = safe_json(value)
        return "enc:v1:" + self.cipher.encrypt(encoded.encode()).decode() if self.cipher else encoded

    def _load(self, encoded: str):
        if encoded.startswith("enc:v1:"):
            if self.cipher is None:
                raise RuntimeError("AUTOHUNT_DATA_KEY is required to read encrypted runtime state")
            try:
                encoded = self.cipher.decrypt(encoded[7:].encode()).decode()
            except InvalidToken as exc:
                raise RuntimeError("Runtime state could not be decrypted; execution is not resumed") from exc
        return json.loads(encoded)

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
        session = {"id": uuid.uuid4().hex, "title": title.strip()[:80] or "新会话", "created_at": now()}
        with self.lock, self.connect() as db:
            db.execute("INSERT INTO agent_sessions VALUES (?,?,?)", tuple(session.values()))
        return session

    def search_state(self, session_id: str) -> dict | None:
        from .filter_policy_migration import project_conditions
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM agent_sessions WHERE id=?", (session_id,)).fetchone():
                raise KeyError("会话不存在")
            row = db.execute("SELECT conditions FROM session_search_state WHERE session_id=?", (session_id,)).fetchone()
            return project_conditions(self._load(row[0]), repairable=True) if row else None

    def save_search_state(self, session_id: str, conditions: dict) -> dict:
        from .search_state import SearchState
        conditions = SearchState.model_validate(conditions).model_dump(mode="json")
        with self.lock, self.connect() as db:
            if not db.execute("SELECT 1 FROM agent_sessions WHERE id=?", (session_id,)).fetchone():
                raise KeyError("会话不存在")
            previous_row = db.execute("SELECT conditions FROM session_search_state WHERE session_id=?", (session_id,)).fetchone()
            previous = self._load(previous_row[0]) if previous_row else None
            conditions["revision"] = (previous or {}).get("revision", 0)
            if previous == conditions:
                return conditions
            conditions["revision"] += 1
            db.execute("INSERT INTO session_search_state VALUES (?,?,?) ON CONFLICT(session_id) DO UPDATE SET conditions=excluded.conditions,updated_at=excluded.updated_at",
                       (session_id, self._encode(conditions), now()))
        return conditions

    def dialogue_state(self, session_id: str) -> dict:
        from .dialogue import DialogueState
        from .filter_policy_migration import project_dialogue
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM agent_sessions WHERE id=?", (session_id,)).fetchone():
                raise KeyError("会话不存在")
            row = db.execute("SELECT state FROM session_dialogue_state WHERE session_id=?", (session_id,)).fetchone()
        return DialogueState.model_validate(project_dialogue(self._load(row[0]) if row else {},
            session_id=session_id, repairable=True)).model_dump(mode="json")

    def mutate_dialogue(self, session_id: str, operation: dict) -> dict:
        from .dialogue import apply_operation
        from .filter_policy_migration import project_dialogue
        # Background watch updates and foreground turns share atomic read-modify-write.
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM agent_sessions WHERE id=?", (session_id,)).fetchone():
                raise KeyError("会话不存在")
            row = db.execute("SELECT state FROM session_dialogue_state WHERE session_id=?", (session_id,)).fetchone()
            value = apply_operation(self._load(row[0]) if row else None, operation)
            value = project_dialogue(value, session_id=session_id, repairable=True)
            encoded = self._encode(value)
            db.execute("INSERT INTO session_dialogue_state VALUES (?,?,?) ON CONFLICT(session_id) DO UPDATE SET state=excluded.state,updated_at=excluded.updated_at",
                       (session_id, encoded, now()))
            return self._load(encoded)

    def create(self, session_id: str, request_key: str, kind: str, payload: dict, parent_id: str | None = None, *, control: bool = False) -> tuple[dict, bool]:
        digest = hashlib.sha256(safe_json(payload).encode()).hexdigest()
        encoded = self._encode(payload)
        with self.lock, self.connect() as db:
            if not db.execute("SELECT 1 FROM agent_sessions WHERE id=?", (session_id,)).fetchone():
                raise KeyError("会话不存在")
            previous = db.execute("SELECT * FROM agent_runs WHERE session_id=? AND request_key=?", (session_id, request_key)).fetchone()
            if previous:
                # Legacy request hashes were calculated from redacted JSON. Compare
                # decoded canonical content; never infer missing/masked originals.
                if self._load(previous["payload"]) != payload or previous["kind"] != kind:
                    raise ValueError("同一请求 ID 已用于不同内容")
                return self.decode(previous), False
            # Persisted HR monitors do not occupy the execution admission budget.
            active = db.execute("SELECT COUNT(*) FROM agent_runs WHERE status IN ('queued','running','waiting_user','waiting_confirmation','cancelling')").fetchone()[0]
            if active >= 100 and not control:
                raise ValueError("运行队列已满，请先处理或停止已有任务")
            if parent_id:
                parent = db.execute("SELECT session_id FROM agent_runs WHERE id=?", (parent_id,)).fetchone()
                if not parent or parent[0] != session_id:
                    raise ValueError("接续来源必须属于同一会话")
            run_id = uuid.uuid4().hex
            timestamp = now()
            db.execute("""INSERT INTO agent_runs
                (id,session_id,request_key,request_hash,kind,payload,status,parent_id,created_at,updated_at)
                VALUES (?,?,?,?,?,?,'queued',?,?,?)""",
                (run_id, session_id, request_key, digest, kind, encoded, parent_id, timestamp, timestamp))
        self.event(run_id, "status", {"status": "queued", "detail": "任务已进入队列"})
        return self.get(run_id), True

    def decode(self, row) -> dict:
        item = dict(row)
        for name in ("payload", "result", "checkpoint"):
            item[name] = self._load(item[name]) if item[name] else None
        if item.get("error", "") and item["error"].startswith("enc:v1:"):
            item["error"] = self._load(item["error"])
        return item

    def get(self, run_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError("运行记录不存在")
        return self.decode(row)

    def list(self, session_id: str | None = None, *, active: bool = False, limit: int | None = 500) -> list[dict]:
        with self.connect() as db:
            condition = " AND status NOT IN ('completed','failed','cancelled','timed_out','interrupted','outcome_indeterminate')" if active else ""
            rows = db.execute("SELECT * FROM agent_runs WHERE (? IS NULL OR session_id=?)" + condition + " ORDER BY created_at DESC" +
                              (" LIMIT ?" if limit is not None else ""),
                              (session_id, session_id, limit) if limit is not None else (session_id, session_id)).fetchall()
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
                payload, result = self._load(row["payload"]), self._load(row["result"] or "{}")
                checkpoint = self._load(row["checkpoint"] or "{}")
                if payload.get("control_kind") in {"stop", "progress"} or checkpoint.get("phase") == "control_result":
                    continue  # A progress question is not a replacement task request.
                started = bool(row["mutation_started"])
                if started and is_continuation(payload.get("message", "")) and not any(checkpoint.get(k) for k in ("browser_id", "delivery_id", "plan")):
                    facts = [{"kind": event["kind"], **self._load(event["data"])} for event in db.execute(
                        "SELECT kind,data FROM agent_run_events WHERE run_id=? AND kind IN ('tool_started','tool_finished') ORDER BY seq", (row["id"],))]
                    if proven_idle_resume(facts):
                        started = False
                output.append({"id": row["id"], "message": payload.get("message", ""), "status": row["status"],
                    "execution_started": started, "reply": result.get("message", ""),
                    "created_at": row["created_at"], "resume_stamp": (checkpoint.get("plan") or {}).get("resume_stamp", "")})
        return output

    def memory_source_batch(self, session_id: str, *, epoch: float = 0, keep_recent=8, batch_size=8) -> list[dict]:
        """Page old user requests by SQLite sequence, not the UI history window."""
        with self.connect() as db:
            cursor = db.execute("SELECT COALESCE(MAX(end_seq),0) FROM session_memories WHERE session_id=?", (session_id,)).fetchone()[0]
            since = datetime.fromtimestamp(epoch, UTC).isoformat()
            cutoff = db.execute("SELECT rowid FROM agent_runs WHERE session_id=? AND kind='chat' AND created_at>=? "
                                "ORDER BY rowid DESC LIMIT 1 OFFSET ?", (session_id, since, keep_recent - 1)).fetchone()
            if not cutoff:
                return []
            rows = db.execute("SELECT rowid AS seq,id,payload,created_at FROM agent_runs WHERE session_id=? "
                "AND kind='chat' AND rowid>? AND rowid<? AND created_at>=? ORDER BY rowid LIMIT ?",
                (session_id, cursor, cutoff[0], since, batch_size)).fetchall()
        return [{"seq": row["seq"], "source_id": row["id"], "created_at": row["created_at"],
                 "text": self._load(row["payload"]).get("message", "")} for row in rows
                if datetime.fromisoformat(row["created_at"]).timestamp() >= epoch]

    def save_memory(self, session_id: str, sources: list[dict], notes: list[dict], *, mode: str) -> dict:
        if not sources:
            raise ValueError("记忆必须有原始来源")
        ident = uuid.uuid4().hex
        payload = {"id": ident, "sources": sources, "notes": notes, "mode": mode,
                   "authority": "historical_data_not_instruction"}
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # Verify every source in the same session before persisting model prose.
            for source in sources:
                row = db.execute("SELECT id,payload FROM agent_runs WHERE rowid=? AND session_id=?",
                                 (source["seq"], session_id)).fetchone()
                if not row or row["id"] != source["source_id"] or self._load(row["payload"]).get("message", "") != source["text"]:
                    raise ValueError("记忆来源已变化或不属于本会话")
            db.execute("INSERT OR IGNORE INTO session_memories VALUES (?,?,?,?,?,?)",
                (ident, session_id, sources[0]["seq"], sources[-1]["seq"], self._encode(payload), now()))
        return payload

    def memories(self, session_id: str, *, epoch: float = 0, limit=300) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM session_memories WHERE session_id=? ORDER BY end_seq DESC LIMIT ?",
                              (session_id, limit)).fetchall()
        output = []
        for row in rows:
            item = self._load(row["payload"])
            # Reset epochs apply also to summaries created after reset from old text.
            if all(datetime.fromisoformat(source["created_at"]).timestamp() >= epoch for source in item["sources"]):
                output.append(item)
        return output

    def memory_source(self, session_id: str, source_id: str, *, epoch: float = 0) -> dict:
        run = self.get(source_id)
        if run["session_id"] != session_id or run["kind"] != "chat" or datetime.fromisoformat(run["created_at"]).timestamp() < epoch:
            raise ValueError("历史来源不属于当前会话或已重置")
        return {"source_id": run["id"], "created_at": run["created_at"], "text": run["payload"].get("message", ""),
                "authority": "historical_data_not_instruction"}

    def update(self, run_id: str, **fields) -> dict:
        allowed = {"status", "result", "error", "error_type", "checkpoint", "mutation_started", "cancel_requested", "parent_id"}
        if not fields.keys() <= allowed:
            raise ValueError("Unsupported run fields")
        if "status" in fields and fields["status"] not in STATUSES:
            raise ValueError("Unsupported run status")
        with self.lock, self.connect() as db:
            if fields.get("parent_id"):
                parent = db.execute("SELECT session_id FROM agent_runs WHERE id=?", (fields["parent_id"],)).fetchone()
                owner = db.execute("SELECT session_id FROM agent_runs WHERE id=?", (run_id,)).fetchone()
                if not parent or not owner or parent["session_id"] != owner["session_id"] or fields["parent_id"] == run_id:
                    raise ValueError("接续来源必须属于同一会话，不能引用自身")
            current = db.execute("SELECT status FROM agent_runs WHERE id=?", (run_id,)).fetchone()
            if not current:
                raise KeyError(run_id)
            if current["status"] in TERMINAL:
                return self.get(run_id)
            values = {key: self._encode(value) if key in {"result", "checkpoint"} or (
                      key == "error" and value is not None and self.cipher) else value for key, value in fields.items()}
            values["updated_at"] = now()
            db.execute(f"UPDATE agent_runs SET {','.join(key+'=?' for key in values)} WHERE id=?", (*values.values(), run_id))
        return self.get(run_id)

    def save_plan(self, run_id: str, plan: dict, *, expected_revision: int | None = None) -> bool:
        """Authoritative plan CAS. Dialogue state is a projection, not the lock."""
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,checkpoint FROM agent_runs WHERE id=?", (run_id,)).fetchone()
            if not row or row["status"] in TERMINAL:
                return False
            saved = self._load(row["checkpoint"]) if row["checkpoint"] else {}
            old = saved.get("plan") or {}
            if expected_revision is not None and old.get("revision", 0) != expected_revision:
                raise ValueError("计划版本已变化，请重新读取后合并修订")
            if old.get("id") == plan["id"] and old.get("revision", 0) > plan.get("revision", 0):
                return False
            saved.update(phase="action_plan", plan=plan)
            db.execute("UPDATE agent_runs SET checkpoint=?,updated_at=? WHERE id=?", (self._encode(saved), now(), run_id))
        return True

    def event(self, run_id: str, kind: str, data: dict):
        from ..llm.tracing import PRIVATE_MODEL_EVENTS, public_model_data
        if kind in PRIVATE_MODEL_EVENTS and self.cipher is None:
            data = {**public_model_data(data), "details_omitted": "runtime_encryption_not_configured"}
        with self.lock, self.connect() as db:
            db.execute("INSERT INTO agent_run_events (run_id,kind,data,created_at) VALUES (?,?,?,?)",
                       (run_id, kind, self._encode(data), now()))

    def all_events(self, run_id: str) -> list[dict]:
        result, cursor = [], 0
        while page := self.events(run_id, cursor):
            result.extend(page)
            cursor = page[-1]["seq"]
        return result

    def execution_ledger(self, run_id: str, *, include_parents: bool = False, model_rates: dict | None = None) -> dict:
        from .ledger import execution_ledger
        current = self.get(run_id)
        runs, seen = [current], {run_id}
        lineage_truncated = False
        while include_parents and runs[-1].get("parent_id"):
            parent_id = runs[-1]["parent_id"]
            if parent_id in seen or len(runs) >= 32:
                lineage_truncated = True
                break
            parent = self.get(parent_id)
            if parent["session_id"] != current["session_id"]:
                raise ValueError("执行账本不能读取其他会话的接续记录")
            runs.append(parent)
            seen.add(parent_id)
        # Read projection only: no checkpoint/authority mutation or event writes.
        result = execution_ledger(runs, [event for run in reversed(runs) for event in self.all_events(run["id"])],
                                  model_rates=model_rates)
        return {**result, "lineage_truncated": lineage_truncated}

    def events(self, run_id: str, after: int = 0) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM agent_run_events WHERE run_id=? AND seq>? ORDER BY seq LIMIT 200", (run_id, after)).fetchall()
        return [{**dict(row), "data": self._load(row["data"])} for row in rows]

    def public_events(self, run_id: str, after: int = 0) -> list[dict]:
        """Boundary copy for diagnostic HTTP/SSE; internal consumers use events()."""
        from ..llm.tracing import PRIVATE_MODEL_EVENTS, public_model_data
        events = self.events(run_id, after)
        for event in events:
            if event["kind"] in PRIVATE_MODEL_EVENTS:
                event["data"] = {**public_model_data(event["data"]), "details_internal": True}
        return redact(events)

    def approval(self, approval_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM action_approvals WHERE id=?", (approval_id,)).fetchone()
        if not row:
            raise KeyError("确认记录不存在")
        return {**dict(row), "snapshot": self._load(row["snapshot"])}

    def approvals(self, run_id: str | None = None, *, session_id: str | None = None, pending=False) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT id FROM action_approvals WHERE (? IS NULL OR run_id=?) "
                "AND (? IS NULL OR session_id=?)" + (" AND status='pending'" if pending else "") +
                " ORDER BY created_at", (run_id, run_id, session_id, session_id)).fetchall()
        return [self.approval(row["id"]) for row in rows]

    def ensure_approval(self, run_id: str, session_id: str, step_id: str, snapshot: dict, *, requested=False) -> dict:
        """Authority comes from server-owned action snapshots, never model tokens.

        Plan revision is intentionally absent: an unrelated edit cannot invalidate
        this action. Business parameters, evidence and resume are compared losslessly.
        """
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = db.execute("SELECT session_id,status,cancel_requested FROM agent_runs WHERE id=?", (run_id,)).fetchone()
            if not owner or owner["session_id"] != session_id or owner["status"] in TERMINAL or owner["cancel_requested"]:
                raise ValueError("任务已停止或不属于本会话")
            rows = db.execute("SELECT * FROM action_approvals WHERE run_id=? AND step_id=? ORDER BY created_at DESC",
                              (run_id, step_id)).fetchall()
            # A rejected scope cannot be silently revived by a retry. A new explicit
            # task/action has a new identity and can request it again.
            if rows and self._load(rows[0]["snapshot"]) == snapshot and rows[0]["status"] != "invalidated":
                item = {**dict(rows[0]), "snapshot": snapshot}
                if requested and item["status"] == "pending":
                    db.execute("UPDATE action_approvals SET status='approved',source='instruction',updated_at=? "
                               "WHERE id=? AND status='pending'", (now(), item["id"]))
                    item.update(status="approved", source="instruction")
                return item
            db.execute("UPDATE action_approvals SET status='invalidated',updated_at=? "
                       "WHERE run_id=? AND step_id=? AND status IN ('pending','approved')", (now(), run_id, step_id))
            approval_id, timestamp = uuid.uuid4().hex, now()
            status, source = ("approved", "instruction") if requested else ("pending", "confirmation")
            db.execute("INSERT INTO action_approvals VALUES (?,?,?,?,?,?,?,?,?)", (approval_id, run_id, session_id,
                step_id, self._encode(snapshot), status, source, timestamp, timestamp))
        return self.approval(approval_id)

    def settle_approval(self, approval_id: str, *, run_id: str, session_id: str, approve: bool, snapshot: dict) -> dict:
        """CAS decision; double clicks are idempotent, stale/foreign decisions fail."""
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM action_approvals WHERE id=?", (approval_id,)).fetchone()
            owner = db.execute("SELECT session_id,status,cancel_requested FROM agent_runs WHERE id=?", (run_id,)).fetchone()
            if not row or row["run_id"] != run_id or row["session_id"] != session_id or not owner or owner["session_id"] != session_id:
                raise ValueError("确认记录不属于此任务和会话")
            if owner["status"] in TERMINAL or owner["cancel_requested"] or self._load(row["snapshot"]) != snapshot:
                raise ValueError("动作范围已变化或任务已停止，请核对当前记录")
            status = "approved" if approve else "rejected"
            if row["status"] != "pending":
                if row["status"] != status:
                    raise ValueError("该确认已处理或失效")
            else:
                db.execute("UPDATE action_approvals SET status=?,source='confirmation',updated_at=? WHERE id=? AND status='pending'",
                           (status, now(), approval_id))
        return self.approval(approval_id)

    def invalidate_approvals(self, run_id: str, step_ids: list[str] | None = None) -> None:
        with self.lock, self.connect() as db:
            db.execute("UPDATE action_approvals SET status='invalidated',updated_at=? WHERE run_id=? "
                "AND status IN ('pending','approved')" + (" AND step_id IN (" + ",".join("?" for _ in step_ids) + ")" if step_ids else ""),
                (now(), run_id, *(step_ids or [])))
