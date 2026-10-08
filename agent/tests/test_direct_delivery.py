from __future__ import annotations

from pathlib import Path

from app.core.models import BrowserMode, DeliveryTaskStatus
from app.delivery.automation import DeliveryAutomationService
from app.delivery.mock_site_adapter import MockApplyObservation, MockJobSiteAdapter, MockSubmitResult
from app.jobs.store import JobStore


class FakePage:
    url = "http://127.0.0.1:8080/job.html?id=77&flow=direct"


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


class DirectReplyAdapter(MockJobSiteAdapter):
    def __init__(self):
        self.submitted = False
        self.checks_after_submit = 0

    async def prepare(self, _page, _url):
        if not self.submitted:
            return MockApplyObservation(True, None, True, False, "立即投递", direct_apply=True)
        self.checks_after_submit += 1
        if self.checks_after_submit == 1:
            return MockApplyObservation(
                True, None, False, True, "已投递", application_submitted=True, direct_apply=True
            )
        return MockApplyObservation(
            True,
            None,
            False,
            False,
            "已投递",
            "您好，请补充项目经历。",
            application_submitted=True,
            direct_apply=True,
        )

    async def submit(self, _page):
        self.submitted = True
        return MockSubmitResult(True, False, "已投递", await_reply=True)


def add_direct_job(store: JobStore) -> int:
    job_id, _ = store.upsert_job({
        "source": "MockJobSite",
        "source_url": "http://127.0.0.1:8080/job.html?id=77&flow=direct",
        "external_id": "77-direct",
        "title": "AI 算法工程师",
        "company": "模拟科技",
        "location": "深圳",
        "salary": "20-30K",
        "description": "负责 AI 算法开发。",
        "tags": ["Python", "LLM"],
        "score": 80,
        "decision": "REVIEW",
        "reasoning": "直接投递流程测试",
        "stage": "review",
    })
    return job_id


def test_direct_flow_url_is_preserved() -> None:
    adapter = MockJobSiteAdapter()
    assert adapter.normalize_job_url(
        "http://127.0.0.1:8080/job.html?flow=direct&id=77"
    ) == "http://127.0.0.1:8080/job.html?id=77&flow=direct"


async def test_direct_apply_waits_for_and_records_employer_reply(tmp_path: Path, monkeypatch) -> None:
    store = JobStore(tmp_path / "jobs.db")
    job_id = add_direct_job(store)
    preview = store.create_delivery_preview(job_id)
    monkeypatch.setattr("app.delivery.automation.BrowserSession", FakeSession)
    service = DeliveryAutomationService(
        store,
        tmp_path,
        headless=False,
        cdp_url="http://127.0.0.1:9223",
        reply_poll_seconds=0.01,
    )
    service.adapter = DirectReplyAdapter()

    await service.authorize_and_start(preview.id)
    await service.worker

    task = service.current()
    stored = store.get_job(job_id)
    assert task.status == DeliveryTaskStatus.COMPLETED
    assert task.submitted is True
    assert task.recruiter_reply == "您好，请补充项目经历。"
    assert stored["recruiter_reply"] == "您好，请补充项目经历。"
    assert stored["stage"] == "waiting"