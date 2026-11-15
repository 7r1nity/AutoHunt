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
                CREATE TABLE IF NOT EXISTS search_candidates (
                    task_id TEXT NOT NULL, source_url TEXT NOT NULL, job_id INTEGER NOT NULL,
                    status TEXT NOT NULL, group_index INTEGER, candidate_json TEXT NOT NULL,
                    created_at TEXT NOT NULL, PRIMARY KEY(task_id,source_url)
                );
                CREATE TABLE IF NOT EXISTS job_review_decisions (
                    job_id INTEGER PRIMARY KEY REFERENCES jobs(id), session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL, content_hash TEXT NOT NULL, decided_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_task ON browser_events(task_id);
                CREATE TABLE IF NOT EXISTS search_recovery (
                    task_id TEXT PRIMARY KEY, request_json TEXT NOT NULL,
                    processed_urls_json TEXT NOT NULL DEFAULT '[]'
                );
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
                    max_search_jobs INTEGER NOT NULL DEFAULT 30,
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
            self._ensure_column(db, "jobs", "filter_status", "TEXT NOT NULL DEFAULT 'pass'")
            self._ensure_column(db, "jobs", "board_visible", "INTEGER NOT NULL DEFAULT 1")
            self._ensure_column(db, "jobs", "removed_at", "TEXT")
            db.execute("CREATE TABLE IF NOT EXISTS job_removals (job_id INTEGER PRIMARY KEY,session_id TEXT NOT NULL,run_id TEXT NOT NULL,content_hash TEXT NOT NULL,removed_at TEXT NOT NULL)")
            self._ensure_column(db, "jobs", "filter_evidence", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column(db, "jobs", "qualification_evidence", "TEXT")
            self._ensure_column(db, "jobs", "source_tags", "TEXT")
            self._ensure_column(db, "mock_delivery_queue", "scope_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column(db, "mock_delivery_queue", "scope_evidence_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column(db, "jobs", "recruiter_reply_at", "TEXT")
            self._ensure_column(db, "jobs", "reply_last_checked_at", "TEXT")
            self._ensure_column(db, "mock_workflow_settings", "last_reported_run_id", "TEXT")
            self._ensure_column(db, "mock_workflow_settings", "auto_submit_enabled_at", "TEXT")
            self._ensure_column(db, "mock_workflow_settings", "daily_claim_id", "TEXT")
            self._ensure_column(db, "mock_workflow_settings", "daily_next_due_at", "TEXT")
            self._ensure_column(db, "mock_workflow_settings", "max_search_jobs", "INTEGER NOT NULL DEFAULT 30")
            self._ensure_column(db, "delivery_runs", "resume_document_id", "TEXT")
            self._ensure_column(db, "delivery_runs", "target_url", "TEXT")
            self._ensure_column(db, "delivery_runs", "resume_version", "INTEGER")
            self._ensure_column(db, "delivery_runs", "resume_digest", "TEXT")
            self._ensure_column(db, "delivery_runs", "mock_resume_id", "INTEGER")
            self._ensure_column(db, "delivery_runs", "mock_resume_digest", "TEXT")
            self._ensure_column(db, "delivery_runs", "reconciliation_json", "TEXT")
            self._ensure_column(db, "delivery_runs", "reconciled_at", "TEXT")
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
        db.execute(
            """CREATE TABLE IF NOT EXISTS authorization_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tool TEXT NOT NULL, risk TEXT NOT NULL, would_confirm INTEGER NOT NULL,
                policy TEXT NOT NULL DEFAULT '', threshold TEXT NOT NULL DEFAULT '',
                candidates INTEGER NOT NULL DEFAULT 0, in_scope INTEGER NOT NULL DEFAULT 0,
                out_of_scope INTEGER NOT NULL DEFAULT 0, detail TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
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
                                      max_contacts: int, max_search_jobs: int | None = None) -> dict[str, Any]:
        if max_search_jobs is not None and (type(max_search_jobs) is not int or not 1 <= max_search_jobs <= 200):
            raise ValueError("每轮搜索岗位数必须是 1–200 的整数")
        with self.lock, self._connect() as db:
            previous = db.execute(
                "SELECT auto_submit,auto_submit_enabled_at,daily_enabled,daily_time,last_run_date,daily_next_due_at "
                "FROM mock_workflow_settings WHERE id=1"
            ).fetchone()
            now_utc = datetime.now(UTC)
            beijing_now = now_utc + timedelta(hours=8)
            enabled_at = (now_utc.replace(tzinfo=None).isoformat() if auto_submit and not previous["auto_submit"]
                          else previous["auto_submit_enabled_at"] if auto_submit else None)
            last_run_date = previous["last_run_date"]
            next_due = previous["daily_next_due_at"]
            if not daily_enabled:
                next_due = None
            elif not previous["daily_enabled"] or previous["daily_time"] != daily_time:
                hour, minute = map(int, daily_time.split(":"))
                due = beijing_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if due <= beijing_now:
                    due += timedelta(days=1)
                next_due = (due - timedelta(hours=8)).isoformat()
            if daily_enabled and not previous["daily_enabled"] and beijing_now.strftime("%H:%M") >= daily_time:
                last_run_date = beijing_now.date().isoformat()
            db.execute(
                "UPDATE mock_workflow_settings SET monitor_enabled=?,auto_submit=?,auto_submit_enabled_at=?,"
                "daily_enabled=?,daily_time=?,min_score=?,max_contacts=?,max_search_jobs=COALESCE(?,max_search_jobs),"
                "last_run_date=?,daily_next_due_at=?,last_error=NULL WHERE id=1",
                (int(monitor_enabled), int(auto_submit), enabled_at, int(daily_enabled), daily_time,
                 min_score, max_contacts, max_search_jobs, last_run_date, next_due),
            )
        return self.mock_workflow_settings()

    def advance_daily_schedule(self, expected: str | None, next_due: str, *, run_date: str | None = None) -> bool:
        with self.lock, self._connect() as db:
            cursor = db.execute("UPDATE mock_workflow_settings SET daily_next_due_at=?,"
                "last_run_date=COALESCE(?,last_run_date) WHERE id=1 AND daily_enabled=1 AND daily_next_due_at IS ?",
                (next_due, run_date, expected))
        return cursor.rowcount == 1

    def bind_mock_applicant(self, applicant_id: int) -> None:
        if applicant_id < 1:
            raise ValueError("无效的求职者账号")
        with self.lock, self._connect() as db:
            db.execute("UPDATE mock_workflow_settings SET applicant_id=? WHERE id=1", (applicant_id,))

    def claim_daily_mock_run(self, local_date: str) -> str | None:
        """Reserve today's launch without losing the previous run/report pointer."""
        claim_id = secrets.token_hex(16)
        with self.lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE mock_workflow_settings SET last_run_date=?,daily_claim_id=?,last_error=NULL "
                "WHERE id=1 AND daily_enabled=1 AND (last_run_date IS NULL OR last_run_date<>?)",
                (local_date, claim_id, local_date),
            )
        return claim_id if cursor.rowcount == 1 else None

    def record_daily_mock_run(self, task_id: str, local_date: str, claim_id: str) -> bool:
        with self.lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE mock_workflow_settings SET last_run_id=?,daily_claim_id=NULL "
                "WHERE id=1 AND last_run_date=? AND daily_claim_id=?",
                (task_id, local_date, claim_id),
            )
        return cursor.rowcount == 1

    def release_daily_mock_run(self, local_date: str, claim_id: str) -> None:
        with self.lock, self._connect() as db:
            db.execute(
                "UPDATE mock_workflow_settings SET last_run_date=NULL,daily_claim_id=NULL "
                "WHERE id=1 AND last_run_date=? AND daily_claim_id=?",
                (local_date, claim_id),
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
        # Hashes are internal references, not phone numbers. Keep valid JSON:
        # slicing serialized text corrupts larger condition trees and evidence.
        for key in ("filter_fingerprint", "content_hash"):
            value = (sample or {}).get(key)
            if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
                safe_sample[key] = value
        digest = (sample or {}).get("digest") if event_type == "filter_evaluated" else None
        if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
            safe_sample["digest"] = digest
        filter_sample = (sample or {}).get("filter_evidence")
        filter_digest = filter_sample.get("digest") if isinstance(filter_sample, dict) else None
        if isinstance(filter_digest, str) and re.fullmatch(r"[0-9a-f]{64}", filter_digest):
            safe_sample["filter_evidence"]["digest"] = filter_digest
        with self.lock, self._connect() as db:
            db.execute(
                "INSERT INTO browser_events (task_id,event_type,detail,reason_code,sample_json,created_at) VALUES (?,?,?,?,?,?)",
                (task_id, event_type, redact_text(detail)[:500], reason_code, json.dumps(safe_sample, ensure_ascii=False), utc_now()),
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
        display_limit = 120_000 if run_id and run_id.startswith(("summary:", "report-update:", "daily-search:")) else 12_000
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
            "attempts", "mock_resume_digest",
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
            current_job = db.execute("SELECT * FROM jobs WHERE id=?", (row["job_id"],)).fetchone()
            if current_job is None or current_job["source_url"] != payload.get("source_url"):
                raise ValueError("职位链接已变化，请重新生成投递预览")
            self._check_delivery_filter(dict(current_job), payload)
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
                "UPDATE jobs SET mode='人工已授权',last_seen_at=? WHERE id=?",
                (now.isoformat(), row["job_id"]),
            )
            db.execute(
                """INSERT INTO delivery_runs
                (id,preview_id,job_id,target_url,resume_document_id,resume_version,resume_digest,action,
                 status,checkpoint,detail,submitted,started_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, preview_id, row["job_id"], payload.get("target_url", payload["source_url"]), payload["resume_document_id"], payload["resume_version"],
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
                elif bool(row["submitted"]) or row["checkpoint"] in {"communication_started", "communication_unconfirmed", "submit_started", "submission_unconfirmed"}:
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
        with self.lock, self._connect() as db:
            return self._upsert_job(db, job)

    def _upsert_job(self, db, job: dict[str, Any]) -> tuple[int, bool]:
        source_tags = job.get("source_tags")
        if isinstance(source_tags, str):
            source_tags = json.loads(source_tags)
        source_tags_json = json.dumps(source_tags, ensure_ascii=False) if source_tags is not None else None
        is_mock = job.get("source") == "MockJobSite" and job.get("external_id")
        dedupe = (
            normalized_key("MockJobSite", str(job["external_id"])) if is_mock
            else normalized_key(job["company"], job["title"], job["location"])
        )
        description = re.sub(r"\s+", " ", job.get("description", "")).strip()
        content_hash = hashlib.sha256(description.encode()).hexdigest()
        now = utc_now()
        existing = db.execute("SELECT id FROM jobs WHERE source_url=?", (job["source_url"],)).fetchone() if is_mock else None
        if existing is None:
            existing = db.execute("SELECT id FROM jobs WHERE dedupe_key=?", (dedupe,)).fetchone()
        if existing:
            db.execute(
                "UPDATE jobs SET source_url=?,salary=?,description=?,tags=?,score=?,decision=?,reasoning=?,stage=CASE WHEN stage='review' THEN ? ELSE stage END,content_hash=?,last_seen_at=? WHERE id=?",
                (job["source_url"], job.get("salary", "未注明"), description, json.dumps(job.get("tags", []), ensure_ascii=False), job["score"], job["decision"], job["reasoning"], job.get("stage", "review"), content_hash, now, existing["id"]),
            )
            db.execute("UPDATE jobs SET source=? WHERE id=?", (job["source"], existing["id"]))
            # An import/contact sync is not a replacement of screening evidence.
            # Omitted fields preserve their value; explicit null clears raw tags.
            if "filter_status" in job:
                db.execute("UPDATE jobs SET filter_status=? WHERE id=?", (job["filter_status"], existing["id"]))
            if "filter_evidence" in job:
                db.execute("UPDATE jobs SET filter_evidence=? WHERE id=?",
                    (json.dumps(job["filter_evidence"], ensure_ascii=False), existing["id"]))
            if "source_tags" in job:
                db.execute("UPDATE jobs SET source_tags=? WHERE id=?", (source_tags_json, existing["id"]))
            if "qualification_evidence" in job:
                db.execute("UPDATE jobs SET qualification_evidence=? WHERE id=?",
                           (json.dumps(job["qualification_evidence"],ensure_ascii=False),existing["id"]))
            db.execute("UPDATE jobs SET board_visible=CASE WHEN removed_at IS NOT NULL THEN 0 ELSE MAX(board_visible,?) END WHERE id=?",
                       (int(job.get("board_visible", True)), existing["id"]))
            return int(existing["id"]), False
        cursor = db.execute(
            """INSERT INTO jobs
            (dedupe_key,source,source_url,external_id,title,company,location,salary,description,tags,score,decision,reasoning,stage,content_hash,first_seen_at,last_seen_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (dedupe, job["source"], job["source_url"], job.get("external_id"), job["title"], job["company"], job["location"], job.get("salary", "未注明"), description, json.dumps(job.get("tags", []), ensure_ascii=False), job["score"], job["decision"], job["reasoning"], job.get("stage", "review"), content_hash, now, now),
        )
        db.execute("UPDATE jobs SET filter_status=?,filter_evidence=? WHERE id=?",
            (job.get("filter_status", "pass"), json.dumps(job.get("filter_evidence", {}), ensure_ascii=False), cursor.lastrowid))
        db.execute("UPDATE jobs SET source_tags=? WHERE id=?", (source_tags_json, cursor.lastrowid))
        if "qualification_evidence" in job:
            db.execute("UPDATE jobs SET qualification_evidence=? WHERE id=?",
                       (json.dumps(job["qualification_evidence"],ensure_ascii=False),cursor.lastrowid))
        db.execute("UPDATE jobs SET board_visible=? WHERE id=?", (int(job.get("board_visible", True)), cursor.lastrowid))
        return int(cursor.lastrowid), True

    def admit_search_result(self, task_id: str, job: dict, *, limit: int | None, groups: list[dict] | None) -> dict:
        """Atomically budget, publish and receipt a result; other candidates stay off board."""
        with self.lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM search_candidates WHERE task_id=? AND source_url=?",
                             (task_id, job["source_url"])).fetchone()
            if old and old["status"] == "accepted":
                return {"counts": (0, 0, 0), "status": "already_recorded", "job_id": old["job_id"]}
            existing = db.execute("SELECT * FROM jobs WHERE source_url=?", (job["source_url"],)).fetchone()
            if existing is None:
                dedupe = normalized_key("MockJobSite", str(job["external_id"])) if job.get("source") == "MockJobSite" and job.get("external_id") else normalized_key(job["company"], job["title"], job["location"])
                existing = db.execute("SELECT * FROM jobs WHERE dedupe_key=?", (dedupe,)).fetchone()
            # Legacy receipts also occupy slots, so resumed pre-upgrade searches cannot overflow.
            receipts = db.execute("SELECT sample_json FROM browser_events WHERE task_id=? AND event_type IN ('job_stored','job_duplicate')",
                                  (task_id,)).fetchall()
            admitted = {int(item.get("job_id", 0)) for row in receipts
                        if (item := json.loads(row[0] or "{}")).get("filter_status", "pass") == "pass"}
            group_counts = {}
            for row in db.execute("SELECT group_index,COUNT(DISTINCT job_id) AS n FROM search_candidates WHERE task_id=? AND status='accepted' GROUP BY group_index", (task_id,)):
                group_counts[row["group_index"]] = row["n"]
            status = "pending_verification" if job.get("filter_status") != "pass" else "accepted"
            if existing and existing["removed_at"]:
                status = "user_removed"
            if job.get("decision") not in {"APPLY", "REVIEW"} or job.get("stage") == "rejected":
                status = "not_admitted"
            group_index = None
            if status == "accepted" and groups:
                group_index = next((item["index"] for item in job.get("result_group_evidence", [])
                                    if item["status"] == "pass" and group_counts.get(item["index"], 0) < groups[item["index"]]["count"]), None)
                if group_index is None:
                    status = "quota_full"
            if status == "accepted" and limit and len(admitted) >= limit:
                status = "target_full"
            if existing and existing["id"] in admitted:
                return {"counts": (0, 0, 0), "status": "already_recorded", "job_id": existing["id"]}
            newly_visible = existing is None or not existing["board_visible"]
            if status == "accepted" or existing is None:
                job_id, _ = self._upsert_job(db, {**job, "board_visible": status == "accepted"})
            else:
                job_id = int(existing["id"])  # Preserve an older admitted/active job's facts.
            db.execute("INSERT OR REPLACE INTO search_candidates VALUES (?,?,?,?,?,?,?)",
                       (task_id, job["source_url"], job_id, status, group_index, json.dumps(job, ensure_ascii=False), utc_now()))
            event = ("job_stored" if newly_visible else "job_duplicate") if status == "accepted" else "candidate_recorded"
            sample = {"job_id": job_id, "title": job["title"], "company": job["company"],
                      "filter_status": job.get("filter_status", "pass"), "filter_evidence": job.get("filter_evidence", {}),
                      "result_group": group_index, "admission": status,
                      "filter_fingerprint": self.review_fingerprint(self.get_job_from_connection(db, job_id))}
            db.execute("INSERT INTO browser_events(task_id,event_type,detail,sample_json,created_at) VALUES (?,?,?,?,?)",
                       (task_id, event, f"{job['company']} / {job['title']}：{status}", json.dumps(sample, ensure_ascii=False), utc_now()))
            return {"counts": (int(newly_visible), int(not newly_visible), 1) if status == "accepted" else (0, 0, 0),
                    "status": status, "job_id": job_id}

    @staticmethod
    def get_job_from_connection(db, job_id):
        return dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def list_jobs(self, stage: str | None = None, limit: int = 100) -> list[StoredJobView]:
        query = "SELECT j.*,d.content_hash AS review_hash FROM jobs j LEFT JOIN job_review_decisions d ON d.job_id=j.id WHERE j.board_visible=1 AND j.removed_at IS NULL AND (j.filter_status='pass' OR j.stage!='review')"
        params: list[Any] = []
        if stage:
            query += " AND j.stage=?"
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
            filter_status=row["filter_status"],
        ) for row in rows]

    def job_id_for_source_url(self, source_url: str) -> int | None:
        with self.lock, self._connect() as db:
            row = db.execute("SELECT id FROM jobs WHERE source_url=?", (source_url,)).fetchone()
        return int(row["id"]) if row is not None else None

    def update_job_filter_result(self, job_id: int, result: dict) -> None:
        with self.lock, self._connect() as db:
            db.execute("UPDATE jobs SET filter_status=?,filter_evidence=? WHERE id=?",
                (result["status"], json.dumps(result, ensure_ascii=False), job_id))

    def search_source(self, task_id: str) -> dict | None:
        """Read-only search identity/ownership; no jobs or application text."""
        with self.lock, self._connect() as db:
            row = db.execute("SELECT id,status,resume_id FROM browser_tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                return None
            saved = db.execute("SELECT request_json FROM search_recovery WHERE task_id=?", (task_id,)).fetchone()
            request = json.loads(saved[0]) if saved else {}
        return {"task_id": row["id"], "status": row["status"], "resume_id": row["resume_id"],
                "session_id": request.get("conversation_id")}

    def refilter_candidates(self, task_id: str, *, resume_id: str, session_id: str, limit: int = 30) -> list[dict]:
        """Read this completed search's recorded JDs, including previously unknown ones."""
        with self.lock, self._connect() as db:
            task = db.execute("SELECT status,resume_id FROM browser_tasks WHERE id=?", (task_id,)).fetchone()
            saved = db.execute("SELECT request_json FROM search_recovery WHERE task_id=?", (task_id,)).fetchone()
            if not task or task["status"] != "completed" or task["resume_id"] != resume_id:
                raise ValueError("只能重新筛选当前简历已完成搜索的职位")
            request = json.loads(saved[0]) if saved else {}
            if request.get("conversation_id") != session_id:
                raise ValueError("搜索不属于当前会话，不能修改其筛选结果")
            rows = db.execute("SELECT DISTINCT j.* FROM jobs j JOIN browser_events e "
                "ON j.id=json_extract(e.sample_json,'$.job_id') WHERE e.task_id=? "
                "AND e.event_type IN ('job_stored','job_duplicate','candidate_recorded') ORDER BY j.id LIMIT ?",
                (task_id, min(limit, 30))).fetchall()
        return [dict(row) for row in rows]

    def save_refilter_results(self, task_id: str, candidates: list[dict], verdicts: list[dict], constraints: dict) -> None:
        """Commit new evidence together; never leave a new status with an old receipt."""
        from ..runtime.context import effect_checkpoint
        from ..runtime.resilience import PreflightRefused
        if len(candidates) != len(verdicts):
            raise PreflightRefused("重新筛选结果不完整，未写入")
        with self.lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            from ..core.result_groups import normalize_groups
            groups = normalize_groups(constraints.get("result_groups"))
            group_counts, admitted_count = {}, 0
            limit = sum(item["count"] for item in groups) if groups else constraints.get("result_limit")
            for candidate in candidates:
                row = db.execute("SELECT * FROM jobs WHERE id=?", (candidate["id"],)).fetchone()
                if not row or self.review_fingerprint(dict(row)) != self.review_fingerprint(candidate):
                    raise PreflightRefused("职位证据已变化，请重新读取再筛选，未写入")
            checked_ids = {item["id"] for item in candidates}
            recorded = db.execute("SELECT DISTINCT j.* FROM jobs j JOIN browser_events e "
                "ON j.id=json_extract(e.sample_json,'$.job_id') WHERE e.task_id=? "
                "AND e.event_type IN ('job_stored','job_duplicate','candidate_recorded','job_refiltered')", (task_id,)).fetchall()
            effect_checkpoint("started")
            # Even an unchanged filter digest cannot preserve old slots when the
            # count/quota changes. A bounded refresh admits only what it checked.
            for row in recorded:
                if row["id"] in checked_ids:
                    continue
                sample = {"job_id": row["id"], "filter_status": "unknown", "admission": "pending_verification",
                          "filter_evidence": {"status": "unknown", "reason": "未纳入本次有界重新核对"}}
                db.execute("INSERT OR REPLACE INTO search_candidates VALUES (?,?,?,?,?,?,?)",
                    (task_id, row["source_url"], row["id"], "pending_verification", None,
                     json.dumps(dict(row), ensure_ascii=False), utc_now()))
                db.execute("UPDATE jobs SET board_visible=0 WHERE id=? AND stage='review' AND NOT EXISTS "
                    "(SELECT 1 FROM search_candidates WHERE job_id=? AND task_id!=? AND status='accepted')",
                    (row["id"], row["id"], task_id))
                db.execute("INSERT INTO browser_events(task_id,event_type,detail,sample_json,created_at) VALUES (?,?,?,?,?)",
                    (task_id, "job_refiltered", "旧准入未沿用到新配额", json.dumps(sample, ensure_ascii=False), utc_now()))
            for candidate, verdict in zip(candidates, verdicts):
                group_index = None
                admitted = verdict["status"] == "pass"
                if admitted and groups:
                    group_index = next((item["index"] for item in verdict.get("result_group_evidence", [])
                                        if item["status"] == "pass" and group_counts.get(item["index"], 0) < groups[item["index"]]["count"]), None)
                    admitted = group_index is not None
                current_row = db.execute("SELECT removed_at FROM jobs WHERE id=?", (candidate["id"],)).fetchone()
                admitted = admitted and not current_row["removed_at"] and (not limit or admitted_count < limit)
                if admitted:
                    admitted_count += 1
                    group_counts[group_index] = group_counts.get(group_index, 0) + 1
                encoded = json.dumps(verdict, ensure_ascii=False)
                db.execute("UPDATE jobs SET filter_status=?,filter_evidence=? WHERE id=?",
                    (verdict["status"], encoded, candidate["id"]))
                updated = {**candidate, "filter_status": verdict["status"], "filter_evidence": encoded}
                sample = {"job_id": candidate["id"], "filter_status": verdict["status"],
                    "filter_evidence": verdict, "filter_fingerprint": self.review_fingerprint(updated)}
                status = "accepted" if admitted else "pending_verification" if verdict["status"] == "unknown" else "not_admitted"
                sample.update(admission=status, result_group=group_index)
                db.execute("UPDATE jobs SET board_visible=? WHERE id=?",
                       (int((admitted or candidate["stage"] != "review") and not current_row["removed_at"]), candidate["id"]))
                db.execute("INSERT OR REPLACE INTO search_candidates VALUES (?,?,?,?,?,?,?)",
                           (task_id, candidate["source_url"], candidate["id"], status, group_index,
                            json.dumps(updated, ensure_ascii=False), utc_now()))
                db.execute("INSERT INTO browser_events(task_id,event_type,detail,sample_json,created_at) VALUES (?,?,?,?,?)",
                    (task_id, "job_refiltered", "按新条件重新筛选已有JD", json.dumps(sample, ensure_ascii=False), utc_now()))
            db.execute("INSERT INTO browser_events(task_id,event_type,detail,sample_json,created_at) VALUES (?,?,?,?,?)",
                (task_id, "refilter_constraints", "更新已有结果的筛选条件；未启动新搜索",
                 json.dumps(constraints, ensure_ascii=False), utc_now()))
        effect_checkpoint("committed")

    def save_search_request(self, task_id: str, request: dict) -> None:
        """Canonical internal checkpoint, never a redacted event projection."""
        with self.lock, self._connect() as db:
            db.execute("INSERT OR IGNORE INTO search_recovery(task_id,request_json) VALUES (?,?)",
                       (task_id, json.dumps(request, ensure_ascii=False, sort_keys=True)))

    def search_recovery(self, task_id: str) -> dict:
        with self.lock, self._connect() as db:
            row = db.execute("SELECT * FROM search_recovery WHERE task_id=?", (task_id,)).fetchone()
            touched = db.execute("SELECT DISTINCT jobs.source_url FROM jobs JOIN browser_events e ON "
                "jobs.id=json_extract(e.sample_json,'$.job_id') WHERE e.task_id=? "
                "AND e.event_type IN ('job_stored','job_duplicate','candidate_recorded')", (task_id,)).fetchall()
        return {"request": json.loads(row["request_json"]) if row else None,
                "processed_urls": set(json.loads(row["processed_urls_json"]) if row else []) |
                                  {item["source_url"] for item in touched}}

    def mark_search_processed(self, task_id: str, url: str) -> None:
        with self.lock, self._connect() as db:
            row = db.execute("SELECT processed_urls_json FROM search_recovery WHERE task_id=?", (task_id,)).fetchone()
            if row:
                urls = set(json.loads(row[0])) | {url}
                db.execute("UPDATE search_recovery SET processed_urls_json=? WHERE task_id=?",
                           (json.dumps(sorted(urls), ensure_ascii=False), task_id))

    def job_touched_by_task(self, task_id: str, job_id: int) -> bool:
        """Find a durable receipt for this task/job pair."""
        with self.lock, self._connect() as db:
            rows = db.execute("SELECT sample_json FROM browser_events WHERE task_id=? AND event_type IN ('job_stored','job_duplicate')", (task_id,)).fetchall()
        return any(json.loads(row["sample_json"] or "{}").get("job_id") == job_id for row in rows)

    def reconcile_search_counts(self, task_id: str) -> None:
        """Repair a crash between a durable job receipt and its counter update."""
        with self.lock, self._connect() as db:
            rows = db.execute("SELECT event_type,COUNT(DISTINCT json_extract(sample_json,'$.job_id')) AS n "
                "FROM browser_events WHERE task_id=? AND event_type IN ('job_stored','job_duplicate') "
                "GROUP BY event_type", (task_id,)).fetchall()
        if rows:
            counts = {row["event_type"]: row["n"] for row in rows}
            self.update_task(task_id, stored=counts.get("job_stored", 0),
                duplicates=counts.get("job_duplicate", 0), matched=self.search_report(task_id)["qualified_jobs"])

    def search_report(self, task_id: str) -> dict[str, Any]:
        """Summarize only jobs touched by this run, including deduplicated jobs."""
        task = self.get_task(task_id)
        with self.lock, self._connect() as db:
            candidates = db.execute("SELECT job_id,status,group_index,candidate_json FROM search_candidates WHERE task_id=?", (task_id,)).fetchall()
            events = db.execute(
                "SELECT sample_json FROM browser_events WHERE task_id=? "
                "AND event_type IN ('job_stored','job_duplicate','job_refiltered') ORDER BY id",
                (task_id,),
            ).fetchall()
            constraint_row = db.execute("SELECT event_type,sample_json FROM browser_events WHERE task_id=? AND event_type IN ('search_constraints','refilter_constraints') ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
            constraints = json.loads(constraint_row["sample_json"] or "{}") if constraint_row else {}
            if constraint_row and constraint_row["event_type"] == "refilter_constraints":
                from ..core.job_filters import filter_digest, validate_filter
                expression = constraints.get("filters")
                digest = filter_digest(validate_filter(expression) if expression else None)
                events = [dict(event) for event in events]
                for event in events:
                    sample = json.loads(event["sample_json"] or "{}")
                    # Partial refresh must not count untouched old decisions as
                    # matching the newly displayed conditions in statistics.
                    if sample.get("filter_evidence", {}).get("digest") != digest:
                        sample.update(filter_status="unknown", filter_evidence={"status": "unknown",
                            "reason": "尚未按本轮新条件重新核对", "conditions": []})
                    event["sample_json"] = json.dumps(sample, ensure_ascii=False)
            job_ids: list[int] = []
            outcomes: dict[int, dict] = {}
            for event in events:
                try:
                    sample = json.loads(event["sample_json"] or "{}")
                    job_id = int(sample.get("job_id") or 0)
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue
                if job_id > 0 and job_id not in job_ids:
                    job_ids.append(job_id)
                outcomes[job_id] = sample
            rows = [] if not job_ids else db.execute(
                f"SELECT * FROM jobs WHERE id IN ({','.join('?' for _ in job_ids)}) ORDER BY id",
                job_ids,
            ).fetchall()

        rows = [{**dict(row), "admitted": outcomes.get(row["id"], {}).get("admission", "accepted") == "accepted",
                 "filter_status": outcomes.get(row["id"], {}).get("filter_status", row["filter_status"]),
                 "filter_evidence": json.dumps(outcomes.get(row["id"], {}).get("filter_evidence",
                    json.loads(row["filter_evidence"])), ensure_ascii=False)} for row in rows]

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
            qualified = row["admitted"] and row["filter_status"] == "pass" and row["stage"] != "rejected"
            if qualified:
                salaries[salary_bucket] = salaries.get(salary_bucket, 0) + 1
            characteristics = job_characteristics(title, description)
            employment_type = characteristics["employment_type"]
            if qualified:
                employment[employment_type] = employment.get(employment_type, 0) + 1
            present_benefits = characteristics["benefits"]
            for term in present_benefits if qualified else []:
                benefits[term] = benefits.get(term, 0) + 1
            jd = jd_evidence(description)
            if qualified:
                evidence.append(jd)
            jd_excerpt = "" if "初筛阶段未打开详情页" in description else jd["brief"]
            if not qualified:
                continue
            summaries.append({
                "id": int(row["id"]), "title": title, "company": str(row["company"] or ""),
                "location": str(row["location"] or ""), "salary": salary,
                "score": int(row["score"] or 0), "stage": str(row["stage"] or ""),
                "filter_status": row["filter_status"], "filter_evidence": json.loads(row["filter_evidence"]),
                "employment_type": employment_type, "benefits": present_benefits,
                "jd_excerpt": jd_excerpt, "jd_summary": jd["brief"],
                "source_url": str(row["source_url"] or ""), "content_hash": self.review_fingerprint(dict(row)),
            })
        summaries.sort(key=lambda item: (item["stage"] != "rejected", item["score"]), reverse=True)
        pending_ids = {item["id"] for item in rows if item["filter_status"] == "unknown"}
        pending_ids.update(row["job_id"] for row in candidates if row["status"] == "pending_verification")
        covered_count = len({item["id"] for item in rows} | {row["job_id"] for row in candidates})
        summaries = [item for item in summaries if item["filter_status"] == "pass" and item["stage"] != "rejected"]
        qualified_rows = [row for row in rows if row["admitted"] and row["filter_status"] == "pass" and row["stage"] != "rejected"]
        report = {
            "covered_jobs": covered_count,
            "qualified_jobs": sum(item["stage"] != "rejected" and item["filter_status"] == "pass" for item in summaries),
            "pending_verification": len(pending_ids),
            "result_groups": [{"label": group["label"], "target": group["count"],
                               "matched": sum(row["status"] == "accepted" and row["group_index"] == index for row in candidates)}
                              for index, group in enumerate(constraints.get("result_groups") or [])],
            "constraints": constraints,
            "salary_distribution": salaries,
            "employment_distribution": employment,
            "benefit_distribution": benefits,
            "jd_overview": aggregate_jd(evidence),
            "salary_overview": salary_overview([row["salary"] for row in qualified_rows]),
            "jobs": summaries,
            "highlights": summaries,  # Compatibility: no silent three-item truncation.
        }
        from .search_reporting import render_search_report
        report["message"] = render_search_report(task, report)
        return report

    def stats(self) -> AutomationStats:
        task = self.latest_task()
        with self.lock, self._connect() as db:
            stages = {row["stage"]: row["count"] for row in db.execute("SELECT stage,COUNT(*) AS count FROM jobs WHERE board_visible=1 AND (filter_status='pass' OR stage!='review') GROUP BY stage")}
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
                    "SELECT title,description,location,salary,tags,score,stage,filter_status FROM jobs WHERE id IN ("
                    + ",".join("?" for _ in job_ids) + ")",
                    sorted(job_ids),
                ).fetchall() if job_ids else []
            else:
                task_rows = db.execute(
                    "SELECT discovered,stored,duplicates,status FROM browser_tasks WHERE status IN ('completed','stopped','failed')"
                ).fetchall()
                job_rows = db.execute("SELECT title,description,location,salary,tags,score,stage,filter_status FROM jobs").fetchall()

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
            if row["filter_status"] != "pass":
                continue
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
            "stored_total": sum(row["filter_status"] == "pass" for row in job_rows),
            "pending_verification": sum(row["filter_status"] == "unknown" for row in job_rows),
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

    def resume_analysis_evidence(self, resume_id: str, *, limit: int = 20,
                                 legacy_since: str | None = None, min_score: int | None = None,
                                 max_score: int | None = None, salary_min: int | None = None,
                                 task_id: str | None = None) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """Return saved JD evidence plus why other candidates were excluded.

        The gaps mapping separates "no matching job" from "cannot decide because
        the search evidence is missing or stale". Delivery uses that distinction
        to wait for the user instead of reporting a completed zero-job batch.
        """
        gaps = {"no_search_evidence": 0, "no_candidates": 0, "unknown_filter": 0,
                "stale_filter": 0, "missing_job": 0, "removed": 0, "missing_score": 0,
                "below_min_score": 0, "above_max_score": 0, "salary_unverified": 0,
                "salary_below_min": 0}
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
                gaps["no_search_evidence"] = 1
                return [], gaps
            placeholders = ",".join("?" for _ in task_ids)
            from ..core.job_filters import filter_digest, validate_filter
            updated_digests = {}
            for selected_task in task_ids:
                updated = db.execute("SELECT sample_json FROM browser_events WHERE task_id=? "
                    "AND event_type='refilter_constraints' ORDER BY id DESC LIMIT 1", (selected_task,)).fetchone()
                if updated:
                    value = json.loads(updated[0]).get("filters")
                    updated_digests[selected_task] = filter_digest(validate_filter(value) if value else None)
            events = db.execute(
                "SELECT task_id,sample_json FROM browser_events WHERE task_id IN (" + placeholders + ") "
                "AND event_type IN ('job_stored','job_duplicate','job_refiltered') ORDER BY id DESC",
                task_ids,
            ).fetchall()
            ids: list[int] = []
            outcomes: dict[int, dict] = {}
            seen_ids: set[int] = set()
            for event in events:
                try:
                    sample = json.loads(event["sample_json"] or "{}")
                    job_id = int(sample.get("job_id") or 0)
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue
                if job_id <= 0 or job_id in seen_ids:
                    continue
                seen_ids.add(job_id)
                outcomes[job_id] = sample
                fresh = event["task_id"] not in updated_digests or sample.get("filter_evidence", {}).get("digest") == updated_digests[event["task_id"]]
                if not fresh:
                    gaps["stale_filter"] += 1
                    continue
                if sample.get("filter_status", "pass") != "pass" or sample.get("admission", "accepted") != "accepted":
                    gaps["unknown_filter"] += 1
                    continue
                ids.append(job_id)
            if not ids:
                gaps["no_candidates"] = 1
                return [], gaps
            rows = db.execute(
                "SELECT * "
                "FROM jobs WHERE id IN (" + ",".join("?" for _ in ids) + ")", ids,
            ).fetchall()
        by_id = {int(row["id"]): dict(row) for row in rows}
        from ..core.search_contract import monthly_salary_lower_bound
        selected: list[dict[str, Any]] = []
        for job_id in ids:
            row = by_id.get(job_id)
            if row is None:
                gaps["missing_job"] += 1
                continue
            if row.get("removed_at"):
                gaps["removed"] += 1
                continue
            if row.get("filter_status", "pass") != "pass":
                gaps["unknown_filter"] += 1
                continue
            outcome = outcomes.get(job_id, {})
            if outcome.get("filter_status", "pass") != "pass":
                gaps["unknown_filter"] += 1
                continue
            if (outcome.get("filter_fingerprint")
                    and outcome["filter_fingerprint"] != self.review_fingerprint(row)):
                gaps["stale_filter"] += 1
                continue
            score = row.get("score")
            if score is None:
                gaps["missing_score"] += 1
                continue
            if min_score is not None and score < min_score:
                gaps["below_min_score"] += 1
                continue
            if max_score is not None and score > max_score:
                gaps["above_max_score"] += 1
                continue
            if salary_min is not None:
                salary = monthly_salary_lower_bound(row["salary"])
                if salary is None:
                    gaps["salary_unverified"] += 1
                    continue
                if salary < salary_min:
                    gaps["salary_below_min"] += 1
                    continue
            selected.append(row)
            if len(selected) >= limit:
                break
        return selected, gaps

    def jobs_for_resume_analysis(self, resume_id: str, *, limit: int = 20,
                                 legacy_since: str | None = None, min_score: int | None = None,
                                 max_score: int | None = None, salary_min: int | None = None,
                                 task_id: str | None = None) -> list[dict[str, Any]]:
        """Return saved JD evidence from terminal runs for one resume, not the global board."""
        return self.resume_analysis_evidence(resume_id, limit=limit, legacy_since=legacy_since,
            min_score=min_score, max_score=max_score, salary_min=salary_min, task_id=task_id)[0]

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
                                binding: dict, min_score: int, scope: dict | None = None,
                                scope_evidence: dict | None = None) -> list[int]:
        inserted = []
        with self.lock, self._connect() as db:
            for item in items:
                current = db.execute("SELECT removed_at FROM jobs WHERE id=?", (item["id"],)).fetchone()
                if not current or current["removed_at"]:
                    continue
                selected_unknown = item.get("filter_status") == "unknown" and (scope_evidence or {}).get(item["id"], {}).get("allow_unverified") is True
                if item.get("filter_status", "pass") != "pass" and not selected_unknown:
                    continue
                cursor = db.execute(
                    "INSERT OR IGNORE INTO mock_delivery_queue "
                    "(job_id,session_id,resume_stamp,binding_json,job_hash,min_score,status,updated_at,scope_json,scope_evidence_json) "
                    "VALUES (?,?,?,?,?,?,'queued',?,?,?)",
                    (item["id"], session_id, resume_stamp, json.dumps(binding),
                     self.review_fingerprint(item), min_score, utc_now(), json.dumps(scope or {}),
                     json.dumps((scope_evidence or {}).get(item["id"], {}))),
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

    def record_authorization_audit(self, row: dict) -> None:
        """Store what the authorization layer would have decided. Never blocks anything."""
        with self.lock, self._connect() as db:
            db.execute(
                "INSERT INTO authorization_audit(tool,risk,would_confirm,policy,threshold,"
                "candidates,in_scope,out_of_scope,detail,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (row["tool"], row["risk"], row["would_confirm"], row["policy"], row["threshold"],
                 row["candidates"], row["in_scope"], row["out_of_scope"], row["detail"], utc_now()))

    def authorization_audit(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self.lock, self._connect() as db:
            rows = db.execute("SELECT * FROM authorization_audit ORDER BY id DESC LIMIT ?",
                              (limit,)).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            try:
                item["detail"] = json.loads(item.get("detail") or "{}")
            except ValueError:
                item["detail"] = {}
            output.append(item)
        return output

    def authorization_audit_summary(self) -> dict[str, Any]:
        """The numbers stage (c) exists to produce.

        Reads the whole table, so the operator can see how often the gate would have
        fired and how much the scope boundary would have excluded before either is wired
        into execution.
        """
        with self.lock, self._connect() as db:
            row = db.execute(
                "SELECT COUNT(*) runs, COALESCE(SUM(would_confirm),0) confirms, "
                "COALESCE(SUM(candidates),0) candidates, COALESCE(SUM(in_scope),0) in_scope, "
                "COALESCE(SUM(out_of_scope),0) out_of_scope FROM authorization_audit").fetchone()
            by_risk = {r["risk"]: r["n"] for r in db.execute(
                "SELECT risk, COUNT(*) n FROM authorization_audit GROUP BY risk")}
        total = int(row["runs"] or 0)
        return {
            "batches": total,
            "would_confirm": int(row["confirms"] or 0),
            "would_confirm_rate": round((row["confirms"] or 0) / total, 3) if total else 0.0,
            "candidates": int(row["candidates"] or 0),
            "in_scope": int(row["in_scope"] or 0),
            "out_of_scope": int(row["out_of_scope"] or 0),
            "out_of_scope_rate": round((row["out_of_scope"] or 0) / row["candidates"], 3)
                                 if row["candidates"] else 0.0,
            "by_risk": by_risk,
        }

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

    def mark_job_needs_review(self, job_id: int, detail: str) -> None:
        """Park a job for human review, with the reason visible on the card.

        Used when an automatic path has run out of budget: the job must stop looking
        like active work, and the board must say what needs a human.
        """
        with self.lock, self._connect() as db:
            db.execute("UPDATE jobs SET stage='review',mode='需要人工核验',last_seen_at=? WHERE id=? "
                       "AND stage NOT IN ('waiting','interview','rejected')", (utc_now(), job_id))
            db.execute("UPDATE mock_delivery_queue SET status='blocked',detail=?,updated_at=? WHERE job_id=?",
                       (detail[:400], utc_now(), job_id))

    def all_delivery_runs(self, *, limit: int = 200) -> list[dict]:
        """Every run, newest first, for the automation loop's observation."""
        with self.lock, self._connect() as db:
            rows = db.execute("SELECT * FROM delivery_runs ORDER BY started_at DESC LIMIT ?",
                              (limit,)).fetchall()
        return [dict(row) for row in rows]

    def deliveries_to_reconcile(self, *, limit: int = 10, applicant_id: int | None = None) -> list[dict]:
        with self.lock, self._connect() as db:
            rows = db.execute("SELECT * FROM delivery_runs WHERE status='outcome_indeterminate' "
                "AND (? IS NULL OR mock_applicant_id=?) "
                "ORDER BY COALESCE(reconciled_at,''),started_at LIMIT ?", (applicant_id, applicant_id, limit)).fetchall()
        return [dict(row) for row in rows]

    def record_delivery_reconciliation(self, expected: dict, observation: dict) -> bool:
        """CAS a read receipt and local completion together; never schedule sends."""
        keys = ("job_id", "target_url", "mock_applicant_id", "mock_resume_id", "mock_resume_digest",
                "resume_document_id", "resume_version", "resume_digest", "action", "updated_at")
        with self.lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM delivery_runs WHERE id=?", (expected["id"],)).fetchone()
            if not row or row["status"] != 'outcome_indeterminate' or any(row[key] != expected.get(key) for key in keys):
                return False
            confirmed = observation.get("status") == "confirmed" and observation.get("application_id") is not None
            latest = db.execute("SELECT id FROM delivery_runs WHERE job_id=? ORDER BY started_at DESC,rowid DESC LIMIT 1",
                                (row["job_id"],)).fetchone()
            # A later delivery attempt owns the visible job state.
            if not latest or latest["id"] != expected["id"]:
                return False
            stamp = utc_now()
            db.execute("UPDATE delivery_runs SET reconciliation_json=?,reconciled_at=?,updated_at=? WHERE id=?",
                (json.dumps(observation, ensure_ascii=False), stamp, stamp, row["id"]))
            db.execute("INSERT INTO browser_events(task_id,event_type,detail,sample_json,created_at) VALUES (?,?,?,?,?)",
                (row["id"], "delivery_reconciliation", observation.get("reason", "")[:500],
                 json.dumps(observation, ensure_ascii=False), stamp))
            if not confirmed:
                return False
            db.execute("UPDATE delivery_runs SET status='completed',submitted=1,checkpoint='read_receipt_confirmed',detail=? WHERE id=?",
                (observation["reason"], row["id"]))
            db.execute("UPDATE jobs SET stage='waiting',mode='已核实投递',last_seen_at=? WHERE id=? "
                       "AND removed_at IS NULL AND stage NOT IN ('interview','rejected')", (stamp, row["job_id"]))
            db.execute("UPDATE mock_delivery_queue SET status='completed',detail=?,updated_at=? WHERE run_id=? AND status<>'cancelled'",
                       (observation["reason"], stamp, row["id"]))
            return True

    def delivery_runs_failed_before_submit(self, *, limit: int = 50) -> list[dict]:
        """Explicit transient pre-submit failures, including exhausted budgets.

        Legacy/unclassified failures and binding/precondition failures are not
        evidence for automatic retry. The coordinator also checks the budget,
        original run binding and withdrawal; it never mints another capability.
        """
        with self.lock, self._connect() as db:
            rows = db.execute(
                "SELECT * FROM delivery_runs WHERE status='failed' AND submitted=0 "
                "AND checkpoint='failed_transient_before_submit' ORDER BY started_at LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def delivery_runs_waiting_reply(self, *, limit: int | None = 50) -> list[dict]:
        """Granted runs waiting for HR, least recently checked first.

        Reply monitoring is a read-only periodic check, so it must be able to find
        these without holding the single delivery worker.
        """
        with self.lock, self._connect() as db:
            rows = db.execute(
                "SELECT r.*,j.recruiter_reply AS observed_reply FROM delivery_runs r JOIN jobs j ON j.id=r.job_id "
                "WHERE r.status='waiting_for_reply' AND r.submitted=0 "
                "ORDER BY CASE WHEN COALESCE(j.recruiter_reply,'')='' THEN 1 ELSE 0 END,COALESCE(r.last_checked_at,''),r.started_at" +
                (" LIMIT ?" if limit is not None else ""), (limit,) if limit is not None else ()).fetchall()
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
        return [{**dict(row), "binding": json.loads(row["binding_json"]),
                 "scope": json.loads(row["scope_json"] or "{}"),
                 "scope_evidence": json.loads(row["scope_evidence_json"] or "{}")} for row in rows]

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

    def remove_selection(self, items: list[dict], *, session_id: str, run_id: str) -> dict:
        """Atomic reversible exclusion; retain jobs, facts and all send receipts."""
        from ..runtime.context import effect_checkpoint
        from ..runtime.resilience import PreflightRefused
        if not items or len(items) > 200 or len({item["id"] for item in items}) != len(items):
            raise PreflightRefused("移除对象数量无效，未写入")
        ids, now = [item["id"] for item in items], utc_now()
        with self.lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for item in items:
                row = db.execute("SELECT * FROM jobs WHERE id=?", (item["id"],)).fetchone()
                if not row or self.review_fingerprint(dict(row)) != item.get("content_hash"):
                    raise PreflightRefused("岗位证据已变化，未移除任何对象，请重新查看列表")
                queue = db.execute("SELECT session_id,status FROM mock_delivery_queue WHERE job_id=?", (item["id"],)).fetchone()
                if queue and queue["session_id"] != session_id and queue["status"] in {"queued", "dispatched"}:
                    raise PreflightRefused("所选岗位由另一个会话执行中，未停止其他会话任务")
            effect_checkpoint("started")
            for item in items:
                db.execute("UPDATE jobs SET board_visible=0,removed_at=COALESCE(removed_at,?) WHERE id=?", (now, item["id"]))
                db.execute("INSERT OR REPLACE INTO job_removals VALUES (?,?,?,?,?)", (item["id"], session_id, run_id, item["content_hash"], now))
                db.execute("UPDATE mock_delivery_queue SET status='cancelled',detail='用户移除岗位；已发送内容保留',updated_at=? WHERE job_id=? AND session_id=? AND status IN ('queued','dispatched','blocked')", (now, item["id"], session_id))
                db.execute("UPDATE delivery_previews SET status='cancelled' WHERE job_id=? AND consumed_at IS NULL", (item["id"],))
        effect_checkpoint("committed")
        return {"removed_ids": ids, "history_preserved": True}

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
        facts = {key: job.get(key) for key in ("id", "title", "company", "location", "salary", "source_url", "content_hash", "filter_status", "filter_evidence")}
        if job.get("qualification_evidence") is not None and job.get("qualification_evidence")!="null":
            facts["qualification_evidence"]=job["qualification_evidence"]
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
        allow_unverified: bool = False, delivery_flow: str = "native",
    ) -> DeliveryPreviewView:
        if action != "submit_resume":
            raise ValueError("不支持的投递动作")
        if delivery_flow not in {"native", "gated", "direct"}:
            raise ValueError("不支持的模拟投递流程")
        job = self.get_job(job_id)
        now = datetime.now(UTC)
        self._check_delivery_filter(job, {"allow_unverified": allow_unverified})
        expires = now + timedelta(minutes=30)
        payload = {
            "job_id": job_id,
            "filter_fingerprint": self.review_fingerprint(job),
            "title": job["title"],
            "company": job["company"],
            "source_url": job["source_url"],
            "resume_label": resume_label,
            "resume_document_id": resume_document_id,
            "resume_version": resume_version,
            "resume_digest": resume_digest,
            "action": action,
            "action_summary": "填写申请表并上传所选简历；遇到验证码、MFA 或未知问题立即暂停",
            "allow_unverified": allow_unverified,
        }
        if delivery_flow in {"direct", "gated"}:
            from ..delivery.mock_site_adapter import MockJobSiteAdapter
            from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
            target = MockJobSiteAdapter.normalize_job_url(job["source_url"])
            if not target:
                raise ValueError("投递流程切换仅支持本地模拟招聘站")
            parsed = urlparse(target)
            query = {"id": parse_qs(parsed.query)["id"][0]}
            if delivery_flow == "direct":
                query["flow"] = "direct"
            payload["target_url"] = urlunparse(parsed._replace(query=urlencode(query)))
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
            current_job = db.execute("SELECT * FROM jobs WHERE id=?", (row["job_id"],)).fetchone()
            if current_job is None or current_job["source_url"] != payload.get("source_url"):
                raise ValueError("职位链接已变化，请重新生成投递预览")
            self._check_delivery_filter(dict(current_job), payload)
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
                "UPDATE jobs SET mode='人工已授权',last_seen_at=? WHERE id=?",
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
            job = db.execute("SELECT * FROM jobs WHERE id=?", (row["job_id"],)).fetchone()
            self._check_delivery_filter(dict(job) if job else {}, json.loads(row["payload_json"]))
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

    def _check_delivery_filter(self, job: dict, payload: dict | None = None) -> None:
        if job.get("removed_at"):
            raise ValueError("岗位已由用户移除，不能继续投递")
        selected_unknown = job and job.get("filter_status") == "unknown" and (payload or {}).get("allow_unverified") is True
        if not job or job.get("filter_status", "pass") != "pass" and not selected_unknown:
            raise ValueError("职位筛选条件未核实通过，不能沟通或投递；请先核对条件并重新搜索")
        if payload and "filter_fingerprint" in payload and payload["filter_fingerprint"] != self.review_fingerprint(job):
            raise ValueError("筛选条件或职位证据已变化，请重新生成预览并授权")

    def assert_delivery_facts(self, job_id: int, preview_id: str | None) -> None:
        with self.lock, self._connect() as db:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            preview = db.execute("SELECT * FROM delivery_previews WHERE id=? AND job_id=?", (preview_id, job_id)).fetchone()
        if preview is None:
            raise ValueError("缺少与职位绑定的投递预览，需重新授权")
        self._check_delivery_filter(dict(job) if job else {}, json.loads(preview["payload_json"]))

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
