from pathlib import Path

import pytest
from playwright.async_api import async_playwright
from playwright.async_api import Error as PlaywrightError

from app.browser.boss_adapter import BOSS_SELECTOR_VERSION, BossAdapter


FIXTURES = Path(__file__).parent / "fixtures"


def test_safe_job_url_rejects_generic_detail_route() -> None:
    assert BossAdapter.safe_job_url("https://www.zhipin.com/job_detail/") is None
    assert BossAdapter.safe_job_url("https://www.zhipin.com/job_detail/abc123.html") is not None


async def launch_test_browser(playwright):
    try:
        return await playwright.chromium.launch(headless=True)
    except PlaywrightError as exc:
        if "spawn EPERM" in str(exc):
            pytest.skip("sandbox does not allow launching Chromium")
        raise


@pytest.mark.asyncio
async def test_boss_search_fixture_extracts_versioned_cards() -> None:
    async with async_playwright() as playwright:
        browser = await launch_test_browser(playwright)
        page = await browser.new_page()
        await page.set_content((FIXTURES / "boss_search.html").read_text(encoding="utf-8"))
        adapter = BossAdapter()

        evidence = await adapter.inspect(page)
        cards = await adapter.extract_search_cards(page)

        assert evidence.authenticated_signal is True
        assert evidence.card_count >= 1
        assert cards[0]["title"] == "Python Agent 工程师"
        assert cards[0]["company"] == "示例科技"
        assert cards[0]["selector_version"] == BOSS_SELECTOR_VERSION
        await browser.close()


@pytest.mark.asyncio
async def test_boss_search_uses_visible_form() -> None:
    async with async_playwright() as playwright:
        browser = await launch_test_browser(playwright)
        page = await browser.new_page()
        await page.set_content((FIXTURES / "boss_search.html").read_text(encoding="utf-8"))

        submitted = await BossAdapter().submit_search(page, "软件工程师")

        assert submitted is True
        assert await page.locator("input").input_value() == "软件工程师"
        assert await page.get_by_role("button", name="搜索").get_attribute("data-clicked") == "yes"
        await browser.close()


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_boss_city_filter_is_selected_separately_from_keyword() -> None:
    async with async_playwright() as playwright:
        browser = await launch_test_browser(playwright)
        page = await browser.new_page()
        await page.set_content((FIXTURES / "boss_search.html").read_text(encoding="utf-8"))

        selected = await BossAdapter().select_city(page, "深圳")
        submitted = await BossAdapter().submit_search(page, "AI agent")

        assert selected is True
        assert submitted is True
        assert await page.locator(".city-label").inner_text() == "深圳"
        assert await page.locator("input").input_value() == "AI agent"
        await browser.close()


@pytest.mark.asyncio
async def test_boss_city_route_is_recognized_without_reopening_filter() -> None:
    class CityPage:
        url = "https://www.zhipin.com/shenzhen/?seoRefer=index"

    assert await BossAdapter().select_city(CityPage(), "深圳") is True  # type: ignore[arg-type]


async def test_boss_verification_fixture_requires_human() -> None:
    async with async_playwright() as playwright:
        browser = await launch_test_browser(playwright)
        page = await browser.new_page()
        await page.set_content((FIXTURES / "boss_verification.html").read_text(encoding="utf-8"))

        evidence = await BossAdapter().inspect(page)

        assert evidence.verification_visible is True
        assert evidence.intervention_reason == "检测到验证码或安全验证"
        await browser.close()


@pytest.mark.asyncio
async def test_wait_for_search_cards_sees_rendered_cards() -> None:
    async with async_playwright() as playwright:
        browser = await launch_test_browser(playwright)
        page = await browser.new_page()
        await page.set_content((FIXTURES / "boss_search.html").read_text(encoding="utf-8"))
        adapter = BossAdapter()

        assert await adapter.wait_for_search_cards(page, timeout_ms=2_000) is True
        await browser.close()


@pytest.mark.asyncio
async def test_wait_for_search_cards_waits_for_progressive_card_render() -> None:
    async with async_playwright() as playwright:
        browser = await launch_test_browser(playwright)
        page = await browser.new_page()
        await page.set_content("""
            <div id="jobs"><a href="https://www.zhipin.com/job_detail/one.html">one</a></div>
            <script>
              setTimeout(() => {
                const a = document.createElement('a');
                a.href = 'https://www.zhipin.com/job_detail/two.html';
                a.textContent = 'two';
                document.querySelector('#jobs').appendChild(a);
              }, 500);
            </script>
        """)
        adapter = BossAdapter()

        assert await adapter.wait_for_search_cards(page, timeout_ms=3_000) is True
        cards = await adapter.extract_search_cards(page)
        assert len(cards) == 2
        await browser.close()

@pytest.mark.asyncio
async def test_wait_for_job_detail_is_tolerant_when_container_missing() -> None:
    async with async_playwright() as playwright:
        browser = await launch_test_browser(playwright)
        page = await browser.new_page()
        # The search fixture has no job-detail container; the wait must return
        # False instead of raising so extraction can fall back to card data.
        await page.set_content((FIXTURES / "boss_search.html").read_text(encoding="utf-8"))

        assert await BossAdapter().wait_for_job_detail(page, timeout_ms=1_000) is False
        await browser.close()


@pytest.mark.asyncio
async def test_logged_in_account_signal_wins_over_unrelated_login_class() -> None:
    async with async_playwright() as playwright:
        browser = await launch_test_browser(playwright)
        page = await browser.new_page()
        await page.set_content((FIXTURES / "boss_logged_in_home.html").read_text(encoding="utf-8"))

        evidence = await BossAdapter().inspect(page)

        assert evidence.login_control_visible is False
        assert evidence.authenticated_signal is True
        assert evidence.intervention_reason is None
        await browser.close()


@pytest.mark.asyncio
async def test_logged_in_search_skeleton_is_loading_not_login_loss() -> None:
    async with async_playwright() as playwright:
        browser = await launch_test_browser(playwright)
        page = await browser.new_page()
        await page.set_content((FIXTURES / "boss_search_skeleton.html").read_text(encoding="utf-8"))

        evidence = await BossAdapter().inspect(page)

        assert evidence.authenticated_signal is True
        assert evidence.loading_visible is True
        assert evidence.intervention_reason is None
        await browser.close()


def test_root_and_city_search_shells_are_recoverable() -> None:
    from app.browser.automation import BrowserAutomationService

    target = "https://www.zhipin.com/web/geek/jobs?query=AI"
    assert BrowserAutomationService._is_recoverable_search_shell(
        target, "https://www.zhipin.com/",
        authenticated=True, loading=False,
    )
    assert BrowserAutomationService._is_recoverable_search_shell(
        target, "https://www.zhipin.com/shenzhen/?seoRefer=index",
        authenticated=False, loading=True,
    )
    assert not BrowserAutomationService._is_recoverable_search_shell(
        target, "https://www.zhipin.com/shenzhen/?seoRefer=index",
        authenticated=False, loading=False,
    )
    assert not BrowserAutomationService._is_recoverable_search_shell(
        "https://www.zhipin.com/job_detail/abc.html",
        "https://www.zhipin.com/shenzhen/",
        authenticated=True, loading=True,
    )


@pytest.mark.asyncio
async def test_search_waits_for_city_page_form_replacement() -> None:
    async with async_playwright() as playwright:
        browser = await launch_test_browser(playwright)
        page = await browser.new_page()
        await page.set_content("""
            <main id="app"></main>
            <script>
              setTimeout(() => {
                document.querySelector('#app').innerHTML =
                  '<input class="ipt-search" name="query">' +
                  '<button class="btn-search">搜索</button>';
                document.querySelector('.btn-search').onclick = event => {
                  event.preventDefault();
                  event.currentTarget.dataset.clicked = 'yes';
                };
              }, 250);
            </script>
        """)

        submitted = await BossAdapter().submit_search(page, "AI agent")

        assert submitted is True
        assert await page.locator("input.ipt-search").input_value() == "AI agent"
        assert await page.locator("button.btn-search").get_attribute("data-clicked") == "yes"
        await browser.close()


@pytest.mark.asyncio
async def test_readable_job_detail_does_not_trigger_second_login() -> None:
    async with async_playwright() as playwright:
        browser = await launch_test_browser(playwright)
        page = await browser.new_page()
        await page.set_content("""
          <header><a>登录</a></header>
          <section class="job-banner"><h1>大模型 Agent 算法工程师</h1></section>
          <div class="job-sec-text">负责 Agent、RAG 与大模型应用研发，职位详情可以正常读取。</div>
        """)

        evidence = await BossAdapter().inspect(page)

        assert evidence.login_control_visible is False
        assert evidence.authenticated_signal is True
        assert evidence.intervention_reason is None
        await browser.close()
