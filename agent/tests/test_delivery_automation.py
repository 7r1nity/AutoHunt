from __future__ import annotations

import asyncio
from pathlib import Path

from app.core.models import BrowserMode, DeliveryTaskStatus
from app.delivery.automation import DeliveryAutomationService
from app.delivery.mock_site_adapter import MockApplyObservation, MockJobSiteAdapter, MockSubmitResult
from app.jobs.store import JobStore


def add_mock_job(store: JobStore) -> int:
    job_id, _ = store.upsert_job({
        "source": "MockJobSite",
        "source_url": "http://127.0.0.1:8080/job.html?id=1",
        "external_id": "1",
        "title": "AI Agent 工程师",
        "company": "模拟科技",
        "location": "深圳",
        "salary": "20-30K",
        "description": "负责 AI Agent 应用开发和部署。",
        "tags": ["Python", "LLM"],
        "score": 88,
        "decision": "APPLY",
        "reasoning": "方向匹配",
        "stage": "review",
    })
    return job_id


async def test_stop_during_submission_preserves_uncertain_outcome(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job_id = add_mock_job(store)
    service = DeliveryAutomationService(store, tmp_path, headless=False, cdp_url="http://127.0.0.1:9223")
    service.state = store.create_delivery_run("cancel-in-submit", job_id)
    service._update(DeliveryTaskStatus.RUNNING, "正在发送", checkpoint="submit_started")
    service.worker = asyncio.create_task(asyncio.Event().wait())
    state = await service.stop()
    assert state.status == DeliveryTaskStatus.OUTCOME_INDETERMINATE
    assert store.get_job(job_id)["stage"] == "outcome_indeterminate"
    assert service.worker.cancelled()


async def test_stop_reply_monitor_keeps_confirmed_submission(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job_id = add_mock_job(store)
    store.mark_delivery_completed(job_id, "已发送")
    service = DeliveryAutomationService(store, tmp_path, headless=False, cdp_url="http://127.0.0.1:9223")
    service.state = store.create_delivery_run("cancel-monitor", job_id)
    service._update(DeliveryTaskStatus.WAITING_FOR_REPLY, "等待回复", checkpoint="submission_confirmed", submitted=True)
    state = await service.stop()
    assert state.status == DeliveryTaskStatus.CANCELLED
    assert state.submitted is True
    assert store.get_job(job_id)["stage"] == "waiting"
    assert (await service.stop()).status == DeliveryTaskStatus.CANCELLED


class FakePage:
    url = "http://127.0.0.1:8080/job.html?id=1"


class FakeSession:
    def __init__(self, *_args, **_kwargs):
        self.page = FakePage()

    async def reuse(self, _mode):
        return BrowserMode.CDP.value

    async def ensure_cdp_window(self, _url):
        return None

    def page_for_url(self, _url):
        return self.page

    async def close(self):
        return None


class ReadyAdapter(MockJobSiteAdapter):
    async def prepare(self, _page, _url):
        return MockApplyObservation(True, None, True, False, "发送简历")

    async def submit(self, _page):
        return MockSubmitResult(True, False, "简历已发送给招聘方")


class MfaThenReadyAdapter(ReadyAdapter):
    def __init__(self):
        self.calls = 0

    async def prepare(self, _page, _url):
        self.calls += 1
        if self.calls == 1:
            return MockApplyObservation(False, "等待用户完成 MFA", False, False, "")
        return await super().prepare(_page, _url)


def authorize(store: JobStore, job_id: int):
    preview = store.create_delivery_preview(job_id)
    authorization_id, token, authorized_job_id, _ = store.authorize_delivery(preview.id)
    return authorization_id, token, authorized_job_id


def test_mock_adapter_only_accepts_local_job_detail_urls() -> None:
    adapter = MockJobSiteAdapter()
    assert adapter.normalize_job_url("http://127.0.0.1:8080/job.html?id=12") == "http://127.0.0.1:8080/job.html?id=12"
    assert adapter.normalize_job_url("http://localhost:8080/api/jobs/12") == "http://localhost:8080/job.html?id=12"
    assert adapter.normalize_job_url("https://www.zhipin.com/job_detail/abc.html") is None
    assert adapter.normalize_job_url("http://127.0.0.1:9000/job.html?id=12") is None


async def test_authorized_mock_delivery_moves_review_to_waiting(tmp_path: Path, monkeypatch) -> None:
    store = JobStore(tmp_path / "jobs.db")
    job_id = add_mock_job(store)
    authorization_id, token, authorized_job_id = authorize(store, job_id)
    monkeypatch.setattr("app.delivery.automation.BrowserSession", FakeSession)
    service = DeliveryAutomationService(store, tmp_path, headless=False, cdp_url="http://127.0.0.1:9223")
    service.adapter = ReadyAdapter()

    await service.start(authorization_id, token, authorized_job_id)
    await service.worker

    assert service.current().status == DeliveryTaskStatus.COMPLETED
    assert service.current().submitted is True
    assert store.get_job(job_id)["stage"] == "waiting"
    assert store.get_job(job_id)["mode"] == "自动投递"


async def test_mfa_pauses_until_explicit_human_resume(tmp_path: Path, monkeypatch) -> None:
    store = JobStore(tmp_path / "jobs.db")
    job_id = add_mock_job(store)
    authorization_id, token, authorized_job_id = authorize(store, job_id)
    monkeypatch.setattr("app.delivery.automation.BrowserSession", FakeSession)
    service = DeliveryAutomationService(store, tmp_path, headless=False, cdp_url="http://127.0.0.1:9223")
    service.adapter = MfaThenReadyAdapter()

    await service.start(authorization_id, token, authorized_job_id)
    for _ in range(50):
        if service.current().status == DeliveryTaskStatus.WAITING_FOR_USER:
            break
        await asyncio.sleep(0.01)

    assert service.current().status == DeliveryTaskStatus.WAITING_FOR_USER
    assert "MFA" in (service.current().takeover_reason or "")
    await service.resume()
    await service.worker

    assert service.current().status == DeliveryTaskStatus.COMPLETED
    assert store.get_job(job_id)["stage"] == "waiting"


async def test_waiting_for_user_survives_service_restart_and_requires_resume(tmp_path: Path, monkeypatch) -> None:
    store = JobStore(tmp_path / "jobs.db")
    job_id = add_mock_job(store)
    preview = store.create_delivery_preview(job_id)
    _authorization_id, _job_id, _expires_at, persisted = store.authorize_delivery_run(
        preview.id, "delivery_restart_waiting_user"
    )
    store.update_delivery_run(
        persisted.id,
        status=DeliveryTaskStatus.WAITING_FOR_USER,
        checkpoint="waiting_for_user",
        detail="等待用户完成 MFA",
        takeover_reason="需要 MFA",
    )
    store.reconcile_incomplete_runs()

    monkeypatch.setattr("app.delivery.automation.BrowserSession", FakeSession)
    restarted = DeliveryAutomationService(store, tmp_path, headless=False, cdp_url="http://127.0.0.1:9223")
    restarted.adapter = ReadyAdapter()

    assert restarted.current().status == DeliveryTaskStatus.WAITING_FOR_USER
    assert restarted.worker is None
    await restarted.resume()
    assert restarted.worker is not None
    await restarted.worker

    assert restarted.current().status == DeliveryTaskStatus.COMPLETED
    assert restarted.current().checkpoint == "submission_confirmed"
    assert store.get_job(job_id)["stage"] == "waiting"
class WaitingReplyAdapter(ReadyAdapter):
    async def prepare(self, _page, _url):
        return MockApplyObservation(True, None, False, True, "发送简历")


async def test_waiting_for_employer_reply_keeps_job_in_applying(tmp_path: Path, monkeypatch) -> None:
    store = JobStore(tmp_path / "jobs.db")
    job_id = add_mock_job(store)
    preview = store.create_delivery_preview(job_id)
    monkeypatch.setattr("app.delivery.automation.BrowserSession", FakeSession)
    service = DeliveryAutomationService(store, tmp_path, headless=False, cdp_url="http://127.0.0.1:9223", reply_poll_seconds=None)
    service.adapter = WaitingReplyAdapter()

    _authorization_id, authorized_job_id, _expires_at, _task = await service.authorize_and_start(preview.id)
    assert authorized_job_id == job_id
    await service.worker

    assert service.current().status == DeliveryTaskStatus.WAITING_FOR_REPLY
    assert store.get_job(job_id)["stage"] == "applying"
    assert store.get_job(job_id)["mode"] == "人工已授权"


class ReplyThenReadyAdapter(ReadyAdapter):
    def __init__(self):
        self.calls = 0

    async def prepare(self, _page, _url):
        self.calls += 1
        if self.calls == 1:
            return MockApplyObservation(True, None, False, True, "发送简历")
        return MockApplyObservation(True, None, True, False, "发送简历", "您好，可以发送简历了")


async def test_reply_monitor_automatically_records_reply_and_sends_resume(tmp_path: Path, monkeypatch) -> None:
    store = JobStore(tmp_path / "jobs.db")
    job_id = add_mock_job(store)
    preview = store.create_delivery_preview(job_id)
    monkeypatch.setattr("app.delivery.automation.BrowserSession", FakeSession)
    service = DeliveryAutomationService(
        store, tmp_path, headless=False, cdp_url="http://127.0.0.1:9223", reply_poll_seconds=0.01
    )
    service.adapter = ReplyThenReadyAdapter()

    await service.authorize_and_start(preview.id)
    await service.worker

    task = service.current()
    stored = store.get_job(job_id)
    assert task.status == DeliveryTaskStatus.COMPLETED
    assert task.recruiter_reply == "您好，可以发送简历了"
    assert stored["recruiter_reply"] == "您好，可以发送简历了"
    assert stored["recruiter_reply_at"]
    assert stored["stage"] == "waiting"


async def test_unsupported_site_is_rejected_before_authorization(tmp_path: Path) -> None:
    from app.delivery.automation import UnsupportedDeliverySite

    store = JobStore(tmp_path / "jobs.db")
    job_id, _ = store.upsert_job({
        "source": "BOSS",
        "source_url": "https://www.zhipin.com/job_detail/abc.html",
        "external_id": "abc",
        "title": "AI 工程师",
        "company": "示例公司",
        "location": "深圳",
        "salary": "20-30K",
        "description": "负责 AI 应用开发。",
        "tags": ["Python"],
        "score": 80,
        "decision": "APPLY",
        "reasoning": "匹配",
        "stage": "review",
    })
    preview = store.create_delivery_preview(job_id)
    service = DeliveryAutomationService(store, tmp_path, headless=False, cdp_url="http://127.0.0.1:9223")

    try:
        await service.authorize_and_start(preview.id)
    except UnsupportedDeliverySite:
        pass
    else:
        raise AssertionError("BOSS delivery must remain disabled")

    assert store.get_job(job_id)["stage"] == "review"
