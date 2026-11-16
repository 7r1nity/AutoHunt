"""Real isolated simulator + production API/Next button + browser daily workflow.

Uses a local mock model (zero external calls), synthetic documents/accounts, an
ephemeral simulator port/DB and a browser-owned virtual localhost:8080 origin.
Run from agent/: python -m evaluation.daily_site_e2e
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import time
import traceback
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import uvicorn
from cryptography.fernet import Fernet
from playwright.async_api import async_playwright, Error as PlaywrightError


def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def until(check, *, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        await asyncio.sleep(.1)
    raise TimeoutError("Expected checkpoint was not reached")


async def run():
    started = time.monotonic()
    root = Path(__file__).resolve().parents[1]
    output = Path(os.environ.get("AUTOHUNT_ACCEPTANCE_UI_OUTPUT") or
                  root / "evaluation/results/daily-site-e2e" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")).resolve()
    if output.parent != (root / "evaluation/results/daily-site-e2e").resolve() or output.exists() or not re.fullmatch(r"\d{8}T\d{6}\.\d+Z", output.name):
        raise ValueError("Daily UI acceptance requires a new timestamp directory in evaluation/results/daily-site-e2e")
    output.mkdir(parents=True)
    token = "isolated-daily-" + os.urandom(18).hex()
    # Set before importing application configuration/dependencies. Never read .env.
    os.environ.update(AUTOHUNT_DATA_ROOT=str(output / "data"), AUTOHUNT_API_KEY=token,
        AUTOHUNT_DATA_KEY=Fernet.generate_key().decode(), AUTOHUNT_LLM_PROVIDER="mock",
        AUTOHUNT_LLM_MODEL="mock-autohunt-v1", AUTOHUNT_BROWSER_HEADLESS="true",
        AUTOHUNT_RATE_LIMIT="1000", AUTOHUNT_READ_RATE_LIMIT="3000")
    from evaluation.benchmark.execution import IsolatedSite
    from app.api.application import app
    from app.core import dependencies as d
    from app.core.models import CandidateProfileView
    from app.workflow.daily import DailyWorkflow

    site = IsolatedSite()
    frontend = server = server_task = context = browser = None
    rows = []
    report = {"passed": False, "external_model_calls": 0, "scope": "Real isolated simulator/browser/API/UI; local mock model, not semantic accuracy"}
    try:
        await site.start(output)
        applicant, employer, _, jd = await site.create("daily-e2e")
        profile = CandidateProfileView(resume_id="synthetic-daily", skills=["Python", "FastAPI", "SQL"],
            role_suggestions=["Python"], summary="虚构候选人：Python、FastAPI、SQL 后端项目，仅用于隔离验收。")
        d.candidate_state_store.apply_resume_profile(profile, "Python")
        d.candidate_state_store.confirm_search_query("Python", ["深圳"])
        d.resume_workspace_store.sync_profile(profile)
        d.job_store.update_mock_workflow_settings(monitor_enabled=True, auto_submit=True, daily_enabled=False,
            daily_time="09:00", min_score=0, max_contacts=1)

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(service_workers="block", viewport={"width": 1440, "height": 1000})
            await context.tracing.start(screenshots=True, snapshots=True, sources=True)
            async def forward(route):
                req = route.request
                parsed = urlsplit(req.url)
                path = parsed.path + ("?" + parsed.query if parsed.query else "")
                headers = {k: v for k, v in req.headers.items() if k.lower() not in {"host", "content-length", "connection", "accept-encoding"}}
                response = await site.client.request(req.method, path, content=req.post_data_buffer, headers=headers)
                rows.append({"method": req.method, "path": path, "status": response.status_code})
                try:
                    await route.fulfill(status=response.status_code, content_type=response.headers.get("content-type", "application/json"), body=response.content)
                except PlaywrightError as exc:
                    if "closed" not in str(exc):
                        raise
            allowed_ui_ports = set()
            async def isolated_route(route):
                parsed = urlsplit(route.request.url)
                if parsed.hostname == "127.0.0.1" and parsed.port == 8080:
                    await forward(route)
                elif parsed.hostname == "127.0.0.1" and parsed.port in allowed_ui_ports:
                    await route.continue_()
                else:
                    await route.abort()
            await context.route("**/*", isolated_route)
            page = await context.new_page()
            await page.goto("http://127.0.0.1:8080/")

            class Session:
                mode = "cdp"
                disconnected = asyncio.Event()
                async def reuse(self, mode): return "cdp"
                async def ensure_cdp_window(self, *args): pass
                async def recover_page(self, *args): return page
                async def navigate(self, url, **kwargs):
                    await page.goto(url, wait_until="domcontentloaded")
                    return page
                def lifecycle_snapshot(self): return "isolated-real-browser"
                def pages(self): return [page]
                async def close(self): pass
            d.browser_service.session = Session()

            api_port, ui_port = port(), port()
            allowed_ui_ports.update({api_port, ui_port})
            # Scheduler turns are driven explicitly below. Ordinary polling is
            # deliberately off so no background API can contact the user's site.
            server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=api_port,
                lifespan="off", log_level="warning", access_log=False))
            server_task = asyncio.create_task(server.serve())
            await until(lambda: server.started)
            front_root = root.parent / "frontend"
            log = (output / "frontend.log").open("w", encoding="utf-8")
            env = {**os.environ, "AUTOHUNT_BACKEND_URL": f"http://127.0.0.1:{api_port}", "AUTOHUNT_BACKEND_TOKEN": token,
                   "NEXT_TELEMETRY_DISABLED": "1"}
            frontend = subprocess.Popen([shutil.which("node"), str(front_root / "node_modules/next/dist/bin/next"),
                "dev", "--webpack", "--hostname", "127.0.0.1", "--port", str(ui_port)], cwd=front_root,
                env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            async with httpx.AsyncClient(trust_env=False, timeout=2) as probe:
                for _ in range(120):
                    try:
                        if (await probe.get(f"http://127.0.0.1:{ui_port}")).status_code == 200: break
                    except httpx.HTTPError: pass
                    await asyncio.sleep(.5)
                else: raise TimeoutError("Isolated frontend did not start")
            ui = await context.new_page()
            await ui.goto(f"http://127.0.0.1:{ui_port}")
            await ui.get_by_role("button", name="模型设置", exact=True).click()
            await ui.get_by_role("button", name="立即测试每日任务", exact=True).wait_for()
            await ui.wait_for_function("() => [...document.querySelectorAll('button')].some(b => b.textContent === '立即测试每日任务' && !b.disabled)")
            await ui.screenshot(path=str(output / "settings.png"), full_page=True)
            await ui.get_by_role("button", name="立即测试每日任务", exact=True).click()
            await until(lambda: d.daily_workflow.records())
            run_id = d.daily_workflow.records()[0]["id"]
            report["run_id"] = run_id
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{api_port}", trust_env=False,
                                         headers={"X-AutoHunt-Key": token}) as api:
                key = d.run_store.get(run_id)["request_key"].removeprefix("daily-test:")
                duplicate = await api.post("/api/v1/mock-workflow/test", json={"request_id": key})
                assert duplicate.status_code == 202 and duplicate.json()["id"] == run_id
            await until(lambda: d.browser_service.current().status == "waiting_for_user")
            assert not site.records(jd["id"], applicant["id"])["messages"]
            report["mfa_pauses_agent"] = True
            # Test actor completes the simulator's displayed demo-MFA UI. The
            # production agent remains paused and never reads/solves this code.
            await page.locator("#auth-email").fill("daily-e2e@example.invalid")
            await page.locator("#auth-name").fill("虚构评测候选人")
            await page.locator("#auth-submit").click()
            await page.locator("#demo-code").wait_for(state="visible")
            code = re.search(r"\d{6}", await page.locator("#demo-code").inner_text()).group()
            await page.locator("#auth-code").fill(code)
            await page.locator("#auth-submit").click()
            await page.wait_for_function("localStorage.getItem('userId') !== null")
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{api_port}", trust_env=False,
                                         headers={"X-AutoHunt-Key": token}) as api:
                response = await api.post(f"/api/v1/runs/{run_id}/resume")
                response.raise_for_status()
            await until(lambda: d.run_store.get(run_id)["status"] in {"waiting_reply", "completed", "failed", "outcome_indeterminate"}, timeout=120)
            run = d.run_store.get(run_id)
            assert run["status"] == "waiting_reply", json.dumps(run, ensure_ascii=False, default=str)
            records = site.records(jd["id"], applicant["id"])
            assert len(records["messages"]) == 1 and not records["applications"]
            greeting = records["messages"][0]["content"]
            report["greeting_then_wait"] = True

            # Reconstruct scheduler and DB objects from persisted state, retaining
            # the external site. No in-memory job receipts are carried forward.
            await d.daily_workflow.shutdown()
            from app.runtime.store import RunStore
            from app.jobs.store import JobStore
            recovered_jobs = JobStore(output / "data/autohunt.db")
            recovered_runs = RunStore(output / "data/runtime.db", os.environ["AUTOHUNT_DATA_KEY"])
            recovered_jobs.reconcile_incomplete_runs()
            recovered = DailyWorkflow(recovered_jobs, recovered_runs, d.browser_service, d.delivery_service,
                                      d.candidate_state_store, d.resume_workspace_store)
            d.mock_workflow.daily = d.agent_runtime.daily = recovered
            await recovered.recover()
            await recovered.tick()
            await recovered.worker
            assert recovered_runs.get(run_id)["status"] == "waiting_reply"
            assert len(site.records(jd["id"], applicant["id"])["messages"]) == 1
            report["restart_no_duplicate_greeting"] = True
            response = await site.client.post("/api/messages", json={"recipient_id": applicant["id"], "job_id": jd["id"],
                "content": "请发送当前授权简历"}, headers={"X-User-Id": str(employer["id"]), "X-User-Role": "employer"})
            response.raise_for_status()
            # Simulate loss after the real site commits but before the daily run
            # persists success. Only the durable pre-click intent survives.
            adapter = d.browser_service.mock_delivery_adapter
            submit = adapter.submit
            async def crash_after_submit(*args, **kwargs):
                result = await submit(*args, **kwargs)
                assert result.confirmed
                raise asyncio.CancelledError("injected process-loss boundary")
            adapter.submit = crash_after_submit
            await recovered.resume(run_id)
            await asyncio.gather(recovered.worker, return_exceptions=True)
            adapter.submit = submit
            assert recovered_runs.get(run_id)["status"] == "queued"
            assert any(j["phase"] == "submit_started" for j in recovered_runs.get(run_id)["checkpoint"]["jobs"].values())
            assert len(site.records(jd["id"], applicant["id"])["applications"]) == 1
            await recovered.shutdown()
            recovered_jobs = JobStore(output / "data/autohunt.db")
            recovered_runs = RunStore(output / "data/runtime.db", os.environ["AUTOHUNT_DATA_KEY"])
            recovered_jobs.reconcile_incomplete_runs()
            recovered = DailyWorkflow(recovered_jobs, recovered_runs, d.browser_service, d.delivery_service,
                                      d.candidate_state_store, d.resume_workspace_store)
            d.mock_workflow.daily = d.agent_runtime.daily = recovered
            await recovered.recover()
            await recovered.tick()
            await recovered.worker
            run = recovered_runs.get(run_id)
            assert run["status"] == "completed", json.dumps(run, ensure_ascii=False, default=str)
            records = site.records(jd["id"], applicant["id"])
            assert len(records["applications"]) == 1
            assert len([m for m in records["messages"] if m["sender_role"] == "applicant" and m["content"] == greeting]) == 1
            assert records["applications"][0]["resume_id"] == records["resumes"][0]["id"]
            report["reply_then_exact_resume_once"] = True
            report["crash_after_submit_reconciled"] = True
            ledger = recovered_runs.execution_ledger(run_id)
            assert ledger["calls"] and ledger["tools"] and ledger["steps"]
            assert all(c["session_id"] == "default" for c in ledger["calls"])
            (output / "ledger.json").write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
            report["model_calls"] = len(ledger["calls"])
            report["tool_calls"] = len(ledger["tools"])
            await ui.get_by_text("本轮已结束", exact=True).wait_for(timeout=20000)
            await ui.screenshot(path=str(output / "metrics.png"), full_page=True)
            report["button_and_metrics_ui"] = True
            await recovered.recover()
            assert not recovered.records(active=True)
            report["completed_not_replayed"] = True

            # Deterministic missed-clock test uses the actual persistent scheduler.
            recovered_jobs.update_mock_workflow_settings(monitor_enabled=True, auto_submit=False, daily_enabled=True,
                daily_time="09:00", min_score=100, max_contacts=1)
            with recovered_jobs._connect() as db:
                db.execute("UPDATE mock_workflow_settings SET daily_next_due_at='2099-01-01T01:00:00+00:00' WHERE id=1")
            recovered.clock = lambda: datetime(2099, 1, 4, 2, tzinfo=UTC)
            recovered.materialize_due()
            recovered.materialize_due()
            catchups = [r for r in recovered.records() if r["payload"]["trigger"] == "catch_up"]
            assert len(catchups) == 1 and catchups[0]["payload"]["coalesced_occurrences"] == 4
            # Use the production coordinator/rule observation to dispatch the
            # catch-up, and actually complete its read-only search (score=100).
            observation = await d.mock_workflow.observe(with_site=False)
            rule = next(r for r in d.mock_workflow.engine.rules if r.name == "daily_report")
            assert rule.condition(observation)
            await rule.action(observation)
            await recovered.worker
            assert recovered_runs.get(catchups[0]["id"])["status"] == "completed"
            assert len(site.records(jd["id"], applicant["id"])["applications"]) == 1
            report["missed_four_days_one_catchup"] = True
            # Publish a bounded summary, then load this exact report through the
            # real frontend history view. finally overwrites it if this check fails.
            (output / "summary.json").write_text(json.dumps({**report, "tests": 9, "failures": 0, "errors": 0}), encoding="utf-8")
            expected_report_id = "local--daily-site-e2e--" + output.name
            async with ui.expect_response(lambda response: response.url.endswith("/evaluation-reports/" + expected_report_id)) as loaded:
                await ui.get_by_role("button", name="历史评测", exact=True).click()
                await ui.get_by_label("报告类别", exact=True).select_option("模拟站端到端")
            detail = await loaded.value
            assert detail.status == 200 and (await detail.json())["id"] == expected_report_id
            report["history_report_id"] = expected_report_id
            await ui.get_by_role("heading", name="每日任务模拟站端到端验收", exact=True).wait_for()
            await ui.get_by_text("验收通过", exact=True).wait_for()
            await ui.screenshot(path=str(output / "history.png"), full_page=True)
            report["history_report_ui"] = True
            report["passed"] = True
            await context.tracing.stop(path=str(output / "browser-trace.zip"))
            await context.unroute_all(behavior="ignoreErrors")
            await context.close()
            context = None
            await browser.close()
            browser = None
    except BaseException:
        report["error"] = traceback.format_exc()
    finally:
        if d.mock_workflow.daily:
            await d.mock_workflow.daily.shutdown()
        if server:
            server.should_exit = True
        if server_task:
            await asyncio.wait_for(server_task, 15)
        if frontend:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(frontend.pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            elif frontend.poll() is None:
                frontend.terminate()
                try: await asyncio.to_thread(frontend.wait, 10)
                except subprocess.TimeoutExpired:
                    frontend.kill()
                    await asyncio.to_thread(frontend.wait, 10)
            log.close()
        await site.close()
        report.update(tests=9, failures=0 if report["passed"] else 1, errors=0,
                      duration_seconds=round(time.monotonic() - started, 3),
                      real_browser_runs=1, real_submissions=int(bool(report.get("reply_then_exact_resume_once"))))
        (output / "http-trace.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"output": str(output), **report}, ensure_ascii=False), flush=True)
    return int(not report["passed"])


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
