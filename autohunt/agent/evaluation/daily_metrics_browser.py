"""Read-only frontend regression: local routes, unknown usage, daily navigation.

Uses synthetic API receipts, blocks API writes and all external hosts. Requires
the existing frontend on port 3000. Does not run a model/search/application.
Run from agent/: python -m evaluation.daily_metrics_browser
"""
import asyncio
import json
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect

from app.runtime.ledger import execution_ledger


def fixtures():
    ids = ["local", "legacy", "missing", "mixed", "daily"]
    runs = [{"id": ident, "session_id": "default", "kind": "daily" if ident == "daily" else "chat",
             "status": "completed", "error": None, "result": None, "plan_steps": [], "approvals": [],
             "target_choices": [], "can_resume": False, "browser_id": "search" if ident == "daily" else None,
             "created_at": "2026-11-15T09:00:00Z" if ident == "daily" else "2026-11-15T09:05:00Z",
             "updated_at": "2026-11-15T09:06:00Z"} for ident in ids]
    ledgers = {}
    for run in runs:
        ident = run["id"]
        events = []
        if ident in {"local", "mixed"}:
            events.append({"seq": 1, "run_id": ident, "kind": "execution_path", "data": {
                "mode": "local_rule", "reason": "search_summary", "source_browser_id": "search",
                "detail": "读取已保存的搜索结果，无需模型推理"}})
            events.append({"seq": 2, "run_id": ident, "kind": "tool_finished", "data": {
                "tool": "job_results_read", "execution_id": "read", "status": "ok", "duration_ms": 2, "attempts": 1}})
        if ident in {"missing", "mixed"}:
            events.append({"seq": 3, "run_id": ident, "kind": "model_request", "data": {"call_id": "incomplete-call"}})
        if ident == "daily":
            for i in range(31):
                events.append({"seq": i, "run_id": ident, "kind": "model_response", "data": {
                    "call_id": str(i), "status": "ok", "latency_ms": 10,
                    "usage": {"prompt_tokens": 100, "completion_tokens": 50}}})
            for i in range(13):
                events.append({"seq": 100 + i, "run_id": ident, "kind": "tool_finished", "data": {
                    "tool": "synthetic_tool", "execution_id": str(i), "status": "ok", "duration_ms": 10, "attempts": 1}})
        ledgers[ident] = execution_ledger([run], events)
    return runs, ledgers


async def main():
    runs, ledgers = fixtures()
    errors, writes, checks = [], [], []
    notification_mode, poll_runs, streams = False, [], []
    hydrated = asyncio.Event()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(service_workers="block")
        async def intercept(route):
            request = route.request
            url = urlsplit(request.url)
            if url.netloc != "127.0.0.1:3000":
                await route.abort()
            elif request.method not in {"GET", "HEAD"}:
                writes.append(url.path)
                await route.abort()
            elif url.path == "/api/agent/runs":
                await route.fulfill(json=poll_runs if notification_mode else runs)
                hydrated.set()
            elif url.path.endswith("/events") and notification_mode:
                streams.append(url.path)
                events = [
                    {"seq": 1, "kind": "browser_summary", "data": {"message": "合成每日任务搜索总结：30 个候选，选中 5 个"}},
                    {"seq": 2, "kind": "workflow_summary", "data": {"message": "合成每日任务最终结果：处理 5 个岗位"}},
                    {"seq": 3, "kind": "status", "data": {"status": "completed"}},
                ]
                await route.fulfill(content_type="text/event-stream", body="".join(
                    "data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events))
            elif url.path == "/api/agent/chat":
                await route.fulfill(json={"messages": []})
            elif url.path.startswith("/api/agent/runs/") and url.path.endswith("/ledger"):
                await route.fulfill(json=ledgers[url.path.split("/")[-2]])
            else:
                await route.continue_()
        await context.route("**/*", intercept)
        page = await context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            await page.goto("http://127.0.0.1:3000/", wait_until="domcontentloaded")
            await asyncio.wait_for(hydrated.wait(), timeout=20)
            await page.get_by_role("button", name="验收与指标", exact=True).click()
            panel = page.locator(".metrics-dashboard")
            strip = panel.get_by_label("本轮执行指标")
            await expect(strip.locator("dd").nth(2)).to_have_text("0 / 0")
            await expect(strip.locator("dd").nth(1)).to_have_text("1")
            await expect(strip.locator("dd").nth(4)).to_have_text("0 ms")
            checks.append("confirmed_local_route_zero_usage")
            await panel.get_by_role("button", name="查看对应每日任务指标", exact=True).click()
            await expect(strip.locator("dd").nth(0)).to_have_text("31")
            await expect(strip.locator("dd").nth(1)).to_have_text("13")
            await expect(strip.locator("dd").nth(2)).to_have_text("3,100 / 1,550")
            checks.append("associated_daily_run_has_separate_accounting")
            await panel.get_by_label("执行轮次", exact=True).select_option("legacy")
            await expect(strip.locator("dd").nth(2)).to_have_text("无调用记录")
            await expect(panel.locator(".metrics-data-note")).to_have_count(0)
            checks.append("legacy_unknown_not_fabricated_zero")
            for ident in ("missing", "mixed"):
                await panel.get_by_label("执行轮次", exact=True).select_option(ident)
                await expect(strip.locator("dd").nth(2)).to_have_text("未记录")
                await expect(panel.locator(".metrics-data-note")).to_contain_text("token 数据不完整")
                checks.append(ident + "_usage_remains_unknown")
            notification_mode = True
            hydrated.clear()
            await page.clock.install()
            await page.reload(wait_until="networkidle")
            await asyncio.wait_for(hydrated.wait(), timeout=20)
            await page.get_by_role("button", name="Agent 对话", exact=True).click()
            poll_runs.append({**runs[-1], "id": "fast-daily"})
            await page.clock.fast_forward(21_000)
            await expect(page.get_by_text("合成每日任务搜索总结：30 个候选，选中 5 个", exact=True)).to_be_visible()
            await expect(page.get_by_text("合成每日任务最终结果：处理 5 个岗位", exact=True)).to_be_visible()
            await page.clock.fast_forward(21_000)
            await expect(page.get_by_text("合成每日任务搜索总结：30 个候选，选中 5 个", exact=True)).to_have_count(1)
            assert len(streams) == 1, streams
            checks.append("fast_daily_completion_reaches_chat_once_without_reload")
            assert not errors, errors
            assert not writes, writes
            print(json.dumps({"passed": True, "checks": checks, "page_errors": errors,
                              "api_writes": writes, "external_model_calls": 0}))
        finally:
            await context.close()
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
