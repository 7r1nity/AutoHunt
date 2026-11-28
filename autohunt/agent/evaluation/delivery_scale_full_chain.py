"""Command/API -> new browser search -> queue -> real fixture DB -> board.

Uses scripted model responses (not an intent-quality/model-cost evaluation), a
fresh browser context and the bundled simulator on an ephemeral port. Nothing
contacts the user's simulator, CDP profile, model provider, queue or documents.
The frontend assets may come from a running local Next server, but every /api
request from the test browser is intercepted. Production waits are unchanged.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import time
import traceback
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit


def command(count):
    return (f"在模拟招聘网站新搜索{search_count(count)}个当前简历方向的岗位，清除旧筛选条件、不限城市薪资和分数，"
            f"对本轮结果自动投递前{count}个，已沟通或已投递的跳过，先沟通，收到HR回复并确认可发送后自动发送当前主简历，"
            f"不足{count}个则处理现有岗位并报告缺额。")


def search_count(count):
    # More current-round eligible rows than the delivery budget prove that the
    # batch limit is actually applied, rather than incidentally matching supply.
    return min(200, count + 20)


async def until(check, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        await asyncio.sleep(.1)
    raise TimeoutError("Expected isolated chain checkpoint was not reached")


async def run(count, origin):
    from cryptography.fernet import Fernet
    import httpx
    from playwright.async_api import async_playwright, expect

    root = Path(__file__).resolve().parents[1]
    output = root / "evaluation/results/delivery-scale-full-chain" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    # Set BEFORE importing application/dependencies: no personal .env/data root.
    for name in list(os.environ):
        if name.startswith("AUTOHUNT_") or name.endswith(("_API_KEY", "_TOKEN")):
            os.environ.pop(name, None)
    os.environ.update(AUTOHUNT_DATA_ROOT=str(output / "data"),
        AUTOHUNT_DATA_KEY=Fernet.generate_key().decode(), AUTOHUNT_API_KEY="synthetic-scale-chain-key",
        AUTOHUNT_LLM_PROVIDER="mock", AUTOHUNT_LLM_MODEL="mock-autohunt-v1",
        AUTOHUNT_RATE_LIMIT="10000", AUTOHUNT_READ_RATE_LIMIT="20000",
        AUTOHUNT_BROWSER_HEADLESS="true", AUTOHUNT_ROUTER_SMALL_API_KEY="")
    from app.api.application import app
    from app.core import dependencies as d
    from app.core.models import CandidateProfileView, BrowserSearchRequest
    from app.delivery.resume_binding import mock_resume_payload
    from app.memory.resume_workspace import resume_document_digest
    from app.llm.base import LLMResult
    from evaluation.benchmark.execution import IsolatedSite
    from evaluation.frontend_board_sync_browser import Fixture

    paths = [root / p for p in ("app/core/agent_tools.py", "app/core/models.py",
        "app/core/search_limits.py", "app/core/result_groups.py", "app/core/filter_policy.py",
        "app/runtime/manager.py", "app/runtime/store.py", "app/browser/automation.py",
        "app/runtime/dialogue.py", "app/runtime/search_state.py", "app/runtime/job_references.py",
        "app/browser/mock_site_adapter.py", "app/workflow/planning.py", "app/workflow/search_projection.py", "app/workflow/execution.py",
        "app/workflow/mock_coordinator.py", "app/workflow/observations.py", "app/workflow/delivery_rules.py",
        "app/jobs/store.py", "app/api/application.py")]
    paths.extend(root.parent / "frontend" / p for p in ("app/page.tsx", "app/api/agent/jobs/route.ts",
        "lib/use-job-board.ts", "lib/job-board-pages.ts", "lib/latest-snapshot.ts"))
    paths.append(Path(__file__).resolve())
    label = lambda path: str(path.relative_to(root.parent))
    hashes = {label(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    report = {"passed": False, "target": count, "search_target": search_count(count), "command": command(count),
        "external_model_calls": 0, "user_data_access": False, "source_sha256": hashes,
        "scope": "Scripted model; actual API/planner/executor/new browser search/queue/DOM/poller and isolated simulator SQLite; actual frontend rendering with intercepted business APIs.",
        "fixture_seams": ["scripted model responses", "fresh headless browser attachment",
                          "virtual localhost:8080 forwarded to isolated simulator", "test actor HR replies"],
        "production_lifespan_started": False}
    trace, model_calls, browser_requests, violations = [], [], [], []
    site = IsolatedSite()
    context = fixture = None
    real_client = httpx.AsyncClient
    api = real_client(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1",
                     headers={"X-AutoHunt-Key": "synthetic-scale-chain-key"}, timeout=30)
    started = time.perf_counter()
    preparing_previous_round = False
    def checkpoint(phase, **data):
        row = {"phase": phase, "elapsed_seconds": round(time.perf_counter()-started, 3), **data}
        trace.append(row)
        (output / "progress.json").write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(row, ensure_ascii=False), flush=True)

    async def synthetic_model(request):
        model_calls.append({"context": request.context, "instructions_prefix": request.instructions[:80]})
        if request.instructions.startswith("你是AutoHunt任务解释器"):
            draft = {"steps": [
                {"id": "conditions", "tool": "search_state_update", "user_requested": True,
                 "arguments": {"locations": [], "salary_min": None, "filters": None,
                    "result_groups": None, "result_limit": search_count(count), "max_jobs": 200,
                    "source": "mock", "use_resume_profile": True}},
                {"id": "search", "tool": "job_search_start", "user_requested": True,
                 "arguments": {"queries": ["合成Python后端工程师"], "source": "mock",
                    "result_limit": search_count(count), "max_jobs": 200}, "depends_on": ["conditions"]},
                {"id": "results", "tool": "job_results_read", "arguments": {"summary": True},
                 "depends_on": ["search"]},
                {"id": "delivery", "tool": "mock_delivery_start", "user_requested": True,
                 "arguments": {"limit": count, "min_score": 0}, "depends_on": ["results"]}],
                "goals": [{"quote": command(count), "disposition": "steps", "step_ids": ["conditions", "search", "results", "delivery"]}]}
        elif request.context == "条件语义核对，不产生执行授权":
            body = json.loads(request.user_message)
            draft = {"scopes": [{"step_id": row["step_id"], "status": "pass", "changes": {},
                                 "reason": "合成请求范围与参数一致"} for row in body["scopes"]]}
        elif "职位初筛器" in request.instructions:
            cards = json.loads(request.context)["cards"]
            draft = [{"index": i, "decision": "APPLY", "reasoning": "虚构Python方向匹配"} for i in range(len(cards))]
        elif "职位匹配评估器" in request.instructions:
            draft = {"score": 100 if preparing_previous_round else 90, "decision": "APPLY", "matched_skills": ["Python", "FastAPI", "SQL"],
                     "missing_skills": [], "reasoning": "虚构简历与Python岗位匹配", "requirements": []}
        elif request.instructions.startswith("你是任务结果回答器"):
            body = json.loads(request.user_message)
            draft = {"sections": [{"step_id": row["step_id"], "kind": "result", "text": "", "job_ids": []}
                for row in body["results"] if row["tool"] == "job_results_read"
                and row.get("presentation_role") != "background_only"]}
        else:
            raise AssertionError("Unexpected scripted-model request: " + request.instructions[:160])
        return LLMResult(json.dumps(draft, ensure_ascii=False), "scripted", "scale-chain")

    try:
        await site.start(output / "simulator")
        # Intercepted browser navigations can cancel a forwarded HTTP request.
        # Do not share pooled connections with unrelated later route handlers.
        await site.client.aclose()
        site.client = real_client(base_url=f"http://127.0.0.1:{site.port}", trust_env=False,
            timeout=12, limits=httpx.Limits(max_keepalive_connections=0))
        applicant, employer, _, first = await site.create("scale-chain")
        employer_headers = {"X-User-Id": str(employer["id"]), "X-User-Role": "employer"}
        external_ids = []
        extra_candidates = min(5, 200-search_count(count))
        for number in range(search_count(count) + extra_candidates):
            response = await site.client.post("/api/jobs", json={"title": f"合成Python后端工程师 {number+1}",
                "category": "后端开发", "city": "深圳", "department": "合成验证",
                "salary_min": 18, "salary_max": 25, "experience_required": "不限", "education": "本科",
                "skills": ["Python", "FastAPI", "SQL"],
                "description": "【虚构测试】负责Python与FastAPI接口开发、SQL查询与质量验证。"}, headers=employer_headers)
            response.raise_for_status()
            external_ids.append(response.json()["id"])
        profile = CandidateProfileView(resume_id="synthetic-scale-resume", skills=["Python", "FastAPI", "SQL"],
            role_suggestions=["合成Python后端工程师"], summary="完全虚构：Python、FastAPI、SQL后端项目，仅用于隔离测试。")
        d.candidate_state_store.apply_resume_profile(profile, "合成Python后端工程师")
        d.candidate_state_store.confirm_search_query("合成Python后端工程师", ["深圳"])
        d.resume_workspace_store.sync_profile(profile)
        # Reproduce a restrictive old session. The example explicitly clears it,
        # so a new task cannot silently inherit its city, salary or old quota.
        previous = d.agent_runtime.search_state("default")
        previous.update(locations=["北京"], salary_min=999999, max_jobs=5, result_limit=5,
            filters={"op": "condition", "salary": {"unit": "monthly", "minimum": 999999}})
        d.run_store.save_search_state("default", previous)
        d.llm_manager.generate = synthetic_model
        # Rules still run; the unconfigured small-model client cannot call out.
        d.job_store.update_mock_workflow_settings(monitor_enabled=True, auto_submit=False, daily_enabled=False,
            daily_time="09:00", min_score=0, max_contacts=count)
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(service_workers="block", viewport={"width": 1480, "height": 1100})
            await context.add_init_script(f"localStorage.setItem('userId','{applicant['id']}');localStorage.setItem('userRole','applicant')")
            async def forward(route):
                req, target = route.request, urlsplit(route.request.url)
                if target.scheme != "http" or target.netloc != "127.0.0.1:8080":
                    violations.append(req.url); await route.abort(); return
                path = target.path + ("?"+target.query if target.query else "")
                headers = {key: value for key, value in req.headers.items()
                    if key.lower() not in {"host", "content-length", "connection", "accept-encoding"}}
                entry = {"method": req.method, "path": path}
                browser_requests.append(entry)
                try:
                    response = await site.client.request(req.method, path, content=req.post_data_buffer, headers=headers)
                    entry["status"] = response.status_code
                except httpx.HTTPError as exc:
                    entry["forward_error"] = type(exc).__name__
                    # An intercepted transport failure must become a failed
                    # browser request, not poison Playwright's event dispatcher.
                    await route.abort()
                    return
                try:
                    await route.fulfill(status=response.status_code,
                        content_type=response.headers.get("content-type", "application/json"), body=response.content)
                except Exception as exc:
                    if "closed" not in str(exc).lower() and "cancel" not in str(exc).lower():
                        raise
            await context.route("**/*", forward)
            page = await context.new_page()
            await page.goto("http://127.0.0.1:8080/")

            class Session:
                mode = "cdp"
                disconnected = asyncio.Event()
                async def reuse(self, mode): return "cdp"
                async def ensure_cdp_window(self, *args): pass
                async def recover_page(self, *args): return page
                async def navigate(self, url, **kwargs):
                    await page.goto(url, wait_until="domcontentloaded"); return page
                def lifecycle_snapshot(self): return "isolated-scale-browser"
                def pages(self): return [page]
                async def close(self): pass
            d.browser_service.session = Session()
            d.delivery_service.session = NS(attach_existing_cdp=AsyncMock(return_value="cdp"), page_for_url=lambda _:page,
                ensure_cdp_browser=AsyncMock(side_effect=AssertionError("Must not access user browser")),
                ensure_cdp_window=AsyncMock(), close=AsyncMock())
            # A real prior search creates a high-scored, otherwise eligible
            # decoy. A broken fallback to the full board would select it first.
            preparing_previous_round = True
            old_search = await d.browser_service.start(BrowserSearchRequest(source="mock", query=first["title"],
                result_limit=1, max_jobs=1, conversation_id="default"))
            await asyncio.wait_for(d.browser_service.worker, timeout=60)
            assert d.job_store.get_task(old_search.id).matched == 1
            old_job_id = d.job_store.job_id_for_source_url(f"http://127.0.0.1:8080/job.html?id={first['id']}")
            old_job = d.job_store.get_job(old_job_id)
            assert old_job["score"] == 100
            preparing_previous_round = False
            d.browser_service.session = Session()
            checkpoint("fixture_ready")
            request_body = {"conversation_id": "default", "request_id": "synthetic-scale-chain", "message": command(count)}
            response = await api.post("/api/v1/runs", json=request_body)
            response.raise_for_status()
            run_id = response.json()["id"]
            assert (await api.post("/api/v1/runs", json=request_body)).json()["id"] == run_id
            # Browser discovery and matching must produce new receipts; no seed
            # into agent jobs/search_result tables is used anywhere in this test.
            await until(lambda: d.run_store.get(run_id)["status"] in {"completed", "failed", "outcome_indeterminate", "waiting_confirmation"}, timeout=240)
            run_state = d.run_store.get(run_id)
            (output / "root-run.json").write_text(json.dumps(run_state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            assert run_state["status"] == "completed", (run_state["status"], run_state.get("error"), run_state.get("result"))
            plan = run_state["checkpoint"]["plan"]
            search = next(row for row in plan["steps"] if row["tool"] == "job_search_start")
            delivery = next(row for row in plan["steps"] if row["tool"] == "mock_delivery_start")
            task_id = search["task_id"]
            assert d.job_store.get_task(task_id).matched == search_count(count)
            assert d.job_store.get_task(task_id).discovered == search_count(count) + extra_candidates
            effective = d.job_store.search_recovery(task_id)["request"]
            assert not effective.get("locations") and effective.get("filters") is None and not effective.get("salary_min"), effective
            assert delivery["status"] == "completed" and delivery["result"]["queued"] == count, delivery
            assert delivery["result"]["eligible_count"] == search_count(count), delivery["result"]
            assert delivery["arguments"]["source_task_id"] == task_id
            assert len(d.job_store.mock_delivery_queue()) == count
            selected_job_ids = {row["job_id"] for row in d.job_store.mock_delivery_queue()}
            assert old_job_id not in selected_job_ids
            unselected = {row.id: d.job_store.get_job(row.id) for row in d.job_store.list_jobs(limit=1000)
                if row.id not in selected_job_ids | {old_job_id}}
            assert len(unselected) == search_count(count)-count
            target_external_ids = {int(d.job_store.get_job(ident)["external_id"]) for ident in selected_job_ids}
            assert len(target_external_ids) == count and target_external_ids <= set(external_ids)
            if extra_candidates:
                assert target_external_ids < set(external_ids)
            history = d.job_store.conversation_history(conversation_id="default", limit=100)
            assert any("搜索完成" in row["content"] for row in history if row["role"] == "assistant")
            checkpoint("new_search_and_authorized_queue", matched=search_count(count), queued=count, source_task_id=task_id)
            def site_counts():
                with sqlite3.connect(site.database) as db:
                    messages = db.execute("SELECT job_id,COUNT(*) FROM messages WHERE sender_id=? AND content LIKE '您好，我关注贵司%' GROUP BY job_id", (applicant["id"],)).fetchall()
                    apps = db.execute("SELECT job_id,resume_id FROM applications WHERE applicant_id=?", (applicant["id"],)).fetchall()
                    return messages, apps
            def monitor_client(*args, **kwargs):
                if str(kwargs.get("base_url", "")).rstrip("/") != "http://127.0.0.1:8080":
                    raise AssertionError("Unexpected outbound HTTP client")
                kwargs["base_url"] = f"http://127.0.0.1:{site.port}"
                return real_client(*args, **kwargs)
            with patch("app.workflow.mock_coordinator.httpx.AsyncClient", side_effect=monitor_client):
                # Drive the REAL production loop/rules/wakeup and 30s polling,
                # not individual dispatch or reply-check methods from the test.
                await d.mock_workflow.start()
                last_greetings = -1
                def validate_waiters(runs):
                    if (output / "stop-request.json").exists():
                        raise RuntimeError("Isolated evaluation stopped by its owner; partial records preserved")
                    bad = [row for row in runs if row["status"] in {"failed", "outcome_indeterminate", "waiting_for_user", "cancelled"}]
                    assert not bad, [(row["job_id"], row["status"], row.get("detail")) for row in bad]
                def all_greeted():
                    nonlocal last_greetings
                    runs = d.job_store.all_delivery_runs(limit=200)
                    validate_waiters(runs)
                    ready = sum(row["status"] == "waiting_for_reply" for row in runs)
                    if ready != last_greetings and (ready % 20 == 0 or ready == count):
                        checkpoint("greetings", completed=ready); last_greetings = ready
                    return len(runs) == count and ready == count
                await until(all_greeted, timeout=600)
                greetings, applications = site_counts()
                assert len(greetings) == count and all(n == 1 for _, n in greetings) and not applications
                report["greetings_before_any_reply"] = len(greetings)
                waiting_ids = [row["id"] for row in d.job_store.all_delivery_runs(limit=200)]
                assert len(waiting_ids) == count
                for ident in target_external_ids:
                    response = await site.client.post("/api/messages", json={"recipient_id": applicant["id"],
                        "job_id": ident, "content": "合成HR：请发送当前授权简历。"}, headers=employer_headers)
                    response.raise_for_status()
                checkpoint("hr_replies_posted", count=count)
                last_submitted = -1
                def all_submitted():
                    nonlocal last_submitted
                    runs = d.job_store.all_delivery_runs(limit=200)
                    validate_waiters(runs)
                    submitted = sum(row["submitted"] for row in runs)
                    if submitted != last_submitted and (submitted % 20 == 0 or submitted == count):
                        checkpoint("submissions", completed=submitted); last_submitted = submitted
                    return len(runs) == count and submitted == count
                await until(all_submitted, timeout=600)
                await until(lambda: all(row["status"] == "completed" for row in d.job_store.mock_delivery_queue(active_only=False)), timeout=30)
                # Give a redundant autonomous observation a chance to run; the
                # wakeup is only for this already-completed no-write check.
                d.mock_workflow.wakeup.set()
                await asyncio.sleep(.4)
                await d.mock_workflow.stop()
            greetings, applications = site_counts()
            assert len(greetings) == len(applications) == count and all(n == 1 for _, n in greetings)
            write_counts = Counter(row["path"].split("?", 1)[0] for row in browser_requests if row["method"] == "POST")
            assert write_counts["/api/messages"] == write_counts["/api/applications"] == count, write_counts
            assert {ident for ident, _ in applications} == target_external_ids
            assert len({ident for ident, _ in applications}) == count
            assert len({resume for _, resume in applications}) == 1
            document = d.resume_workspace_store.read_document()
            binding = {"document_id": document.id, "version": document.version, "digest": resume_document_digest(document)}
            expected_resume = mock_resume_payload(document, **binding)
            resume_id = applications[0][1]
            response = await site.client.get(f"/api/resumes/{resume_id}", headers={"X-User-Id":str(applicant["id"]),"X-User-Role":"applicant"})
            response.raise_for_status()
            remote_resume = response.json()
            assert {key: remote_resume[key] for key in expected_resume} == expected_resume
            assert all((row["resume_document_id"], row["resume_version"], row["resume_digest"]) ==
                (binding["document_id"], binding["version"], binding["digest"])
                for row in d.job_store.all_delivery_runs(limit=200))
            assert {row["id"] for row in d.job_store.all_delivery_runs(limit=200)} == set(waiting_ids)
            assert all(row["status"] == "completed" for row in d.job_store.mock_delivery_queue(active_only=False))
            assert d.job_store.count_jobs(include_rejected=False) == search_count(count) + 1
            assert d.job_store.get_job(old_job_id) == old_job
            assert all(d.job_store.get_job(ident) == previous_row for ident, previous_row in unselected.items())
            assert not site.records(first["id"], applicant["id"])["messages"]
            assert not site.records(first["id"], applicant["id"])["applications"]
            checkpoint("remote_and_local_receipts_confirmed", applications=count)

            # The frontend reads the REAL isolated paged API, not a manufactured
            # 120-card array. Other /api paths use the no-write UI fixture.
            fixture = Fixture(origin, output, "new-search-delivery-board")
            fixture.jobs = [row.model_dump(mode="json") for row in d.job_store.list_jobs(limit=search_count(count)+1)]
            fixture.context = await browser.new_context(service_workers="block", viewport={"width":1480,"height":1100})
            board_pages = []
            async def ui_route(route):
                target = urlsplit(route.request.url)
                if target.path == "/api/agent/jobs":
                    assert route.request.method == "GET" and parse_qs(target.query).get("page") == ["true"]
                    params = dict((key, values[-1]) for key, values in parse_qs(target.query).items() if key != "page")
                    response = await api.get("/api/v1/jobs/page", params=params)
                    response.raise_for_status()
                    data = response.json(); board_pages.append({"offset":data["offset"],"total":data["total"],"count":len(data["items"])})
                    await route.fulfill(json=data)
                else:
                    await fixture.intercept(route)
            await fixture.context.route("**/*", ui_route)
            fixture.page = await fixture.context.new_page()
            fixture.page.on("pageerror", lambda error: fixture.page_errors.append(str(error)))
            await fixture.page.goto(origin, wait_until="domcontentloaded", timeout=90_000)
            await fixture.page.get_by_text(f"已保存 {search_count(count)+1} 个职位", exact=True).wait_for(timeout=30_000)
            await fixture.open_board()
            await expect(fixture.page.locator(".standalone-board .job-card")).to_have_count(search_count(count)+1)
            await expect(fixture.page.locator(".standalone-board .kanban-column").filter(has=fixture.page.get_by_text("已投递", exact=False)).locator(".job-card")).to_have_count(count)
            roles = await fixture.page.locator(".standalone-board .job-card h4").all_text_contents()
            expected_ids = selected_job_ids | set(unselected) | {old_job_id}
            by_role = {d.job_store.get_job(ident)["title"]: ident for ident in expected_ids}
            assert len(by_role) == len(roles) == search_count(count)+1
            assert {by_role[role] for role in roles} == expected_ids
            await fixture.finish(); fixture = None
            report.update(passed=True, new_search_matched=search_count(count), authorized_queued=count,
                confirmed_simulator_applications=len(applications), confirmed_agent_applications=count,
                board_cards=search_count(count)+1, submitted_board_cards=count, untouched_prior_round_cards=1,
                untouched_current_round_unselected=len(unselected),
                excess_candidates_not_admitted=extra_candidates, high_score_prior_round_untouched=True,
                board_pages=board_pages, duplicate_greetings=0, duplicate_submissions=0,
                source_bound_to_new_search=True, same_delivery_run_ids_after_reply=True,
                explicit_reset_cleared_old_city_salary_and_quota=True,
                coordinator_loop_drove_delivery=True, production_poll_seconds=d.mock_workflow.poll_seconds,
                test_manual_dispatch_calls=0, test_manual_reply_check_calls=0,
                intercepted_transport_errors=[row for row in browser_requests if row.get("forward_error")],
                exact_authorized_master_resume_verified=True, board_ids_verified_by_unique_roles=True,
                browser_requests=len(browser_requests), scripted_model_calls=len(model_calls))
            report["browser_write_counts"] = dict(write_counts)
            await context.close(); context = None
            await browser.close()
    except Exception:
        report["passed"] = False
        report["error"] = traceback.format_exc()
        checkpoint("failed", error=report["error"])
    finally:
        await d.mock_workflow.stop()
        await d.agent_runtime.shutdown()
        await d.browser_service.shutdown()
        await d.delivery_service.shutdown()
        for item in (getattr(fixture, "context", None), context):
            if item is not None:
                try:
                    await item.close()
                except Exception:
                    pass  # Playwright closes contexts when leaving its manager.
        await api.aclose()
        await site.close()
        report["elapsed_seconds"] = round(time.perf_counter()-started, 3)
        report["source_changed_during_run"] = [label(path) for path in paths
            if hashlib.sha256(path.read_bytes()).hexdigest() != hashes[label(path)]]
        report["violations"] = violations
        if violations or report["source_changed_during_run"]:
            report["passed"] = False
        (output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        for name, rows in (("trace", trace), ("model-requests", model_calls), ("browser-requests", browser_requests)):
            (output / (name+".jsonl")).write_text("".join(json.dumps(row, ensure_ascii=False)+"\n" for row in rows), encoding="utf-8")
    print(json.dumps({"output":str(output), **report}, ensure_ascii=False), flush=True)
    return int(not report["passed"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, choices=[20, 120, 200], default=120)
    parser.add_argument("--origin", default="http://127.0.0.1:3000")
    args = parser.parse_args()
    url = urlsplit(args.origin)
    if url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("Only local frontend assets are allowed")
    raise SystemExit(asyncio.run(run(args.count, args.origin)))
