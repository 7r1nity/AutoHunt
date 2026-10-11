from __future__ import annotations

import re
from urllib.parse import parse_qs, urljoin, urlparse

from playwright.async_api import Page

from .boss_adapter import BossPageEvidence


MOCK_SITE_ORIGIN = "http://127.0.0.1:8080"
MOCK_SITE_SELECTOR_VERSION = "mock-job-site-search-v1"


class MockJobSiteSearchAdapter:
    """Deterministic browser adapter for the local recruitment simulator."""

    selector_version = MOCK_SITE_SELECTOR_VERSION

    @staticmethod
    def safe_job_url(value: str) -> str | None:
        url = urljoin(MOCK_SITE_ORIGIN, str(value or ""))
        parsed = urlparse(url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            return None
        if (parsed.port or 80) != 8080 or parsed.path != "/job.html":
            return None
        job_id = (parse_qs(parsed.query).get("id") or [""])[0]
        return url if job_id.isdigit() else None

    async def inspect(self, page: Page) -> BossPageEvidence:
        raw = await page.evaluate(
            r"""() => {
              const visible = el => !!el && getComputedStyle(el).display !== 'none' &&
                !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
              const bodyText = document.body?.innerText || '';
              const modal = document.querySelector('#login-modal, #auth-modal');
              const mfa = document.querySelector('#mfa-row, #mfa-box');
              const captcha = Array.from(document.querySelectorAll(
                '[class*=captcha], [id*=captcha], [class*=verify], [id*=verify]'
              )).find(visible);
              const userId = localStorage.getItem('userId') || '';
              const userRole = localStorage.getItem('userRole') || '';
              const cards = document.querySelectorAll('#jobs .modern-job-card');
              const loading = !!document.querySelector('#jobs [class*=loading]') || /加载中/.test(bodyText);
              return {
                url: location.href,
                title: document.title || '',
                readyState: document.readyState || '',
                cardCount: cards.length,
                loginControlVisible: !userId || userRole !== 'applicant' || visible(modal),
                verificationVisible: !!captcha || /真人验证|滑块验证|图形验证码|安全验证/.test(bodyText),
                loadingVisible: loading,
                authenticatedSignal: !!userId && userRole === 'applicant' && !visible(modal),
                mfaVisible: visible(mfa),
                userRole
              };
            }"""
        )
        login_visible = bool(raw.get("loginControlVisible"))
        if raw.get("mfaVisible"):
            login_visible = True
        return BossPageEvidence(
            url=str(raw.get("url") or page.url),
            title=str(raw.get("title") or ""),
            ready_state=str(raw.get("readyState") or ""),
            card_count=int(raw.get("cardCount") or 0),
            login_control_visible=login_visible,
            verification_visible=bool(raw.get("verificationVisible")),
            loading_visible=bool(raw.get("loadingVisible")),
            authenticated_signal=bool(raw.get("authenticatedSignal")),
            selector_version=self.selector_version,
        )

    async def open_login_prompt(self, page: Page) -> None:
        evidence = await self.inspect(page)
        if evidence.authenticated_signal:
            return
        button = page.locator("#login-button")
        if await button.count() and await button.first.is_visible():
            await button.first.click()
        await page.wait_for_timeout(200)

    @staticmethod
    def _keyword_variants(query: str) -> list[str]:
        """Search terms for one role label, most specific first.

        The site matches a literal substring over title, company and description, so
        a spaced or slash-joined label misses titles that exist ('NLP 算法工程师'
        vs 'NLP算法工程师'). Every specialisation the label carries is kept as long
        as it still finds something; widening to the bare role word is the last
        resort, because dropping the direction silently changes what was asked for.
        """
        from ..core.search_contract import browser_search_keyword
        text = re.sub(r"\s+", " ", str(query or "")).strip()
        if not text:
            return []
        variants: list[str] = [text]
        joined = re.sub(r"\s+", "", text)
        if joined != text:
            variants.append(joined)
        directions = [item for item in re.findall(r"[A-Za-z][A-Za-z0-9+.#]*", text) if len(item) >= 2]
        broad = browser_search_keyword(text)
        for direction in directions:
            if direction not in variants:
                variants.append(direction)
        if broad and broad not in variants:
            variants.append(broad)
        ordered: list[str] = []
        for item in variants:
            if not item or item in ordered:
                continue
            # A fragment that keeps neither the direction nor the full role phrase is
            # a different job family ('Agent开发工程师' must not become '开发工程师').
            if broad and directions and item != broad and item not in directions:
                if item not in text or not re.search(r"[\u4e00-\u9fff]", item):
                    continue
            ordered.append(item)
        return ordered

    async def _result_count(self, page: Page) -> int:
        text = await page.locator("#result-count").text_content() or ""
        match = re.search(r"(\d+)", text.replace(",", ""))
        return int(match[1]) if match else 0

    async def search(self, page: Page, query: str, city: str | None, *, max_cards: int = 200) -> list[dict]:
        await page.locator("#keyword").wait_for(state="visible", timeout=10_000)
        category_select = page.locator("#category")
        if await category_select.count():
            await category_select.select_option(value="")
        if city:
            city_select = page.locator("#city")
            options = await city_select.locator("option").all_text_contents()
            exact = next((item.split(" · ", 1)[0].strip() for item in options if item.split(" · ", 1)[0].strip() == city), None)
            if exact:
                await city_select.select_option(label=next(item for item in options if item.split(" · ", 1)[0].strip() == city))
            else:
                await city_select.select_option(value="")
        else:
            await page.locator("#city").select_option(value="")
        cards = []
        restricted = False
        for variant in self._keyword_variants(query):
            if restricted:
                # A direction was asked for and the site has no job in it. Returning
                # a different job family instead would silently answer a question the
                # user did not ask, so the direction is reported as unavailable.
                break
            await page.locator("#keyword").fill(variant)
            await page.locator("button.search-btn").click()
            await page.wait_for_function(
                "() => (document.querySelector('#result-count')?.textContent || '').includes('为你找到')",
                timeout=10_000,
            )
            await page.wait_for_timeout(250)
            cards = await self.extract_search_cards(page)
            if not cards:
                continue
            # A widened direction token also matches description prose and even
            # letter runs inside Chinese words ("Agent" hits 配送). The direction
            # must appear as its own token in the card, but it does NOT have to be
            # in the title: RAG/LLM are technologies carried in the job's skill
            # tags, and 600 RAG jobs would collapse to 20 under a title-only rule.
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9+.#]*", variant):
                pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(variant)}(?![A-Za-z0-9])", re.I)
                def matches(card: dict) -> bool:
                    haystack = " ".join([str(card.get("title", "")),
                                         " ".join(str(tag) for tag in card.get("tags") or [])])
                    return bool(pattern.search(haystack))
                matched = [card for card in cards if matches(card)]
                if matched:
                    cards = matched
                    break
                cards = []
                restricted = True
                continue
            break
        if not cards:
            return []
        seen = {card["url"] for card in cards}
        # Follow the simulator's visible pagination controls, not its API.
        # A bounded page budget and duplicate-page guard prevent refresh loops.
        for _ in range(16):
            if len(cards) >= max_cards:
                break
            next_button = page.get_by_role("button", name="下一页", exact=True)
            if not await next_button.count() or await next_button.is_disabled():
                break
            previous_page = await page.locator("#pagination .page-summary").text_content()
            await next_button.click()
            await page.wait_for_function(
                "previous => (document.querySelector('#pagination .page-summary')?.textContent || '') !== previous && "
                "(document.querySelector('#result-count')?.textContent || '').includes('为你找到')",
                arg=previous_page, timeout=10_000,
            )
            rows = await self.extract_search_cards(page)
            fresh = [row for row in rows if row["url"] not in seen]
            if not fresh:
                break
            seen.update(row["url"] for row in fresh)
            cards.extend(fresh)
        return cards[:max_cards]

    async def extract_search_cards(self, page: Page) -> list[dict]:
        rows = await page.evaluate(
            r"""() => Array.from(document.querySelectorAll('#jobs .modern-job-card')).map(card => {
              const onclick = card.getAttribute('onclick') || '';
              const id = (onclick.match(/job[.]html[?]id=(\d+)/) || [])[1] || '';
              const meta = (card.querySelector('.job-card-top p')?.textContent || '').split('·').map(x => x.trim());
              return {
                id,
                title: card.querySelector('h3')?.textContent?.trim() || '',
                company: card.querySelector('.company-line b')?.textContent?.trim() || '',
                location: meta[0] || '',
                salary: card.querySelector('.salary')?.textContent?.trim() || '',
                tags: Array.from(card.querySelectorAll('.job-tags span')).map(x => x.textContent?.trim() || '').filter(Boolean)
              };
            }).filter(item => item.id && item.title)"""
        )
        return [
            {
                **row,
                "url": f"{MOCK_SITE_ORIGIN}/job.html?id={row['id']}",
                "external_id": row["id"],
            }
            for row in rows
        ]

    async def wait_for_job_detail(self, page: Page) -> None:
        await page.locator("#job-hero h1").wait_for(state="visible", timeout=10_000)

    async def extract_job_detail(self, page: Page, card: dict) -> dict:
        raw = await page.evaluate(
            r"""() => ({
              title: document.querySelector('#job-hero h1')?.textContent?.trim() || '',
              company: document.querySelector('.company-card h3')?.textContent?.trim() || '',
              location: document.querySelector('.map-box b')?.textContent?.trim() || '',
              salary: document.querySelector('#job-hero .detail-title-row > strong')?.textContent?.trim() || '',
              description: document.querySelector('.description')?.textContent?.trim() || '',
              requirements: Array.from(document.querySelectorAll('.requirements li')).map(x => x.textContent?.trim() || '').filter(Boolean),
              tags: Array.from(document.querySelectorAll('.detail-tags span')).map(x => x.textContent?.trim() || '').filter(Boolean)
            })"""
        )
        requirements = [str(item) for item in raw.get("requirements") or []]
        description = str(raw.get("description") or "")
        if requirements:
            description += "\n任职要求：\n" + "\n".join(f"- {item}" for item in requirements)
        return {
            "title": str(raw.get("title") or card.get("title") or "未命名职位"),
            "company": str(raw.get("company") or card.get("company") or "未知公司"),
            "location": str(raw.get("location") or card.get("location") or "未注明"),
            "salary": str(raw.get("salary") or card.get("salary") or "未注明"),
            "description": description,
            "tags": list(dict.fromkeys([*(card.get("tags") or []), *(raw.get("tags") or [])])),
            "external_id": card.get("external_id"),
        }
