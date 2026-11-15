"""Read-only browser regression against a running local frontend.

Injects synthetic legacy run responses; blocks all API writes and external hosts.
No task creation, model request, search, communication or submission is performed.
Run from agent/: python -m evaluation.runtime_panel_browser
"""
import asyncio
import json
from urllib.parse import urlsplit

from playwright.async_api import async_playwright


async def main():
    steps = [
        {"id": "null", "tool": "synthetic_null", "status": "completed", "depends_on": None},
        {"id": "missing", "tool": "synthetic_missing", "status": "completed"},
        {"id": "empty", "tool": "synthetic_empty", "status": "completed", "depends_on": []},
        {"id": "linked", "tool": "synthetic_linked", "status": "completed", "depends_on": ["search"]},
    ]
    run = {"id": "synthetic-runtime-panel", "session_id": "default", "kind": "daily",
           "status": "completed", "result": None, "error": None, "can_resume": False,
           "browser_id": None, "created_at": "2026-11-15T00:00:00Z", "plan_steps": steps,
           "daily": {"trigger": "manual"}, "approvals": [], "target_choices": []}
    errors, writes = [], []
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
                await route.fulfill(json=[run])
                hydrated.set()
            else:
                await route.continue_()
        await context.route("**/*", intercept)
        page = await context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: errors.append(message.text) if message.type == "error"
                and ("TypeError" in message.text or "Cannot read properties" in message.text) else None)
        try:
            await page.goto("http://127.0.0.1:3000/", wait_until="domcontentloaded")
            # Wait for React's client effect before clicking server-rendered HTML.
            await asyncio.wait_for(hydrated.wait(), timeout=15)
            await page.get_by_role("button", name="Agent 对话", exact=True).click()
            await page.get_by_role("button", name="任务与上下文", exact=True).click()
            panel = page.locator(".runtime-panel")
            await panel.wait_for(timeout=15000)
            await panel.get_by_text("运行记录", exact=False).click()
            await panel.get_by_text("synthetic_linked · 完成 · 依赖 search", exact=True).wait_for(timeout=10000)
            assert await panel.locator(".runtime-run ol li").count() == 4
            assert not errors, errors
            assert not writes, writes
            print(json.dumps({"passed": True, "dependency_cases": ["null", "missing", "empty", "linked"],
                              "page_errors": errors, "api_writes": writes, "external_model_calls": 0}))
        except BaseException:
            print(json.dumps({"passed": False, "page_errors": errors, "api_writes": writes}))
            raise
        finally:
            await context.close()
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
