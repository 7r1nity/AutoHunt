from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from cryptography.fernet import Fernet

from app.core.models import CandidateProfileView
from app.jobs.store import JobStore
from app.memory.candidate_state import CandidateStateStore
from app.workflow.mock_coordinator import MockWorkflowCoordinator


def _rig(tmp_path: Path):
    jobs = JobStore(tmp_path / "jobs.db")
    candidate = CandidateStateStore(tmp_path, Fernet.generate_key().decode())
    candidate.apply_resume_profile(CandidateProfileView(
        resume_id="a" * 32, role_suggestions=["风险策略分析师", "金融分析师"],
        skills=["Python", "SQL"], summary="金融风控",
    ), "风险策略分析师")
    jobs.bind_mock_applicant(31)
    jobs.update_mock_workflow_settings(
        monitor_enabled=True, auto_submit=False, daily_enabled=False,
        daily_time="09:00", min_score=75, max_contacts=3,
    )
    browser = SimpleNamespace(worker=None)
    delivery = SimpleNamespace(worker=None)
    coordinator = MockWorkflowCoordinator(jobs, browser, delivery, candidate, SimpleNamespace())
    return jobs, coordinator


def test_distinct_mock_postings_do_not_collapse_by_title_company_city(tmp_path: Path) -> None:
    jobs, _ = _rig(tmp_path)
    base = {
        "source": "MockJobSite", "title": "金融分析师", "company": "测试公司",
        "location": "深圳", "description": "金融业务分析与风险策略制定。",
        "score": 70, "decision": "REVIEW", "reasoning": "测试", "stage": "review",
    }
    first, inserted = jobs.upsert_job({**base, "external_id": "101", "source_url": "http://127.0.0.1:8080/job.html?id=101"})
    second, inserted_again = jobs.upsert_job({**base, "external_id": "102", "source_url": "http://127.0.0.1:8080/job.html?id=102"})
    again, duplicate = jobs.upsert_job({**base, "external_id": "101", "source_url": "http://127.0.0.1:8080/job.html?id=101"})
    assert inserted and inserted_again and not duplicate
    assert first != second and again == first


def test_manual_contact_and_hr_reply_sync_to_board(tmp_path: Path) -> None:
    jobs, coordinator = _rig(tmp_path)
    messages = [{"sender_id": 31, "sender_role": "applicant", "created_at": "2026-09-27T08:00:00", "content": "您好，我想了解职位"}]
    remote = {
        "title": "信用风险分析师", "company_name": "测试银行", "city": "深圳",
        "salary_min": 20, "salary_max": 30, "description": "负责信用风险分析和风险策略制定。",
        "requirements": ["Python", "SQL"], "skills": ["Python", "SQL"],
    }

    async def mock_get(_client, path, _headers, params=None):
        if path == "/api/messages/conversations":
            return [{"job_id": 123, "user_id": 8}]
        if path == "/api/applications/mine":
            return []
        if path == "/api/messages/peek":
            return messages
        if path == "/api/jobs/123":
            return remote
        raise AssertionError(path)

    coordinator._get = mock_get
    first = asyncio.run(coordinator.sync_once())
    job_id = jobs.job_id_for_source_url("http://127.0.0.1:8080/job.html?id=123")
    assert first["imported"] == first["awaiting_reply"] == 1
    assert job_id is not None and jobs.get_job(job_id)["stage"] == "applying"
    assert jobs.get_job(job_id)["mode"] == "人工沟通·等待回复"

    messages.append({"sender_id": 8, "sender_role": "employer", "created_at": "2026-09-27T08:01:00", "content": "请发送简历"})
    second = asyncio.run(coordinator.sync_once())
    assert second["replied"] == 1
    assert jobs.get_job(job_id)["recruiter_reply"] == "请发送简历"
    assert jobs.get_job(job_id)["stage"] == "applying"


def test_daily_search_runs_once_without_fake_hr_reply(tmp_path: Path) -> None:
    jobs, coordinator = _rig(tmp_path)
    jobs.update_mock_workflow_settings(
        monitor_enabled=True, auto_submit=False, daily_enabled=True,
        daily_time="00:00", min_score=70, max_contacts=2,
    )
    captured = []

    async def start(request):
        captured.append(request)
        return SimpleNamespace(id="daily-run-1")

    coordinator.browser.start = start
    # Enabling a schedule after today's time must not backfill immediately.
    assert asyncio.run(coordinator.run_daily_if_due()) is False
    with jobs.lock, jobs._connect() as db:
        db.execute("UPDATE mock_workflow_settings SET last_run_date='2000-01-01' WHERE id=1")
    assert asyncio.run(coordinator.run_daily_if_due()) is True
    assert asyncio.run(coordinator.run_daily_if_due()) is False
    assert captured[0].source == "mock"
    assert captured[0].queries == ["风险策略分析师", "金融分析师"]
    assert captured[0].dry_run is True
    assert captured[0].dry_run_simulate_reply is False
    assert jobs.mock_workflow_settings()["last_run_id"] == "daily-run-1"


def test_old_hr_reply_is_not_auto_submitted_when_opt_in_is_new(tmp_path: Path) -> None:
    jobs, coordinator = _rig(tmp_path)
    jobs.update_mock_workflow_settings(
        monitor_enabled=True, auto_submit=True, daily_enabled=False,
        daily_time="09:00", min_score=75, max_contacts=3,
    )
    replies = [
        {"sender_id": 31, "sender_role": "applicant", "created_at": "2020-01-01T08:00:00", "content": "您好"},
        {"sender_id": 8, "sender_role": "employer", "created_at": "2020-01-01T08:01:00", "content": "请发送简历"},
    ]

    async def mock_get(_client, path, _headers, params=None):
        if path == "/api/messages/conversations":
            return [{"job_id": 123, "user_id": 8}]
        if path == "/api/applications/mine":
            return []
        if path == "/api/messages/peek":
            return replies
        if path == "/api/jobs/123":
            return {"title": "金融分析师", "company_name": "测试公司", "city": "深圳", "description": "负责金融业务分析。"}
        raise AssertionError(path)

    async def must_not_submit(_job_id):
        raise AssertionError("Historical HR replies must not trigger a new submission")

    coordinator._get = mock_get
    coordinator._start_authorized_delivery = must_not_submit
    result = asyncio.run(coordinator.sync_once())
    assert result["replied"] == 1
    assert result["delivery_started"] == 0
