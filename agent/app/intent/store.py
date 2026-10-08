"""SQLite persistence for the intent router: events, rule versions, hit stats,
three-way disagreement samples and clarify memory.

Follows the JobStore conventions (WAL, module-level lock, defensive schema
upgrades) and never raises into the routing hot path: every write is
best-effort so observability can never break a conversation.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class RouterStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self.lock, self._connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS router_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    query_hash TEXT NOT NULL,
                    layer TEXT NOT NULL,
                    intent TEXT NOT NULL,
                    confidence REAL,
                    margin REAL,
                    decision TEXT NOT NULL,
                    latency_ms REAL NOT NULL,
                    detail TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_router_events_hash ON router_events(query_hash);
                CREATE TABLE IF NOT EXISTS rule_versions (
                    version INTEGER PRIMARY KEY,
                    payload_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    activated_at TEXT NOT NULL,
                    note TEXT
                );
                CREATE TABLE IF NOT EXISTS active_rule_version (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rule_hit_stats (
                    rule_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    hits INTEGER NOT NULL DEFAULT 0,
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY (rule_id, version)
                );
                CREATE TABLE IF NOT EXISTS router_disagreements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    query_hash TEXT NOT NULL,
                    rule_intent TEXT,
                    small_intent TEXT,
                    small_conf REAL,
                    small_margin REAL,
                    final_intent TEXT,
                    user_outcome TEXT
                );
                CREATE TABLE IF NOT EXISTS clarify_memory (
                    query_hash TEXT PRIMARY KEY,
                    intent TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    source TEXT NOT NULL
                );
                """
            )

    # ------------------------------------------------------------------ events
    def record_event(
        self,
        *,
        query_hash: str,
        layer: str,
        intent: str,
        decision: str,
        latency_ms: float,
        confidence: float | None = None,
        margin: float | None = None,
        detail: str = "",
    ) -> None:
        try:
            with self.lock, self._connect() as db:
                db.execute(
                    "INSERT INTO router_events (ts,query_hash,layer,intent,confidence,margin,decision,latency_ms,detail) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (utc_now(), query_hash, layer, intent, confidence, margin, decision, latency_ms, detail[:300]),
                )
        except sqlite3.Error:
            pass  # observability is best-effort

    # ------------------------------------------------------------ rule versions
    def activate_version(
        self,
        version: int,
        payload_hash: str,
        note: str = "",
        *,
        baseline: int | None = None,
    ) -> None:
        with self.lock, self._connect() as db:
            previous = db.execute(
                "SELECT version FROM active_rule_version WHERE singleton=1"
            ).fetchone()
            if previous is not None:
                self._retire(db, int(previous["version"]))
            elif baseline is not None and baseline != version:
                # First activation: materialize the builtin baseline so a
                # rollback always has a previous version to return to.
                self._retire(db, baseline)
            db.execute(
                "INSERT INTO rule_versions (version,payload_hash,status,activated_at,note) "
                "VALUES (?,?,?,?,?)",
                (version, payload_hash, "active", utc_now(), note),
            )
            db.execute(
                "INSERT INTO active_rule_version (singleton,version) VALUES (1,?) "
                "ON CONFLICT(singleton) DO UPDATE SET version=excluded.version",
                (version,),
            )

    @staticmethod
    def _retire(db: sqlite3.Connection, version: int) -> None:
        """Mark a version retired, materializing the builtin v1 row if needed."""
        row = db.execute(
            "SELECT 1 FROM rule_versions WHERE version=?", (version,)
        ).fetchone()
        if row is None:
            db.execute(
                "INSERT INTO rule_versions (version,payload_hash,status,activated_at,note) "
                "VALUES (?,?,?,?,?)",
                (version, "builtin", "retired", utc_now(), "builtin rules"),
            )
        else:
            db.execute(
                "UPDATE rule_versions SET status='retired' WHERE version=?",
                (version,),
            )

    def rollback(self) -> int | None:
        """Re-activate the most recently retired version; returns it or None."""
        with self.lock, self._connect() as db:
            previous = db.execute(
                "SELECT version FROM rule_versions WHERE status='retired' "
                "ORDER BY activated_at DESC LIMIT 1"
            ).fetchone()
            if previous is None:
                return None
            current = db.execute(
                "SELECT version FROM active_rule_version WHERE singleton=1"
            ).fetchone()
            if current is not None and int(current["version"]) != int(previous["version"]):
                self._retire(db, int(current["version"]))
            db.execute(
                "UPDATE rule_versions SET status='active', activated_at=? WHERE version=?",
                (utc_now(), previous["version"]),
            )
            db.execute(
                "INSERT INTO active_rule_version (singleton,version) VALUES (1,?) "
                "ON CONFLICT(singleton) DO UPDATE SET version=excluded.version",
                (previous["version"],),
            )
            return int(previous["version"])

    def current_version(self) -> int | None:
        with self.lock, self._connect() as db:
            row = db.execute(
                "SELECT version FROM active_rule_version WHERE singleton=1"
            ).fetchone()
        return int(row["version"]) if row is not None else None

    # ---------------------------------------------------------------- hit stats
    def hit_rule(self, rule_id: str, version: int) -> None:
        try:
            with self.lock, self._connect() as db:
                db.execute(
                    "INSERT INTO rule_hit_stats (rule_id,version,hits,last_seen_at) VALUES (?,?,1,?) "
                    "ON CONFLICT(rule_id,version) DO UPDATE SET hits=hits+1, last_seen_at=excluded.last_seen_at",
                    (rule_id, version, utc_now()),
                )
        except sqlite3.Error:
            pass

    # ------------------------------------------------------------ disagreements
    def record_disagreement(
        self,
        *,
        query_hash: str,
        rule_intent: str | None,
        small_intent: str | None,
        small_conf: float | None,
        small_margin: float | None,
        final_intent: str | None,
        user_outcome: str | None = None,
    ) -> None:
        try:
            with self.lock, self._connect() as db:
                db.execute(
                    "INSERT INTO router_disagreements (ts,query_hash,rule_intent,small_intent,"
                    "small_conf,small_margin,final_intent,user_outcome) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        utc_now(), query_hash, rule_intent, small_intent,
                        small_conf, small_margin, final_intent, user_outcome,
                    ),
                )
        except sqlite3.Error:
            pass

    # ------------------------------------------------------------ clarify memory
    def remember_clarify(self, query_hash: str, intent: str, source: str = "user_choice") -> None:
        try:
            with self.lock, self._connect() as db:
                db.execute(
                    "INSERT INTO clarify_memory (query_hash,intent,created_at,source) VALUES (?,?,?,?) "
                    "ON CONFLICT(query_hash) DO UPDATE SET intent=excluded.intent, "
                    "created_at=excluded.created_at, source=excluded.source",
                    (query_hash, intent, utc_now(), source),
                )
        except sqlite3.Error:
            pass

    def lookup_clarify(self, query_hash: str) -> str | None:
        try:
            with self.lock, self._connect() as db:
                row = db.execute(
                    "SELECT intent FROM clarify_memory WHERE query_hash=?", (query_hash,)
                ).fetchone()
        except sqlite3.Error:
            return None
        return str(row["intent"]) if row is not None else None

    # ------------------------------------------------------------------- export
    def summarize(self) -> dict[str, Any]:
        """Compact stats for /insights and drift dashboards."""
        with self.lock, self._connect() as db:
            events = db.execute(
                "SELECT layer, decision, COUNT(*) AS count FROM router_events "
                "GROUP BY layer, decision"
            ).fetchall()
            hits = db.execute(
                "SELECT rule_id, SUM(hits) AS total FROM rule_hit_stats GROUP BY rule_id"
            ).fetchall()
            disagreements = db.execute(
                "SELECT COUNT(*) AS count FROM router_disagreements"
            ).fetchone()
            memory = db.execute(
                "SELECT COUNT(*) AS count FROM clarify_memory"
            ).fetchone()
        return {
            "events": [dict(row) for row in events],
            "rule_hits": [dict(row) for row in hits],
            "disagreements": int(disagreements["count"]) if disagreements else 0,
            "clarify_memory": int(memory["count"]) if memory else 0,
        }
