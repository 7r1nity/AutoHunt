"""Read-only public DOM adapter; no private API, contact or submission actions."""
from __future__ import annotations

import re
from urllib.parse import quote_plus, urljoin, urlsplit

from playwright.async_api import Error as PlaywrightError
from .boss_adapter import BossAdapter, BossPageEvidence

ORIGIN = "https://www.zhaopin.com"
CARDS = 'a[href*="/jobdetail/"], a[href*="jobs.zhaopin.com/"]'
DETAIL = '.describtion, .job-detail, .job-description, [class*="job-description"], [class*="jobDescribe"]'


class ZhaopinAdapter(BossAdapter):
    origin = ORIGIN
    label = "智联招聘"
    selector_version = "zhaopin-public-dom-2026-10-10"

    @staticmethod
    def search_url(query):
        return f"{ORIGIN}/sou/?kw={quote_plus(query)}"

    @staticmethod
    def safe_job_url(value):
        try:
            parsed = urlsplit(urljoin(ORIGIN, value))
            port = parsed.port
        except (TypeError, ValueError):
            return None
        if parsed.scheme != "https" or parsed.username or parsed.password or port not in {None, 443}:
            return None
        if parsed.hostname == "www.zhaopin.com" and re.fullmatch(r"/jobdetail/[A-Za-z0-9_-]+\.htm", parsed.path):
            return parsed.geturl()
        if parsed.hostname == "jobs.zhaopin.com" and re.fullmatch(r"/[A-Za-z0-9_-]+\.htm", parsed.path):
            return parsed.geturl()
        return None

    async def inspect(self, page):
        raw = await self._evaluate_stable(page, r'''config => {
          const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
          const text = (document.body?.innerText || '').slice(0, 12000);
          const account = Array.from(document.querySelectorAll('a,button,span')).some(el => visible(el) && /退出登录|我的简历|我的求职/.test(el.textContent || ''));
          const verification = /安全验证|拖动滑块|完成验证/.test(text) || /captcha|verify/.test(location.href);
          const login = /passport\.zhaopin\.com|\/login/.test(location.href) || Array.from(document.querySelectorAll('input[placeholder*="手机号"], [class*="login-modal"], [class*="loginDialog"]')).some(visible);
          return {url: location.href, title: document.title, ready_state: document.readyState,
            card_count: document.querySelectorAll(config.cards).length, login_control_visible: login && !account,
            verification_visible: verification, loading_visible: /正在加载|加载中/.test(text), authenticated_signal: account};
        }''', {"cards": CARDS})
        return BossPageEvidence(**raw, selector_version=self.selector_version)

    async def select_city(self, page, city):
        # Do not invent city IDs or silently trust a keyword as a city filter.
        # A local evidence check enforces the requested city after detail reading.
        return False

    async def submit_search(self, page, query):
        inputs = page.locator('input[placeholder*="职位"], input[placeholder*="公司"]')
        for index in range(await inputs.count()):
            field = inputs.nth(index)
            if await field.is_visible():
                await field.fill(query)
                await field.press("Enter")
                return True
        return False

    async def wait_for_search_cards(self, page, *, timeout_ms=15000):
        try:
            await page.locator(CARDS).first.wait_for(state="attached", timeout=timeout_ms)
            return True
        except PlaywrightError:
            return False

    async def wait_for_job_detail(self, page, *, timeout_ms=8000):
        try:
            await page.locator(DETAIL).first.wait_for(state="attached", timeout=timeout_ms)
            return True
        except PlaywrightError:
            return False

    async def extract_search_cards(self, page):
        cards = await self._evaluate_stable(page, r'''config => {
          const seen = new Set();
          return Array.from(document.querySelectorAll(config.cards)).map(a => {
            if (seen.has(a.href)) return null; seen.add(a.href);
            const card = a.closest('.joblist-box__item, .jobinfo, [class*="job-card"], li') || a.parentElement;
            const pick = selectors => selectors.map(s => card?.querySelector(s)?.textContent?.trim()).find(Boolean) || '';
            return {url: a.href, title: a.textContent?.trim().slice(0, 200) || '未命名职位',
              company: pick(['.companyinfo__name', '.company-name', 'a[href*="/companydetail/"]']),
              salary: pick(['.jobinfo__salary', '.salary', '[class*="salary"]']),
              location: pick(['.jobinfo__other-info-item', '.job-location', '[class*="location"]']),
              tags: Array.from(card?.querySelectorAll('[class*="tag"]') || []).map(el => el.textContent?.trim()).filter(Boolean).slice(0, 15)};
          }).filter(Boolean);
        }''', {"cards": CARDS})
        return [card for card in cards if self.safe_job_url(card.get("url", ""))]

    async def extract_job_detail(self, page, card):
        detail = await self._evaluate_stable(page, r'''config => {
          const pick = selectors => selectors.map(s => document.querySelector(s)?.innerText?.trim()).find(Boolean) || '';
          return {title: pick(['h1', '.summary-plane__title']),
            company: pick(['.company-name', '.company__title', 'a[href*="/companydetail/"]']),
            location: pick(['.summary-plane__info .info__city', '[class*="job-address"]', '[class*="location"]']),
            salary: pick(['.summary-plane__salary', '[class*="salary"]']),
            description: pick(config.detail.split(', ')),
            tags: Array.from(document.querySelectorAll('[class*="welfare"], .job-tags span')).map(el => el.innerText?.trim()).filter(Boolean)};
        }''', {"detail": DETAIL})
        return {key: detail.get(key) or card.get(key) or ("" if key != "tags" else [])
                for key in ("title", "company", "location", "salary", "description", "tags")}
