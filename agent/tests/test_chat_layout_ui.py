"""Opt-in layout checks; all API calls are intercepted, never real searches."""
import os
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.sync_api import expect, sync_playwright


pytestmark = pytest.mark.skipif(os.getenv("AUTOHUNT_CHAT_UI") != "1", reason="Requires frontend UI")


@pytest.mark.parametrize("width,height", [(1600, 900), (1024, 768), (390, 844)])
def test_chat_uses_available_space_and_inspector_does_not_shrink_height(width, height):
    errors = []
    conditions = {"queries": [], "locations": [], "query_source": "resume", "result_limit": None,
                  "salary_min": None, "revision": 0}
    history = {"messages": [
        {"role": "user", "content": "帮我找工作", "created_at": "2026-10-07T04:05:00Z"},
        {"role": "assistant", "content": "你还没有上传或启用简历，本会话也没有明确的搜索岗位，因此我还没有启动搜索。请先上传简历，或告诉我想找什么岗位；没有简历也可以按你指定的岗位搜索。", "created_at": "2026-10-07T04:05:01Z"},
    ]}

    def api(route):
        assert route.request.method == "GET", "Layout tests must not mutate business data"
        path = urlparse(route.request.url).path
        if path.endswith("/conversations"):
            data = [{"id": "default", "title": "默认会话"}]
        elif path.endswith(("/jobs", "/resumes", "/materials", "/runs")):
            data = []
        elif path.endswith("/chat"):
            data = history
        elif path.endswith("/search-state"):
            data = conditions
        elif path.endswith("/context"):
            data = {"active_intent": "search", "revision": 1, "frames": {}, "pending": [], "trace": [], "tasks": {}}
        elif path.endswith("/llm-config"):
            data = {"provider": "mock", "model": "mock-autohunt-v1", "credential_configured": True}
        elif path.endswith(("/browser", "/delivery")):
            data = {"status": "idle", "id": None}
        elif path.endswith("/candidate-state"):
            data = {"profile": {"resume_id": None, "role_suggestions": [], "skills": [], "evidence": [], "summary": ""},
                    "preferences": {"version": 0, "locations": {"values": []}, "target_roles": {"values": [], "source": "default"},
                                    "search_keywords": {"values": []}}, "resume_draft": ""}
        elif path.endswith("/documents"):
            data = {"source_mode": "structured", "version": 1, "entries": []}
        elif path.endswith("/insights"):
            data = {"tags": [], "stored_total": 0}
        else:
            data = {}
        route.fulfill(json=data)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page(viewport={"width": width, "height": height})
        page.route("**/api/agent/**", api)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(os.getenv("AUTOHUNT_FRONTEND_URL", "http://127.0.0.1:3000"))
        page.get_by_role("button", name="Agent 对话", exact=True).click()
        thread = page.get_by_role("log", name="对话消息")
        expect(thread).to_contain_text("我还没有启动搜索")
        inspector = page.get_by_role("complementary", name="任务与上下文")
        expect(inspector).to_be_hidden()
        expect(page.get_by_role("combobox", name="当前会话")).to_have_count(1)
        expect(page.get_by_role("button", name="新会话", exact=True)).to_have_count(1)
        bounds = thread.bounding_box()
        composer = page.locator(".chat-input-dock").bounding_box()
        workspace = page.locator(".chat-workspace").bounding_box()
        assert bounds["height"] > height * .5
        assert bounds["width"] > workspace["width"] * .85
        assert composer["y"] + composer["height"] <= height + 1
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

        artifacts = Path(__file__).resolve().parents[2] / ".runlogs"
        artifacts.mkdir(exist_ok=True)
        page.screenshot(path=str(artifacts / f"chat-layout-{width}.png"))
        page.get_by_role("button", name="任务与上下文", exact=True).click()
        expect(inspector).to_be_visible()
        assert abs(thread.bounding_box()["height"] - bounds["height"]) < 2
        expect(inspector.get_by_text("本会话搜索条件", exact=False)).to_be_visible()
        page.screenshot(path=str(artifacts / f"chat-layout-{width}-inspector.png"))
        page.get_by_role("button", name="关闭任务详情").click()
        expect(inspector).to_be_hidden()
        assert abs(thread.bounding_box()["width"] - bounds["width"]) < 2
        expect(page.get_by_placeholder("给 AutoHunt Agent 发消息…")).to_be_visible()
        assert not errors
        browser.close()
