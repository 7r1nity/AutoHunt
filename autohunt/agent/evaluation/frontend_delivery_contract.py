"""One-click UI wire contract: synthetic APIs, fresh browser, no live Agent calls."""
from __future__ import annotations

import asyncio
import json
import re
import traceback
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright


async def run():
    posts, errors = [], []
    output = Path(__file__).parent / "results" / "frontend-delivery" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True, exist_ok=True)
    result = {"passed": False}
    job = {"id": 133, "role": "合成单岗位投递测试", "company": "合成雇主", "location": "深圳", "salary": "15-20K",
           "source": "MockJobSite", "source_url": "http://127.0.0.1:8080/job.html?id=133", "score": 88,
           "stage": "review", "mode": "人工确认", "tags": [], "reason": "合成匹配", "posted": "合成时间", "filter_status": "pass"}
    uncertain = {**job, "id": 134, "role": "合成待核验岗位", "stage": "outcome_indeterminate",
                 "mode": "需要人工核验", "reason": "发送结果不确定，不可重试"}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(service_workers="block")
            async def intercept(route):
                target = urlsplit(route.request.url)
                if target.netloc != "127.0.0.1:3000":
                    await route.abort(); return
                if not target.path.startswith("/api/"):
                    await route.continue_(); return
                status, payload = 200, []
                if target.path == "/api/agent/jobs":
                    if route.request.method == "POST":
                        posts.append(route.request.post_data_json)
                        payload = {"queued": 1, "job_ids": [133], "message": "合成岗位已入队"}
                    else:
                        payload = [job, uncertain]
                elif target.path == "/api/agent/conversations":
                    payload = [{"id": "default", "title": "合成会话", "created_at": "2026-01-01T00:00:00Z"}]
                elif target.path.startswith("/api/agent/runs") or target.path in {"/api/agent/chat", "/api/agent/resumes", "/api/agent/documents", "/api/agent/materials"}:
                    payload = []
                elif target.path == "/api/agent/browser" or target.path == "/api/agent/delivery":
                    payload = {"status": "idle", "detail": "合成空闲"}
                elif target.path == "/api/agent/stats":
                    payload = {"review": 1, "applying": 0, "waiting": 0, "interview": 0, "discovered": 1, "stored": 1, "duplicates": 0, "matched": 1}
                else:
                    status, payload = 503, {"detail": "隔离测试不读取真实接口"}
                await route.fulfill(status=status, content_type="application/json", body=json.dumps(payload, ensure_ascii=False))
            await context.route("**/*", intercept)
            page = await context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto("http://127.0.0.1:3000/", wait_until="domcontentloaded", timeout=60000)
            # The SSR shell is already clickable before React has attached handlers.
            # Await a synthetic API-driven render, not an arbitrary sleep.
            await page.get_by_text("已保存 2 个职位", exact=True).wait_for()
            await page.get_by_role("button", name=re.compile(r"^申请看板")).click()
            assert await page.locator(".kanban-column").count() == 4
            tracks = await page.locator(".kanban").evaluate("el => getComputedStyle(el).gridTemplateColumns.split(' ').length")
            assert tracks == 4, tracks
            review = page.locator(".kanban-column").first
            assert await review.locator(".job-card").count() == 2
            await page.get_by_role("heading", name=job["role"], exact=True).click()
            await page.get_by_role("button", name="投递", exact=True).click()
            await page.get_by_text("合成岗位已入队").wait_for()
            assert posts == [{"action": "deliver", "id": 133, "conversation_id": "default"}], posts
            assert await page.get_by_role("dialog", name="投递预览").count() == 0
            await page.get_by_role("heading", name=uncertain["role"], exact=True).click()
            modal = page.get_by_role("dialog", name="职位详情")
            assert await modal.get_by_role("button", name="投递", exact=True).count() == 0
            assert "结果不确定" in await modal.inner_text()
            assert not errors, errors
            result = {"passed": True, "posts": posts, "preview_dialogs": 0, "page_errors": errors}
        except Exception:
            result.update(error=traceback.format_exc(), posts=posts, page_errors=errors,
                          body=await page.locator("body").inner_text() if 'page' in locals() else "")
            raise
        finally:
            (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            await browser.close()
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), **result}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(run())
