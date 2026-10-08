from __future__ import annotations

from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from app.browser.mock_site_adapter import MockJobSiteSearchAdapter
from app.core.models import BrowserSearchRequest


FIXTURES = Path(__file__).parent / "fixtures"


async def _launch_browser(playwright):
    try:
        return await playwright.chromium.launch(headless=True)
    except Exception as exc:
        pytest.skip(f"Playwright Chromium is unavailable: {exc}")


async def _open_fixture(page, name: str, url: str) -> None:
    html = (FIXTURES / name).read_text(encoding="utf-8")
    await page.route(url, lambda route: route.fulfill(status=200, content_type="text/html", body=html))
    await page.goto(url)


def test_mock_is_the_default_search_source() -> None:
    assert BrowserSearchRequest(query="AI Agent").source == "mock"


def test_mock_job_url_is_restricted_to_local_simulator() -> None:
    adapter = MockJobSiteSearchAdapter()
    assert adapter.safe_job_url("/job.html?id=101") == "http://127.0.0.1:8080/job.html?id=101"
    assert adapter.safe_job_url("http://localhost:8080/job.html?id=9") is not None
    assert adapter.safe_job_url("https://www.zhipin.com/job_detail/1") is None
    assert adapter.safe_job_url("http://127.0.0.1:8080/manager.html?id=101") is None


@pytest.mark.asyncio
async def test_detects_takeover_then_extracts_search_results() -> None:
    async with async_playwright() as playwright:
        browser = await _launch_browser(playwright)
        page = await browser.new_page()
        await _open_fixture(page, "mock_search.html", "http://mock.test/")
        adapter = MockJobSiteSearchAdapter()

        before = await adapter.inspect(page)
        assert before.authenticated_signal is False
        assert before.login_control_visible is True

        await adapter.open_login_prompt(page)
        takeover = await adapter.inspect(page)
        assert takeover.login_control_visible is True

        await page.evaluate("localStorage.setItem('userId', '7'); localStorage.setItem('userRole', 'applicant')")
        await page.locator("#login-modal").evaluate("el => el.style.display = 'none'")
        authenticated = await adapter.inspect(page)
        assert authenticated.authenticated_signal is True

        cards = await adapter.search(page, "AI Agent", "深圳")
        assert await page.locator("#keyword").input_value() == "AI Agent"
        assert await page.locator("#city").input_value() == "深圳"
        assert cards == [{
            "id": "101",
            "title": "AI Agent 工程师",
            "company": "示例科技",
            "location": "深圳",
            "salary": "25-40K",
            "tags": ["Python", "LangGraph"],
            "url": "http://127.0.0.1:8080/job.html?id=101",
            "external_id": "101",
        }]
        await browser.close()


@pytest.mark.asyncio
async def test_extracts_mock_job_detail() -> None:
    async with async_playwright() as playwright:
        browser = await _launch_browser(playwright)
        page = await browser.new_page()
        await _open_fixture(page, "mock_detail.html", "http://mock.test/job.html?id=101")
        adapter = MockJobSiteSearchAdapter()
        await adapter.wait_for_job_detail(page)
        detail = await adapter.extract_job_detail(page, {"tags": ["Python"], "external_id": "101"})
        assert detail["title"] == "AI Agent 工程师"
        assert detail["company"] == "示例科技"
        assert detail["location"] == "深圳 · 南山区"
        assert "LangGraph 项目经验" in detail["description"]
        assert detail["tags"] == ["Python", "RAG"]
        await browser.close()


@pytest.mark.asyncio
async def test_search_follows_visible_pagination_and_candidate_budget() -> None:
    async with async_playwright() as playwright:
        browser = await _launch_browser(playwright)
        page = await browser.new_page()
        await page.set_content("""
          <input id="keyword"><select id="city"><option value="">全国</option><option>深圳</option></select>
          <button class="search-btn" onclick="render(1)">搜索职位</button>
          <div id="result-count"></div><div id="jobs"></div><div id="pagination"></div>
          <script>
            function render(page) {
              document.querySelector('#result-count').textContent = '为你找到 36 个职位';
              document.querySelector('#jobs').innerHTML = Array.from({length:12}, (_,i)=>{
                const id=(page-1)*12+i+1;
                return `<article class="modern-job-card" onclick="location.href='/job.html?id=${id}'"><h3>风控分析师${id}</h3><strong class="salary">15-25K</strong></article>`;
              }).join('');
              document.querySelector('#pagination').innerHTML = `<span class="page-summary">第 ${page} / 3 页</span><button aria-label="下一页" ${page===3?'disabled':''} onclick="render(${page+1})">下一页</button>`;
            }
            render(1);
          </script>
        """)
        cards = await MockJobSiteSearchAdapter().search(page, "风控", "深圳", max_cards=20)
        assert len(cards) == 20
        assert len({card["url"] for card in cards}) == 20
        assert cards[-1]["external_id"] == "20"
        assert "第 2" in await page.locator(".page-summary").text_content()
        await browser.close()
