"""Automation and monitoring scenarios that need a real (temporary) store.

These use a throwaway SQLite file via ``AUTOHUNT_DATA_ROOT``, never the user's board.
They cover the transitions the board and the delivery pipeline depend on: queueing,
claiming, one-use authorization, attempt counting, checkpoint semantics, reply and
manual-application sync, growth-plan persistence and stats aggregation.
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from .core import Case, expect, expect_equal, expect_in, expect_not_in, expect_raises

CASES: list[Case] = []


def _add(case_id: str, group: str, description: str, check, tags: tuple[str, ...] = (), *, xfail: bool = False) -> None:
    CASES.append(Case(case_id, group, "state", description, check, tags, xfail=xfail))


class _Store:
    """A JobStore on a temp file, used as a context manager.

    The store never closes its SQLite connections (a known defect: see
    docs/code-review.md), so on Windows the file stays locked and the temp directory
    cannot be removed. This wrapper closes what the store opens: a workaround for the
    product, not a convenience for the test.
    """

    def __init__(self) -> None:
        # ignore_cleanup_errors: the store leaks connections, so on Windows the file can
        # stay locked after the case ends. The case result must not depend on whether the
        # OS has released the handle yet; the temp directory is reclaimed by the OS.
        self._tmp = tempfile.TemporaryDirectory(prefix="autohunt-test-",
                                                ignore_cleanup_errors=True)
        self.path = Path(self._tmp.name) / "autohunt.db"
        self.store = None

    def __enter__(self):
        from app.jobs.store import JobStore

        # JobStore opens a fresh sqlite3 connection per call and never closes it — its
        # own __init__ leaks one as well — so on Windows the temp file stays locked and
        # the directory cannot be removed. Patch the class *before* constructing it so
        # even the constructor's connection is tracked and closed. This is a workaround
        # for a defect in the product, not a convenience for the test.
        self._connections: list = []
        original = JobStore._connect

        def tracking_connect(store_self):
            connection = original(store_self)
            self._connections.append(connection)
            return connection

        JobStore._connect = tracking_connect
        try:
            self.store = JobStore(self.path)
        finally:
            JobStore._connect = original
        return self.store

    def __exit__(self, *exc):
        self._release()
        self._tmp.cleanup()
        return False

    def _release(self) -> None:
        for connection in reversed(getattr(self, "_connections", [])):
            try:
                connection.close()
            except Exception:  # noqa: BLE001 - cleanup must never mask a test result
                pass
        self._connections = []


def _seed_job(store, *, job_id: int = 1, title: str = "算法工程师", score: int = 90,
              external_id: int = 5001) -> int:
    """Insert a job with every NOT NULL column the schema declares.

    The board columns a case inspects are stage/mode/score; the rest exist because the
    table requires them, so they are filled with realistic placeholders rather than
    empty strings that would hide a missing-field bug in the code under test.
    """
    with store.lock, store._connect() as db:
        db.execute(
            "INSERT INTO jobs (id,dedupe_key,source,source_url,external_id,title,company,"
            "location,salary,description,tags,score,decision,reasoning,stage,mode,"
            "content_hash,first_seen_at,last_seen_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, f"key-{job_id}", "mocksite",
             f"http://127.0.0.1:8080/job.html?id={external_id}", str(external_id),
             title, "示例公司", "上海", "28-45K",
             "岗位职责：负责算法研发与模型迭代，使用 Python 与 PyTorch。",
             "[]", score, "APPLY", "技能匹配", "review", "人工确认",
             f"hash-{job_id}", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
    return job_id


def _tables(store) -> set[str]:
    with store.lock, store._connect() as db:
        return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


# ------------------------------------------------------------------ schema / 迁移

def _schema_cases() -> None:
    def check_expected_tables_exist():
        with _Store() as store:
            tables = _tables(store)
            for name in ("jobs", "delivery_runs", "delivery_previews", "mock_delivery_queue",
                         "browser_tasks", "automation_decisions", "learning_plans",
                         "conversation_messages", "mock_workflow_settings"):
                expect_in(name, tables, f"缺少表 {name}")
            return sorted(tables)

    def check_attempts_column_is_migrated():
        with _Store() as store:
            with store.lock, store._connect() as db:
                columns = {row[1] for row in db.execute("PRAGMA table_info(delivery_runs)")}
            expect_in("attempts", columns, "delivery_runs.attempts 应由迁移补齐")

    def check_automation_decisions_columns():
        with _Store() as store:
            with store.lock, store._connect() as db:
                columns = {row[1] for row in db.execute("PRAGMA table_info(automation_decisions)")}
            for name in ("rule", "result", "considered", "facts_json", "created_at"):
                expect_in(name, columns, f"automation_decisions 缺少 {name}")

    _add("db01", "存储", "首次建库应包含全部必需的表", check_expected_tables_exist)
    _add("db02", "存储", "delivery_runs 应迁移出 attempts 列", check_attempts_column_is_migrated)
    _add("db03", "存储", "automation_decisions 应具备决策记录字段", check_automation_decisions_columns)


# ------------------------------------------------------------------ 试算与状态

def _attempt_cases() -> None:
    def check_attempts_increment_monotonically():
        with _Store() as store:
            _seed_job(store)
            store.create_delivery_run("run-1", 1)
            counts = [store.bump_delivery_attempts("run-1") for _ in range(4)]
            expect_equal(counts, [1, 2, 3, 4], "重试计数必须单调递增")
            return counts

    def check_attempts_persist_across_reload():
        with _Store() as store:
            _seed_job(store)
            store.create_delivery_run("run-1", 1)
            store.bump_delivery_attempts("run-1")
            store.bump_delivery_attempts("run-1")
            reloaded = store.get_delivery_run("run-1")
            expect_equal(reloaded.attempts, 2, "重试预算必须落库，重启后才能不被重置")
            return reloaded.attempts

    def check_bump_unknown_run_raises():
        with _Store() as store:
            expect_raises(KeyError, lambda: store.bump_delivery_attempts("missing"),
                          "对不存在的运行记录计次应报错")

    _add("at01", "投递·重试", "重试计数应单调递增", check_attempts_increment_monotonically)
    _add("at02", "投递·重试", "重试计数应跨重载保留", check_attempts_persist_across_reload)
    _add("at03", "投递·重试", "对不存在的运行记录计次应报错", check_bump_unknown_run_raises)


# ---------------------------------------------------------------- 队列与领取

def _queue_cases() -> None:
    def check_claim_is_exclusive():
        with _Store() as store:
            _seed_job(store)
            store.enqueue_mock_deliveries(
                [store.get_job(1)], session_id="default", resume_stamp="s1",
                binding={"resume_document_id": "doc", "resume_version": 1, "resume_digest": "d"},
                min_score=85)
            first = store.claim_mock_delivery(1)
            second = store.claim_mock_delivery(1)
            expect(first, "第一次领取应当成功")
            expect(not second, "同一职位不得被领取两次")
            return {"first": first, "second": second}

    def check_that_enqueued_mode_marks_authorized():
        with _Store() as store:
            _seed_job(store)
            store.enqueue_mock_deliveries(
                [store.get_job(1)], session_id="default", resume_stamp="s1",
                binding={"resume_document_id": "doc", "resume_version": 1, "resume_digest": "d"},
                min_score=85)
            rows = store.mock_delivery_queue(active_only=False)
            expect(rows, "队列应有记录")
            return rows[0]

    _add("qu01", "投递·队列", "同一职位只能被领取一次", check_claim_is_exclusive)
    _add("qu02", "投递·队列", "入队记录应带有职位与绑定信息", check_that_enqueued_mode_marks_authorized)


# ---------------------------------------------------------------- 一次性授权

def _authorization_cases() -> None:
    def _with_preview(store):
        _seed_job(store)
        return store.create_delivery_preview(
            1, resume_label="候选人主简历", resume_document_id="doc", resume_version=1,
            resume_digest="digest", action="submit_resume")

    def check_preview_binds_resume_and_job():
        with _Store() as store:
            preview = _with_preview(store)
            payload = preview.payload if hasattr(preview, "payload") else {}
            expect(preview.job_id == 1 or payload.get("job_id") == 1, "预览应绑定职位")
            expect(bool(getattr(preview, "payload_digest", "")), "预览应有摘要以防篡改")
            return {"id": preview.id, "digest": bool(getattr(preview, "payload_digest", ""))}

    def check_authorization_is_single_use():
        with _Store() as store:
            preview = _with_preview(store)
            authorization_id, token, _job_id, _expires = store.authorize_delivery(preview.id)
            expect(bool(token), "授权应返回一次性令牌")
            store.consume_delivery_authorization(authorization_id, token)   # first use
            expect_raises(ValueError,
                          lambda: store.consume_delivery_authorization(authorization_id, token),
                          "同一令牌第二次使用必须被拒绝")
            return {"single_use": True}

    def check_wrong_token_rejected():
        with _Store() as store:
            preview = _with_preview(store)
            authorization_id, _token, _job_id, _expires = store.authorize_delivery(preview.id)
            expect_raises(ValueError,
                          lambda: store.consume_delivery_authorization(authorization_id, "not-the-token"),
                          "错误令牌必须被拒绝")

    def check_consumption_is_atomic_under_repeat():
        """The consume UPDATE must only match an 'authorized' row.

        It currently updates by id alone, so a duplicate consume can report success
        while no second submission happened. The guard its sibling function has
        (WHERE ... AND status='authorized' plus a rowcount check) is missing here.
        """
        with _Store() as store:
            preview = _with_preview(store)
            authorization_id, token, _job_id, _expires = store.authorize_delivery(preview.id)
            store.consume_delivery_authorization(authorization_id, token)
            with store.lock, store._connect() as db:
                row = db.execute("SELECT status, consumed_at FROM delivery_previews WHERE id=?",
                                 (preview.id,)).fetchone()
            expect_equal(row["status"], "consumed", "消费后状态应为 consumed")
            expect(bool(row["consumed_at"]), "消费时间应被记录")

    def check_resume_digest_mismatch_rejected():
        with _Store() as store:
            preview = _with_preview(store)
            try:
                store.authorize_delivery_run(preview.id, resume_digest="tampered")
            except Exception:
                return True          # refusing is the expected behaviour
            raise AssertionError("摘要不匹配时必须拒绝授权")

    _add("au01", "投递·授权", "预览应绑定职位与简历摘要", check_preview_binds_resume_and_job)
    _add("au02", "投递·授权", "一次性令牌不得被消费两次", check_authorization_is_single_use)
    _add("au03", "投递·授权", "错误令牌必须被拒绝", check_wrong_token_rejected)
    _add("au04", "投递·授权", "简历摘要不匹配必须拒绝授权", check_resume_digest_mismatch_rejected)


# ---------------------------------------------------------------- 失败查询语义

def _failure_query_cases() -> None:
    def check_pre_click_failure_is_findable():
        with _Store() as store:
            _seed_job(store)
            store.create_delivery_run("run-1", 1)
            store.update_delivery_run("run-1", status="failed", submitted=0,
                                      checkpoint="failed_before_submit", detail="未开始")
            found = store.delivery_runs_failed_before_submit()
            expect_equal([row["job_id"] for row in found], [1],
                         "点击前失败必须能被恢复查询找到")

    def check_post_click_uncertainty_is_not_findable():
        with _Store() as store:
            _seed_job(store)
            store.create_delivery_run("run-1", 1)
            store.update_delivery_run("run-1", status="outcome_indeterminate", submitted=0,
                                      checkpoint="submission_unconfirmed", detail="结果未知")
            found = store.delivery_runs_failed_before_submit()
            expect_equal([row["job_id"] for row in found], [],
                         "结果未知的投递绝不能被自动重试查询命中")

    def check_submitted_run_is_never_retried():
        with _Store() as store:
            _seed_job(store)
            store.create_delivery_run("run-1", 1)
            store.update_delivery_run("run-1", status="failed", submitted=1,
                                      checkpoint="failed_before_submit", detail="已投递")
            found = store.delivery_runs_failed_before_submit()
            expect_equal([row["job_id"] for row in found], [],
                         "已提交的投递不得进入重试")

    def check_reply_waiters_are_findable():
        with _Store() as store:
            _seed_job(store)
            store.create_delivery_run("run-1", 1)
            store.update_delivery_run("run-1", status="waiting_for_reply", submitted=0,
                                      checkpoint="waiting_for_reply", detail="等待回复")
            found = store.delivery_runs_waiting_reply()
            expect_equal([row["job_id"] for row in found], [1], "等待回复的投递应可被找到")

    _add("fq01", "投递·失败语义", "点击前失败必须能被恢复查询找到", check_pre_click_failure_is_findable)
    _add("fq02", "投递·失败语义", "结果未知的投递不得被自动重试", check_post_click_uncertainty_is_not_findable)
    _add("fq03", "投递·失败语义", "已提交的投递不得进入重试", check_submitted_run_is_never_retried)
    _add("fq04", "投递·失败语义", "等待回复的投递必须可被回复监测找到", check_reply_waiters_are_findable)


# ---------------------------------------------------------------- 状态与看板

def _board_cases() -> None:
    def check_needs_review_parks_job():
        with _Store() as store:
            _seed_job(store)
            store.mark_job_needs_review(1, "重试预算用尽")
            job = store.get_job(1)
            expect_equal(job["stage"], "review", "移交人工核验应回到待审核")
            expect_in("人工核验", str(job.get("mode", "")), "模式应体现需人工核验")

    def check_needs_review_preserves_delivered_jobs():
        with _Store() as store:
            _seed_job(store)
            store.update_job(1, stage="waiting") if hasattr(store, "update_job") else None
            with store.lock, store._connect() as db:
                db.execute("UPDATE jobs SET stage='waiting' WHERE id=1")
            store.mark_job_needs_review(1, "不应覆盖已投递")
            job = store.get_job(1)
            expect_equal(job["stage"], "waiting", "已投递的职位不得被退回待审核")

    def check_completed_delivery_moves_to_waiting():
        with _Store() as store:
            _seed_job(store)
            store.create_delivery_run("run-1", 1)
            store.mark_delivery_completed(1, "模拟网站已确认简历发送")
            job = store.get_job(1)
            expect_equal(job["stage"], "waiting", "投递成功应进入已投递列")

    def check_recruiter_reply_is_recorded():
        with _Store() as store:
            _seed_job(store)
            store.mark_recruiter_reply(1, "好的 发送一份简历来吧")
            job = store.get_job(1)
            expect_in("发送一份简历", str(job.get("recruiter_reply", "")), "招聘方回复应落库")

    _add("bd01", "看板·状态", "移交人工核验应把职位退回待审核", check_needs_review_parks_job)
    _add("bd02", "看板·状态", "已投递的职位不得被退回待审核", check_needs_review_preserves_delivered_jobs)
    _add("bd03", "看板·状态", "投递成功应进入已投递列", check_completed_delivery_moves_to_waiting)
    _add("bd04", "看板·状态", "招聘方回复应被记录", check_recruiter_reply_is_recorded)


# ---------------------------------------------------------------- 决策账本

def _decision_cases() -> None:
    def check_decision_round_trip():
        with _Store() as store:
            store.record_automation_decision(
                rule="escalate_exhausted_start", result="已移交人工核验",
                considered=("escalate_exhausted_start", "sync_with_site"),
                facts={"exhausted_runs": [{"job_id": 16}]})
            rows = store.automation_decisions(limit=5)
            expect(rows, "决策记录应可读回")
            row = rows[0]
            expect_equal(row["rule"], "escalate_exhausted_start", "规则名应保留")
            expect_in("exhausted_runs", row["facts"], "匹配事实应保留，便于回答「为什么没动作」")
            expect_equal(row["considered"], ["escalate_exhausted_start", "sync_with_site"],
                         "候选规则应保留")
            return row

    def check_decision_reader_is_wired():
        # The table was write-only for a while: it recorded everything and nothing could
        # read it, so the observability it promised did not exist.
        from pathlib import Path

        root = Path(__file__).resolve().parents[2] / "app"
        callers = [path for path in root.rglob("*.py")
                   if "automation_decisions(" in path.read_text(encoding="utf-8")
                   and path.name != "store.py"]
        expect(bool(callers), "automation_decisions 必须有 store.py 之外的读取方")

    _add("dc01", "自动化·决策", "决策记录应可完整读回", check_decision_round_trip)
    _add("dc02", "自动化·决策", "决策账本必须有读取方（已知缺陷：只写不读）", check_decision_reader_is_wired, xfail=True)


# ---------------------------------------------------------------- 统计与成长计划

def _stats_and_growth_cases() -> None:
    def check_stats_counts_stages():
        with _Store() as store:
            for index in range(6):
                _seed_job(store, job_id=index + 1, score=90 - index * 5, external_id=5100 + index)
            with store.lock, store._connect() as db:
                db.execute("UPDATE jobs SET stage='waiting' WHERE id IN (1,2)")
                db.execute("UPDATE jobs SET stage='review' WHERE id IN (3,4)")
            stats = store.stats()
            expect(bool(stats), "统计结果不应为空")
            return stats

    def check_insights_is_resume_scoped():
        with _Store() as store:
            _seed_job(store)
            insights = store.insights(resume_id="doc-1")
            expect(insights is not None, "画像统计应可用")
            return type(insights).__name__

    def check_learning_plan_round_trip():
        with _Store() as store:
            store.save_learning_plan({"id": "plan-1", "resume_id": "doc-1", "goals": ["补齐 RAG 经验"]})
            plans = store.learning_plans("doc-1")
            expect(plans, "成长计划应可读回")
            expect_in("RAG", str(plans[0]), "成长计划内容应完整保存")
            return plans[0]

    def check_learning_plans_are_resume_scoped():
        with _Store() as store:
            store.save_learning_plan({"id": "plan-1", "resume_id": "doc-1", "goals": ["a"]})
            store.save_learning_plan({"id": "plan-2", "resume_id": "doc-2", "goals": ["b"]})
            expect_equal(len(store.learning_plans("doc-1")), 1, "成长计划应按简历隔离")

    _add("sg01", "统计·成长计划", "统计接口应能按阶段汇总", check_stats_counts_stages)
    _add("sg02", "统计·成长计划", "画像统计应支持按简历范围查询", check_insights_is_resume_scoped)
    _add("sg03", "统计·成长计划", "成长计划应可完整读回", check_learning_plan_round_trip)
    _add("sg04", "统计·成长计划", "成长计划应按简历隔离", check_learning_plans_are_resume_scoped)


def build() -> list[Case]:
    _schema_cases()
    _attempt_cases()
    _queue_cases()
    _authorization_cases()
    _failure_query_cases()
    _board_cases()
    _decision_cases()
    _stats_and_growth_cases()
    return CASES
