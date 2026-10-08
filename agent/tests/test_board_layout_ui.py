"""Read-only frontend layout regression with isolated jobs and no backend writes."""
import os
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.sync_api import expect, sync_playwright

pytestmark = pytest.mark.skipif(os.getenv("AUTOHUNT_CHAT_UI") != "1", reason="Requires running frontend")


@pytest.mark.parametrize("width", [1440, 980, 620, 390])
@pytest.mark.parametrize("theme", ["dark", "light"])
def test_board_cards_stay_in_columns_and_actions_do_not_wrap(width, theme):
    jobs = [{"id": index, "role": "大模型算法工程师" if index == 1 else "长职位名称LongUnbrokenRoleNameForResponsiveLayoutTesting",
        "company": "云图数据", "location": "上海", "salary": "27-70K", "source": "MockJobSite", "score": 88,
        "stage": "review", "mode": "人工确认", "review_approved": index == 2,
        "tags": ["AIGC", "VeryLongUnbrokenTechnicalTagForWrappingTest"],
        "reason": "搜索方向为大模型算法，JD核心栈与候选人LLM/RAG/PyTorch高度匹配，仅CTR广告与部分工程流程经验略缺，建议申请。",
        "posted": "2026-10-08T03:39:51.231772+00:00", "sourceUrl": "https://example.com/job"} for index in (1, 2)]
    errors = []
    def api(route):
        assert route.request.method == "GET", "Layout tests must not write user data"
        path = urlparse(route.request.url).path
        if path.endswith("/jobs"):
            data = jobs
        elif path.endswith("/conversations"):
            data = [{"id": "default", "title": "布局测试"}]
        elif path.endswith("/runs") or path.endswith(("/resumes", "/materials", "/learning-plans")):
            data = []
        elif path.endswith("/chat"):
            data = {"messages": []}
        elif path.endswith("/context"):
            data = {"active_intent": "general", "frames": {}, "pending": [], "focus": [], "trace": [], "tasks": {}}
        elif path.endswith("/search-state"):
            data = {"queries": [], "locations": [], "result_limit": None, "salary_min": None, "revision": 0}
        elif path.endswith(("/browser", "/delivery")):
            data = {"id": None, "status": "idle"}
        elif path.endswith("/candidate-state"):
            data = {"profile": {"resume_id": None, "role_suggestions": [], "skills": [], "evidence": [], "summary": ""},
                "preferences": {"version": 0, "locations": {"values": []}, "target_roles": {"values": [], "source": "default"}, "search_keywords": {"values": []}}, "resume_draft": ""}
        elif path.endswith("/insights"):
            data = {"tasks_total": 0, "stored_total": 0, "discovered_total": 0, "duplicates_total": 0, "rejected": 0,
                "avg_score": 0, "score_high": 0, "score_good": 0, "score_fair": 0, "tags": [], "salaries": [], "cities": []}
        else:
            data = {}
        route.fulfill(json=data)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page(viewport={"width": width, "height": 1000}, color_scheme=theme)
        page.route("**/api/agent/**", api)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(os.getenv("AUTOHUNT_FRONTEND_URL", "http://127.0.0.1:3000"))
        page.get_by_role("button", name="申请看板", exact=True).click()
        expect(page.locator(".job-card")).to_have_count(2)
        expect(page.locator(".job-card time").first).to_have_text("2026/10/08")
        expect(page.locator(".job-card time").first).to_have_attribute("title", "2026-10-08T03:39:51.231772+00:00")
        for card in page.locator(".job-card").all():
            geometry = card.evaluate("""card => {
                const column=card.closest('.kanban-column'), c=card.getBoundingClientRect(), p=column.getBoundingClientRect();
                const action=card.querySelector('footer button'), a=action.getBoundingClientRect(), s=getComputedStyle(action);
                return {left:c.left-p.left, right:p.right-c.right, overflow:card.scrollWidth-card.clientWidth,
                    columnOverflow:column.scrollWidth-column.clientWidth, buttonLines:a.height/(parseFloat(s.lineHeight)||16)};
            }""")
            assert geometry["left"] >= 0 and geometry["right"] >= 0, geometry
            assert geometry["overflow"] <= 1 and geometry["columnOverflow"] <= 1, geometry
            assert geometry["buttonLines"] <= 2, geometry
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth+1")
        screenshot = Path(__file__).resolve().parents[2] / ".runlogs" / f"board-layout-{theme}-{width}.png"
        page.screenshot(path=str(screenshot), full_page=True)
        assert not errors
        browser.close()
