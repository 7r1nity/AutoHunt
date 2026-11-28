"""Isolated UI acceptance for live-result queries and stop-result delivery.

All business API calls are intercepted with synthetic responses. No real model,
recruitment action, browser profile or existing task is accessed. Requires the
local frontend; run ``python -m evaluation.task_results_browser``.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import async_playwright, expect
from evaluation.job_page_fixture import job_page_response


def check_control_parity():
    """Urgent-command recognition must agree before a request reaches the API."""
    from app.runtime.control import progress_requested, stop_scope
    root = Path(__file__).resolve().parents[2]
    node = shutil.which("node")
    if not node:
        metadata = json.loads((root / ".runlogs/launcher/dependencies.json").read_text(encoding="utf-8"))
        node = metadata["node"]
    commands = ["stop", "STOP", "Stop for now", "stop now", "please stop", "stop all tasks",
        "cancel all", "stop everything", "abort", "terminate work", "stop please", "interrupt",
        "stop current task now", "stop the current task", "stop my current task", "please stop for now and show current results", "stop all work for now",
        "停止", "先停止", "停止每日任务", "中断定时任务", "停止并返回当前结果", "停止并把当前结果给我",
        "停止所有任务并显示本轮结果", "don't stop", "不要停止", "停止功能是什么", '"stop"',
        "他说‘stop’", "stop signal engineer", "why did it stop", "停止后继续搜索", "停止搜索是什么意思",
        "Status?", "How many jobs?", "How's it going?", "How’s it going?", "How many jobs today?",
        "当前结果", "查看当前申请进度", "进度如何", "展示阶段性结果", "今天投递了多少份简历"]
    program = """
const fs = require('fs'), vm = require('vm');
const ts = require(process.argv[2]);
const text = fs.readFileSync(process.argv[1], 'utf8');
const code = ts.transpileModule(text, {compilerOptions: {module: ts.ModuleKind.CommonJS}}).outputText;
const exports = {}; vm.runInNewContext(code, {exports, module: {exports}});
const messages = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(messages.map(message => ({scope: exports.interruptionScope(message), progress: exports.progressCommand(message)}))));
"""
    completed = subprocess.run([node, "-e", program, str(root / "frontend/lib/agent-runtime.ts"),
        str(root / "frontend/node_modules/typescript")], input=json.dumps(commands, ensure_ascii=False),
        capture_output=True, text=True, encoding="utf-8", check=True)
    answers = json.loads(completed.stdout)
    for message, frontend in zip(commands, answers, strict=True):
        assert frontend["scope"] == stop_scope(message), (message, frontend, stop_scope(message))
        if progress_requested(message):
            assert frontend["progress"], (message, frontend)
    return f"frontend_backend_control_semantics_agree_for_{len(commands)}_phrases"


async def exercise(origin: str, output: Path):
    allowed = urlsplit(origin)
    if allowed.scheme != "http" or allowed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Only a local frontend is allowed")
    checks, requests, blocked, errors = [], [], [], []
    histories = {"default": [], "other": []}
    mode = {"run": None, "queries": 0, "query_error": False, "stop_status": "stopped", "stop_polls": 0}
    release_stream = asyncio.Event()
    release_stop = asyncio.Event()
    pending_query, release_query = asyncio.Event(), asyncio.Event()
    mode["delay_query"] = False

    def run_view():
        return {"id": "synthetic-working-run", "session_id": "default", "kind": "daily",
            "status": mode["run"], "result": None, "error": None, "can_resume": False,
            "browser_id": None, "created_at": "2026-01-01T00:00:00Z"}

    def result(message: str, key: str, session="default", *, status=None):
        return {"conversation_id": session, "message": message, "result_message": message,
            "run_id": "control-" + key, "message_id": key, "snapshot_id": "snapshot-" + key,
            "snapshot": {"scope": {"conversation_id": session}, "counts": {"reviewed": 7,
                "qualified": 4, "contacted": 3, "waiting_reply": 2, "submitted": 1,
                "failed": 0, "unknown": 0}, "jobs": [], "as_of": "2026-01-01T00:00:00Z"},
            **({"status": status} if status else {})}

    def persist(session, user, response):
        histories[session] += [
            {"id": len(histories[session]) + 1, "run_id": response["run_id"], "role": "user",
             "content": user, "created_at": "2026-01-01T00:00:00Z"},
            {"id": len(histories[session]) + 2, "run_id": response["run_id"], "role": "assistant",
             "content": response["result_message"], "response": response, "created_at": "2026-01-01T00:00:00Z"}]

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(service_workers="block", viewport={"width": 1400, "height": 900})

        async def intercept(route):
            request, target = route.request, urlsplit(route.request.url)
            if (target.scheme, target.netloc) != (allowed.scheme, allowed.netloc):
                blocked.append({"method": request.method, "host": target.netloc})
                await route.abort()
                return
            if not target.path.startswith("/api/"):
                await route.continue_()
                return
            body = json.loads(request.post_data or "{}")
            requests.append({"method": request.method, "path": target.path, "body": body})
            session = parse_qs(target.query).get("conversation_id", [body.get("conversation_id", "default")])[0]
            payload, code = {}, 200
            if target.path == "/api/agent/runs/control/results" and request.method == "POST":
                assert body.get("request_id") and body.get("message")
                mode["queries"] += 1
                if mode["query_error"]:
                    await route.fulfill(status=503, json={"detail": "合成结果读取暂时失败；原任务仍在执行"})
                    return
                if mode["delay_query"]:
                    pending_query.set()
                    await release_query.wait()
                payload = result(f"合成即时结果 {mode['queries']}：已核对7个，已投递1个，等待HR回复2个；搜索仍在继续。",
                    f"query-{mode['queries']}", session)
                persist(session, body["message"], payload)
            elif target.path == "/api/agent/runs/control/stop":
                if request.method == "POST":
                    assert body.get("request_id") and body.get("message")
                    mode["stop_polls"] = 0
                    mode["stop_message"] = body["message"]
                    mode["stop_session"] = session
                    mode["stop_scope"] = body["scope"]
                else:
                    if mode["stop_status"] == "stopping":
                        await release_stop.wait()
                    mode["stop_polls"] += 1
                if mode["stop_status"] == "stopping" and not mode["stop_polls"]:
                    payload = result("正在停止；当前已投递1个，等待回复2个，尚未确认全部停止。", "pending-stop", session, status="stopping")
                    payload["message_id"] = None
                else:
                    uncertain = mode["stop_status"] == "failed"
                    text = ("停止尚未确认：已核实投递1个，结果未知1个；请人工核验，不会重发。" if uncertain
                            else "合成停止结果：已核实投递1个，已沟通3个；已停止后续执行，已有结果保留。")
                    key = "stop-" + str(sum(item["role"] == "assistant" for item in histories[session]))
                    payload = result(text, key, session, status="failed" if uncertain else "stopped")
                    mode["run"] = "outcome_indeterminate" if uncertain else "cancelled"
                    release_stream.set()
                    persist(session, mode["stop_message"], payload)
            elif target.path == "/api/agent/runs" and request.method == "POST":
                mode["run"] = "running"
                release_stream.clear()
                payload = {**run_view(), "kind": "chat"}
            elif request.method != "GET":
                blocked.append({"method": request.method, "path": target.path})
                await route.abort()
                return
            elif target.path == "/api/agent/runs/synthetic-working-run/events":
                await release_stream.wait()
                await route.fulfill(content_type="text/event-stream", body='data: {"seq":1,"kind":"status","data":{"status":"cancelled","detail":"合成原任务已停止"}}\n\n')
                return
            elif target.path == "/api/agent/runs":
                payload = [run_view()] if mode["run"] and session == "default" else []
            elif target.path == "/api/agent/conversations":
                payload = [{"id": "default", "title": "合成任务会话"}, {"id": "other", "title": "合成空会话"}]
            elif target.path == "/api/agent/chat":
                payload = {"messages": histories[session]}
            elif target.path == "/api/agent/jobs":
                payload = job_page_response([], target.query)
            elif target.path in {"/api/agent/resumes", "/api/agent/materials"}:
                payload = []
            elif target.path in {"/api/agent/browser", "/api/agent/delivery"}:
                payload = {"status": "idle", "detail": "合成浏览器空闲"}
            elif target.path == "/api/agent/stats":
                payload = {"review": 0, "applying": 0, "waiting": 0, "interview": 0, "indeterminate": 0}
            else:
                payload, code = {"detail": "合成 UI 验收：真实业务 API 已隔离"}, 503
            await route.fulfill(status=code, json=payload)

        await context.route("**/*", intercept)
        page = await context.new_page()
        page.on("pageerror", lambda err: errors.append(str(err)))
        message = page.get_by_label("给 Agent 的消息", exact=True)
        send = page.get_by_role("button", name="发送", exact=True)
        stop = page.get_by_role("button", name="停止当前会话任务", exact=True)
        thread = page.get_by_role("log", name="对话消息", exact=True)

        async def open_chat():
            async with page.expect_response(lambda response: urlsplit(response.url).path == "/api/agent/conversations"):
                await page.goto(origin, wait_until="domcontentloaded", timeout=90_000)
            await page.get_by_role("button", name=re.compile(r"^Agent 对话")).click()
            await expect(message).to_be_visible()

        async def query(text):
            index = mode["queries"] + 1
            await message.fill(text)
            await expect(send).to_be_enabled()
            await send.click()
            await expect(thread.get_by_text(f"合成即时结果 {index}：", exact=False)).to_be_visible()

        try:
            await open_chat()
            await message.fill("开始合成搜索")
            await send.click()
            await expect(stop).to_be_visible()
            for text in ("Status?", "How many jobs?", "How's it going?", "当前结果", "查看当前申请进度"):
                await query(text)
                await message.fill("再启动一个新搜索")
                await expect(send).to_be_disabled()
                assert mode["run"] == "running"
            checks.append("english_and_chinese_queries_bypass_busy_chat_without_settling_it")
            for index in range(1, mode["queries"] + 1):
                await expect(page.locator(".message.agent").filter(has_text=f"合成即时结果 {index}：")).to_have_count(1)
            checks.append("http_and_control_notifications_show_each_result_once")
            for text in ("不要停止", "停止功能是什么", "他说‘stop’", "搜索后返回结果"):
                await message.fill(text)
                await expect(send).to_be_disabled()
            assert not [item for item in requests if item["path"].endswith("/control/stop")]
            checks.append("negation_questions_quotes_and_mixed_actions_do_not_stop_or_read")
            mode["query_error"] = True
            await message.fill("进度如何")
            await send.click()
            await expect(thread.get_by_text("合成结果读取暂时失败；原任务仍在执行", exact=True)).to_be_visible()
            mode["query_error"] = False
            await message.fill("继续另一个搜索")
            await expect(send).to_be_disabled()
            checks.append("query_failure_preserves_executing_chat")
            mode["delay_query"] = True
            await message.fill("当前结果")
            await send.click()
            await asyncio.wait_for(pending_query.wait(), 3)
            await page.get_by_label("当前会话", exact=True).select_option("other")
            release_query.set()
            await expect(page.locator(".chat-starters")).to_be_visible()
            await expect(thread).not_to_contain_text("合成即时结果")
            mode["delay_query"] = False
            await page.get_by_label("当前会话", exact=True).select_option("default")
            await expect(thread.get_by_text(f"合成即时结果 {mode['queries']}：", exact=False)).to_be_visible()
            checks.append("inflight_query_is_not_rendered_in_another_session")
            mode["stop_status"] = "stopping"
            await message.fill("Stop for now")
            await message.press("Enter")
            await expect(stop).to_be_disabled()
            await expect(thread.get_by_text("正在停止；当前已投递1个，等待回复2个，尚未确认全部停止。", exact=True)).to_be_visible()
            release_stop.set()
            await expect(thread.get_by_text("合成停止结果：已核实投递1个，已沟通3个；已停止后续执行，已有结果保留。", exact=True)).to_be_visible()
            await expect(stop).to_have_count(0)
            assert mode["stop_scope"] == "session"
            await expect(page.locator(".message.agent").filter(has_text="合成停止结果：")).to_have_count(1)
            checks.append("english_stop_enter_shows_pending_snapshot_then_one_final_result")
            await open_chat()
            await expect(page.locator(".message.agent").filter(has_text="合成停止结果：")).to_have_count(1)
            checks.append("reload_restores_persisted_stop_result_without_duplicates")
            mode["run"], mode["stop_status"] = "waiting_reply", "stopped"
            release_stream.clear()
            await open_chat()
            await expect(stop).to_be_visible()
            await stop.click()
            await expect(page.locator(".message.agent").filter(has_text="合成停止结果：")).to_have_count(2)
            checks.append("stop_button_delivers_result_for_background_hr_wait")
            mode["run"], mode["stop_status"] = "running", "failed"
            release_stream.clear()
            await open_chat()
            await message.fill("stop all tasks")
            await send.click()
            await expect(thread.get_by_text("停止尚未确认：已核实投递1个，结果未知1个；请人工核验，不会重发。", exact=True)).to_be_visible()
            assert mode["stop_scope"] == "all"
            checks.append("all_scope_and_uncertain_stop_keep_evidence_in_chat")
            assert not errors and not blocked, (errors, blocked)
            checks.append("no_page_errors_models_real_api_calls_or_recruitment_actions")
            await page.screenshot(path=str(output / "stop-results.png"))
            return checks, requests
        finally:
            (output / "ui-debug.json").write_text(json.dumps({"checks": checks, "requests": requests,
                "errors": errors, "blocked": blocked, "history_counts": {key: len(value) for key, value in histories.items()},
                "page_text": await page.locator("body").inner_text()}, ensure_ascii=False, indent=2), encoding="utf-8")
            release_stream.set()
            release_stop.set()
            release_query.set()
            await context.close()
            await browser.close()


async def exercise_api(origin: str, output: Path):
    """Real UI -> production HTTP handlers -> temporary stores and fake workers."""
    import httpx
    from cryptography.fernet import Fernet

    allowed = urlsplit(origin)
    checks, requests, errors, blocked = [], [], [], []
    with tempfile.TemporaryDirectory(prefix="autohunt-results-ui-", ignore_cleanup_errors=True) as directory:
        token = "synthetic-results-browser-key"
        os.environ.update(AUTOHUNT_DATA_ROOT=directory, AUTOHUNT_API_KEY=token,
            AUTOHUNT_DATA_KEY=Fernet.generate_key().decode(), AUTOHUNT_LLM_PROVIDER="mock",
            AUTOHUNT_LLM_MODEL="mock-autohunt-v1", AUTOHUNT_BROWSER_HEADLESS="true")
        from app.api import application as api
        from app.runtime.models import public_run
        from evaluation.task_control_contract import ControlHarness

        h = ControlHarness(Path(directory) / "harness")
        other = h.store.create_session("合成隔离会话")["id"]
        seeded = h.seed(suffix="ui-main")
        other_seed = h.seed(other, suffix="ui-other", offset=1000)
        await h.start_worker(seeded)
        api.agent_runtime, api.run_store, api.job_store = h.runtime, h.store, h.jobs
        lock = h.runtime.sessions.setdefault("default", asyncio.Lock())
        await lock.acquire()
        slots = h.runtime.slots._value
        for _ in range(slots):
            await h.runtime.slots.acquire()
        release_stream = asyncio.Event()
        responses = []
        mode = {"query_error": False, "creates": 0}
        before = h.store.get(seeded["run"]["id"])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://testserver",
                headers={"X-AutoHunt-Key": token}) as client, async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(service_workers="block", viewport={"width": 1440, "height": 1000})

            async def intercept(route):
                request, target = route.request, urlsplit(route.request.url)
                if (target.scheme, target.netloc) != (allowed.scheme, allowed.netloc):
                    blocked.append({"method": request.method, "host": target.netloc})
                    await route.abort()
                    return
                if not target.path.startswith("/api/"):
                    await route.continue_()
                    return
                body = json.loads(request.post_data or "{}")
                entry = {"method": request.method, "path": target.path, "body": body}
                requests.append(entry)
                if target.path in {"/api/agent/runs/control/results", "/api/agent/runs/control/stop"}:
                    if target.path.endswith("/results") and mode["query_error"]:
                        await route.fulfill(status=503, json={"detail": "合成查询通道暂时失败；原任务不变"})
                        return
                    backend_path = target.path.replace("/api/agent/", "/api/v1/")
                    started = time.monotonic()
                    response = await asyncio.wait_for(client.request(request.method, backend_path +
                        ("?" + target.query if target.query else ""), json=body if request.method == "POST" else None), 3)
                    payload = response.json()
                    entry.update(status=response.status_code, elapsed_ms=round((time.monotonic() - started) * 1000),
                        message_id=payload.get("message_id"), counts=(payload.get("snapshot") or {}).get("counts"))
                    responses.append(payload)
                    await route.fulfill(status=response.status_code, json=payload)
                    if target.path.endswith("/stop") and payload.get("status") == "stopped":
                        release_stream.set()
                    return
                if target.path == "/api/agent/runs" and request.method == "POST":
                    # Only stand in for an already-running foreground request.
                    # No model or production submit() is entered by this fixture.
                    mode["creates"] += 1
                    await route.fulfill(json=public_run(h.store.get(seeded["run"]["id"])))
                    return
                if request.method != "GET":
                    blocked.append({"method": request.method, "path": target.path})
                    await route.abort()
                    return
                session = parse_qs(target.query).get("conversation_id", ["default"])[0]
                if target.path == "/api/agent/runs":
                    payload = [public_run(run) for run in h.store.list(session)]
                elif target.path.endswith("/events") and target.path.startswith("/api/agent/runs/"):
                    await release_stream.wait()
                    run_id = target.path.split("/")[4]
                    events = h.store.public_events(run_id)
                    current = h.store.get(run_id)
                    if not events or events[-1]["kind"] != "status":
                        events += [{"seq": max([e["seq"] for e in events], default=0) + 1,
                                    "kind": "status", "data": {"status": current["status"], "detail": "合成执行器已停止"}}]
                    await route.fulfill(content_type="text/event-stream", body="".join(
                        "data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events))
                    return
                elif target.path == "/api/agent/conversations":
                    payload = h.store.sessions()
                elif target.path == "/api/agent/chat":
                    response = await client.get("/api/v1/conversations/current", params={"conversation_id": session})
                    await route.fulfill(status=response.status_code, json=response.json())
                    return
                elif target.path == "/api/agent/jobs":
                    payload = job_page_response([], target.query)
                elif target.path in {"/api/agent/resumes", "/api/agent/materials"}:
                    payload = []
                elif target.path in {"/api/agent/browser", "/api/agent/delivery"}:
                    payload = {"status": "idle", "detail": "合成UI状态"}
                elif target.path == "/api/agent/stats":
                    payload = {"review": 0, "applying": 0, "waiting": 0, "interview": 0, "indeterminate": 0}
                else:
                    await route.fulfill(status=503, json={"detail": "未声明的UI接口已隔离"})
                    return
                await route.fulfill(json=payload)

            await context.route("**/*", intercept)
            page = await context.new_page()
            page.on("pageerror", lambda err: errors.append(str(err)))
            message = page.get_by_label("给 Agent 的消息", exact=True)
            send = page.get_by_role("button", name="发送", exact=True)
            stop = page.get_by_role("button", name="停止当前会话任务", exact=True)
            thread = page.get_by_role("log", name="对话消息", exact=True)

            async def open_chat():
                async with page.expect_response(lambda r: urlsplit(r.url).path == "/api/agent/conversations"):
                    await page.goto(origin, wait_until="domcontentloaded")
                await page.get_by_role("button", name=re.compile(r"^Agent 对话")).click()
                await expect(message).to_be_visible()

            try:
                await open_chat()
                await message.fill("执行中的合成原请求")
                await send.click()
                await expect(stop).to_be_visible()
                for text in ("Status?", "当前结果"):
                    await message.fill(text)
                    await expect(send).to_be_enabled()
                    count = len(responses)
                    await send.click()
                    await expect(thread.get_by_role("link", name=seeded["titles"][0], exact=True)).to_have_count(count + 1)
                    reply = responses[-1]
                    assert reply["snapshot"]["counts"]["reviewed"] == 4, reply
                    assert reply["snapshot"]["counts"]["submitted"] == 1
                    assert reply["snapshot"]["counts"]["uncertain"] == 1
                    assert not any(title in reply["message"] for title in other_seed["titles"])
                    assert h.store.get(seeded["run"]["id"])["checkpoint"] == before["checkpoint"]
                    assert not h.browser.worker.done()
                    await message.fill("再发起一个搜索")
                    await expect(send).to_be_disabled()
                checks.append("busy_ui_queries_use_real_api_and_saved_receipts_under_held_locks")
                checks.append("current_scope_counts_unknowns_and_job_links_are_not_other_session_data")
                checks.append("query_responses_and_http_notifications_do_not_clear_busy_run_or_duplicate")
                mode["query_error"] = True
                await message.fill("进度如何")
                await send.click()
                await expect(thread.get_by_text("合成查询通道暂时失败；原任务不变", exact=True)).to_be_visible()
                mode["query_error"] = False
                await message.fill("发起另一个新任务")
                await expect(send).to_be_disabled()
                assert not h.browser.worker.done()
                checks.append("query_transport_failure_keeps_real_original_worker_running")
                await message.fill("Stop for now")
                await message.press("Enter")
                await expect(thread.get_by_text("停止后的当前结果", exact=True)).to_be_visible()
                await expect(stop).to_have_count(0)
                final = next(item for item in reversed(responses) if item.get("status") == "stopped")
                assert final["snapshot"]["counts"]["submitted"] == 1
                assert h.jobs.get_task(seeded["task_id"]).status == "stopped"
                assert not h.store.get(other_seed["run"]["id"])["cancel_requested"]
                checks.append("english_stop_stops_fake_executor_via_real_api_and_returns_partial_results")
                assistant = [item for item in h.jobs.conversation_history(limit=100)
                    if item["role"] == "assistant" and (item.get("response") or {}).get("message_id") == final["message_id"]]
                assert len(assistant) == 1
                await message.fill("stop")
                await send.click()
                await expect(thread.get_by_text("停止后的当前结果", exact=True)).to_have_count(1)
                checks.append("repeated_stop_keeps_one_durable_result_after_http_and_sse_delivery")
                await open_chat()
                await expect(thread.get_by_text("停止后的当前结果", exact=True)).to_have_count(1)
                checks.append("real_persisted_stop_history_restores_once_on_reload")
                for suffix, command, offset in (("ui-chinese-stop", "停止并返回当前结果", 2000),
                                                ("ui-stop-now", "stop now", 3000)):
                    seeded = h.seed(suffix=suffix, offset=offset)
                    await h.start_worker(seeded)
                    release_stream.clear()
                    await open_chat()
                    await message.fill("执行中的合成原请求")
                    await send.click()
                    await expect(stop).to_be_visible()
                    await message.fill(command)
                    await expect(send).to_be_enabled()
                    await message.press("Enter")
                    await expect(thread.get_by_role("link", name=seeded["titles"][0], exact=True)).to_have_count(1)
                    assert h.jobs.get_task(seeded["task_id"]).status == "stopped"
                    await expect(stop).to_have_count(0)
                checks.append("busy_chinese_stop_with_results_and_stop_now_share_real_control_lane")
                assert h.model_calls == 0 and mode["creates"] == 3
                assert not errors and not blocked, (errors, blocked)
                checks.append("no_model_admission_real_browser_operations_or_existing_data")
                await page.screenshot(path=str(output / "api-stop-results.png"))
                return checks, requests
            finally:
                (output / "api-debug.json").write_text(json.dumps({"checks": checks, "requests": requests,
                    "errors": errors, "blocked": blocked, "page_text": await page.locator("body").inner_text()},
                    ensure_ascii=False, indent=2), encoding="utf-8")
                release_stream.set()
                await context.close()
                await browser.close()
                lock.release()
                for _ in range(slots):
                    h.runtime.slots.release()
                await h.close()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--origin", default="http://127.0.0.1:3000")
    parser.add_argument("--mode", choices=("synthetic", "api", "both"), default="both")
    args = parser.parse_args()
    output = Path(__file__).parent / "results" / "task-results-browser" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True, exist_ok=True)
    result = {"passed": False, "model_calls": 0, "real_recruitment_actions": 0}
    try:
        checks, requests = [check_control_parity()], []
        if args.mode in {"synthetic", "both"}:
            component_checks, component_requests = await exercise(args.origin, output)
            checks += component_checks
            requests += component_requests
        if args.mode in {"api", "both"}:
            api_checks, api_requests = await exercise_api(args.origin, output)
            checks += api_checks
            requests += api_requests
        result.update(passed=True, checks=checks, intercepted_requests=requests)
    except BaseException:
        result["error"] = traceback.format_exc()
        raise
    finally:
        (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"output": str(output), **{key: value for key, value in result.items() if key != "intercepted_requests"}}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
