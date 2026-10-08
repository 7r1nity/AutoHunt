"""Opt-in real frontend/SSE regression. Every business API is mocked/read-only."""
import json
import os
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.sync_api import expect, sync_playwright

pytestmark = pytest.mark.skipif(os.getenv("AUTOHUNT_CHAT_UI") != "1", reason="Requires running frontend")


def test_runtime_partial_report_refreshes_statistics_and_detail_link():
    requests, errors = [], []
    run = {"id": "test-run", "session_id": "default", "kind": "chat", "status": "running",
           "created_at": "2026-10-07T10:00:00Z", "can_resume": False, "browser_id": "test-browser"}
    overview = {"scope": "active_resume", "tasks_total": 1, "discovered_total": 150, "stored_total": 23,
        "duplicates_total": 0, "rejected": 0, "tags": [], "avg_score": 88, "score_high": 0,
        "score_good": 23, "score_fair": 0, "cities": [{"city": "杭州", "count": 23}],
        "salaries": [{"bucket": "20-30K", "count": 23}], "partial_tasks": 1,
        "jd_overview": {"responsibilities": [{"text": "开发海外客户", "count": 23}], "requirements": []},
        "employment_distribution": {"社招": 23}, "benefit_distribution": {"双休": 23}}
    sent = False

    def api(route):
        nonlocal sent
        assert route.request.method == "GET", "UI verification must not write user data"
        path = urlparse(route.request.url).path
        requests.append(path)
        if path.endswith("/events"):
            sent = True
            events = [
                {"seq": 1, "kind": "browser_summary", "data": {"message": "整体薪资与 JD 总结：已保存23个职位；中断结果已进入统计。", "statistics_changed": True}},
                {"seq": 2, "kind": "status", "data": {"status": "cancelled", "detail": "已停止"}},
            ]
            body = "".join(f"event: {event['kind']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n" for event in events)
            route.fulfill(content_type="text/event-stream", body=body)
            return
        if path.endswith("/conversations"):
            data = [{"id": "default", "title": "测试会话"}]
        elif path.endswith("/runs"):
            data = [{**run, "status": "cancelled" if sent else "running"}]
        elif path.endswith("/chat"):
            data = {"messages": [{"role": "assistant", "content": "[打开职位页面](https://example.com/jobs/8)", "created_at": "2026-10-07T10:00:00Z"}]}
        elif path.endswith("/insights"):
            data = overview
        elif path.endswith(("/jobs", "/resumes", "/materials")):
            data = []
        elif path.endswith("/context"):
            data = {"active_intent": "applications", "revision": 1, "frames": {}, "pending": [], "trace": [], "tasks": {}}
        elif path.endswith("/search-state"):
            data = {"queries": ["外贸业务员"], "locations": ["杭州"], "result_limit": 15,
                    "salary_min": 12000, "query_source": "resume", "revision": 1}
        elif path.endswith(("/browser", "/delivery")):
            data = {"id": None, "status": "idle"}
        elif path.endswith("/candidate-state"):
            data = {"profile": {"resume_id": None, "role_suggestions": [], "skills": [], "evidence": [], "summary": ""},
                    "preferences": {"version": 0, "locations": {"values": []}, "target_roles": {"values": [], "source": "default"},
                                    "search_keywords": {"values": []}}, "resume_draft": ""}
        elif path.endswith("/llm-config"):
            data = {"provider": "mock", "model": "test", "credential_configured": True}
        else:
            data = {}
        route.fulfill(json=data)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.route("**/api/agent/**", api)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script("""window.__statisticsReads=0; const f=window.fetch;
            window.fetch=function(...args) {if(String(args[0]).includes('/api/agent/insights')) window.__statisticsReads++;
                return f.apply(this,args);};""")
        page.goto(os.getenv("AUTOHUNT_FRONTEND_URL", "http://127.0.0.1:3000"))
        page.wait_for_function("window.__statisticsReads >= 2")  # No statistics-tab click needed.
        page.get_by_role("button", name="Agent 对话", exact=True).click()
        expect(page.get_by_role("log", name="对话消息")).to_contain_text("整体薪资与 JD 总结")
        link = page.get_by_role("link", name="打开职位页面")
        expect(link).to_have_attribute("href", "https://example.com/jobs/8")
        expect(link).to_have_attribute("target", "_blank")
        page.context.route("https://example.com/jobs/8", lambda route: route.fulfill(body="Job detail test page"))
        with page.expect_popup() as popup:
            link.click()
        expect(popup.value).to_have_url("https://example.com/jobs/8")
        popup.value.close()
        expect(page.get_by_text("AutoHunt Agent 保持静默", exact=True)).to_have_count(0)
        page.get_by_role("button", name="统计", exact=True).click()
        expect(page.get_by_role("heading", name="整体 JD 画像")).to_be_visible()
        expect(page.get_by_text("开发海外客户 · 23 个岗位", exact=True)).to_be_visible()
        expect(page.get_by_text("双休 · 23 个岗位", exact=True)).to_be_visible()
        expect(page.get_by_text("含 1 次未完整结束的搜索。", exact=False)).to_be_visible()
        page.screenshot(path=str(Path(__file__).resolve().parents[2] / ".runlogs" / "search-report-statistics.png"))
        assert not errors
        assert sum(path.endswith("/insights") for path in requests) >= 2
        browser.close()
