"""120-job production delivery benchmark with intercepted mock-site HTML/APIs.

No existing profile, site database, current queue, resume or external model is
accessed. Browser writes go only to this runner's in-memory synthetic backend.
The production queue, one-use previews, browser adapter, reply HTTP poller and
SQLite receipt checks are real. Resume export and browser attachment are the
explicit fixture seams; production waits/timeouts are not shortened.

Assets default to the local mock-site/frontend when present, otherwise the
repository's evaluation/fixtures/job_site/frontend. --site-root selects an
explicit asset directory; the result records its identity and file hashes.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import sqlite3
import tempfile
import time
import traceback
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import httpx
from playwright.async_api import async_playwright

from app.delivery.automation import DeliveryAutomationService
from app.jobs.store import JobStore
from app.workflow.mock_coordinator import MockWorkflowCoordinator
from evaluation.search_delivery_contract import job


def distribution(samples):
    ordered = sorted(samples)
    def percentile(fraction):
        return round(ordered[max(0, math.ceil(len(ordered) * fraction) - 1)], 4) if ordered else None
    return {"count": len(samples), "mean_seconds": round(sum(samples) / len(samples), 4) if samples else None,
            "p50_seconds": percentile(.5), "p95_seconds": percentile(.95),
            "minimum_seconds": round(min(samples), 4) if samples else None,
            "maximum_seconds": round(max(samples), 4) if samples else None}


async def run(count=120, site_root: Path | None = None):
    if not 1 <= count <= 200:
        raise ValueError("Synthetic browser batch must contain 1–200 jobs")
    agent_root = Path(__file__).resolve().parents[1]
    output = agent_root / "evaluation/results/delivery-scale-browser" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    workspace = agent_root.parents[1]
    local_site = workspace / "mock-site/frontend"
    bundled_site = agent_root / "evaluation/fixtures/job_site/frontend"
    mock_root = (site_root if site_root is not None else local_site if local_site.is_dir() else bundled_site).resolve()
    def source_label(path):
        try:
            return str(path.relative_to(workspace))
        except ValueError:
            return str(path)
    source_paths = [agent_root / relative for relative in (
        "app/workflow/mock_coordinator.py", "app/jobs/store.py", "app/delivery/automation.py",
        "app/delivery/mock_site_adapter.py", "app/delivery/resume_binding.py")]
    source_paths += [mock_root / relative for relative in ("job.html", "js/api.js", "css/style.css")]
    source_hashes = {source_label(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in source_paths}
    report = {"passed": False, "count": count, "model_calls": 0, "real_recruitment_actions": 0,
              "source_sha256": source_hashes,
              "asset_source_root": source_label(mock_root),
              "asset_source_kind": ("bundled_isolated_fixture" if mock_root == bundled_site.resolve()
                                    else "local_mock_site" if mock_root == local_site.resolve() else "explicit_site_root"),
              "scope": "Production queue, gated browser HTML flow, read-only reply HTTP polling and local SQLite against intercepted synthetic APIs; not a natural-language planner/search or real recruiting-site benchmark.",
              "fixture_seams": ["synthetic resume export", "fresh headless browser attachment", "in-memory mock-site API", "frozen resume-stamp fixture"]}
    traces, violations, requests, writes, monitor_reads = [], [], [], [], []
    applications, resumes, messages = [], [], {}
    payload = {"name": "合成压力测试候选人", "education": "本科", "skills": ["Python", "SQL"],
               "experience": "完全虚构的浏览器批量投递验证材料"}
    greeting_seconds, submission_seconds = [], []
    started = time.monotonic()
    service = None
    temp = tempfile.TemporaryDirectory(prefix="autohunt-scale-browser-", ignore_cleanup_errors=True)
    database = Path(temp.name) / "jobs.db"
    def save_progress(phase, completed, **extra):
        state = {"phase": phase, "completed": completed, "count": count,
                 "elapsed_seconds": round(time.monotonic() - started, 3), **extra}
        traces.append(state)
        (output / "progress.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        if completed % 10 == 0 or completed == count:
            print(json.dumps(state, ensure_ascii=False), flush=True)

    try:
        assets = {"/job.html": ("job.html", "text/html"), "/static/js/api.js": ("js/api.js", "application/javascript"),
                  "/static/css/style.css": ("css/style.css", "text/css")}
        asset_text = {url: ((mock_root / file).read_text(encoding="utf-8"), mime) for url, (file, mime) in assets.items()}
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context(service_workers="block")
            await context.add_init_script("localStorage.setItem('userId','987');localStorage.setItem('userRole','applicant')")
            async def intercept(route):
                request, target = route.request, urlsplit(route.request.url)
                requests.append({"method": request.method, "path": target.path})
                if target.scheme != "http" or target.netloc != "127.0.0.1:8080":
                    violations.append({"method": request.method, "url": request.url})
                    await route.abort(); return
                if target.path in asset_text:
                    assert request.method == "GET"
                    text, mime = asset_text[target.path]
                    await route.fulfill(content_type=mime + "; charset=utf-8", body=text); return
                body = request.post_data_json if request.method == "POST" else None
                assert request.headers.get("x-user-id") == "987" and request.headers.get("x-user-role") == "applicant"
                if request.method == "POST":
                    writes.append({"path": target.path, "body": body, "at_seconds": round(time.monotonic() - started, 4)})
                if target.path.startswith("/api/jobs/") and request.method == "GET":
                    ident = int(target.path.rsplit("/", 1)[1])
                    data = {"id": ident, "title": f"合成岗位{ident}", "company_name": "合成压力测试雇主", "city": "深圳",
                            "category": "数据分析", "department": "合成部门", "employer_id": 986, "salary_min": 15,
                            "salary_max": 20, "experience_required": "不限", "education": "本科", "headcount": 1,
                            "description": "完全虚构，仅用于隔离浏览器压力评测。", "skills": [], "requirements": []}
                elif target.path == "/api/resumes/mine" and request.method == "GET":
                    data = resumes
                elif target.path == "/api/resumes" and request.method == "POST":
                    assert body == payload
                    assert not resumes, "Synthetic resume must be uploaded once, then reused"
                    data = {"id": 991, "applicant_id": 987, **payload}; resumes.append(data)
                elif target.path == "/api/resumes/991" and request.method == "GET":
                    data = resumes[0]
                elif target.path == "/api/applications/mine" and request.method == "GET":
                    data = applications
                elif target.path == "/api/applications" and request.method == "POST":
                    assert body["resume_id"] == 991
                    assert any(message["sender_role"] == "employer" for message in messages[body["job_id"]])
                    assert not any(app["job_id"] == body["job_id"] for app in applications), "Duplicate submission request"
                    data = {"id": len(applications) + 1, **body, "applicant_id": 987, "status": "submitted"}; applications.append(data)
                elif target.path == "/api/messages" and request.method == "POST":
                    assert body["recipient_id"] == 986
                    assert not any(m["sender_role"] == "applicant" for m in messages.get(body["job_id"], [])), "Duplicate greeting request"
                    data = {"id": len(writes) + 1, **body, "sender_id": 987, "sender_role": "applicant",
                            "created_at": "2026-01-01T00:00:00Z"}
                    messages.setdefault(body["job_id"], []).append(data)
                elif target.path == "/api/messages" and request.method == "GET":
                    data = messages.get(int(parse_qs(target.query)["job_id"][0]), [])
                else:
                    violations.append({"method": request.method, "url": request.url})
                    await route.abort(); return
                await route.fulfill(content_type="application/json", body=json.dumps(data, ensure_ascii=False))
            await context.route("**/*", intercept)
            page = await context.new_page()
            page_errors = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            await page.goto("http://127.0.0.1:8080/job.html?id=999999")
            jobs = JobStore(database)
            jobs.bind_mock_applicant(987)
            jobs.update_mock_workflow_settings(monitor_enabled=True, auto_submit=False, daily_enabled=False,
                daily_time="09:00", min_score=0, max_contacts=count)
            service = DeliveryAutomationService(jobs, Path(temp.name), headless=True, cdp_url="synthetic-only")
            service.session = NS(attach_existing_cdp=AsyncMock(return_value="cdp"), page_for_url=lambda _: page,
                ensure_cdp_browser=AsyncMock(side_effect=AssertionError("Must not launch user browser")),
                ensure_cdp_window=AsyncMock(side_effect=AssertionError("Must not activate user browser")), close=AsyncMock())
            def export_synthetic():
                jobs.assert_delivery_facts(service.state.job_id, service.state.preview_id)
                return payload.copy()
            service._authorized_resume_payload = export_synthetic
            coordinator = MockWorkflowCoordinator(jobs, NS(worker=None), service, NS(), NS(read_document=lambda: NS()))
            ids = [jobs.upsert_job(job(999999 + position))[0] for position in range(count)]
            binding = {"resume_document_id": "synthetic-scale-document", "resume_version": 1,
                       "resume_digest": "synthetic-scale-digest"}
            queue_args = {"session_id": "default", "resume_stamp": "synthetic", "binding": binding, "min_score": 0,
                          "scope_evidence": {ident: {"delivery_flow": "gated"} for ident in ids}}
            rows = [jobs.get_job(ident) for ident in ids]
            queued = jobs.enqueue_mock_deliveries(rows, **queue_args)
            assert len(queued) == count
            assert not jobs.enqueue_mock_deliveries(rows, **queue_args), "Replayed queue request must be idempotent"
            phase_started = time.monotonic()
            with patch("app.memory.resume_identity.resume_stamp", return_value="synthetic"):
                for number in range(count):
                    tick = time.monotonic()
                    await coordinator.process_delivery_queue()
                    assert service.worker is not None
                    await asyncio.wait_for(service.worker, timeout=90)
                    view = service.current()
                    assert view.status == "waiting_for_reply" and not view.submitted, view
                    greeting_seconds.append(time.monotonic() - tick)
                    assert not applications, "No HR reply has been injected yet"
                    save_progress("greetings", number + 1, last_job_id=view.job_id,
                        last_seconds=round(greeting_seconds[-1], 4), mean_seconds=round(sum(greeting_seconds)/len(greeting_seconds), 4))
                greeting_elapsed = time.monotonic() - phase_started
                runs = jobs.all_delivery_runs(limit=count + 1)
                assert len(runs) == count and all(r["status"] == "waiting_for_reply" and not r["submitted"] for r in runs)
                assert all(jobs.get_job(ident)["stage"] == "applying" for ident in ids)
                assert len([write for write in writes if write["path"] == "/api/messages"]) == count
                assert len(messages) == count

                # Use the actual read-only production poller; do not inject local
                # recruiter_reply fields directly as a shortcut around discovery.
                real_client = httpx.AsyncClient
                def monitor_handler(request):
                    target = urlsplit(str(request.url))
                    assert request.method == "GET" and target.netloc == "127.0.0.1:8080"
                    assert request.headers.get("X-User-Id") == "987" and request.headers.get("X-User-Role") == "applicant"
                    monitor_reads.append({"method": request.method, "path": target.path})
                    if target.path == "/api/messages/conversations":
                        data = [{"job_id": ident, "user_id": 986} for ident in messages]
                    elif target.path == "/api/messages/peek":
                        data = messages.get(int(request.url.params["job_id"]), [])
                    elif target.path == "/api/applications/mine":
                        data = applications
                    else:
                        raise AssertionError("Unexpected monitor endpoint: " + str(request.url))
                    return httpx.Response(200, json=data)
                def client_factory(*args, **kwargs):
                    kwargs["transport"] = httpx.MockTransport(monitor_handler)
                    return real_client(*args, **kwargs)
                with patch("app.workflow.mock_coordinator.httpx.AsyncClient", side_effect=client_factory):
                    before_reply = await coordinator.sync_once()
                    assert before_reply["awaiting_reply"] == count and before_reply["replied"] == 0
                    assert not applications
                    for position, external in enumerate(messages):
                        messages[external].append({"id": 200000 + position, "sender_id": 986, "sender_role": "employer",
                            "content": "合成招聘方回复：请发送简历", "created_at": "2026-01-01T00:01:00Z"})
                    replies = await coordinator.sync_once()
                    assert replies["replied"] == count and replies["submitted"] == 0 and replies["delivery_started"] == 0
                    assert all(jobs.get_job(ident)["recruiter_reply"] for ident in ids)
                    phase_started = time.monotonic()
                    for number in range(count):
                        tick = time.monotonic()
                        outcome = await coordinator.check_reply_queue()
                        assert outcome == {"checked": 1, "submitted": 1}, outcome
                        submission_seconds.append(time.monotonic() - tick)
                        save_progress("submissions", number + 1, last_seconds=round(submission_seconds[-1], 4),
                            mean_seconds=round(sum(submission_seconds)/len(submission_seconds), 4))
                    submission_elapsed = time.monotonic() - phase_started
                    await coordinator.process_delivery_queue()
                    assert all(item["status"] == "completed" for item in jobs.mock_delivery_queue(active_only=False))
                    before_writes = len(writes)
                    for _ in range(3):
                        await coordinator.process_delivery_queue()
                        assert await coordinator.check_reply_queue() == {"checked": 0, "submitted": 0}
                    after_reply = await coordinator.sync_once()
                    assert after_reply["submitted"] == count
                    assert len(writes) == before_writes, "Redundant monitoring must not repeat a browser write"

                assert not jobs.enqueue_mock_deliveries([jobs.get_job(ident) for ident in ids], **queue_args)
                greetings = Counter(write["body"]["job_id"] for write in writes if write["path"] == "/api/messages")
                submissions = Counter(write["body"]["job_id"] for write in writes if write["path"] == "/api/applications")
                assert len(greetings) == len(submissions) == count
                assert all(value == 1 for value in [*greetings.values(), *submissions.values()])
                assert len(applications) == count and all(app["resume_id"] == 991 for app in applications)
                final_runs = jobs.all_delivery_runs(limit=count + 1)
                assert len(final_runs) == count and all(run["submitted"] and run["status"] == "completed" for run in final_runs)
                assert all(jobs.get_job(ident)["stage"] == "waiting" for ident in ids)
                assert not violations and not page_errors, (violations, page_errors)
                report.update(passed=True, queued=count, greetings_before_any_reply=count,
                    reply_poll_conversations=before_reply["conversations"], replies_detected=replies["replied"],
                    confirmed_remote_applications=len(applications), confirmed_local_applications=len(final_runs),
                    duplicate_greetings=0, duplicate_applications=0, task_success_rate=1.0,
                    browser_phases_attempted=2*count, browser_phases_succeeded=2*count, browser_phase_success_rate=1.0,
                    greeting_elapsed_seconds=round(greeting_elapsed, 4), submission_elapsed_seconds=round(submission_elapsed, 4),
                    greeting_latency=distribution(greeting_seconds), submission_latency=distribution(submission_seconds),
                    reply_checks_per_call=1, synthetic_resume_uploads=len(resumes),
                    browser_http_requests=len(requests), read_only_monitor_requests=len(monitor_reads),
                    browser_write_counts=dict(Counter(write["path"] for write in writes)),
                    page_errors=page_errors, violations=violations,
                    backend_assertions={"remote_unique_job_ids":len({app["job_id"] for app in applications}),
                        "local_queue_completed":sum(row["status"]=="completed" for row in jobs.mock_delivery_queue(active_only=False))})
            await service.shutdown()
            await context.close()
            await browser.close()
    except Exception:
        report["error"] = traceback.format_exc()
        print(json.dumps({"failed": True, "error": report["error"]}, ensure_ascii=False), flush=True)
        if service is not None:
            try:
                await service.shutdown()
            except Exception:
                pass
    finally:
        report["elapsed_seconds"] = round(time.monotonic() - started, 4)
        report["source_changed_during_run"] = [source_label(path) for path in source_paths
            if hashlib.sha256(path.read_bytes()).hexdigest() != source_hashes[source_label(path)]]
        if report["source_changed_during_run"]:
            report["passed"] = False
        if database.exists():
            with sqlite3.connect(database) as source, sqlite3.connect(output / "synthetic-jobs.db") as dest:
                source.backup(dest)
        report["observed_greeting_requests"] = sum(write["path"]=="/api/messages" for write in writes)
        report["observed_application_requests"] = sum(write["path"]=="/api/applications" for write in writes)
        (output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        for name, records in (("trace.jsonl", traces), ("browser-requests.jsonl", requests),
                              ("browser-writes.jsonl", writes), ("monitor-reads.jsonl", monitor_reads)):
            (output / name).write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records), encoding="utf-8")
        temp.cleanup()
    print(json.dumps({"output": str(output), "passed": report["passed"], "count": count,
                      "observed_greetings": report["observed_greeting_requests"],
                      "observed_applications": report["observed_application_requests"],
                      "elapsed_seconds": report["elapsed_seconds"]}), flush=True)
    return int(not report["passed"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=120)
    parser.add_argument("--site-root", type=Path, help="Mock frontend assets; defaults to local mock-site, then bundled isolated fixture")
    options = parser.parse_args()
    raise SystemExit(asyncio.run(run(options.count, options.site_root)))
