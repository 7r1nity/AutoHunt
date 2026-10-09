from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..core.models import (
    AutomationStats,
    BrowserTaskStatus,
    BrowserTaskView,
    DeliveryPreviewView,
    DeliveryTaskStatus,
    DeliveryTaskView,
    SearchFunnelView,
    StoredJobView,
)
from ..core.security import redact, redact_text
from .analysis import KNOWN_SKILLS


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def normalized_key(*values: str) -> str:
    text = "|".join(re.sub(r"\s+", " ", value).strip().lower() for value in values)
    return hashlib.sha256(text.encode()).hexdigest()


_CITY_PREFIXES = (
    "深圳", "北京", "上海", "广州", "杭州", "成都", "武汉", "南京", "苏州", "西安",
    "长沙", "重庆", "合肥", "悉尼", "墨尔本", "远程",
)


def city_from_location(location: str) -> str:
    text = (location or "").strip()
    if not text:
        return "未注明"
    for city in _CITY_PREFIXES:
        if text.startswith(city):
            return city
    first = re.split(r"[·\s]", text)[0].strip()
    return first if first else "未注明"


class JobStore:
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
                CREATE TABLE IF NOT EXISTS browser_tasks (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, source TEXT NOT NULL,
                    query TEXT NOT NULL, detail TEXT NOT NULL, takeover_reason TEXT,
                    discovered INTEGER NOT NULL DEFAULT 0, stored INTEGER NOT NULL DEFAULT 0,
                    duplicates INTEGER NOT NULL DEFAULT 0, matched INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, dedupe_key TEXT NOT NULL UNIQUE,
                    source TEXT NOT NULL, source_url TEXT NOT NULL, external_id TEXT,
                    title TEXT NOT NULL, company TEXT NOT NULL, location TEXT NOT NULL,
                    salary TEXT NOT NULL, description TEXT NOT NULL, tags TEXT NOT NULL,
                    score INTEGER NOT NULL DEFAULT 0, decision TEXT NOT NULL DEFAULT 'REVIEW',
                    reasoning TEXT NOT NULL DEFAULT '', stage TEXT NOT NULL DEFAULT 'review',
                    mode TEXT NOT NULL DEFAULT '人工确认', content_hash TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS browser_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                    event_type TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_stage ON jobs(stage);
                CREATE TABLE IF NOT EXISTS job_review_decisions (
                    job_id INTEGER PRIMARY KEY REFERENCES jobs(id), session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL, content_hash TEXT NOT NULL, decided_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_task ON browser_events(task_id);
                CREATE TABLE IF NOT EXISTS delivery_previews (
                    id TEXT PRIMARY KEY, job_id INTEGER NOT NULL, status TEXT NOT NULL,
                    payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL,
                    authorization_id TEXT, authorization_hash TEXT,
                    created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                    authorized_at TEXT, consumed_at TEXT,
                    FOREIGN KEY(job_id) REFERENCES jobs(id)
                );
                CREATE INDEX IF NOT EXISTS idx_delivery_job ON delivery_previews(job_id);
                CREATE TABLE IF NOT EXISTS delivery_runs (
                    id TEXT PRIMARY KEY, preview_id TEXT, job_id INTEGER NOT NULL,
                    target_url TEXT,
                    resume_document_id TEXT, resume_version INTEGER, resume_digest TEXT,
                    action TEXT NOT NULL DEFAULT 'submit_resume',
                    status TEXT NOT NULL, checkpoint TEXT, detail TEXT NOT NULL,
                    takeover_reason TEXT, current_url TEXT, submitted INTEGER NOT NULL DEFAULT 0,
                    recruiter_reply TEXT, recruiter_reply_at TEXT, last_checked_at TEXT,
                    started_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(id)
                );
                CREATE INDEX IF NOT EXISTS idx_delivery_runs_job ON delivery_runs(job_id);
                CREATE TABLE IF NOT EXISTS conversation_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL DEFAULT 'default',
                    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
                    content TEXT NOT NULL, response_json TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_conversation_created
                    ON conversation_messages(conversation_id, created_at);
                CREATE TABLE IF NOT EXISTS mock_workflow_settings (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    applicant_id INTEGER,
                    monitor_enabled INTEGER NOT NULL DEFAULT 0,
                    auto_submit INTEGER NOT NULL DEFAULT 0,
                    auto_submit_enabled_at TEXT,
                    daily_enabled INTEGER NOT NULL DEFAULT 0,
                    daily_time TEXT NOT NULL DEFAULT '09:00',
                    min_score INTEGER NOT NULL DEFAULT 75,
                    max_contacts INTEGER NOT NULL DEFAULT 3,
                    last_run_date TEXT,
                    last_run_id TEXT,
                    last_reported_run_id TEXT,
                    last_error TEXT
                );
                INSERT OR IGNORE INTO mock_workflow_settings(id) VALUES (1);
                CREATE TABLE IF NOT EXISTS mock_delivery_queue (
                    job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
                    session_id TEXT NOT NULL, resume_stamp TEXT NOT NULL,
                    binding_json TEXT NOT NULL, job_hash TEXT NOT NULL,
                    min_score INTEGER NOT NULL, status TEXT NOT NULL,
                    run_id TEXT, detail TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS learning_plans (
                    id TEXT PRIMARY KEY, resume_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_learning_plans_resume
                    ON learning_plans(resume_id, created_at DESC);
                """
            )

            self._ensure_column(db, "browser_tasks", "browser_mode", "TEXT NOT NULL DEFAULT 'unknown'")
            self._ensure_column(db, "browser_tasks", "selector_version", "TEXT")
            self._ensure_column(db, "browser_tasks", "checkpoint", "TEXT")
            self._ensure_column(db, "conversation_messages", "run_id", "TEXT")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_run_role ON conversation_messages(run_id,role) WHERE run_id IS NOT NULL")
            self._ensure_column(db, "browser_tasks", "current_url", "TEXT")
            self._ensure_column(db, "browser_tasks", "screenshot_path", "TEXT")
            self._ensure_column(db, "browser_tasks", "login_evidence_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column(db, "browser_tasks", "dry_run", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(db, "browser_tasks", "dry_run_candidates", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(db, "browser_tasks", "dry_run_contacted", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(db, "browser_tasks", "dry_run_replied", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(db, "browser_tasks", "dry_run_submitted", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(db, "browser_tasks", "dry_run_waiting_reply", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(db, "browser_tasks", "dry_run_failed", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(db, "browser_tasks", "dry_run_report", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(db, "browser_tasks", "adapter_version", "TEXT")
            self._ensure_column(db, "browser_tasks", "resume_id", "TEXT")
            for column in (
                "links_seen", "cards_parsed", "unique_jobs", "limited", "visited",
                "filtered", "funnel_errors", "cancelled",
            ):
                self._ensure_column(db, "browser_tasks", column, "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(db, "browser_events", "reason_code", "TEXT")
            self._ensure_column(db, "browser_events", "sample_json", "TEXT")
            self._ensure_column(db, "jobs", "recruiter_reply", "TEXT")
            self._ensure_column(db, "jobs", "recruiter_reply_at", "TEXT")
            self._ensure_column(db, "jobs", "reply_last_checked_at", "TEXT")
            self._ensure_column(db, "mock_workflow_settings", "last_reported_run_id", "TEXT")
            self._ensure_column(db, "mock_workflow_settings", "auto_submit_enabled_at", "TEXT")
            self._ensure_column(db, "delivery_runs", "resume_document_id", "TEXT")
            self._ensure_column(db, "delivery_runs", "target_url", "TEXT")
            self._ensure_column(db, "delivery_runs", "resume_version", "INTEGER")
            self._ensure_column(db, "delivery_runs", "resume_digest", "TEXT")
            self._ensure_column(db, "delivery_runs", "mock_resume_id", "INTEGER")
            self._ensure_column(db, "delivery_runs", "mock_applicant_id", "INTEGER")
            self._ensure_column(db, "delivery_runs", "action", "TEXT NOT NULL DEFAULT 'submit_resume'")
        self._ensure_column(db, "delivery_runs", "attempts", "INTEGER NOT NULL DEFAULT 0")
        db.execute(
            """CREATE TABLE IF NOT EXISTS automation_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule TEXT NOT NULL, result TEXT NOT NULL, considered TEXT NOT NULL DEFAULT '',
                facts_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
            )"""
        )

    @staticmethod
    def _ensure_column(db: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
        existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def create_task(
        self, task_id: str, source: str, query: str, browser_mode: str = "auto", *,
        dry_run: bool = False, adapter_version: str | None = None,
        resume_id: str | None = None,
    ) -> BrowserTaskView:
        now = utc_now()
        with self.lock, self._connect() as db:
            db.execute(
                "INSERT INTO browser_tasks (id,status,source,query,detail,browser_mode,dry_run,adapter_version,resume_id,started_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, BrowserTaskStatus.STARTING, source, query, "正在连接浏览器", browser_mode, int(dry_run), adapter_version, resume_id, now, now),
            )
        return self.get_task(task_id)

    def update_task(self, task_id: str, **fields: Any) -> BrowserTaskView:
        allowed = {
            "status", "detail", "takeover_reason", "discovered", "stored", "duplicates", "matched",
            "browser_mode", "selector_version", "checkpoint", "current_url", "screenshot_path",
            "login_evidence_json", "dry_run", "dry_run_candidates", "dry_run_contacted",
            "dry_run_replied", "dry_run_submitted", "dry_run_waiting_reply", "dry_run_failed",
            "dry_run_report",
            "adapter_version", "links_seen", "cards_parsed", "unique_jobs", "limited",
            "visited", "filtered", "funnel_errors", "cancelled",
        }
        values = {key: (value.value if hasattr(value, "value") else value) for key, value in fields.items() if key in allowed}
        values["updated_at"] = utc_now()
        assignments = ",".join(f"{key}=?" for key in values)
        with self.lock, self._connect() as db:
            db.execute(f"UPDATE browser_tasks SET {assignments} WHERE id=?", (*values.values(), task_id))
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> BrowserTaskView:
        with self.lock, self._connect() as db:
            row = db.execute("SELECT * FROM browser_tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return self._task_view(row)

    def latest_task(self) -> BrowserTaskView:
        with self.lock, self._connect() as db:
            row = db.execute("SELECT * FROM browser_tasks ORDER BY started_at DESC LIMIT 1").fetchone()
        return BrowserTaskView() if row is None else self._task_view(row)

    def mock_workflow_settings(self) -> dict[str, Any]:
        with self.lock, self._connect() as db:
            row = db.execute("SELECT * FROM mock_workflow_settings WHERE id=1").fetchone()
        if row is None:
            raise RuntimeError("模拟站自动化配置缺失")
        result = dict(row)
        for key in ("monitor_enabled", "auto_submit", "daily_enabled"):
            result[key] = bool(result[key])
        return result

    def update_mock_workflow_settings(self, *, monitor_enabled: bool, auto_submit: bool,
                                      daily_enabled: bool, daily_time: str, min_score: int,
                                      max_contacts: int) -> dict[str, Any]:
        with self.lock, self._connect() as db:
            previous = db.execute(
                "SELECT auto_submit,auto_submit_enabled_at,daily_enabled,last_run_date "
                "FROM mock_workflow_settings WHERE id=1"
            ).fetchone()
            now_utc = datetime.now(UTC)
            beijing_now = now_utc + timedelta(hours=8)
            enabled_at = (now_utc.replace(tzinfo=None).isoformat() if auto_submit and not previous["auto_submit"]
                          else previous["auto_submit_enabled_at"] if auto_submit else None)
            last_run_date = previous["last_run_date"]
            if daily_enabled and not previous["daily_enabled"] and beijing_now.strftime("%H:%M") >= daily_time:
                last_run_date = beijing_now.date().isoformat()
            db.execute(
                "UPDATE mock_workflow_settings SET monitor_enabled=?,auto_submit=?,auto_submit_enabled_at=?,"
                "daily_enabled=?,daily_time=?,min_score=?,max_contacts=?,last_run_date=?,last_error=NULL WHERE id=1",
                (int(monitor_enabled), int(auto_submit), enabled_at, int(daily_enabled), daily_time,
                 min_score, max_contacts, last_run_date),
            )
        return self.mock_workflow_settings()

    def bind_mock_applicant(self, applicant_id: int) -> None:
        if applicant_id < 1:
            raise ValueError("无效的求职者账号")
        with self.lock, self._connect() as db:
            db.execute("UPDATE mock_workflow_settings SET applicant_id=? WHERE id=1", (applicant_id,))

    def claim_daily_mock_run(self, local_date: str) -> bool:
        with self.lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE mock_workflow_settings SET last_run_date=?,last_error=NULL "
                "WHERE id=1 AND daily_enabled=1 AND (last_run_date IS NULL OR last_run_date<>?)",
                (local_date, local_date),
            )
        return cursor.rowcount == 1

    def record_daily_mock_run(self, task_id: str) -> None:
        with self.lock, self._connect() as db:
            db.execute("UPDATE mock_workflow_settings SET last_run_id=? WHERE id=1", (task_id,))

    def release_daily_mock_run(self, local_date: str) -> None:
        with self.lock, self._connect() as db:
            db.execute(
                "UPDATE mock_workflow_settings SET last_run_date=NULL WHERE id=1 AND last_run_date=? AND last_run_id IS NULL",
                (local_date,),
            )

    def record_daily_mock_report(self, task_id: str) -> None:
        with self.lock, self._connect() as db:
            db.execute("UPDATE mock_workflow_settings SET last_reported_run_id=? WHERE id=1", (task_id,))

    def record_mock_workflow_error(self, detail: str) -> None:
        with self.lock, self._connect() as db:
            db.execute("UPDATE mock_workflow_settings SET last_error=? WHERE id=1", (detail[:300],))

    def _task_view(self, row: sqlite3.Row) -> BrowserTaskView:
        payload = dict(row)
        raw_evidence = payload.pop("login_evidence_json", "{}") or "{}"
        try:
            payload["login_evidence"] = json.loads(raw_evidence)
        except json.JSONDecodeError:
            payload["login_evidence"] = {}
        funnel_values = {
            "links_seen": int(payload.pop("links_seen", 0) or 0),
            "cards_parsed": int(payload.pop("cards_parsed", 0) or 0),
            "unique_jobs": int(payload.pop("unique_jobs", 0) or 0),
            "limited": int(payload.pop("limited", 0) or 0),
            "visited": int(payload.pop("visited", 0) or 0),
            "filtered": int(payload.pop("filtered", 0) or 0),
            "stored": int(payload.get("stored", 0) or 0),
            "duplicates": int(payload.get("duplicates", 0) or 0),
            "errors": int(payload.pop("funnel_errors", 0) or 0),
            "cancelled": int(payload.pop("cancelled", 0) or 0),
        }
        with self.lock, self._connect() as db:
            reason_rows = db.execute(
                "SELECT reason_code,COUNT(*) AS count FROM browser_events "
                "WHERE task_id=? AND reason_code IS NOT NULL AND reason_code<>'' GROUP BY reason_code",
                (payload.get("id"),),
            ).fetchall()
            sample_rows = db.execute(
                "SELECT event_type,reason_code,detail,sample_json FROM browser_events "
                "WHERE task_id=? AND reason_code IS NOT NULL AND reason_code<>'' "
                "ORDER BY id DESC LIMIT 12",
                (payload.get("id"),),
            ).fetchall()
        samples: list[dict[str, str]] = []
        for item in sample_rows:
            sample = {"stage": str(item["event_type"]), "reason_code": str(item["reason_code"]), "detail": str(item["detail"])}
            try:
                sample.update({str(key): str(value) for key, value in json.loads(item["sample_json"] or "{}").items()})
            except (json.JSONDecodeError, AttributeError):
                pass
            samples.append(sample)
        payload["funnel"] = SearchFunnelView(
            **funnel_values,
            reason_counts={str(item["reason_code"]): int(item["count"]) for item in reason_rows},
            samples=samples,
        )
        return BrowserTaskView(**payload)

    def increment_funnel(self, task_id: str, **deltas: int) -> BrowserTaskView:
        allowed = {"links_seen", "cards_parsed", "unique_jobs", "limited", "visited", "filtered", "funnel_errors", "cancelled"}
        values = {key: int(value) for key, value in deltas.items() if key in allowed and int(value) != 0}
        if not values:
            return self.get_task(task_id)
        assignments = ",".join(f"{key}=MAX(0,{key}+?)" for key in values)
        with self.lock, self._connect() as db:
            db.execute(
                f"UPDATE browser_tasks SET {assignments},updated_at=? WHERE id=?",
                (*values.values(), utc_now(), task_id),
            )
        return self.get_task(task_id)

    def checkpoint(
        self,
        task_id: str,
        name: str,
        *,
        current_url: str,
        evidence: dict[str, Any],
        screenshot_path: str | None = None,
    ) -> BrowserTaskView:
        return self.update_task(
            task_id,
            checkpoint=name,
            current_url=current_url[:1000],
            selector_version=str(evidence.get("selector_version") or ""),
            screenshot_path=screenshot_path,
            login_evidence_json=json.dumps(evidence, ensure_ascii=False),
        )

    def event(
        self, task_id: str, event_type: str, detail: str, *,
        reason_code: str | None = None, sample: dict[str, Any] | None = None,
    ) -> None:
        safe_sample = redact(sample or {})
        with self.lock, self._connect() as db:
            db.execute(
                "INSERT INTO browser_events (task_id,event_type,detail,reason_code,sample_json,created_at) VALUES (?,?,?,?,?,?)",
                (task_id, event_type, redact_text(detail)[:500], reason_code, json.dumps(safe_sample, ensure_ascii=False)[:2000], utc_now()),
            )

    def add_conversation_message(
        self, role: str, content: str, *, response: dict[str, Any] | None = None,
        conversation_id: str = "default",
        run_id: str | None = None,
    ) -> None:
        if role not in {"user", "assistant"}:
            raise ValueError("Unsupported conversation role")
        # Full search reports can contain up to 200 numbered jobs. Keep their
        # persisted display intact; model-context budgets are applied separately.
        display_limit = 120_000 if run_id and run_id.startswith(("summary:", "report-update:")) else 12_000
        safe_content = redact_text(content)[:display_limit]
        safe_response = json.dumps(redact(response), ensure_ascii=False, default=str) if response is not None else None
        with self.lock, self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO conversation_messages (conversation_id,role,content,response_json,created_at,run_id) VALUES (?,?,?,?,?,?)",
                (conversation_id[:100], role, safe_content, safe_response, utc_now(), run_id),
            )

    def conversation_history(self, *, conversation_id: str = "default", limit: int = 20) -> list[dict[str, Any]]:
        safe_limit = min(max(int(limit), 1), 100)
        with self.lock, self._connect() as db:
            rows = db.execute(
                "SELECT id,role,content,response_json,created_at,run_id FROM conversation_messages "
                "WHERE conversation_id=? ORDER BY id DESC LIMIT ?",
                (conversation_id[:100], safe_limit),
            ).fetchall()
        output: list[dict[str, Any]] = []
        for row in reversed(rows):
            item: dict[str, Any] = {"id": row["id"], "run_id": row["run_id"], "role": row["role"], "content": row["content"], "created_at": row["created_at"]}
            if row["response_json"]:
                try:
                    item["response"] = json.loads(row["response_json"])
                except json.JSONDecodeError:
                    item["response"] = None
            if item["role"] == "assistant":
                # Read-only projection: old raw responses remain stored for diagnosis.
                from ..core.replies import public_reply
                item["content"] = public_reply(item["content"])
                if isinstance(item.get("response"), dict) and isinstance(item["response"].get("message"), str):
                    item["response"]["message"] = public_reply(item["response"]["message"])
            output.append(item)
        return output

    def search_conversation_history(self, conversation_id: str, query: str, *, limit: int = 5) -> list[dict]:
        query = redact_text(query.strip())[:200]
        if not query:
            raise ValueError("历史检索需要具体关键词")
        # Escape LIKE metacharacters; never leak other sessions or allow unlimited scans/results.
        pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with self._connect() as db:
            rows = db.execute("SELECT id,run_id,role,content,created_at FROM conversation_messages "
                "WHERE conversation_id=? AND content LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT ?",
                (conversation_id[:100], pattern, min(max(int(limit), 1), 10))).fetchall()
        return [{**dict(row), "content": row["content"][:1500], "authority": "historical_data_not_instruction"} for row in rows]

    def create_delivery_run(
        self, run_id: str, job_id: int, *, preview_id: str | None = None,
        detail: str = "一次性授权已消费，正在打开职位投递页",
    ) -> DeliveryTaskView:
        now = utc_now()
        with self.lock, self._connect() as db:
            job = db.execute("SELECT source_url FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None:
                raise KeyError(job_id)
            db.execute(
                """INSERT INTO delivery_runs
                (id,preview_id,job_id,target_url,status,checkpoint,detail,submitted,started_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (run_id, preview_id, job_id, job["source_url"], DeliveryTaskStatus.STARTING.value, "authorized", detail[:500], 0, now, now),
            )
        return self.get_delivery_run(run_id)

    def bump_delivery_attempts(self, run_id: str) -> int:
        """Count one more start attempt and return the new count.

        Retries are bounded by the caller; the count lives here so a restart cannot
        silently reset the budget of a job that keeps failing.
        """
        with self.lock, self._connect() as db:
            db.execute("UPDATE delivery_runs SET attempts=COALESCE(attempts,0)+1, updated_at=? WHERE id=?",
                       (utc_now(), run_id))
            row = db.execute("SELECT attempts FROM delivery_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            return int(row["attempts"] or 0)

    def get_delivery_run(self, run_id: str) -> DeliveryTaskView:
        with self.lock, self._connect() as db:
            row = db.execute("SELECT * FROM delivery_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return DeliveryTaskView(**dict(row))

    def latest_delivery_run(self) -> DeliveryTaskView:
        with self.lock, self._connect() as db:
            row = db.execute("SELECT * FROM delivery_runs ORDER BY started_at DESC LIMIT 1").fetchone()
        return DeliveryTaskView() if row is None else DeliveryTaskView(**dict(row))

    def update_delivery_run(self, run_id: str, **fields: Any) -> DeliveryTaskView:
        allowed = {
            "status", "checkpoint", "detail", "takeover_reason", "current_url", "submitted",
            "recruiter_reply", "recruiter_reply_at", "last_checked_at", "mock_resume_id", "mock_applicant_id",
            "attempts",
        }
        values = {
            key: (value.value if hasattr(value, "value") else value)
            for key, value in fields.items() if key in allowed
        }
        if "submitted" in values:
            values["submitted"] = int(bool(values["submitted"]))
        for key in ("recruiter_reply_at", "last_checked_at"):
            if isinstance(values.get(key), datetime):
                values[key] = values[key].isoformat()
        values["updated_at"] = utc_now()
        assignments = ",".join(f"{key}=?" for key in values)
        with self.lock, self._connect() as db:
            cursor = db.execute(f"UPDATE delivery_runs SET {assignments} WHERE id=?", (*values.values(), run_id))
            if cursor.rowcount == 0:
                raise KeyError(run_id)
        return self.get_delivery_run(run_id)

    def authorize_delivery_run(
        self, preview_id: str, run_id: str, *, resume_document_id: str | None = None,
        resume_version: int | None = None, resume_digest: str | None = None,
        action: str = "submit_resume",
    ) -> tuple[str, int, datetime, DeliveryTaskView]:
        """Authorize, consume and create the durable run in one SQLite transaction."""
        now = datetime.now(UTC)
        authorization_id = f"dau_{secrets.token_hex(16)}"
        with self.lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM delivery_previews WHERE id=?", (preview_id,)).fetchone()
            if row is None:
                raise KeyError(preview_id)
            payload = self._validate_delivery_preview_row(
                row,
                resume_document_id=resume_document_id,
                resume_version=resume_version,
                resume_digest=resume_digest,
                action=action,
            )
            current_job = db.execute("SELECT source_url FROM jobs WHERE id=?", (row["job_id"],)).fetchone()
            if current_job is None or current_job["source_url"] != payload.get("source_url"):
                raise ValueError("职位链接已变化，请重新生成投递预览")
            expires = datetime.fromisoformat(row["expires_at"])
            if expires <= now:
                db.execute("UPDATE delivery_previews SET status='expired' WHERE id=?", (preview_id,))
                raise ValueError("投递预览已过期，请重新生成")
            if row["status"] != "preview_ready":
                raise ValueError("投递预览已经授权或不可用")
            changed = db.execute(
                """UPDATE delivery_previews SET status='consumed',authorization_id=?,authorization_hash=NULL,
                authorized_at=?,consumed_at=? WHERE id=? AND status='preview_ready'""",
                (authorization_id, now.isoformat(), now.isoformat(), preview_id),
            )
            if changed.rowcount != 1:
                raise ValueError("投递预览已被其他执行器消费")
            db.execute(
                "UPDATE jobs SET stage='applying',mode='人工已授权',last_seen_at=? WHERE id=?",
                (now.isoformat(), row["job_id"]),
            )
            db.execute(
                """INSERT INTO delivery_runs
                (id,preview_id,job_id,target_url,resume_document_id,resume_version,resume_digest,action,
                 status,checkpoint,detail,submitted,started_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, preview_id, row["job_id"], payload["source_url"], payload["resume_document_id"], payload["resume_version"],
                 payload["resume_digest"], payload["action"], DeliveryTaskStatus.STARTING.value, "authorized",
                 "一次性授权已消费，正在打开职位投递页", 0, now.isoformat(), now.isoformat()),
            )
        return authorization_id, int(row["job_id"]), expires, self.get_delivery_run(run_id)

    def mark_delivery_failed(self, job_id: int, detail: str) -> None:
        with self.lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE jobs SET stage='review',mode='需要重新授权',reasoning=?,last_seen_at=? WHERE id=?",
                (detail[:500], utc_now(), job_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(job_id)

    def reconcile_incomplete_runs(self) -> dict[str, int]:
        """Fail closed after process loss; never infer a successful submission."""
        now = utc_now()
        with self.lock, self._connect() as db:
            search_rows = db.execute(
                "SELECT id FROM browser_tasks WHERE status IN ('starting','running','paused','waiting_for_user')"
            ).fetchall()
            if search_rows:
                db.execute(
                    """UPDATE browser_tasks SET status='stopped',detail=?,takeover_reason=NULL,
                    checkpoint='backend_restart_reconciled',cancelled=cancelled+1,updated_at=?
                    WHERE status IN ('starting','running','paused','waiting_for_user')""",
                    ("后端重启；原搜索任务已安全停止，可查看已有部分结果后重新搜索", now),
                )
            delivery_rows = db.execute(
                "SELECT r.*,j.stage AS job_stage FROM delivery_runs r JOIN jobs j ON j.id=r.job_id "
                "WHERE r.status IN ('starting','running','waiting_for_user','waiting_for_reply')"
            ).fetchall()
            reconciled_delivery = 0
            for row in delivery_rows:
                status = str(row["status"])
                if status in {DeliveryTaskStatus.WAITING_FOR_USER.value, DeliveryTaskStatus.WAITING_FOR_REPLY.value}:
                    detail = (
                        "后端已重启；仍需你确认登录/MFA 后点击继续"
                        if status == DeliveryTaskStatus.WAITING_FOR_USER.value
                        else "后端已重启；等待招聘方回复的任务已恢复，可点击检查回复并继续"
                    )
                    db.execute("UPDATE delivery_runs SET detail=?,updated_at=? WHERE id=?", (detail, now, row["id"]))
                elif row["job_stage"] == "waiting":
                    detail = "已存在提交确认记录；后端重启后保持等待回复，不会重复投递"
                    db.execute(
                        "UPDATE delivery_runs SET status='waiting_for_reply',submitted=1,detail=?,checkpoint='submission_confirmed',updated_at=? WHERE id=?",
                        (detail, now, row["id"]),
                    )
                elif bool(row["submitted"]) or row["checkpoint"] in {"submit_started", "submission_unconfirmed"}:
                    detail = "后端在提交阶段重启，结果无法确定；已禁止自动重试，请人工核验"
                    db.execute(
                        "UPDATE delivery_runs SET status='outcome_indeterminate',detail=?,checkpoint='restart_after_submit',updated_at=? WHERE id=?",
                        (detail, now, row["id"]),
                    )
                    db.execute(
                        "UPDATE jobs SET stage='outcome_indeterminate',mode='需要人工核验',reasoning=?,last_seen_at=? WHERE id=?",
                        (detail, now, row["job_id"]),
                    )
                else:
                    detail = "后端在提交前重启；未报告成功，职位已退回待审核并需要重新授权"
                    db.execute(
                        "UPDATE delivery_runs SET status='failed',detail=?,checkpoint='restart_before_submit',updated_at=? WHERE id=?",
                        (detail, now, row["id"]),
                    )
                    db.execute(
                        "UPDATE jobs SET stage='review',mode='需要重新授权',reasoning=?,last_seen_at=? WHERE id=?",
                        (detail, now, row["job_id"]),
                    )
                reconciled_delivery += 1
        for row in search_rows:
            self.event(
                str(row["id"]), "task_reconciled", "后端重启，搜索任务安全停止",
                reason_code="backend_restart",
            )
        return {"search_runs": len(search_rows), "delivery_runs": reconciled_delivery}

    def upsert_job(self, job: dict[str, Any]) -> tuple[int, bool]:
        is_mock = job.get("source") == "MockJobSite" and job.get("external_id")
        dedupe = (
            normalized_key("MockJobSite", str(job["external_id"])) if is_mock
            else normalized_key(job["company"], job["title"], job["location"])
        )
        description = re.sub(r"\s+", " ", job.get("description", "")).strip()
        content_hash = hashlib.sha256(description.encode()).hexdigest()
        now = utc_now()
        with self.lock, self._connect() as db:
            existing = db.execute("SELECT id FROM jobs WHERE source_url=?", (job["source_url"],)).fetchone() if is_mock else None
            if existing is None:
                existing = db.execute("SELECT id FROM jobs WHERE dedupe_key=?", (dedupe,)).fetchone()
            if existing:
                db.execute(
                    "UPDATE jobs SET source_url=?,salary=?,description=?,tags=?,score=?,decision=?,reasoning=?,stage=CASE WHEN stage='review' THEN ? ELSE stage END,content_hash=?,last_seen_at=? WHERE id=?",
                    (job["source_url"], job.get("salary", "未注明"), description, json.dumps(job.get("tags", []), ensure_ascii=False), job["score"], job["decision"], job["reasoning"], job.get("stage", "review"), content_hash, now, existing["id"]),
                )
                return int(existing["id"]), False
            cursor = db.execute(
                """INSERT INTO jobs
                (dedupe_key,source,source_url,external_id,title,company,location,salary,description,tags,score,decision,reasoning,stage,content_hash,first_seen_at,last_seen_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (dedupe, job["source"], job["source_url"], job.get("external_id"), job["title"], job["company"], job["location"], job.get("salary", "未注明"), description, json.dumps(job.get("tags", []), ensure_ascii=False), job["score"], job["decision"], job["reasoning"], job.get("stage", "review"), content_hash, now, now),
            )
            return int(cursor.lastrowid), True

    def list_jobs(self, stage: str | None = None, limit: int = 100) -> list[StoredJobView]:
        query = "SELECT j.*,d.content_hash AS review_hash FROM jobs j LEFT JOIN job_review_decisions d ON d.job_id=j.id"
        params: list[Any] = []
        if stage:
            query += " WHERE j.stage=?"
            params.append(stage)
        query += " ORDER BY j.score DESC,j.last_seen_at DESC LIMIT ?"
        params.append(limit)
        with self.lock, self._connect() as db:
            rows = db.execute(query, params).fetchall()
        return [StoredJobView(
            id=row["id"], role=row["title"], company=row["company"], location=row["location"],
            salary=row["salary"], source=row["source"], source_url=row["source_url"], score=row["score"],
            stage=row["stage"], mode=row["mode"], tags=json.loads(row["tags"]), reason=row["reasoning"],
            posted=row["last_seen_at"], recruiter_reply=row["recruiter_reply"],
            recruiter_reply_at=row["recruiter_reply_at"],
            review_approved=row["review_hash"] == self.review_fingerprint(dict(row)),
        ) for row in rows]

    def job_id_for_source_url(self, source_url: str) -> int | None:
        with self.lock, self._connect() as db:
            row = db.execute("SELECT id FROM jobs WHERE source_url=?", (source_url,)).fetchone()
        return int(row["id"]) if row is not None else None

    def job_touched_by_task(self, task_id: str, job_id: int) -> bool:
        with self.lock, self._connect() as db:
            rows = db.execute("SELECT sample_json FROM browser_events WHERE task_id=? AND event_type IN ('job_stored','job_duplicate')", (task_id,)).fetchall()
        return any(json.loads(row["sample_json"] or "{}").get("job_id") == job_id for row in rows)

    def search_report(self, task_id: str) -> dict[str, Any]:
        """Summarize only jobs touched by this run, including deduplicated jobs."""
        task = self.get_task(task_id)
        with self.lock, self._connect() as db:
            events = db.execute(
                "SELECT sample_json FROM browser_events WHERE task_id=? "
                "AND event_type IN ('job_stored','job_duplicate') ORDER BY id",
                (task_id,),
            ).fetchall()
            constraint_row = db.execute("SELECT sample_json FROM browser_events WHERE task_id=? AND event_type='search_constraints' ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
            constraints = json.loads(constraint_row["sample_json"] or "{}") if constraint_row else {}
            job_ids: list[int] = []
            for event in events:
                try:
                    job_id = int(json.loads(event["sample_json"] or "{}").get("job_id") or 0)
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue
                if job_id > 0 and job_id not in job_ids:
                    job_ids.append(job_id)
            rows = [] if not job_ids else db.execute(
                f"SELECT * FROM jobs WHERE id IN ({','.join('?' for _ in job_ids)}) ORDER BY id",
                job_ids,
            ).fetchall()

        salaries: dict[str, int] = {}
        employment: dict[str, int] = {}
        benefits: dict[str, int] = {}
        summaries: list[dict[str, Any]] = []
        evidence = []
        from .search_reporting import jd_evidence, aggregate_jd, salary_overview, job_characteristics
        for row in rows:
            title = str(row["title"] or "")
            description = str(row["description"] or "")
            salary = str(row["salary"] or "未注明")
            salary_bucket = self._salary_bucket(salary)
            if salary_bucket == "未注明":
                if "元/天" in salary or "元／天" in salary:
                    salary_bucket = "按日计薪"
                elif "元/小时" in salary or "元／小时" in salary:
                    salary_bucket = "按时计薪"
                elif "面议" in salary:
                    salary_bucket = "面议"
            salaries[salary_bucket] = salaries.get(salary_bucket, 0) + 1
            characteristics = job_characteristics(title, description)
            employment_type = characteristics["employment_type"]
            employment[employment_type] = employment.get(employment_type, 0) + 1
            present_benefits = characteristics["benefits"]
            for term in present_benefits:
                benefits[term] = benefits.get(term, 0) + 1
            jd = jd_evidence(description)
            evidence.append(jd)
            jd_excerpt = "" if "初筛阶段未打开详情页" in description else jd["brief"]
            summaries.append({
                "id": int(row["id"]), "title": title, "company": str(row["company"] or ""),
                "location": str(row["location"] or ""), "salary": salary,
                "score": int(row["score"] or 0), "stage": str(row["stage"] or ""),
                "employment_type": employment_type, "benefits": present_benefits,
                "jd_excerpt": jd_excerpt, "jd_summary": jd["brief"],
                "source_url": str(row["source_url"] or ""), "content_hash": self.review_fingerprint(dict(row)),
            })
        summaries.sort(key=lambda item: (item["stage"] != "rejected", item["score"]), reverse=True)
        report = {
            "covered_jobs": len(summaries),
            "qualified_jobs": sum(item["stage"] != "rejected" for item in summaries),
            "constraints": constraints,
            "salary_distribution": salaries,
            "employment_distribution": employment,
            "benefit_distribution": benefits,
            "jd_overview": aggregate_jd(evidence),
            "salary_overview": salary_overview([item["salary"] for item in summaries]),
            "jobs": summaries,
            "highlights": summaries,  # Compatibility: no silent three-item truncation.
        }
        from .search_reporting import render_search_report
        report["message"] = render_search_report(task, report)
        return report

    def stats(self) -> AutomationStats:
        task = self.latest_task()
        with self.lock, self._connect() as db:
            stages = {row["stage"]: row["count"] for row in db.execute("SELECT stage,COUNT(*) AS count FROM jobs GROUP BY stage")}
        return AutomationStats(
            discovered=task.discovered, stored=task.stored, duplicates=task.duplicates, matched=task.matched,
            review=stages.get("review", 0), applying=stages.get("applying", 0),
            waiting=stages.get("waiting", 0), interview=stages.get("interview", 0),
            indeterminate=stages.get("outcome_indeterminate", 0),
            dry_run_candidates=task.dry_run_candidates,
            dry_run_contacted=task.dry_run_contacted,
            dry_run_replied=task.dry_run_replied,
            dry_run_submitted=task.dry_run_submitted,
            dry_run_waiting_reply=task.dry_run_waiting_reply,
            dry_run_failed=task.dry_run_failed,
        )

    @staticmethod
    def _salary_bucket(salary: str) -> str:
        numbers = re.findall(r"\d+(?:\.\d+)?", salary)
        if not numbers:
            return "未注明"
        ceiling = max(float(number) for number in numbers)
        if "万" in salary:
            ceiling *= 10
        if "k" not in salary.lower() and "万" not in salary:
            return "未注明"
        if ceiling <= 10:
            return "≤10K"
        if ceiling <= 20:
            return "10-20K"
        if ceiling <= 30:
            return "20-30K"
        if ceiling <= 50:
            return "30-50K"
        return "50K+"

    def insights(self, *, current_only: bool = False, resume_id: str | None = None,
                 legacy_since: str | None = None) -> dict:
        """Aggregate all history, or only runs bound to the active resume.

        Before resume IDs were recorded on tasks, at most the latest terminal
        run after the active resume was loaded can be shown. It is labelled as
        a legacy run rather than silently attributed to that resume.
        """
        with self.lock, self._connect() as db:
            scope = "all_history"
            if current_only:
                task_rows = db.execute(
                    "SELECT id,discovered,stored,duplicates,status FROM browser_tasks "
                    "WHERE status IN ('completed','stopped','failed') AND resume_id=? ORDER BY started_at DESC",
                    (resume_id,),
                ).fetchall() if resume_id else []
                if not task_rows and resume_id and legacy_since:
                    latest = db.execute(
                        "SELECT id,discovered,stored,duplicates,status FROM browser_tasks "
                        "WHERE status IN ('completed','stopped','failed') AND resume_id IS NULL AND started_at>=? "
                        "ORDER BY started_at DESC LIMIT 1",
                        (legacy_since,),
                    ).fetchone()
                    task_rows = [latest] if latest else []
                    if task_rows:
                        scope = "latest_legacy_run"
                elif task_rows:
                    scope = "active_resume"
                task_ids = [row["id"] for row in task_rows]
                job_ids: set[int] = set()
                if task_ids:
                    placeholders = ",".join("?" for _ in task_ids)
                    events = db.execute(
                        "SELECT sample_json FROM browser_events WHERE task_id IN ("
                        + placeholders + ") AND event_type IN ('job_stored','job_duplicate')",
                        task_ids,
                    ).fetchall()
                    for event in events:
                        try:
                            job_id = int(json.loads(event["sample_json"] or "{}").get("job_id") or 0)
                        except (ValueError, TypeError, json.JSONDecodeError):
                            continue
                        if job_id > 0:
                            job_ids.add(job_id)
                job_rows = db.execute(
                    "SELECT title,description,location,salary,tags,score,stage FROM jobs WHERE id IN ("
                    + ",".join("?" for _ in job_ids) + ")",
                    sorted(job_ids),
                ).fetchall() if job_ids else []
            else:
                task_rows = db.execute(
                    "SELECT discovered,stored,duplicates,status FROM browser_tasks WHERE status IN ('completed','stopped','failed')"
                ).fetchall()
                job_rows = db.execute("SELECT title,description,location,salary,tags,score,stage FROM jobs").fetchall()

        cities: dict[str, int] = {}
        salaries: dict[str, int] = {"≤10K": 0, "10-20K": 0, "20-30K": 0, "30-50K": 0, "50K+": 0, "未注明": 0}
        tags: dict[str, int] = {}
        known_skills = {skill.lower() for skill in KNOWN_SKILLS}
        scores: list[int] = []
        rejected = 0
        employment, benefits = {}, {}
        jd_samples = []
        from .search_reporting import jd_evidence, aggregate_jd, job_characteristics, BENEFITS
        for row in job_rows:
            description = str(row["description"] or "")
            jd_samples.append(jd_evidence(description))
            characteristics = job_characteristics(str(row["title"] or ""), description)
            kind = characteristics["employment_type"]
            employment[kind] = employment.get(kind, 0) + 1
            for benefit in characteristics["benefits"]:
                benefits[benefit] = benefits.get(benefit, 0) + 1
            if row["stage"] == "rejected":
                rejected += 1
            city = city_from_location(row["location"])
            cities[city] = cities.get(city, 0) + 1
            bucket = self._salary_bucket(row["salary"] or "")
            salaries[bucket] = salaries.get(bucket, 0) + 1
            try:
                raw_tags = json.loads(row["tags"] or "[]")
            except json.JSONDecodeError:
                raw_tags = []
            for tag in raw_tags:
                # Only real skills belong in the skill chart; benefit keywords
                # (五险一金/交通补助/…), locations and years stay out.
                skill = str(tag).lower()
                if skill in known_skills or (2 <= len(skill) <= 50 and not re.search(
                    r"工资|薪资|补助|补贴|福利|[0-9]+年|[0-9]+[kK]|" + "|".join(map(re.escape, BENEFITS)), skill)
                    and skill not in {city_from_location(row["location"]).lower(), "实习", "社招", "应届", "本科", "硕士", "博士"}):
                    tags[skill] = tags.get(skill, 0) + 1
            scores.append(int(row["score"] or 0))

        return {
            "scope": scope,
            "tasks_total": len(task_rows),
            "discovered_total": sum(int(row["discovered"] or 0) for row in task_rows),
            "duplicates_total": sum(int(row["duplicates"] or 0) for row in task_rows),
            "stored_total": len(job_rows),
            "rejected": rejected,
            "partial_tasks": sum(row["status"] != "completed" for row in task_rows),
            "employment_distribution": employment,
            "benefit_distribution": benefits,
            "jd_overview": aggregate_jd(jd_samples),
            "cities": [{"city": city, "count": count} for city, count in sorted(cities.items(), key=lambda item: -item[1])[:8]],
            "salaries": [{"bucket": bucket, "count": count} for bucket, count in salaries.items() if count],
            "tags": [{"tag": tag, "count": count} for tag, count in sorted(tags.items(), key=lambda item: -item[1])[:10]],
            "avg_score": round(sum(scores) / len(scores)) if scores else 0,
            "score_high": sum(1 for score in scores if score >= 90),
            "score_good": sum(1 for score in scores if 80 <= score < 90),
            "score_fair": sum(1 for score in scores if score < 80),
        }

    def jobs_for_resume_analysis(self, resume_id: str, *, limit: int = 20,
                                 legacy_since: str | None = None, min_score: int | None = None,
                                 max_score: int | None = None, salary_min: int | None = None,
                                 task_id: str | None = None) -> list[dict[str, Any]]:
        """Return saved JD evidence from terminal runs for one resume, not the global board."""
        with self.lock, self._connect() as db:
            task_ids = [row["id"] for row in db.execute(
                "SELECT id FROM browser_tasks WHERE status IN ('completed','stopped','failed') AND resume_id=? "
                "ORDER BY started_at DESC LIMIT 20", (resume_id,),
            )]
            if task_id is not None:
                task_ids = [row["id"] for row in db.execute(
                    "SELECT id FROM browser_tasks WHERE id=? AND resume_id=? AND status='completed'",
                    (task_id, resume_id),
                )]
            if task_id is None and not task_ids and legacy_since:
                legacy = db.execute(
                    "SELECT id FROM browser_tasks WHERE status IN ('completed','stopped','failed') "
                    "AND resume_id IS NULL AND started_at>=? ORDER BY started_at DESC LIMIT 1",
                    (legacy_since,),
                ).fetchone()
                task_ids = [legacy["id"]] if legacy else []
            if not task_ids:
                return []
            placeholders = ",".join("?" for _ in task_ids)
            events = db.execute(
                "SELECT sample_json FROM browser_events WHERE task_id IN (" + placeholders + ") "
                "AND event_type IN ('job_stored','job_duplicate') ORDER BY id DESC",
                task_ids,
            ).fetchall()
            ids: list[int] = []
            for event in events:
                try:
                    job_id = int(json.loads(event["sample_json"] or "{}").get("job_id") or 0)
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue
                if job_id > 0 and job_id not in ids:
                    ids.append(job_id)
                if len(ids) >= limit and min_score is None and max_score is None and salary_min is None:
                    break
            if not ids:
                return []
            rows = db.execute(
                "SELECT id,title,company,location,salary,description,source_url,stage,score "
                "FROM jobs WHERE id IN (" + ",".join("?" for _ in ids) + ")", ids,
            ).fetchall()
        by_id = {int(row["id"]): dict(row) for row in rows}
        from ..core.search_contract import monthly_salary_lower_bound
        return [by_id[job_id] for job_id in ids if job_id in by_id
                and (min_score is None or by_id[job_id]["score"] >= min_score)
                and (max_score is None or by_id[job_id]["score"] <= max_score)
                and (salary_min is None or (monthly_salary_lower_bound(by_id[job_id]["salary"]) is not None
                     and monthly_salary_lower_bound(by_id[job_id]["salary"]) >= salary_min))][:limit]

    def save_learning_plan(self, plan: dict[str, Any]) -> None:
        now = utc_now()
        with self.lock, self._connect() as db:
            db.execute(
                "INSERT INTO learning_plans(id,resume_id,payload_json,created_at,updated_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at",
                (plan["id"], plan["resume_id"], json.dumps(plan, ensure_ascii=False),
                 plan.get("created_at", now), now),
            )

    def enqueue_mock_deliveries(self, items: list[dict], *, session_id: str, resume_stamp: str,
                                binding: dict, min_score: int) -> list[int]:
        inserted = []
        with self.lock, self._connect() as db:
            for item in items:
                cursor = db.execute(
                    "INSERT OR IGNORE INTO mock_delivery_queue "
                    "(job_id,session_id,resume_stamp,binding_json,job_hash,min_score,status,updated_at) "
                    "VALUES (?,?,?,?,?,?,'queued',?)",
                    (item["id"], session_id, resume_stamp, json.dumps(binding),
                     self.review_fingerprint(item), min_score, utc_now()),
                )
                if cursor.rowcount:
                    # The user authorised this batch in words ("85分以上的自动投递").
                    # Leaving the job at the default '人工确认' label made it look like
                    # it still needed a manual approval that never existed, so the
                    # badge now states what actually happened.
                    db.execute("UPDATE jobs SET mode='自动投递·已授权',last_seen_at=? WHERE id=? AND mode='人工确认'",
                               (utc_now(), item["id"]))
                    inserted.append(item["id"])
        return inserted

    def record_automation_decision(self, *, rule: str | None, result: str,
                                   considered: tuple[str, ...], facts: dict) -> None:
        """Persist what the automation loop noticed and decided.

        Without this, "why did nothing happen" can only be answered by re-running the
        loop; with it, each cycle is readable after the fact, together with the facts
        the rules were matched against.
        """
        with self.lock, self._connect() as db:
            db.execute("INSERT INTO automation_decisions(rule,result,considered,facts_json,created_at) "
                       "VALUES (?,?,?,?,?)",
                       (rule or "", result[:500], ",".join(considered),
                        json.dumps(facts, ensure_ascii=False, default=str)[:6000], utc_now()))

    def automation_decisions(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self.lock, self._connect() as db:
            rows = db.execute("SELECT * FROM automation_decisions ORDER BY id DESC LIMIT ?",
                              (limit,)).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            try:
                item["facts"] = json.loads(item.pop("facts_json", "") or "{}")
            except ValueError:
                item["facts"] = {}
            item["considered"] = [name for name in str(item.get("considered") or "").split(",") if name]
            output.append(item)
        return output

    def all_delivery_runs(self, *, limit: int = 200) -> list[dict]:
        """Every run, newest first, for the automation loop's observation."""
        with self.lock, self._connect() as db:
            rows = db.execute("SELECT * FROM delivery_runs ORDER BY started_at DESC LIMIT ?",
                              (limit,)).fetchall()
        return [dict(row) for row in rows]

    def delivery_runs_failed_before_submit(self, *, limit: int = 50) -> list[dict]:
        """Runs whose start failed before any click, so nothing was submitted.

        These are safe to re-dispatch automatically: the checkpoint proves the submit
        action never began, and the one-use authorisation is still unspent.
        """
        with self.lock, self._connect() as db:
            rows = db.execute(
                "SELECT * FROM delivery_runs WHERE status='failed' AND submitted=0 "
                "AND checkpoint='failed_before_submit' ORDER BY started_at LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def delivery_runs_waiting_reply(self, *, limit: int = 50) -> list[dict]:
        """Granted runs that still wait for the recruiter, oldest first.

        Reply monitoring is a read-only periodic check, so it must be able to find
        these without holding the single delivery worker.
        """
        with self.lock, self._connect() as db:
            rows = db.execute(
                "SELECT * FROM delivery_runs WHERE status='waiting_for_reply' "
                "AND submitted=0 ORDER BY started_at LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def mock_delivery_queue(self, *, session_id: str | None = None, active_only: bool = True) -> list[dict]:
        query = "SELECT * FROM mock_delivery_queue WHERE 1=1"
        values = []
        if active_only:
            query += " AND status IN ('queued','dispatched')"
        if session_id is not None:
            query += " AND session_id=?"
            values.append(session_id)
        with self.lock, self._connect() as db:
            rows = db.execute(query + " ORDER BY updated_at,job_id", values).fetchall()
        return [{**dict(row), "binding": json.loads(row["binding_json"])} for row in rows]

    def claim_mock_delivery(self, job_id: int) -> bool:
        with self.lock, self._connect() as db:
            return db.execute("UPDATE mock_delivery_queue SET status='dispatched',updated_at=? "
                              "WHERE job_id=? AND status='queued'", (utc_now(), job_id)).rowcount == 1

    def mark_job_retrying_delivery(self, job_id: int) -> None:
        """Return a job to the applying column while its submit is retried.

        Without this the board keeps showing "需要重新授权" for a job the system has
        already resumed, so the user is told to approve something that needs no
        approval.
        """
        with self.lock, self._connect() as db:
            db.execute("UPDATE jobs SET stage='applying',mode='自动投递·重试中',last_seen_at=? WHERE id=? "
                       "AND stage NOT IN ('waiting','interview','rejected')", (utc_now(), job_id))

    def update_mock_delivery(self, job_id: int, status: str, detail: str, *, run_id: str | None = None) -> None:
        if status not in {"dispatched", "completed", "blocked", "stale", "cancelled"}:
            raise ValueError("无效的投递队列状态")
        with self.lock, self._connect() as db:
            db.execute("UPDATE mock_delivery_queue SET status=?,detail=?,run_id=COALESCE(?,run_id),updated_at=? "
                       "WHERE job_id=? AND (status!='cancelled' OR ?='cancelled')",
                       (status, detail[:400], run_id, utc_now(), job_id, status))
            if status == "cancelled":
                db.execute("UPDATE jobs SET mode='自动投递·已停止' WHERE id=? AND stage IN ('review','applying')", (job_id,))

    def learning_plans(self, resume_id: str) -> list[dict[str, Any]]:
        with self.lock, self._connect() as db:
            rows = db.execute(
                "SELECT payload_json FROM learning_plans WHERE resume_id=? ORDER BY created_at DESC LIMIT 20",
                (resume_id,),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def learning_plan(self, plan_id: str) -> dict[str, Any]:
        with self.lock, self._connect() as db:
            row = db.execute("SELECT payload_json FROM learning_plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise KeyError(plan_id)
        return json.loads(row["payload_json"])

    def update_stage(self, job_id: int, stage: str) -> None:
        with self.lock, self._connect() as db:
            cursor = db.execute("UPDATE jobs SET stage=?,last_seen_at=? WHERE id=?", (stage, utc_now(), job_id))
            if cursor.rowcount == 0:
                raise KeyError(job_id)

    def review_selection(self, items: list[dict], *, session_id: str, run_id: str) -> None:
        if not items or len(items) > 30:
            raise ValueError("审核对象数量无效")
        with self.lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for item in items:
                row = db.execute("SELECT * FROM jobs WHERE id=?", (item["id"],)).fetchone()
                if not row or row["stage"] != "review" or self.review_fingerprint(dict(row)) != item.get("content_hash"):
                    raise ValueError("职位内容或阶段已变化，请重新查看结果后审核")
            for item in items:
                db.execute("INSERT INTO job_review_decisions VALUES (?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET session_id=excluded.session_id,run_id=excluded.run_id,content_hash=excluded.content_hash,decided_at=excluded.decided_at",
                    (item["id"], session_id, run_id, item["content_hash"], utc_now()))

    def review_decisions(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT j.*,d.job_id,d.session_id,d.run_id,d.decided_at,d.content_hash AS review_hash FROM job_review_decisions d JOIN jobs j ON j.id=d.job_id ORDER BY d.decided_at DESC LIMIT 100").fetchall()
        return [{key: row[key] for key in ("job_id", "session_id", "run_id", "decided_at")} for row in rows
                if row["review_hash"] == self.review_fingerprint(dict(row))]

    @staticmethod
    def review_fingerprint(job: dict) -> str:
        facts = {key: job.get(key) for key in ("id", "title", "company", "location", "salary", "source_url", "content_hash")}
        return hashlib.sha256(json.dumps(facts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def get_job(self, job_id: int) -> dict[str, Any]:
        with self.lock, self._connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return dict(row)

    def latest_delivery_status_for_job(self, job_id: int) -> str | None:
        with self.lock, self._connect() as db:
            row = db.execute(
                "SELECT status FROM delivery_runs WHERE job_id=? ORDER BY started_at DESC LIMIT 1",
                (job_id,),
            ).fetchone()
        return str(row["status"]) if row else None

    def create_delivery_preview(
        self, job_id: int, resume_label: str = "候选人默认简历", *,
        resume_document_id: str = "master", resume_version: int = 0,
        resume_digest: str = "unavailable", action: str = "submit_resume",
    ) -> DeliveryPreviewView:
        if action != "submit_resume":
            raise ValueError("不支持的投递动作")
        job = self.get_job(job_id)
        now = datetime.now(UTC)
        expires = now + timedelta(minutes=30)
        payload = {
            "job_id": job_id,
            "title": job["title"],
            "company": job["company"],
            "source_url": job["source_url"],
            "resume_label": resume_label,
            "resume_document_id": resume_document_id,
            "resume_version": resume_version,
            "resume_digest": resume_digest,
            "action": action,
            "action_summary": "填写申请表并上传所选简历；遇到验证码、MFA 或未知问题立即暂停",
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        preview_id = f"dpv_{secrets.token_hex(12)}"
        with self.lock, self._connect() as db:
            db.execute(
                """INSERT INTO delivery_previews
                (id,job_id,status,payload_json,payload_digest,created_at,expires_at)
                VALUES (?,?,?,?,?,?,?)""",
                (preview_id, job_id, "preview_ready", canonical, digest, now.isoformat(), expires.isoformat()),
            )
        return DeliveryPreviewView(
            id=preview_id,
            status="preview_ready",
            payload_digest=digest,
            created_at=now,
            expires_at=expires,
            **payload,
        )

    def get_delivery_preview_job_id(self, preview_id: str) -> int:
        with self.lock, self._connect() as db:
            row = db.execute("SELECT job_id FROM delivery_previews WHERE id=?", (preview_id,)).fetchone()
        if row is None:
            raise KeyError(preview_id)
        return int(row["job_id"])

    @staticmethod
    def _validate_delivery_preview_row(
        row: sqlite3.Row, *, resume_document_id: str | None = None,
        resume_version: int | None = None, resume_digest: str | None = None,
        action: str = "submit_resume",
    ) -> dict[str, Any]:
        try:
            payload = json.loads(row["payload_json"])
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("投递预览内容损坏，请重新生成") from exc
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        actual_digest = hashlib.sha256(canonical.encode()).hexdigest()
        if not secrets.compare_digest(actual_digest, str(row["payload_digest"])):
            raise ValueError("投递预览完整性校验失败，请重新生成")
        if int(payload.get("job_id", -1)) != int(row["job_id"]):
            raise ValueError("投递预览绑定的职位不一致")
        expected = {
            "resume_document_id": resume_document_id,
            "resume_version": resume_version,
            "resume_digest": resume_digest,
            "action": action,
        }
        for field, value in expected.items():
            if value is not None and payload.get(field) != value:
                raise ValueError("简历、职位或投递动作已变化，请重新生成投递预览")
        return payload

    def authorize_delivery(
        self, preview_id: str, *, resume_document_id: str | None = None,
        resume_version: int | None = None, resume_digest: str | None = None,
        action: str = "submit_resume",
    ) -> tuple[str, str, int, datetime]:
        now = datetime.now(UTC)
        with self.lock, self._connect() as db:
            row = db.execute("SELECT * FROM delivery_previews WHERE id=?", (preview_id,)).fetchone()
            if row is None:
                raise KeyError(preview_id)
            self._validate_delivery_preview_row(
                row,
                resume_document_id=resume_document_id,
                resume_version=resume_version,
                resume_digest=resume_digest,
                action=action,
            )
            payload = json.loads(row["payload_json"])
            current_job = db.execute("SELECT source_url FROM jobs WHERE id=?", (row["job_id"],)).fetchone()
            if current_job is None or current_job["source_url"] != payload.get("source_url"):
                raise ValueError("职位链接已变化，请重新生成投递预览")
            expires = datetime.fromisoformat(row["expires_at"])
            if expires <= now:
                db.execute("UPDATE delivery_previews SET status='expired' WHERE id=?", (preview_id,))
                raise ValueError("投递预览已过期，请重新生成")
            if row["status"] != "preview_ready":
                raise ValueError("投递预览已经授权或不可用")
            token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            authorization_id = f"dau_{secrets.token_hex(16)}"
            db.execute(
                """UPDATE delivery_previews
                SET status='authorized',authorization_id=?,authorization_hash=?,authorized_at=?
                WHERE id=? AND status='preview_ready'""",
                (authorization_id, token_hash, now.isoformat(), preview_id),
            )
            db.execute(
                "UPDATE jobs SET stage='applying',mode='人工已授权',last_seen_at=? WHERE id=?",
                (now.isoformat(), row["job_id"]),
            )
        return authorization_id, token, int(row["job_id"]), expires

    def consume_delivery_authorization(self, authorization_id: str, one_time_token: str) -> int:
        """Internal executor gate. A token is valid exactly once."""
        token_hash = hashlib.sha256(one_time_token.encode()).hexdigest()
        now = datetime.now(UTC)
        with self.lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM delivery_previews WHERE authorization_id=?", (authorization_id,)
            ).fetchone()
            if row is None or row["status"] != "authorized":
                raise ValueError("投递授权不存在、已使用或已撤销")
            if datetime.fromisoformat(row["expires_at"]) <= now:
                db.execute("UPDATE delivery_previews SET status='expired' WHERE id=?", (row["id"],))
                raise ValueError("投递授权已过期")
            if not secrets.compare_digest(str(row["authorization_hash"]), token_hash):
                raise ValueError("投递授权凭证无效")
            db.execute(
                "UPDATE delivery_previews SET status='consumed',authorization_hash=NULL,consumed_at=? WHERE id=?",
                (now.isoformat(), row["id"]),
            )
        return int(row["job_id"])

    def mark_delivery_waiting_reply(self, job_id: int, detail: str, *, mode: str = "人工已授权") -> None:
        with self.lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE jobs SET stage='applying',mode=?,reasoning=?,reply_last_checked_at=?,last_seen_at=? WHERE id=?",
                (mode[:50], detail[:500], utc_now(), utc_now(), job_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(job_id)

    def mark_recruiter_reply(self, job_id: int, reply: str) -> str:
        now = utc_now()
        clean = re.sub(r"\s+", " ", reply).strip()[:500]
        with self.lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE jobs SET recruiter_reply=?,recruiter_reply_at=?,reply_last_checked_at=?,reasoning=?,last_seen_at=? WHERE id=?",
                (clean, now, now, f"招聘方回复：{clean}；发送状态以投递记录为准", now, job_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(job_id)
        return now

    def mark_delivery_completed(self, job_id: int, detail: str) -> None:
        with self.lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE jobs SET stage='waiting',mode='自动投递',reasoning=?,last_seen_at=? WHERE id=?",
                (detail[:500], utc_now(), job_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(job_id)
    def mark_outcome_indeterminate(self, job_id: int, detail: str) -> None:
        """Fail closed after an ambiguous submit; the executor must never auto-retry."""
        now = utc_now()
        with self.lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE jobs SET stage='outcome_indeterminate',mode='需要人工核验',reasoning=?,last_seen_at=? WHERE id=?",
                (detail[:500], now, job_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(job_id)
