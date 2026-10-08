import pytest

from app.browser.automation import BrowserAutomationService
from app.core.models import BrowserTaskStatus, DeliveryTaskStatus
from app.jobs.store import JobStore


def sample_job(url: str = "https://www.zhipin.com/job_detail/abc.html") -> dict:
    return {
        "source": "BOSS直聘",
        "source_url": url,
        "external_id": "abc",
        "title": "Python 开发工程师",
        "company": "示例科技",
        "location": "上海",
        "salary": "20-30K",
        "description": "负责 Python FastAPI 服务开发、测试和持续交付。",
        "tags": ["Python", "FastAPI"],
        "score": 88,
        "decision": "APPLY",
        "reasoning": "技能匹配",
    }


def test_job_store_deduplicates_and_tracks_stage(tmp_path) -> None:
    store = JobStore(tmp_path / "autohunt.db")
    job_id, inserted = store.upsert_job(sample_job())
    duplicate_id, duplicate_inserted = store.upsert_job(sample_job("https://www.zhipin.com/job_detail/def.html"))

    assert inserted is True
    assert duplicate_inserted is False
    assert duplicate_id == job_id
    assert len(store.list_jobs()) == 1

    store.update_stage(job_id, "applying")
    assert store.stats().applying == 1


def test_search_report_is_run_scoped_and_includes_duplicates(tmp_path) -> None:
    store = JobStore(tmp_path / "autohunt.db")
    store.create_task("finance-run", "mock", "风险策略分析师")
    internship = sample_job("http://127.0.0.1:8080/job.html?id=intern")
    internship.update({
        "title": "金融风控实习生", "salary": "200元/天",
        "description": "岗位职责：参与信用风险策略分析，使用 Python 与 SQL。提供五险一金、双休。",
    })
    job_id, _ = store.upsert_job(internship)
    store.event("finance-run", "job_stored", "金融风控实习生", sample={"job_id": job_id})
    internship["salary"] = "250元/天"
    duplicate_id, inserted = store.upsert_job(internship)
    assert not inserted and duplicate_id == job_id
    store.event("finance-run", "job_duplicate", "金融风控实习生", sample={"job_id": job_id})

    graduate = sample_job("http://127.0.0.1:8080/job.html?id=graduate")
    graduate.update({"title": "信用风险分析师（应届）", "company": "另一家机构", "salary": "12-18K", "description": "应届生岗位，负责风控模型评估。提供餐补。"})
    graduate_id, _ = store.upsert_job(graduate)
    store.event("finance-run", "job_stored", "信用风险分析师", sample={"job_id": graduate_id})

    unrelated = sample_job("http://127.0.0.1:8080/job.html?id=old")
    unrelated.update({"title": "AI Agent 工程师", "company": "旧岗位"})
    store.upsert_job(unrelated)
    report = store.search_report("finance-run")

    assert report["covered_jobs"] == 2
    assert report["salary_distribution"] == {"按日计薪": 1, "10-20K": 1}
    assert any(item["salary"] == "250元/天" for item in report["highlights"])
    assert report["employment_distribution"] == {"实习": 1, "应届/校招": 1}
    assert report["benefit_distribution"] == {"五险一金": 1, "双休": 1, "餐补": 1}
    assert "信用风险策略分析" in str(report["highlights"])
    assert "AI Agent" not in str(report)


def test_insights_skill_chart_excludes_benefit_keywords(tmp_path) -> None:
    store = JobStore(tmp_path / "autohunt.db")
    mixed = sample_job()
    mixed["tags"] = ["Python", "五险一金", "交通补助", "LLM", "保底工资"]
    store.upsert_job(mixed)

    insights = store.insights()
    chart_tags = {item["tag"] for item in insights["tags"]}
    assert chart_tags == {"python", "llm"}
    assert insights["cities"] == [{"city": "上海", "count": 1}]
    assert insights["salaries"] == [{"bucket": "20-30K", "count": 1}]
    assert insights["stored_total"] == 1


def test_insights_follow_active_resume_without_deleting_old_searches(tmp_path) -> None:
    store = JobStore(tmp_path / "autohunt.db")
    store.create_task("ai-run", "mock", "AI Agent", resume_id="ai-resume")
    ai = sample_job("http://127.0.0.1:8080/job.html?id=ai")
    ai.update({"title": "AI Agent 工程师", "tags": ["LLM"], "company": "AI 公司"})
    ai_id, _ = store.upsert_job(ai)
    store.event("ai-run", "job_stored", "AI Agent 工程师", sample={"job_id": ai_id})
    store.update_task("ai-run", status=BrowserTaskStatus.COMPLETED, discovered=12, stored=1)

    store.create_task("finance-run", "mock", "风险策略分析师", resume_id="finance-resume")
    finance = sample_job("http://127.0.0.1:8080/job.html?id=finance")
    finance.update({"title": "信用风险分析师", "tags": ["Python"], "company": "银行"})
    finance_id, _ = store.upsert_job(finance)
    store.event("finance-run", "job_stored", "信用风险分析师", sample={"job_id": finance_id})
    store.update_task("finance-run", status=BrowserTaskStatus.COMPLETED, discovered=9, stored=1)

    current = store.insights(current_only=True, resume_id="finance-resume")
    assert current["scope"] == "active_resume"
    assert current["tasks_total"] == 1
    assert current["discovered_total"] == 9
    assert current["stored_total"] == 1
    assert current["tags"] == [{"tag": "python", "count": 1}]
    assert store.insights()["stored_total"] == 2


def test_insights_legacy_fallback_uses_only_latest_run_after_resume_load(tmp_path) -> None:
    store = JobStore(tmp_path / "autohunt.db")
    store.create_task("old-run", "mock", "AI Agent")
    ai_id, _ = store.upsert_job(sample_job())
    store.event("old-run", "job_stored", "旧 AI 职位", sample={"job_id": ai_id})
    store.update_task("old-run", status=BrowserTaskStatus.COMPLETED, discovered=12)
    with store.lock, store._connect() as db:
        cutoff = db.execute("SELECT started_at FROM browser_tasks WHERE id='old-run'").fetchone()[0]
        db.execute("UPDATE browser_tasks SET started_at='2026-01-01T00:00:00+00:00' WHERE id='old-run'")
    store.create_task("new-run", "mock", "风险策略分析师")
    finance = sample_job("http://127.0.0.1:8080/job.html?id=finance")
    finance.update({"title": "金融风控分析师", "company": "银行", "tags": ["Python"]})
    finance_id, _ = store.upsert_job(finance)
    store.event("new-run", "job_stored", "金融风控分析师", sample={"job_id": finance_id})
    store.update_task("new-run", status=BrowserTaskStatus.COMPLETED, discovered=3)

    current = store.insights(current_only=True, resume_id="new-resume", legacy_since=cutoff)
    assert current["scope"] == "latest_legacy_run"
    assert current["tasks_total"] == 1
    assert current["discovered_total"] == 3
    assert current["stored_total"] == 1
    assert current["tags"] == [{"tag": "python", "count": 1}]
    assert store.insights(current_only=True, resume_id="new-resume", legacy_since="2099-01-01")["stored_total"] == 0


def test_boss_url_allowlist() -> None:
    assert BrowserAutomationService._safe_boss_url("/job_detail/abc.html") == "https://www.zhipin.com/job_detail/abc.html"
    assert BrowserAutomationService._safe_boss_url("https://evil.example/job_detail/abc") is None


def test_delivery_preview_authorization_is_bound_and_one_time(tmp_path) -> None:
    store = JobStore(tmp_path / "autohunt.db")
    job_id, _ = store.upsert_job(sample_job())
    preview = store.create_delivery_preview(job_id)

    authorization_id, token, authorized_job_id, _ = store.authorize_delivery(preview.id)

    assert authorized_job_id == job_id
    assert store.consume_delivery_authorization(authorization_id, token) == job_id
    try:
        store.consume_delivery_authorization(authorization_id, token)
    except ValueError as exc:
        assert "已使用" in str(exc)
    else:
        raise AssertionError("authorization token must be one-time")


def test_indeterminate_outcome_is_visible_in_stats(tmp_path) -> None:
    store = JobStore(tmp_path / "autohunt.db")
    job_id, _ = store.upsert_job(sample_job())
    store.mark_outcome_indeterminate(job_id, "点击提交后页面超时，结果无法确认")

    assert store.stats().indeterminate == 1
    assert store.list_jobs()[0].stage == "outcome_indeterminate"


def test_skip_job_is_not_put_on_review_board(tmp_path) -> None:
    store = JobStore(tmp_path / "autohunt.db")
    skipped = sample_job()
    skipped.update({"decision": "SKIP", "score": 5, "stage": "rejected"})

    job_id, inserted = store.upsert_job(skipped)

    assert inserted is True
    assert store.list_jobs()[0].id == job_id
    assert store.list_jobs()[0].stage == "rejected"
    assert store.list_jobs(stage="review") == []


def test_search_funnel_is_run_scoped_explainable_and_redacted(tmp_path) -> None:
    store = JobStore(tmp_path / "autohunt.db")
    store.create_task("run-1", "mock", "AI Agent", adapter_version="mock-v1")
    store.increment_funnel(
        "run-1", links_seen=127, cards_parsed=120, unique_jobs=100,
        limited=70, visited=18, filtered=12, funnel_errors=2,
    )
    store.update_task("run-1", stored=9, duplicates=3)
    store.event(
        "run-1", "job_skipped", "测试职位 person@example.com",
        reason_code="prescreen_skip", sample={"title": "Java 外包", "email": "person@example.com"},
    )

    task = store.get_task("run-1")

    assert task.adapter_version == "mock-v1"
    assert task.funnel.links_seen == 127
    assert task.funnel.unique_jobs == 100
    assert task.funnel.stored == 9
    assert task.funnel.duplicates == 3
    assert task.funnel.reason_counts == {"prescreen_skip": 1}
    assert "example.com" not in str(task.funnel.samples)


def test_startup_reconcile_stops_search_and_preserves_partial_funnel(tmp_path) -> None:
    store = JobStore(tmp_path / "autohunt.db")
    store.create_task("run-restart", "mock", "算法工程师")
    store.increment_funnel("run-restart", links_seen=20, cards_parsed=18, visited=4)
    store.update_task("run-restart", status=BrowserTaskStatus.WAITING_FOR_USER, stored=2)

    result = store.reconcile_incomplete_runs()
    task = store.get_task("run-restart")

    assert result["search_runs"] == 1
    assert task.status == BrowserTaskStatus.STOPPED
    assert task.checkpoint == "backend_restart_reconciled"
    assert task.funnel.links_seen == 20
    assert task.funnel.cancelled == 1
    assert task.funnel.reason_counts["backend_restart"] == 1


def _authorized_run(store: JobStore, suffix: str = "") -> tuple[int, str]:
    job_id, _ = store.upsert_job(sample_job(f"http://127.0.0.1:8080/job.html?id=91{suffix}"))
    preview = store.create_delivery_preview(job_id)
    _authorization_id, authorized_job_id, _expires, run = store.authorize_delivery_run(
        preview.id, f"delivery-run{suffix or '-1'}"
    )
    assert authorized_job_id == job_id
    return job_id, str(run.id)


def test_delivery_authorization_and_run_creation_are_atomic_and_one_time(tmp_path) -> None:
    store = JobStore(tmp_path / "autohunt.db")
    job_id, _ = store.upsert_job(sample_job("http://127.0.0.1:8080/job.html?id=92"))
    preview = store.create_delivery_preview(job_id)
    authorization_id, authorized_job_id, _expires, run = store.authorize_delivery_run(preview.id, "delivery-atomic")

    assert authorization_id.startswith("dau_")
    assert authorized_job_id == job_id
    assert run.preview_id == preview.id
    assert run.checkpoint == "authorized"
    with pytest.raises(ValueError, match="已经授权|不可用"):
        store.authorize_delivery_run(preview.id, "delivery-race-loser")


def test_delivery_run_is_bound_to_exact_resume_snapshot_job_and_action(tmp_path) -> None:
    store = JobStore(tmp_path / "autohunt.db")
    job_id, _ = store.upsert_job(sample_job("http://127.0.0.1:8080/job.html?id=bound"))
    preview = store.create_delivery_preview(
        job_id,
        resume_document_id="master",
        resume_version=7,
        resume_digest="a" * 64,
    )

    _authorization_id, _job_id, _expires, run = store.authorize_delivery_run(
        preview.id,
        "delivery-bound",
        resume_document_id="master",
        resume_version=7,
        resume_digest="a" * 64,
        action="submit_resume",
    )

    assert run.job_id == job_id
    assert run.target_url == "http://127.0.0.1:8080/job.html?id=bound"
    assert run.resume_document_id == "master"
    assert run.resume_version == 7
    assert run.resume_digest == "a" * 64
    assert run.action == "submit_resume"


def test_delivery_authorization_rejects_changed_job_url(tmp_path) -> None:
    store = JobStore(tmp_path / "changed-url.db")
    job_id, _ = store.upsert_job(sample_job("http://127.0.0.1:8080/job.html?id=before"))
    preview = store.create_delivery_preview(
        job_id, resume_document_id="master", resume_version=1, resume_digest="d" * 64,
    )
    with store.lock, store._connect() as db:
        db.execute("UPDATE jobs SET source_url=? WHERE id=?", ("http://127.0.0.1:8080/job.html?id=after", job_id))

    with pytest.raises(ValueError, match="职位链接已变化"):
        store.authorize_delivery_run(
            preview.id,
            "delivery-changed-url",
            resume_document_id="master",
            resume_version=1,
            resume_digest="d" * 64,
        )

    assert store.get_job(job_id)["stage"] == "review"


@pytest.mark.parametrize(
    ("override", "value"),
    [
        ("resume_document_id", "another-resume"),
        ("resume_version", 8),
        ("resume_digest", "b" * 64),
        ("action", "send_message"),
    ],
)
def test_delivery_authorization_rejects_changed_binding_without_consuming_preview(
    tmp_path, override: str, value,
) -> None:
    store = JobStore(tmp_path / f"{override}.db")
    job_id, _ = store.upsert_job(sample_job(f"http://127.0.0.1:8080/job.html?id={override}"))
    preview = store.create_delivery_preview(
        job_id,
        resume_document_id="master",
        resume_version=3,
        resume_digest="c" * 64,
    )
    expected = {
        "resume_document_id": "master",
        "resume_version": 3,
        "resume_digest": "c" * 64,
        "action": "submit_resume",
    }
    expected[override] = value

    with pytest.raises(ValueError, match="已变化"):
        store.authorize_delivery_run(preview.id, f"delivery-mismatch-{override}", **expected)

    assert store.get_job(job_id)["stage"] == "review"
    _authorization_id, _job_id, _expires, run = store.authorize_delivery_run(
        preview.id,
        f"delivery-after-mismatch-{override}",
        resume_document_id="master",
        resume_version=3,
        resume_digest="c" * 64,
    )
    assert run.status == DeliveryTaskStatus.STARTING


def test_restart_before_submit_fails_without_reporting_success(tmp_path) -> None:
    store = JobStore(tmp_path / "autohunt.db")
    job_id, run_id = _authorized_run(store)

    store.reconcile_incomplete_runs()

    run = store.get_delivery_run(run_id)
    assert run.status == DeliveryTaskStatus.FAILED
    assert run.submitted is False
    assert store.get_job(job_id)["stage"] == "review"
    assert store.get_job(job_id)["mode"] == "需要重新授权"


def test_restart_during_submit_is_indeterminate_and_never_retried(tmp_path) -> None:
    store = JobStore(tmp_path / "autohunt.db")
    job_id, run_id = _authorized_run(store)
    store.update_delivery_run(run_id, status=DeliveryTaskStatus.RUNNING, checkpoint="submit_started")

    store.reconcile_incomplete_runs()

    run = store.get_delivery_run(run_id)
    assert run.status == DeliveryTaskStatus.OUTCOME_INDETERMINATE
    assert store.get_job(job_id)["stage"] == "outcome_indeterminate"


@pytest.mark.parametrize("status", [DeliveryTaskStatus.WAITING_FOR_USER, DeliveryTaskStatus.WAITING_FOR_REPLY])
def test_restart_preserves_explicitly_resumable_delivery_states(tmp_path, status) -> None:
    store = JobStore(tmp_path / "autohunt.db")
    _job_id, run_id = _authorized_run(store)
    store.update_delivery_run(run_id, status=status, checkpoint=status.value)

    store.reconcile_incomplete_runs()

    run = store.get_delivery_run(run_id)
    assert run.status == status
    assert "重启" in run.detail


def test_confirmed_submission_survives_restart_without_downgrade(tmp_path) -> None:
    store = JobStore(tmp_path / "autohunt.db")
    job_id, run_id = _authorized_run(store)
    store.update_delivery_run(
        run_id, status=DeliveryTaskStatus.RUNNING, checkpoint="submit_started", submitted=True,
    )
    store.mark_delivery_completed(job_id, "模拟网站已确认简历发送")

    store.reconcile_incomplete_runs()

    run = store.get_delivery_run(run_id)
    assert run.status == DeliveryTaskStatus.WAITING_FOR_REPLY
    assert run.submitted is True
    assert store.get_job(job_id)["stage"] == "waiting"


def test_conversation_history_is_persistent_ordered_and_redacted(tmp_path) -> None:
    path = tmp_path / "autohunt.db"
    store = JobStore(path)
    store.add_conversation_message("user", "联系我 person@example.com 或 13000000000")
    store.add_conversation_message("assistant", "已记录", response={"message": "token=123456789secret"})

    restored = JobStore(path).conversation_history(limit=8)

    assert [item["role"] for item in restored] == ["user", "assistant"]
    assert "example.com" not in restored[0]["content"]
    assert "13000000000" not in restored[0]["content"]
    assert "123456789secret" not in str(restored[1])
