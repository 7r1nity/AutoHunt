"""Production queue/executor/SQLite + real mock HTML; all site APIs intercepted.

Synthetic resume export is injected; document-version binding is covered by the
store contracts. No real profile, external model, CDP or live site data is used.
"""
from __future__ import annotations

import asyncio
import gc
import json
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import async_playwright
from app.delivery.automation import DeliveryAutomationService
from app.jobs.store import JobStore
from app.workflow.mock_coordinator import MockWorkflowCoordinator
from evaluation.search_delivery_contract import job


async def run():
    output = Path(__file__).parent / "results" / "gated-delivery-browser" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True, exist_ok=True)
    report = {"passed": False}
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context(service_workers="block")
            await context.add_init_script("localStorage.setItem('userId','987');localStorage.setItem('userRole','applicant')")
            root = Path(__file__).resolve().parents[3] / "mock-site" / "frontend"
            applications, resumes, messages, writes = [], [], {}, []
            payload = {"name": "合成候选人", "education": "本科", "skills": ["合成技能"], "experience": "合成项目"}
            async def intercept(route):
                target = urlsplit(route.request.url)
                if target.netloc != "127.0.0.1:8080":
                    await route.abort(); return
                assets = {"/job.html": ("job.html", "text/html"), "/static/js/api.js": ("js/api.js", "application/javascript"),
                          "/static/css/style.css": ("css/style.css", "text/css")}
                if target.path in assets:
                    file, mime = assets[target.path]
                    await route.fulfill(content_type=mime + "; charset=utf-8", body=(root / file).read_text(encoding="utf-8")); return
                data = []
                if target.path.startswith("/api/jobs/"):
                    ident = int(target.path.rsplit("/", 1)[1])
                    data = {"id": ident, "title": f"合成岗位{ident}", "company_name": "合成雇主", "city": "深圳", "category": "数据分析",
                            "department": "合成部门", "employer_id": 986, "salary_min": 15, "salary_max": 20,
                            "experience_required": "不限", "education": "本科", "headcount": 1, "description": "合成职责", "skills": [], "requirements": []}
                elif target.path == "/api/resumes/mine":
                    data = resumes
                elif target.path == "/api/resumes" and route.request.method == "POST":
                    assert route.request.post_data_json == payload
                    data = {"id": 991, "applicant_id": 987, **payload}
                    resumes.append(data)
                elif target.path == "/api/resumes/991":
                    data = resumes[0]
                elif target.path == "/api/applications/mine":
                    data = applications
                elif target.path == "/api/applications" and route.request.method == "POST":
                    body = route.request.post_data_json
                    assert body["resume_id"] == 991
                    assert any(m["sender_role"] == "employer" for m in messages[body["job_id"]])
                    data = {"id": len(applications)+1, **body, "status": "submitted"}
                    applications.append(data)
                elif target.path == "/api/messages":
                    if route.request.method == "POST":
                        body = route.request.post_data_json
                        data = {"id": len(writes)+1, **body, "sender_id": 987, "sender_role": "applicant",
                                "created_at": "2026-01-01T00:00:00Z"}
                        messages.setdefault(body["job_id"], []).append(data)
                    else:
                        ident = int(parse_qs(target.query)["job_id"][0])
                        data = messages.get(ident, [])
                else:
                    await route.abort(); return
                if route.request.method == "POST":
                    writes.append({"path": target.path, "body": route.request.post_data_json})
                await route.fulfill(content_type="application/json", body=json.dumps(data, ensure_ascii=False))
            await context.route("**/*", intercept)
            page = await context.new_page()
            await page.goto("http://127.0.0.1:8080/job.html?id=999999")
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
                jobs = JobStore(Path(directory) / "jobs.db")
                service = DeliveryAutomationService(jobs, Path(directory), headless=True, cdp_url="synthetic-only")
                service.session = NS(attach_existing_cdp=AsyncMock(return_value="cdp"), page_for_url=lambda _: page,
                    ensure_cdp_browser=AsyncMock(side_effect=AssertionError("must not launch")),
                    ensure_cdp_window=AsyncMock(side_effect=AssertionError("must not activate")), close=AsyncMock())
                def export_synthetic():
                    jobs.assert_delivery_facts(service.state.job_id, service.state.preview_id)
                    return payload.copy()
                service._authorized_resume_payload = export_synthetic
                coordinator = MockWorkflowCoordinator(jobs, NS(worker=None), service, NS(), NS(read_document=lambda: NS()))
                ids = [jobs.upsert_job(job(999999+i))[0] for i in range(3)]
                binding = {"resume_document_id": "synthetic", "resume_version": 1, "resume_digest": "synthetic-digest"}
                jobs.enqueue_mock_deliveries([jobs.get_job(i) for i in ids], session_id="default", resume_stamp="synthetic",
                    binding=binding, min_score=0, scope_evidence={i: {"delivery_flow": "direct"} for i in ids})
                started = time.monotonic()
                with patch("app.growth.requests.resume_stamp", return_value="synthetic"):
                    for _ in ids:
                        await coordinator.process_delivery_queue()
                        await service.worker
                    runs = jobs.all_delivery_runs()
                    assert len(runs) == 3 and all(r["status"] == "waiting_for_reply" and not r["submitted"] for r in runs), runs
                    assert all(jobs.get_job(i)["stage"] == "applying" for i in ids)
                    assert len(writes) == 3 and all(w["path"] == "/api/messages" for w in writes), writes
                    initial_elapsed = round(time.monotonic()-started, 3)
                    # All three greetings finished BEFORE any HR reply; one waiting
                    # job cannot occupy the browser slot or prevent the next greeting.
                    for ident in reversed(ids):
                        external = int(jobs.get_job(ident)["external_id"])
                        messages[external].append({"id": 2000+ident, "sender_id": 986, "sender_role": "employer",
                            "content": "请发送简历", "created_at": "2026-01-01T00:01:00Z"})
                        jobs.mark_recruiter_reply(ident, "请发送简历")
                        await page.evaluate("loadChat(false)")
                        outcome = await coordinator.check_reply_queue()
                        assert outcome["submitted"] == 1, outcome
                        assert jobs.get_job(ident)["stage"] == "waiting"
                    await coordinator.process_delivery_queue()
                    assert all(i["status"] == "completed" for i in jobs.mock_delivery_queue(active_only=False))
                    await coordinator.check_reply_queue()
                assert len([w for w in writes if w["path"] == "/api/messages"]) == 3
                assert len([w for w in writes if w["path"] == "/api/applications"]) == 3
                report = {"passed": True, "greetings_before_any_reply": 3, "initial_elapsed_seconds": initial_elapsed,
                          "submissions_after_reply": 3, "stages": [jobs.get_job(i)["stage"] for i in ids], "writes": writes}
                await service.shutdown()
                gc.collect()
            await context.close()
            await browser.close()
    except Exception:
        report["error"] = traceback.format_exc()
    (output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "passed": report["passed"]}))
    return int(not report["passed"])


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
