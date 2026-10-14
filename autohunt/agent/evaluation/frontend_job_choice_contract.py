"""Target choice UI: fully intercepted synthetic APIs, no real Agent requests."""
from __future__ import annotations

import asyncio
import json
import re
import traceback
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright


async def run():
    output = Path(__file__).parent / "results" / "frontend-job-choices" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    posts, errors = [], []
    fixture = {"id": "synthetic-choice-run", "session_id": "default", "kind": "chat", "status": "waiting_user",
        "result": None, "error": None, "can_resume": True, "browser_id": None, "created_at": "2026-01-01T00:00:00Z",
        "plan_steps": [], "target_choices": [{"id": "a"*32, "step_id": "keep", "expected_revision": 2, "selection_mode": "single",
            "options": [{"id": 17, "title": "NLP算法工程师（企业智能体·社招）", "company": "合成星海人工智能", "location": "天津", "salary": "28-33K", "score": 88},
                        {"id": 18, "title": "NLP算法工程师（智能客服·社招）", "company": "合成星海人工智能", "location": "深圳", "salary": "25-35K", "score": 82}]}]}
    result = {"passed": False}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(service_workers="block", viewport={"width": 1440, "height": 1000})
        async def intercept(route):
            target = urlsplit(route.request.url)
            if target.netloc != "127.0.0.1:3000":
                await route.abort(); return
            if not target.path.startswith("/api/"):
                await route.continue_(); return
            status, payload = 503, {"detail": "隔离测试不读取真实接口"}
            if target.path.endswith("/target-choice"):
                status = 200
                posts.append(route.request.post_data_json)
                fixture.update(status="completed", target_choices=[])
                payload = fixture
            elif target.path.endswith("/events"):
                await route.fulfill(content_type="text/event-stream", body='data: {"seq":1,"kind":"heartbeat","data":{}}\n\n'); return
            elif target.path == "/api/agent/runs": status, payload = 200, [fixture]
            elif target.path == "/api/agent/runs/synthetic-choice-run": status, payload = 200, fixture
            elif target.path == "/api/agent/conversations":
                status = 200
                payload = [{"id": "default", "title": "合成会话", "created_at": "2026-01-01T00:00:00Z"}]
            elif target.path in {"/api/agent/chat", "/api/agent/jobs", "/api/agent/resumes", "/api/agent/documents", "/api/agent/materials"}: status, payload = 200, []
            elif target.path in {"/api/agent/browser", "/api/agent/delivery"}: status, payload = 200, {"status": "idle", "detail": "合成空闲"}
            elif target.path == "/api/agent/stats": status, payload = 200, {"review": 0, "applying": 0, "waiting": 0, "interview": 0, "discovered": 0, "stored": 0, "duplicates": 0, "matched": 0}
            elif target.path in {"/api/agent/conversations/default/search-state", "/api/agent/conversations/default/context"}:
                status, payload = 503, {"detail": "隔离测试"}
            await route.fulfill(status=status, content_type="application/json", body=json.dumps(payload, ensure_ascii=False))
        await context.route("**/*", intercept)
        page = await context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            async with page.expect_response(lambda response: urlsplit(response.url).path == "/api/agent/conversations"):
                await page.goto("http://127.0.0.1:3000/", wait_until="domcontentloaded", timeout=60000)
            await page.get_by_role("button", name=re.compile(r"^Agent 对话")).click()
            panel = page.get_by_role("region", name="选择目标岗位")
            await panel.wait_for()
            assert await panel.locator("button.job-choice-card").count() == 2
            await panel.screenshot(path=str(output / "desktop.png"))
            await page.set_viewport_size({"width": 390, "height": 844})
            await panel.screenshot(path=str(output / "mobile.png"))
            assert await panel.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
            async with page.expect_response(lambda response: urlsplit(response.url).path == "/api/agent/conversations"):
                await page.reload(wait_until="domcontentloaded")
            # Tab preference is deliberately not assumed to persist.
            if not await panel.is_visible():
                await page.get_by_role("button", name=re.compile(r"^Agent 对话")).click()
            await panel.wait_for()
            await page.get_by_role("button", name=re.compile(r"^选择2：")).click()
            await page.get_by_role("status").get_by_text("已选定岗位，原任务已接续", exact=True).wait_for()
            assert posts == [{"conversation_id": "default", "choice_id": "a"*32, "step_id": "keep", "expected_revision": 2, "job_ids": [18]}], posts
            assert await panel.count() == 0
            assert not errors, errors
            result = {"passed": True, "posts": posts, "page_errors": errors, "screenshots": ["desktop.png", "mobile.png"],
                      "coverage": "desktop/mobile render, reload persistence, exact-ID POST; synthetic backend only"}
        except Exception:
            result.update(error=traceback.format_exc(), posts=posts, page_errors=errors, body=await page.locator("body").inner_text())
            raise
        finally:
            (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            await browser.close()
    print(json.dumps({"output": str(output), **result}, ensure_ascii=False))


if __name__ == "__main__": asyncio.run(run())
