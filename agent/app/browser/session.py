from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from playwright.async_api import Browser, BrowserContext, Error as PlaywrightError, Page, Playwright, async_playwright

from ..core.models import BrowserMode
from .lifecycle import BrowserLifecycle


CDP_KEEPER_URL = "about:blank#autohunt-keeper"


class BrowserSession:
    """A browser connection that can attach over CDP or own a managed profile."""

    def __init__(self, profile_dir: Path, *, headless: bool, cdp_url: str) -> None:
        self.profile_dir = profile_dir
        self.headless = headless
        self.cdp_url = cdp_url
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.mode = "unknown"
        self.owns_context = False
        self.disconnected = asyncio.Event()
        self.chrome_process: subprocess.Popen | None = None
        self.connection_generation = 0
        self.lifecycle = BrowserLifecycle(profile_dir.parent / 'private' / 'browser-lifecycle.jsonl')

    async def open(self, requested: BrowserMode) -> str:
        self.playwright = await async_playwright().start()
        if requested in {BrowserMode.AUTO, BrowserMode.CDP}:
            try:
                self.context = await self._connect_cdp_with_retry()
                self.mode = BrowserMode.CDP.value
                return self.mode
            except Exception as exc:
                if requested == BrowserMode.CDP:
                    await self.close()
                    raise RuntimeError(
                        f"无法启动或连接 Chrome CDP：{self.cdp_url}（{type(exc).__name__}: {str(exc)[:120]}）"
                    ) from exc

        self.context = await self.playwright.chromium.launch_persistent_context(
            str(self.profile_dir),
            headless=self.headless,
            viewport={"width": 1360, "height": 900},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        self.mode = BrowserMode.MANAGED.value
        self.owns_context = True
        self.context.on("close", lambda: self.disconnected.set())
        self.lifecycle.watch_context(self.context)
        return self.mode

    async def _connect_cdp_with_retry(self):
        """Attach to CDP Chrome, re-resolving (and relaunching Chrome when the
        window was closed) between attempts so a mid-flight browser shutdown does
        not permanently fail the task."""
        last_error: Exception | None = None
        for _ in range(2):
            try:
                websocket_url = await self._ensure_cdp_websocket()
                browser = await self.playwright.chromium.connect_over_cdp(websocket_url, timeout=12_000)
                contexts = browser.contexts
                if not contexts:
                    raise RuntimeError("CDP Chrome 没有可用上下文")
                self.connection_generation += 1
                generation = self.connection_generation
                browser.on("disconnected", lambda: self._mark_disconnected(generation))
                self.browser = browser
                self.lifecycle.record('cdp.connected')
                self.lifecycle.watch_context(contexts[0])
                return contexts[0]
            except Exception as exc:
                last_error = exc
                self.browser = None
                await asyncio.sleep(0.9)
        raise RuntimeError(f"{type(last_error).__name__}: {last_error}") from last_error

    async def ensure_cdp_browser(self, initial_url: str | None = None) -> None:
        """Start/wait for local Chrome without attaching Playwright to it."""
        await self._ensure_cdp_websocket(initial_url)

    async def reuse(self, requested: BrowserMode) -> str | None:
        """Reuse or reconnect this session without stopping Playwright.

        CDP Chrome is intentionally long lived. A completed search must not
        detach and create another browser attachment for the next search: on
        Windows that cycle can close the only visible Chrome window. Return
        ``None`` only when this session belongs to another browser mode.
        """
        if self.mode != requested.value:
            return None
        if requested == BrowserMode.CDP:
            await self._ensure_cdp_websocket()
            connected = bool(self.browser and self.browser.is_connected())
            if not connected or self.context is None:
                if self.playwright is None:
                    self.playwright = await async_playwright().start()
                self.context = await self._connect_cdp_with_retry()
            self.disconnected.clear()
            self.owns_context = False
            return self.mode
        if self.context is not None:
            return self.mode
        return None

    def _mark_disconnected(self, generation: int) -> None:
        self.lifecycle.record('browser.disconnected')
        if generation == self.connection_generation:
            self.disconnected.set()

    async def ensure_cdp_window(self, target_url: str) -> None:
        """Open or focus a visible BOSS tab without attaching Playwright.

        The login phase must remain completely detached. A temporary
        connect_over_cdp followed by playwright.stop can tear down a newly
        launched remote-debugging Chrome on Windows. Chrome's localhost
        discovery endpoints are sufficient for opening or activating a tab.
        """
        try:
            await self._ensure_cdp_websocket(target_url)
            await asyncio.to_thread(self._ensure_visible_cdp_tab, target_url)
        except Exception:
            # Visibility is best-effort; the waiting_for_user phase lets the
            # user open the browser manually.
            return

    def _ensure_visible_cdp_tab(self, target_url: str) -> None:
        """Open or focus a visible BOSS tab without attaching Playwright.

        Legacy keeper tabs (about:blank#autohunt-keeper) are actively closed:
        they were a previous workaround and must not accumulate.
        """
        self._validated_cdp_endpoint()
        base = self.cdp_url.rstrip("/")
        with urlopen(base + "/json/list", timeout=4) as response:
            targets = json.loads(response.read().decode("utf-8"))
        parsed_target = urlparse(target_url)
        target_host = (parsed_target.hostname or "").lower()
        target_port = parsed_target.port
        site_target = None
        for item in targets:
            if item.get("type") != "page":
                continue
            url = str(item.get("url") or "")
            if url == CDP_KEEPER_URL:
                target_id = item.get("id")
                if target_id:
                    self._close_cdp_tab(target_id)
                continue
            parsed = urlparse(url)
            if site_target is None and (parsed.hostname or "").lower() == target_host and parsed.port == target_port:
                site_target = item
        if site_target and site_target.get("id"):
            try:
                with urlopen(base + "/json/activate/" + quote(str(site_target["id"]), safe=""), timeout=4):
                    return
            except Exception:
                # If activation is unavailable, the existing visible tab is
                # still preferable to creating duplicates.
                return
        self._create_cdp_tab(target_url)

    def _create_cdp_tab(self, target_url: str) -> None:
        base = self.cdp_url.rstrip("/")
        encoded_target = quote(target_url, safe=":/?=&%")
        request = Request(base + "/json/new?" + encoded_target, method="PUT")
        with urlopen(request, timeout=8):
            return

    def _close_cdp_tab(self, target_id: str) -> None:
        """Best-effort close of a stray tab via the CDP HTTP endpoint."""
        base = self.cdp_url.rstrip("/")
        request = Request(
            base + "/json/close/" + quote(str(target_id), safe=""),
            method="PUT",
            data=b"",
        )
        try:
            with urlopen(request, timeout=4):
                return
        except Exception:
            return

    async def _ensure_cdp_websocket(self, initial_url: str | None = None) -> str:
        """Connect to local CDP, starting AutoHunt's visible Chrome if needed."""
        try:
            return await asyncio.to_thread(self._resolve_cdp_websocket)
        except Exception:
            await asyncio.to_thread(self._launch_local_cdp_chrome, initial_url)

        deadline = time.monotonic() + 12
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                return await asyncio.to_thread(self._resolve_cdp_websocket)
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(0.35)
        raise RuntimeError(f"Chrome started but CDP did not become ready: {last_error}") from last_error

    def _launch_local_cdp_chrome(self, initial_url: str | None = None) -> None:
        parsed = self._validated_cdp_endpoint()
        if os.name != "nt":
            raise RuntimeError("Automatic CDP Chrome launch is currently supported on Windows only")

        roots = [
            os.environ.get("PROGRAMFILES"),
            os.environ.get("PROGRAMFILES(X86)"),
            os.environ.get("LOCALAPPDATA"),
        ]
        candidates = [
            Path(roots[0]) / "Google/Chrome/Application/chrome.exe" if roots[0] else None,
            Path(roots[1]) / "Google/Chrome/Application/chrome.exe" if roots[1] else None,
            Path(roots[2]) / "Google/Chrome/Application/chrome.exe" if roots[2] else None,
        ]
        chrome_path = next((path for path in candidates if path and path.is_file()), None)
        if chrome_path is None:
            raise RuntimeError("Google Chrome executable was not found")

        cdp_profile = self.profile_dir.parent / "cdp-chrome-profile"
        cdp_profile.mkdir(parents=True, exist_ok=True)
        port = parsed.port or 9223
        # DETACHED_PROCESS keeps the GUI Chrome out of the backend's job object:
        # without it, a backend reload/exit takes the logged-in Chrome window
        # down with it ("登录之后浏览器自动关闭"). CREATE_NEW_PROCESS_GROUP keeps
        # Ctrl+C group semantics separate from the backend console.
        creation_flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        log_path = cdp_profile / "chrome_debug.log"
        self.chrome_process = subprocess.Popen(
            [
                str(chrome_path),
                "--remote-debugging-address=127.0.0.1",
                f"--remote-debugging-port={port}",
                f"--user-data-dir={cdp_profile}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-blink-features=AutomationControlled",
                "--enable-logging",
                f"--log-file={log_path}",
                "--new-window",
                initial_url or "about:blank",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creation_flags,
        )

    def _validated_cdp_endpoint(self):
        parsed = urlparse(self.cdp_url)
        configured_host = parsed.hostname or ""
        if parsed.scheme not in {"http", "https"} or configured_host not in {"127.0.0.1", "localhost", "::1"}:
            raise RuntimeError("CDP endpoint must be a localhost HTTP(S) address")
        return parsed

    def _resolve_cdp_websocket(self) -> str:
        parsed = self._validated_cdp_endpoint()
        configured_host = parsed.hostname or "127.0.0.1"
        endpoint = self.cdp_url.rstrip("/") + "/json/version"
        with urlopen(endpoint, timeout=4) as response:  # localhost-only configuration
            payload = json.loads(response.read().decode("utf-8"))
        websocket_url = str(payload.get("webSocketDebuggerUrl") or "")
        if not websocket_url.startswith("ws://"):
            raise RuntimeError("CDP endpoint did not return a WebSocket URL")
        return websocket_url.replace("ws://localhost:", f"ws://{configured_host}:", 1)

    def lifecycle_snapshot(self) -> str:
        try:
            connected = bool(self.browser and self.browser.is_connected())
        except Exception:
            connected = False
        try:
            page_count = len(self.pages())
        except Exception:
            page_count = -1
        try:
            with urlopen(self.cdp_url.rstrip("/") + "/json/version", timeout=0.5) as response:
                cdp_alive = response.status == 200
        except Exception:
            cdp_alive = False
        return (
            f"mode={self.mode}; browser_connected={connected}; pages={page_count}; "
            f"disconnect_event={self.disconnected.is_set()}; cdp_http={cdp_alive}"
        )

    def pages(self) -> list[Page]:
        if not self.context:
            return []
        return [page for page in self.context.pages if not page.is_closed()]

    def page_for_url(self, target_url: str) -> Page | None:
        """Return the newest open page with the same hostname and port."""
        parsed_target = urlparse(target_url)
        target_host = (parsed_target.hostname or "").lower()
        target_port = parsed_target.port
        candidates = []
        for page in self.pages():
            parsed = urlparse(page.url)
            if (parsed.hostname or "").lower() == target_host and parsed.port == target_port:
                candidates.append(page)
        return candidates[-1] if candidates else None
    def boss_page(self, target_url: str | None = None) -> Page | None:
        pages = self.pages()
        candidates = [page for page in pages if "zhipin.com" in page.url.lower()]
        if not candidates:
            return None
        target_path = urlparse(target_url).path if target_url else ""
        if target_path and target_path != "/":
            matching = [page for page in candidates if target_path in urlparse(page.url).path]
            if matching:
                return matching[-1]
        return candidates[-1]

    async def recover_page(self, target_url: str) -> Page:
        """Rebind to an existing BOSS tab without creating tabs mid-task.

        ``ensure_cdp_window`` is the only place allowed to open a BOSS target,
        once at task startup. During an active task, BOSS may replace its SPA
        target; we wait for and rebind that replacement. If the user or site
        really closed the tab, fail clearly instead of opening a second tab and
        entering a close/open/refresh loop.
        """
        if not self.context:
            raise RuntimeError("浏览器会话未连接")
        page = self.page_for_url(target_url)
        if page is None:
            if self.mode == BrowserMode.CDP.value:
                page = await self._recover_cdp_page(target_url)
            else:
                page = await self.context.new_page()
                page = await self.navigate(target_url, page=page)
        elif page.url.lower() in {"about:blank", "chrome://newtab/"}:
            page = await self.navigate(target_url, page=page)
        page.set_default_timeout(15_000)
        return page

    async def _recover_cdp_page(self, target_url: str) -> Page:
        """Observe and rebind an existing replacement target; never create one.

        This follows the bounded-session lesson from AutoApply AI: browser page
        creation belongs to session startup, while recovery only observes and
        reconnects. It prevents a transient target loss from producing a new
        visible tab followed by repeated ``goto`` calls.
        """
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                await self._ensure_cdp_websocket()
                if self.context is None or self.browser is None or not self.browser.is_connected():
                    self.context = await self._connect_cdp_with_retry()
                    self.disconnected.clear()
                for _ in range(15):
                    page = self.page_for_url(target_url)
                    if page is not None:
                        page.set_default_timeout(15_000)
                        return page
                    await asyncio.sleep(0.2)
                # Refresh only the Playwright binding. Do not call /json/new.
                if attempt == 0:
                    self.context = None
            except Exception as exc:
                last_error = exc
            if attempt == 0:
                await asyncio.sleep(0.4)
        detail = str(last_error) if last_error else self.lifecycle_snapshot()
        host = urlparse(target_url).netloc or target_url
        raise RuntimeError(
            f"目标网站标签页（{host}）已关闭；为避免反复新建标签页和刷新，任务已停止自动恢复。"
            f"请保留或手动打开目标页面后重新搜索。{detail}"
        ) from last_error
    async def navigate(self, target_url: str, *, page: Page | None = None) -> Page:
        """Navigate while tolerating a login/risk-control flow replacing the tab."""
        if not self.context:
            raise RuntimeError("浏览器会话未连接")
        last_error: Exception | None = None
        attempts = 1 if self.mode == BrowserMode.CDP.value else 3
        for _ in range(attempts):
            candidate = page if page and not page.is_closed() else self.page_for_url(target_url)
            if candidate is None:
                pages = [item for item in self.pages() if item.url != CDP_KEEPER_URL]
                if pages:
                    candidate = pages[-1]
                elif self.mode == BrowserMode.CDP.value:
                    candidate = await self._recover_cdp_page(target_url)
                else:
                    candidate = await self.context.new_page()
            try:
                await candidate.goto(target_url, wait_until="domcontentloaded", timeout=45_000)
                candidate.set_default_timeout(15_000)
                return candidate
            except PlaywrightError as exc:
                last_error = exc
                await asyncio.sleep(0.75)
                replacement = self.page_for_url(target_url)
                # BOSS may abort navigation after replacing the document/tab with a
                # login or verification page. That replacement is a valid takeover
                # surface and must be inspected by the adapter, not treated as loss.
                if replacement and not replacement.is_closed() and replacement.url.lower() not in {"about:blank", "chrome://newtab/"}:
                    replacement.set_default_timeout(15_000)
                    return replacement
                page = replacement
        raise RuntimeError(f"目标网站导航无法恢复：{last_error}") from last_error

    async def close(self) -> None:
        """Detach from the browser session.

        A CDP-attached Chrome belongs to the user: Playwright only stops its
        local driver, which never closes a browser it connected to over CDP
        (it does not own it). Managed profiles are owned by us and are closed.
        """
        self.lifecycle.record('session.close_requested')
        if self.owns_context and self.context:
            await self.context.close()
        if self.playwright:
            self.lifecycle.record('driver.stop_requested')
            await self.playwright.stop()
        self.context = None
        self.browser = None
        self.playwright = None
        self.owns_context = False
