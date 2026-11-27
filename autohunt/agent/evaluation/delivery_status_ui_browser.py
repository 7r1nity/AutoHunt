"""Replay real isolated contract traces through the local AutoHunt chat UI.

Only frontend static assets reach the local server. Every business API request
is intercepted; browser contexts are temporary and have no login/profile data.
No model, recruitment browser or real task is accessed. Run after the two
offline contracts: ``python -m evaluation.delivery_status_ui_browser``.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import re
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect


RESULTS = Path(__file__).parent / "results"


def load_case(family: str, suffix: str):
    for directory in sorted((RESULTS / family).iterdir(), reverse=True):
        path = directory / "traces.jsonl"
        if not path.exists() or not (directory / "summary.json").exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row.get("test", "").endswith(suffix):
                return row, str(path)
    raise FileNotFoundError(f"Run the offline {family} contract first: {suffix}")


def source_events(row, *kinds):
    return [copy.deepcopy(event) for event in row["events"] if event["kind"] in kinds]


async def replay(browser, origin: str, output: Path, name: str, command: str,
                 initial: list[dict], probes: list[str], *, updates=None,
                 update_probes=None, waiting_status="waiting_user"):
    allowed = urlsplit(origin)
    context = await browser.new_context(service_workers="block", viewport={"width": 1400, "height": 1000})
    run_id = "ui-replay-" + name
    state = {"created": False, "status": "running", "polls": 0, "history_reads": 0,
             "seq": 0, "generation": 0, "reload": False}
    history, events, requests, violations, errors, checks = [], [], [], [], [], []
    changed = asyncio.Event()
    history_ready = asyncio.Event()
    started = time.monotonic()

    def run_view():
        return {"id": run_id, "session_id": "default", "kind": "chat", "status": state["status"],
                "result": None, "error": None, "can_resume": False, "browser_id": None,
                "created_at": "2026-01-01T00:00:00Z"}

    def publish(items, *, persist=True):
        for original in items:
            event = copy.deepcopy(original)
            state["seq"] += 1
            event["seq"] = state["seq"]
            event["run_id"] = run_id
            events.append(event)
            if not persist or event["kind"] not in {"response", "workflow_summary", "browser_summary"}:
                continue
            data = event["data"]
            response = data.get("result") or {"message": data.get("message", "")}
            text = response.get("message") or data.get("message", "")
            message_id = response.get("message_id")
            if any(item.get("response", {}).get("message_id") == message_id if message_id
                   else item["content"] == text for item in history):
                continue
            history.append({"id": len(history) + 1, "run_id": run_id, "role": "assistant", "content": text,
                            "response": response, "created_at": "2026-01-01T00:00:00Z"})
        changed.set()

    async def intercept(route):
        request, target = route.request, urlsplit(route.request.url)
        if (target.scheme, target.netloc) != (allowed.scheme, allowed.netloc):
            violations.append({"method": request.method, "host": target.netloc})
            await route.abort()
            return
        if not target.path.startswith("/api/"):
            if request.method != "GET":
                violations.append({"method": request.method, "path": target.path})
                await route.abort()
            else:
                await route.continue_()
            return
        body = json.loads(request.post_data or "{}")
        requests.append({"method": request.method, "path": target.path, "query": target.query,
                         **({"body": body} if request.method != "GET" else {})})
        payload, code = {}, 200
        if target.path == "/api/agent/runs" and request.method == "POST":
            assert not state["created"], "A replay must create exactly one synthetic task"
            assert body.get("message") == command and body.get("kind") == "chat", body
            state["created"] = True
            payload = run_view()
            history.append({"id": 1, "run_id": run_id, "role": "user", "content": command,
                            "created_at": "2026-01-01T00:00:00Z"})
            if initial and initial[0]["kind"] == "response":
                publish(initial)
            else:
                # The foreground planner has already returned before its
                # asynchronous search/dependent delivery results are observed.
                publish([{"kind": "response", "data": {"result": {"message": "已启动本轮合成任务，后续投递等待搜索完成。"}}}])
                publish(initial)
            state["status"] = waiting_status
        elif request.method != "GET":
            violations.append({"method": request.method, "path": target.path})
            await route.abort()
            return
        elif target.path == f"/api/agent/runs/{run_id}/events":
            state["polls"] += 1
            if state["reload"]:
                await history_ready.wait()
                await asyncio.sleep(0.15)
            # Send the complete synthetic log on every reconnect, including
            # already seen sequence numbers. The real client must deduplicate.
            if not events:
                await changed.wait()
            await route.fulfill(content_type="text/event-stream", body="".join(
                "data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events))
            return
        elif target.path == f"/api/agent/runs/{run_id}":
            payload = run_view()
        elif target.path == "/api/agent/runs":
            payload = [run_view()] if state["created"] else []
        elif target.path == "/api/agent/conversations":
            payload = [{"id": "default", "title": "隔离投递结果回放"}]
        elif target.path == "/api/agent/chat":
            state["history_reads"] += 1
            payload = {"messages": copy.deepcopy(history)}
            history_ready.set()
        elif target.path.endswith("/search-state"):
            payload = {"queries": [], "locations": [], "result_limit": None, "salary_min": None,
                       "query_source": "synthetic", "revision": 1}
        elif target.path in {"/api/agent/jobs", "/api/agent/resumes", "/api/agent/materials"}:
            payload = []
        elif target.path in {"/api/agent/browser", "/api/agent/delivery"}:
            payload = {"status": "idle", "detail": "隔离回放，不访问招聘浏览器"}
        elif target.path == "/api/agent/stats":
            payload = {"review": 0, "applying": 0, "waiting": 0, "interview": 0, "indeterminate": 0}
        else:
            payload, code = {"detail": "隔离 UI 回放：业务 API 已全部拦截"}, 503
        await route.fulfill(status=code, json=payload)

    await context.route("**/*", intercept)
    page = await context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    message = page.get_by_label("给 Agent 的消息", exact=True)
    thread = page.get_by_role("log", name="对话消息", exact=True)

    async def open_chat():
        async with page.expect_response(lambda response: urlsplit(response.url).path == "/api/agent/conversations"):
            await page.goto(origin, wait_until="domcontentloaded", timeout=90_000)
        await page.get_by_role("button", name=re.compile(r"^Agent 对话")).click()
        await expect(message).to_be_visible()

    async def once(phrases):
        for phrase in phrases:
            await expect(thread.locator(".message.agent").filter(has_text=phrase)).to_have_count(1)
            await expect(thread.locator(".message.agent").filter(has_text=phrase)).to_be_visible()

    async def polls_at_least(number):
        deadline = time.monotonic() + 12
        while state["polls"] < number and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert state["polls"] >= number, state

    try:
        await open_chat()
        await message.fill(command)
        await page.get_by_role("button", name="发送", exact=True).click()
        await once(probes)
        checks.append("user_command_to_real_trace_chat")
        assert state["status"] == waiting_status
        checks.append("wait_keeps_nonterminal_run_status")
        await polls_at_least(2)
        await once(probes)
        checks.append("repeated_sse_poll_does_not_duplicate_chat")
        # A duplicate payload with a new sequence number must be deduplicated
        # by its durable message identity, rather than only the SSE cursor.
        progress = [event for event in initial if event["kind"] == "workflow_summary"]
        if progress:
            before = state["polls"]
            publish(progress, persist=False)
            await polls_at_least(before + 1)
            await once(probes)
            checks.append("new_sequence_same_message_identity_is_not_duplicated")
        history_ready.clear()
        state["reload"] = True
        before = state["polls"]
        await open_chat()
        await polls_at_least(before + 1)
        await once(probes)
        checks.append("persisted_history_and_replayed_events_survive_reload_once")
        assert state["history_reads"] >= 2
        if updates:
            before = state["polls"]
            publish(updates)
            await polls_at_least(before + 1)
            await once(update_probes or [])
            await once(probes)
            checks.append("changed_receipt_or_completion_delivers_new_visible_message")
            state["status"] = "completed"
            publish([{"kind": "status", "data": {"status": "completed", "detail": "隔离回放结束"}}], persist=False)
            await expect(page.get_by_role("button", name="停止当前会话任务", exact=True)).to_have_count(0)
            await once(update_probes or [])
            checks.append("terminal_update_preserves_result_chat")
        assert len([request for request in requests if request["method"] == "POST"]) == 1
        assert not violations and not errors, (violations, errors)
        checks.append("no_real_business_api_external_host_model_or_browser_profile")
        return {"name": name, "passed": True, "checks": checks, "elapsed_ms": round((time.monotonic() - started) * 1000),
                "polls": state["polls"], "history_reads": state["history_reads"], "messages": len(history),
                "intercepted_requests": len(requests)}
    except BaseException:
        await page.screenshot(path=str(output / f"{name}-failure.png"))
        raise
    finally:
        (output / f"{name}.json").write_text(json.dumps({"checks": checks, "state": state,
            "requests": requests, "violations": violations, "page_errors": errors,
            "page_text": await page.locator("body").inner_text()}, ensure_ascii=False, indent=2), encoding="utf-8")
        await context.close()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--origin", default="http://127.0.0.1:3000")
    args = parser.parse_args()
    target = urlsplit(args.origin)
    if target.scheme != "http" or target.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Only a local frontend is allowed")
    output = RESULTS / "delivery-status-ui" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True, exist_ok=True)
    result = {"passed": False, "model_calls": 0, "real_recruitment_actions": 0, "cases": [], "source_traces": []}
    definitions = [
        ("limit-twenty", "delivery-request", "test_command_twenty_limits_real_queue_to_twenty", "投递20个",
         ["实际入队20个", "数量已达到本次上限"], None, None),
        ("prefix-shortfall", "delivery-request", "test_prefix_twenty_with_one_result_queues_one_and_reports_deficit", "投递前20个",
         ["实际入队1个；尚差19个"], None, None),
        ("input-after-search", "workflow-wait-delivery", "test_search_finishes_then_added_delivery_input_wait_reaches_chat", "搜索后投递20个",
         ["本轮只有1个可选岗位，请确认所指岗位", "尚差 19 个"], None, None),
        ("approval-to-queue", "workflow-wait-delivery", "test_approval_completed_in_background_reaches_final_chat_once", "投递20个",
         ["确认本轮选择的合成岗位"], "same", ["本轮1个岗位已入队，缺额19个"]),
        ("approval-reason", "workflow-wait-delivery", "test_pending_approval_reason_is_visible_without_marking_finished", "投递20个",
         ["范围变化：请确认本轮深圳岗位，不沿用旧薪资上限"], None, None),
        ("login-to-failure", "workflow-wait-delivery", "test_search_verification_wait_is_visible_and_not_reported_complete", "搜索后投递20个",
         ["职位搜索等待人工处理：请完成验证码"], "failure", ["合成投递准备失败：账号验证未通过"]),
        ("binding-to-failure", "workflow-wait-delivery", "test_pending_binding_reports_reason_while_preserving_wait", "投递20个",
         ["共享浏览器暂被另一只读任务使用，等待释放后绑定账号"], "failure", ["合成投递准备失败：账号验证未通过"]),
        ("changed-wait-reason", "workflow-wait-delivery", "test_changed_wait_reason_gets_a_new_receipt_once", "投递20个",
         ["请确认第一轮候选"], "same", ["该岗位已下架，请选择其他岗位"]),
        ("failure-after-search", "workflow-wait-delivery", "test_failed_delivery_after_search_is_not_only_a_search_report", "搜索后投递20个",
         ["合成投递准备失败：账号验证未通过", "尚差 19 个"], None, None),
    ]
    try:
        failure, path = load_case("workflow-wait-delivery", "test_failed_delivery_after_search_is_not_only_a_search_report")
        failure_events = source_events(failure, "workflow_summary")
        result["source_traces"].append(path)
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                for name, family, suffix, command, probes, update, update_probes in definitions:
                    row, path = load_case(family, suffix)
                    result["source_traces"].append(path)
                    items = source_events(row, "response", "workflow_summary", "browser_summary")
                    updates = None
                    if update == "same":
                        workflow = [event for event in items if event["kind"] == "workflow_summary"]
                        assert len(workflow) == 2, suffix
                        items, updates = [workflow[0]], [workflow[1]]
                    elif update == "failure":
                        updates = failure_events
                    case = await replay(browser, args.origin, output, name, command, items, probes,
                                        updates=updates, update_probes=update_probes,
                                        waiting_status="waiting_confirmation" if "approval" in name else "waiting_user")
                    result["cases"].append(case)
                    print(json.dumps({"case": name, "passed": True, "checks": len(case["checks"])}, ensure_ascii=False), flush=True)
            finally:
                await browser.close()
        result["passed"] = True
    except BaseException:
        result["error"] = traceback.format_exc()
        raise
    finally:
        result["source_traces"] = sorted(set(result["source_traces"]))
        result["checks_passed"] = sum(len(case["checks"]) for case in result["cases"])
        (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"output": str(output), "passed": result["passed"], "cases_passed": len(result["cases"]),
                          "checks_passed": result["checks_passed"], "model_calls": 0, "real_recruitment_actions": 0}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
