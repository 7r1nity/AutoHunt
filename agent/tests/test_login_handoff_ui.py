"""Opt-in UI regression for mock-site login handoff; all agent APIs are mocked."""
import os
from urllib.parse import urlparse

import pytest
from playwright.sync_api import expect, sync_playwright


pytestmark = pytest.mark.skipif(
    os.getenv("AUTOHUNT_RESUME_UI") != "1", reason="Requires a running AutoHunt frontend"
)


def _task(status):
    return {
        "id": None if status == "idle" else "ui-test-run",
        "status": status,
        "source": "mock",
        "query": "AI Agent",
        "detail": "模拟站搜索完成" if status == "completed" else "浏览器任务执行中",
        "takeover_reason": "需要登录" if status == "waiting_for_user" else None,
        "browser_mode": "cdp",
        "selector_version": "mock-v1",
        "checkpoint": "search",
        "current_url": "http://127.0.0.1:8080/jobs.html",
        "screenshot_path": None,
        "login_evidence": {"authenticated_signal": status != "waiting_for_user"},
        "discovered": 2,
        "stored": 1,
        "duplicates": 0,
        "matched": 1,
        "dry_run": False,
        "dry_run_candidates": 0,
        "dry_run_contacted": 0,
        "dry_run_replied": 0,
        "dry_run_submitted": 0,
        "dry_run_waiting_reply": 0,
        "dry_run_failed": 0,
        "dry_run_report": "",
        "adapter_version": "mock-v1",
        "funnel": {"links_seen": 2, "cards_parsed": 2, "unique_jobs": 2,
                   "limited": 0, "visited": 2, "filtered": 1, "stored": 1,
                   "duplicates": 0, "errors": 0, "cancelled": 0,
                   "reason_counts": {}, "samples": []},
        "pipeline": [],
    }


@pytest.mark.parametrize("requires_login", [False, True])
def test_confirm_login_only_when_browser_waits(requires_login):
    state = {"status": "idle", "resume_calls": 0}
    errors = []

    def api(route):
        path = urlparse(route.request.url).path
        if path.endswith("/browser-report"):
            route.fulfill(json={
                "covered_jobs": 1,
                "salary_distribution": {"20-30K": 1},
                "employment_distribution": {"应届/校招": 1},
                "benefit_distribution": {"五险一金": 1},
                "highlights": [{
                    "id": 1, "title": "金融风控分析师（应届）", "company": "测试公司",
                    "location": "深圳", "salary": "20-30K", "score": 86,
                    "employment_type": "应届/校招", "benefits": ["五险一金"],
                    "jd_excerpt": "负责信用风险策略分析与模型评估。",
                }],
            })
        elif path.endswith("/browser"):
            if route.request.method == "POST":
                action = route.request.post_data_json["action"]
                if action == "start":
                    state["status"] = "waiting_for_user" if requires_login else "running"
                elif action == "resume":
                    state["resume_calls"] += 1
                    state["status"] = "running"
                route.fulfill(json=_task(state["status"]))
            else:
                route.fulfill(json=_task(state["status"]))
        elif path.endswith("/chat") and route.request.method == "POST":
            route.fulfill(json={"message": "开始搜索职位", "action": "confirm_search",
                                "action_query": "AI Agent", "action_queries": ["AI Agent"],
                                "lane": "browser"})
        elif path.endswith(("/jobs", "/resumes", "/materials")):
            route.fulfill(json=[])
        elif path.endswith("/stats"):
            route.fulfill(json={})
        else:
            route.fulfill(status=503, json={"detail": "Not part of this UI test"})

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.route("**/api/agent/**", api)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(os.getenv("AUTOHUNT_FRONTEND_URL", "http://127.0.0.1:3000"))
        page.get_by_role("button", name="Agent 对话").click()
        page.get_by_placeholder("给 AutoHunt Agent 发消息…").fill("帮我找工作")
        page.get_by_role("button", name="发送").click()
        card = page.locator(".chat-confirm-card").filter(has_text="搜索关键词").last
        expect(card).to_be_visible()
        if requires_login:
            expect(card.get_by_role("button", name="确认登录")).to_be_visible()
            card.get_by_role("button", name="确认登录").click()
            expect(card).to_contain_text("正在执行搜索")
            assert state["resume_calls"] == 1
        else:
            expect(card).to_contain_text("正在执行搜索")
            assert card.get_by_role("button", name="确认登录").count() == 0
            assert state["resume_calls"] == 0
        state["status"] = "completed"
        expect(card).to_contain_text("搜索完成", timeout=10000)
        expect(page.locator(".chat-thread")).to_contain_text("新增看板 1 个", timeout=10000)
        expect(page.locator(".chat-thread")).to_contain_text("信用风险策略分析", timeout=10000)
        expect(page.locator(".chat-thread")).to_contain_text("五险一金", timeout=10000)
        assert not errors
        browser.close()
