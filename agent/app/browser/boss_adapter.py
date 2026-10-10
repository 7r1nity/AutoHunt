from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from playwright.async_api import Error as PlaywrightError, Page


BOSS_ORIGIN = "https://www.zhipin.com"
ALLOWED_BOSS_HOSTS = {"www.zhipin.com", "m.zhipin.com"}
BOSS_SELECTOR_VERSION = "2026-08-22.1"
SEARCH_CARD_SELECTOR = "a[href*='/job_detail/']"
DETAIL_CONTENT_SELECTORS = ".job-sec-text, .job-detail-section, .job-detail, .job-banner"


@dataclass(frozen=True)
class BossPageEvidence:
    url: str
    title: str
    ready_state: str
    card_count: int
    login_control_visible: bool
    verification_visible: bool
    loading_visible: bool
    authenticated_signal: bool
    selector_version: str = BOSS_SELECTOR_VERSION

    def as_dict(self) -> dict:
        return self.__dict__.copy()

    @property
    def intervention_reason(self) -> str | None:
        if self.verification_visible:
            return "检测到验证码或安全验证"
        if self.login_control_visible:
            return "需要用户登录或完成 MFA"
        return None


class BossAdapter:
    selector_version = BOSS_SELECTOR_VERSION
    origin = BOSS_ORIGIN
    label = "BOSS直聘"

    @staticmethod
    def search_url(query: str) -> str:
        from urllib.parse import quote_plus
        return f"{BOSS_ORIGIN}/web/geek/jobs?query={quote_plus(query)}"

    @staticmethod
    def safe_job_url(value: str) -> str | None:
        url = urljoin(BOSS_ORIGIN, value)
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in ALLOWED_BOSS_HOSTS
            or not re.search(r"/job_detail/[^/?#]+", parsed.path)
        ):
            return None
        return url

    async def _evaluate_stable(self, page: Page, expression: str, arg=None):
        """Evaluate while tolerating BOSS's mid-flight document replacements.

        BOSS swaps the document right after navigation, which destroys the
        previous execution context. Retry briefly instead of failing the whole
        search task.
        """
        last_error: Exception | None = None
        for _ in range(3):
            try:
                if arg is None:
                    return await page.evaluate(expression)
                return await page.evaluate(expression, arg)
            except PlaywrightError as exc:
                last_error = exc
                if "Execution context was destroyed" not in str(exc) and "navigation" not in str(exc).lower():
                    raise
                await page.wait_for_timeout(600)
        raise RuntimeError(f"BOSS page kept navigating during extraction: {last_error}") from last_error

    async def inspect(self, page: Page) -> BossPageEvidence:
        raw = None
        last_error: Exception | None = None
        for _ in range(3):
            try:
                raw = await page.evaluate(
                    r"""() => {
              const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
              const text = (document.body?.innerText || '').slice(0, 8000);
              const href = location.href;
              const phoneInputs = Array.from(document.querySelectorAll(
                'input[placeholder*="手机号"], input[placeholder*="手机号码"]'
              )).filter(visible);
              const explicitLoginControls = Array.from(document.querySelectorAll(
                'a, button, .login-register'
              )).filter(el => visible(el) && /^(登录|注册|登录\s*[\\/]\s*注册)$/.test((el.textContent || '').trim()));
              const cards = document.querySelectorAll('.job-card-wrapper, .job-card-box, a[href*="/job_detail/"]');
              const verification = /安全验证|拖动滑块|完成验证|验证码/.test(text) || /verify|captcha|security-check/.test(href);
              const skeletons = Array.from(document.querySelectorAll(
                '[class*="skeleton"], [class*="loading-card"], .job-skeleton, .skeleton-item'
              )).filter(visible);
              const loading = /加载中[，,]?\s*请稍候|正在加载/.test(text) || skeletons.length > 0;
              const accountSignal = !!document.querySelector(
                '.nav-figure, .user-nav, .nav-user-wrapper, [class*="user-info"], img[class*="avatar"]'
              );
              const readableJobDetail = !!document.querySelector(
                '.job-sec-text, .job-detail-section, .job-detail, .job-banner'
              );
              const blockingLogin = (
                /passport|[/]login|[/]user[/]sign/.test(href)
                || /短信验证码登录|扫码登录后继续|登录后查看更多职位/.test(text)
                || phoneInputs.length > 0
              );
              const login = !accountSignal && (
                blockingLogin
                || (explicitLoginControls.length > 0 && !readableJobDetail && cards.length === 0)
              );
              return {
                url: href,
                title: document.title || '',
                readyState: document.readyState || '',
                cardCount: cards.length,
                loginControlVisible: login,
                verificationVisible: verification,
                loadingVisible: loading,
                authenticatedSignal: !login && !verification && (accountSignal || cards.length > 0 || readableJobDetail)
              };
                    }"""
                )
                break
            except PlaywrightError as exc:
                last_error = exc
                if "Execution context was destroyed" not in str(exc) and "navigation" not in str(exc).lower():
                    raise
                await page.wait_for_timeout(500)
        if raw is None:
            raise RuntimeError(f"BOSS page did not stabilize after navigation: {last_error}") from last_error
        return BossPageEvidence(
            url=str(raw.get("url") or page.url),
            title=str(raw.get("title") or ""),
            ready_state=str(raw.get("readyState") or ""),
            card_count=int(raw.get("cardCount") or 0),
            login_control_visible=bool(raw.get("loginControlVisible")),
            verification_visible=bool(raw.get("verificationVisible")),
            loading_visible=bool(raw.get("loadingVisible")),
            authenticated_signal=bool(raw.get("authenticatedSignal")),
        )

    async def select_city(self, page: Page, city: str) -> bool:
        """Select a city through BOSS's visible city control."""
        city = city.strip()
        if not city:
            return True
        city_slugs = {
            "北京": "beijing",
            "上海": "shanghai",
            "深圳": "shenzhen",
            "杭州": "hangzhou",
            "广州": "guangzhou",
            "成都": "chengdu",
            "南京": "nanjing",
            "武汉": "wuhan",
            "西安": "xian",
            "苏州": "suzhou",
        }
        expected_slug = city_slugs.get(city)
        current_path = urlparse(page.url).path.strip("/").lower()
        if expected_slug and current_path.split("/", 1)[0] == expected_slug:
            return True
        trigger_selectors = (
            ".nav-city-selected, .switchover-city, .nav-city, "
            ".city-label, .city-choose, .search-form .city-sel, "
            "[class*='city-selector'], [class*='city-select']"
        )
        option_selectors = (
            ".city-box a, .city-list a, .dropdown-menu a, "
            ".dorpdown-city a, .dorpdown-city li, .dorpdown-city span, "
            "[class*='city-list'] a, [class*='city-list'] li, "
            "[class*='city-box'] a, [class*='city-box'] li"
        )
        try:
            triggers = page.locator(trigger_selectors)
            trigger = None
            for index in range(await triggers.count()):
                candidate = triggers.nth(index)
                if not await candidate.is_visible():
                    continue
                label = (await candidate.inner_text()).strip()
                if city in label:
                    return True
                if trigger is None:
                    trigger = candidate
            if trigger is None:
                return await self._navigate_city_route(page, expected_slug)
            # Current BOSS city landing pages open this menu on hover, while
            # older search-result pages use click. Support both generations.
            try:
                await trigger.hover()
            except PlaywrightError:
                pass
            await page.wait_for_timeout(350)
            try:
                await trigger.click()
            except PlaywrightError:
                pass
            await page.wait_for_timeout(350)
            options = page.locator(option_selectors)
            for index in range(await options.count()):
                option = options.nth(index)
                if not await option.is_visible():
                    continue
                label = (await option.inner_text()).strip()
                if label == city or label.startswith(city):
                    await option.click()
                    await page.wait_for_timeout(350)
                    return True
            return await self._navigate_city_route(page, expected_slug)
        except PlaywrightError:
            return await self._navigate_city_route(page, expected_slug)

    @staticmethod
    async def _navigate_city_route(page: Page, city_slug: str | None) -> bool:
        """Use BOSS's first-party city route when its menu is unavailable."""
        if not city_slug:
            return False
        target = f"https://www.zhipin.com/{city_slug}/?seoRefer=index"
        try:
            await page.goto(target, wait_until="domcontentloaded", timeout=30_000)
            current_path = urlparse(page.url).path.strip("/").lower()
            return current_path.split("/", 1)[0] == city_slug
        except PlaywrightError:
            return False

    async def submit_search(self, page: Page, query: str) -> bool:
        """Use BOSS's visible form, waiting through city-page replacement."""
        input_selector = (
            'input.ipt-search, input[name="query"], '
            'input[placeholder*="搜索职位"], input[placeholder*="职位、公司"]'
        )
        for attempt in range(3):
            try:
                inputs = page.locator(input_selector)
                try:
                    await inputs.first.wait_for(state="attached", timeout=5_000)
                except PlaywrightError:
                    pass
                search_input = None
                for index in range(await inputs.count()):
                    candidate = inputs.nth(index)
                    if await candidate.is_visible():
                        search_input = candidate
                        break
                if search_input is None:
                    if attempt < 2:
                        await page.wait_for_timeout(600)
                        continue
                    return False
                await search_input.fill(query)

                buttons = page.locator("button.btn-search, .btn-search, button[type='submit']")
                for index in range(await buttons.count()):
                    button = buttons.nth(index)
                    if await button.is_visible():
                        await button.click()
                        return True
                role_buttons = page.get_by_role("button", name=re.compile(r"^搜索$"))
                for index in range(await role_buttons.count()):
                    button = role_buttons.nth(index)
                    if await button.is_visible():
                        await button.click()
                        return True
                await search_input.press("Enter")
                return True
            except PlaywrightError as exc:
                if attempt < 2 and (
                    "Execution context was destroyed" in str(exc)
                    or "navigation" in str(exc).lower()
                ):
                    try:
                        await page.wait_for_timeout(800)
                    except PlaywrightError:
                        pass
                    continue
                # A closed target is NOT retried here: retrying locators on a
                # dead page is pointless. The caller rebinds the tab first.
                raise
        return False


    async def wait_for_search_cards(self, page: Page, *, timeout_ms: int = 15_000) -> bool:
        """Wait for the progressively rendered result-card count to stabilize."""
        try:
            locator = page.locator(SEARCH_CARD_SELECTOR)
            await locator.first.wait_for(state="attached", timeout=timeout_ms)
            # BOSS renders cards progressively. Waiting only for the first anchor
            # made a page with 100+ links look like a one-job result.
            last_count = -1
            stable_samples = 0
            samples = max(3, min(12, timeout_ms // 400))
            for _ in range(samples):
                count = await locator.count()
                stable_samples = stable_samples + 1 if count == last_count else 0
                last_count = count
                if stable_samples >= 3:
                    break
                await page.wait_for_timeout(400)
            return last_count > 0
        except PlaywrightError:
            return False
    async def wait_for_job_detail(self, page: Page, *, timeout_ms: int = 8_000) -> bool:
        """Tolerant wait for a job-detail container. False means 'proceed anyway':
        :meth:`extract_job_detail` already falls back to card data when the JD
        container is missing, so a layout drift must not stall the run.
        """
        try:
            await page.locator(DETAIL_CONTENT_SELECTORS).first.wait_for(state="attached", timeout=timeout_ms)
            return True
        except PlaywrightError:
            return False

    async def extract_search_cards(self, page: Page) -> list[dict]:
        return await self._evaluate_stable(
            page,
            """config => {
              const anchors = Array.from(document.querySelectorAll(config.selector));
              const seen = new Set();
              return anchors.map(a => {
                const url = a.href;
                if (!url || seen.has(url)) return null;
                seen.add(url);
                const card = a.closest('li, .job-card-wrapper, .job-card-box, .job-list-box') || a.parentElement;
                const pick = selectors => selectors.map(s => card?.querySelector(s)?.textContent?.trim()).find(Boolean) || '';
                const raw = a.textContent?.trim() || '';
                return {
                  url,
                  title: pick(['.job-name', '.job-title']) || raw.split('\\n')[0].trim().slice(0, 60) || '未命名职位',
                  company: pick(['.company-name', '.company-text']),
                  location: pick(['.job-area', '.job-location']),
                  salary: pick(['.salary', '.job-salary']),
                  tags: Array.from(card?.querySelectorAll('.tag-list li, .job-card-footer li') || []).map(x => x.textContent?.trim()).filter(Boolean),
                  selector_version: config.version
                };
              }).filter(Boolean);
            }""",
            {"selector": SEARCH_CARD_SELECTOR, "version": self.selector_version},
        )

    async def extract_job_detail(self, page: Page, card: dict) -> dict:
        extracted = await self._evaluate_stable(
            page,
            """() => {
              const pick = selectors => selectors.map(s => document.querySelector(s)?.textContent?.trim()).find(Boolean) || '';
              const list = selectors => Array.from(document.querySelectorAll(selectors)).map(x => x.textContent?.trim()).filter(Boolean);
              return {
                title: pick(['.name h1', '.job-banner .name', 'h1']),
                company: pick(['.company-info .name', '.company-name', '.sider-company .company-info a']),
                location: pick(['.job-address .location-address', '.job-primary .job-area', '.location-address']),
                salary: pick(['.salary', '.job-banner .salary']),
                description: pick(['.job-sec-text', '.job-detail-section', '.job-detail']),
                tags: list('.job-tags span, .job-tags li, .job-tags-box li')
              };
            }"""
        )
        match = re.search(r"/job_detail/([^./?]+)", page.url)
        return {
            "title": extracted["title"] or card.get("title") or "未命名职位",
            "company": extracted["company"] or card.get("company") or "未知公司",
            "location": extracted["location"] or card.get("location") or "未注明",
            "salary": extracted["salary"] or card.get("salary") or "未注明",
            "description": extracted["description"] or " ".join(card.get("tags", [])),
            "tags": list(dict.fromkeys((card.get("tags") or []) + (extracted["tags"] or []))),
            "external_id": match.group(1) if match else None,
            "selector_version": self.selector_version,
        }
