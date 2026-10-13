"""Explicit local maintenance, never an agent tool or a recruitment action."""
from __future__ import annotations

import os
import sqlite3
import stat
import uuid
from contextlib import ExitStack, closing
from pathlib import Path

RESET_PATH = "/api/v1/maintenance/history"
JOB_TABLES = ("search_candidates", "search_recovery", "mock_delivery_queue", "delivery_runs", "delivery_previews", "job_review_decisions",
    "browser_events", "jobs", "browser_tasks", "conversation_messages", "learning_plans")
RUN_TABLES = ("action_approvals", "agent_run_events", "agent_runs", "session_search_state", "session_dialogue_state", "agent_sessions")
ROUTER_TABLES = ("router_events", "rule_hit_stats", "router_disagreements", "clarify_memory")
MOCK_TABLES = ("form_responses", "messages", "applications", "resumes")
FILES = ("memory.md", "audit", "cache-backups", "skill-history", "skill-proposals",
    "private/resumes", "private/materials", "private/candidate-state", "private/resume-workspace",
    "private/profile.json.enc", "private/browser-evidence", "private/browser-lifecycle.jsonl",
    "private/private/browser-lifecycle.jsonl")


class ResetBusy(ValueError):
    pass


class HistoryResetter:
    def __init__(self, root, jobs, runs, router, agent, browser, delivery, workflow, agent_runtime, *, mock_database=None):
        self.root = Path(root).resolve()
        self.jobs, self.runs, self.router, self.agent = jobs, runs, router, agent
        self.browser, self.delivery, self.workflow = browser, delivery, workflow
        self.agent_runtime = agent_runtime
        self.mock_database = Path(mock_database) if mock_database else None

    def mock_available(self) -> bool:
        path = self.mock_database
        if not path or not path.is_absolute() or not path.is_file() or path.suffix != ".db":
            return False
        try:
            self.safe_path(path, path.parent)
        except ValueError:
            return False
        return True

    def assert_idle(self):
        workers = [*self.agent_runtime.tasks.values(), *self.agent_runtime.cancellations.values(),
            self.browser.worker, self.delivery.worker]
        if self.runs.list(active=True) or any(task and not task.done() for task in workers):
            raise ResetBusy("还有任务正在运行或等待接管，请先中断任务，再清除历史")
        if self.browser.current().status not in {"idle", "completed", "failed", "stopped"} or self.delivery.current().status not in {
            "idle", "completed", "failed", "cancelled", "outcome_indeterminate"}:
            raise ResetBusy("搜索或投递尚未结束，请先停止并核对结果")
        if self.workflow.sync_lock.locked() or self.workflow.queue_lock.locked():
            raise ResetBusy("模拟站正在同步，请稍后再清除历史")

    @staticmethod
    def safe_path(path: Path, root: Path) -> Path:
        if path.resolve() != path or path != root and root not in path.parents:
            raise ValueError("清理路径不在已配置的数据范围内")
        for item in (path, *path.parents):
            if item.exists() and (item.is_symlink() or getattr(item.stat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                raise ValueError("不清理符号链接或目录联接")
            if item == root:
                break
        if path.is_dir():
            for child in path.rglob("*"):
                if child.resolve() != child or getattr(child.stat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise ValueError("历史目录含链接，未清理")
        return path

    async def clear(self, *, include_mock: bool) -> dict:
        self.assert_idle()
        await self.workflow.stop()
        try:
            self.assert_idle()
            result = self._clear_files_and_databases(include_mock)
            self.runs.ensure_default_session()  # Only a fresh default session, no old state.
            self.agent.restore_history([])
            self.agent._local_search_state, self.agent._local_dialogue = None, {}
            if self.agent.router:
                self.agent.router._last_decision.clear()
            self.browser.current_id, self.browser.worker = None, None
            self.delivery.worker = None
            self.delivery.reload_persisted_state()
            self.agent_runtime.tasks.clear()
            self.agent_runtime.cancellations.clear()
            self.agent_runtime.sessions.clear()
            self.workflow.poll_failures.clear()
            self.agent.resume_workspace.ensure_document(self.agent.candidate_state.read().profile)
            return result
        finally:
            await self.workflow.start()

    def _clear_files_and_databases(self, include_mock: bool) -> dict:
        targets = [self.safe_path(self.root / name, self.root) for name in FILES if (self.root / name).exists()]
        specs = [(self.jobs.path, JOB_TABLES), (self.runs.path, RUN_TABLES), (self.router.path, ROUTER_TABLES)]
        for path, _ in specs:
            self.safe_path(path, self.root)
        if include_mock:
            if not self.mock_available():
                raise ValueError("未配置本地模拟站数据库；未清理任何历史")
            self.safe_path(self.mock_database, self.mock_database.parent)
            specs.append((self.mock_database, MOCK_TABLES))
        backup_root = self.safe_path(self.root / "history-backups" / uuid.uuid4().hex, self.root)
        backup_root.mkdir(parents=True)
        connections, snapshots, moved, counts, committed = [], [], [], {}, []
        with ExitStack() as stack:
            for lock in (self.jobs.lock, self.runs.lock, self.router.lock):
                stack.enter_context(lock)
            try:
                for index, (path, tables) in enumerate(specs):
                    db = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=3)
                    stack.callback(db.close)
                    connections.append(db)
                    db.execute("PRAGMA foreign_keys=ON")
                    known = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if not set(tables) <= known or index == 3 and not {"users", "jobs", "form_templates"} <= known:
                        raise ValueError("数据库结构不符；未授权清理其他数据库")
                    db.execute("BEGIN IMMEDIATE")
                    snapshot = backup_root / f"database-{index}.db"
                    # Reserve the writer first; a separate reader snapshots the
                    # unchanged database without blocking on our transaction.
                    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as reader, closing(sqlite3.connect(snapshot)) as copy:
                        reader.backup(copy)
                    snapshots.append(snapshot)
                for index, ((_, tables), db) in enumerate(zip(specs, connections)):
                    counts[str(index)] = {table: db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in tables}
                    for table in tables:
                        db.execute(f'DELETE FROM "{table}"')
                    if db.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone():
                        db.execute("DELETE FROM sqlite_sequence WHERE name IN (" + ",".join("?" for _ in tables) + ")", tables)
                    if index == 0:
                        db.execute("UPDATE mock_workflow_settings SET monitor_enabled=0,auto_submit=0,daily_enabled=0,"
                            "auto_submit_enabled_at=NULL,applicant_id=NULL,last_run_date=NULL,last_run_id=NULL,last_reported_run_id=NULL,last_error=NULL")
                    if db.execute("PRAGMA foreign_key_check").fetchall():
                        raise ValueError("历史清理未通过数据库一致性核验")
                for target in targets:
                    destination = backup_root / "files" / target.relative_to(self.root)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(target, destination)
                    moved.append((target, destination))
                for db in connections:
                    db.commit()
                    committed.append(db)
            except Exception:
                for db in connections:
                    db.rollback()
                for db, snapshot in zip(connections, snapshots):
                    if db in committed:
                        with closing(sqlite3.connect(snapshot)) as copy:
                            copy.backup(db)
                for target, destination in reversed(moved):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(destination, target)
                raise
        return {"cleared": True, "include_mock": include_mock, "backup_id": backup_root.name,
            "archived_paths": len(moved), "removed_rows": counts, "message": "历史数据已清空；模型配置、登录状态和模拟职位库保留。监测与每日任务已关闭，备份可恢复。"}
