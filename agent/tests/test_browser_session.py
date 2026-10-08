from app.browser.session import BrowserSession
from app.core.models import BrowserMode


async def test_cdp_close_stops_local_driver_and_never_closes_remote_chrome(tmp_path):
    """close() detaches cleanly: the user's CDP Chrome context is never closed,
    and Playwright's graceful driver stop is used instead of private-API hacks."""

    class FakeContext:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    class FakePlaywright:
        def __init__(self):
            self.stop_called = False

        async def stop(self):
            self.stop_called = True

    session = BrowserSession(
        tmp_path / "profile",
        headless=False,
        cdp_url="http://127.0.0.1:9223",
    )
    playwright = FakePlaywright()
    context = FakeContext()
    session.playwright = playwright
    session.mode = BrowserMode.CDP.value
    session.context = context  # CDP-attached → not owned by us
    session.owns_context = False

    await session.close()

    assert context.closed is False
    assert playwright.stop_called is True
    assert session.playwright is None
    assert session.context is None


async def test_cdp_reuse_keeps_existing_driver_and_context(tmp_path):
    class FakeBrowser:
        def is_connected(self):
            return True

    session = BrowserSession(
        tmp_path / "profile", headless=False, cdp_url="http://127.0.0.1:9223"
    )
    context = object()
    playwright = object()
    session.mode = BrowserMode.CDP.value
    session.browser = FakeBrowser()
    session.context = context
    session.playwright = playwright

    async def ensure_websocket():
        return "ws://127.0.0.1:9223/devtools/browser/test"

    session._ensure_cdp_websocket = ensure_websocket

    mode = await session.reuse(BrowserMode.CDP)

    assert mode == BrowserMode.CDP.value
    assert session.context is context
    assert session.playwright is playwright


async def test_cdp_reuse_reconnects_without_restarting_playwright(tmp_path):
    class DisconnectedBrowser:
        def is_connected(self):
            return False

    session = BrowserSession(
        tmp_path / "profile", headless=False, cdp_url="http://127.0.0.1:9223"
    )
    playwright = object()
    replacement_context = object()
    session.mode = BrowserMode.CDP.value
    session.browser = DisconnectedBrowser()
    session.context = object()
    session.playwright = playwright
    session.disconnected.set()

    async def ensure_websocket():
        return "ws://127.0.0.1:9223/devtools/browser/test"

    async def reconnect():
        return replacement_context

    session._ensure_cdp_websocket = ensure_websocket
    session._connect_cdp_with_retry = reconnect

    mode = await session.reuse(BrowserMode.CDP)

    assert mode == BrowserMode.CDP.value
    assert session.context is replacement_context
    assert session.playwright is playwright
    assert session.disconnected.is_set() is False


async def test_cdp_checkpoint_skips_browser_screenshot(tmp_path):
    from types import SimpleNamespace

    from app.browser.automation import BrowserAutomationService

    class FakeStore:
        def __init__(self):
            self.events = []
            self.checkpoints = []

        def event(self, *args):
            self.events.append(args)

        def checkpoint(self, *args, **kwargs):
            self.checkpoints.append((args, kwargs))

    class FakePage:
        url = "https://www.zhipin.com/shenzhen/"

        def __init__(self):
            self.screenshot_called = False

        async def screenshot(self, **_kwargs):
            self.screenshot_called = True

    service = BrowserAutomationService.__new__(BrowserAutomationService)
    service.session = SimpleNamespace(mode=BrowserMode.CDP.value)
    service.store = FakeStore()
    service.data_root = tmp_path
    service.evidence_dir = tmp_path / "evidence"
    page = FakePage()

    await service._record_checkpoint(
        "task-1",
        "login_preflight",
        page,
        {"authenticated_signal": True},
        screenshot=True,
    )

    assert page.screenshot_called is False
    assert service.store.events[0][1] == "screenshot_skipped"
    assert service.store.checkpoints[0][1]["screenshot_path"] is None

async def test_page_readiness_recovers_closed_cdp_tab():
    from types import SimpleNamespace

    from app.browser.automation import BrowserAutomationService

    class ClosedPage:
        def is_closed(self):
            return True

    class ReadyPage:
        url = "https://www.zhipin.com/"

        def is_closed(self):
            return False

        async def wait_for_timeout(self, _milliseconds):
            return None

    replacement = ReadyPage()

    class FakeSession:
        def __init__(self):
            self.recoveries = []

        async def recover_page(self, target_url):
            self.recoveries.append(target_url)
            return replacement

    class FakeAdapter:
        async def inspect(self, page):
            assert page is replacement
            return SimpleNamespace(
                intervention_reason=None,
                card_count=1,
                loading_visible=False,
                url=page.url,
                ready_state="complete",
            )

    service = BrowserAutomationService.__new__(BrowserAutomationService)
    service.session = FakeSession()
    service.adapter = FakeAdapter()

    result = await service._wait_for_page_readiness(
        ClosedPage(), "https://www.zhipin.com/", timeout_seconds=1
    )

    assert result is replacement
    assert service.session.recoveries == ["https://www.zhipin.com/"]


async def test_page_readiness_recovers_when_wait_loses_target():
    """A wait that loses its target (BOSS replacing the tab) must rebind
    instead of surfacing a bare TargetClosedError."""
    from types import SimpleNamespace

    from playwright.async_api import Error as PlaywrightError

    from app.browser.automation import BrowserAutomationService

    class ClosingPage:
        url = "https://www.zhipin.com/shenzhen/"

        def is_closed(self):
            return False

        async def wait_for_timeout(self, _milliseconds):
            raise PlaywrightError(
                "Page.wait_for_timeout: Target page, context or browser has been closed"
            )

    class StablePage:
        url = "https://www.zhipin.com/"

        def is_closed(self):
            return False

        async def wait_for_timeout(self, _milliseconds):
            return None

    closing = ClosingPage()
    replacement = StablePage()

    class FakeSession:
        def __init__(self):
            self.recoveries = []

        async def recover_page(self, target_url):
            self.recoveries.append(target_url)
            return replacement

    class FakeAdapter:
        async def inspect(self, page):
            if page is closing:
                return SimpleNamespace(
                    intervention_reason=None,
                    card_count=0,
                    loading_visible=True,
                    url=page.url,
                    ready_state="loading",
                )
            return SimpleNamespace(
                intervention_reason=None,
                card_count=0,
                loading_visible=False,
                url=page.url,
                ready_state="complete",
            )

    service = BrowserAutomationService.__new__(BrowserAutomationService)
    service.session = FakeSession()
    service.adapter = FakeAdapter()

    result = await service._wait_for_page_readiness(
        closing, "https://www.zhipin.com/", timeout_seconds=1
    )

    assert result is replacement
    assert service.session.recoveries == ["https://www.zhipin.com/"]
async def test_cdp_window_closes_stale_keeper_and_activates_boss(tmp_path, monkeypatch):
    import json

    import app.browser.session as session_module
    from app.browser.session import CDP_KEEPER_URL

    calls = []

    class FakeResponse:
        def __init__(self, payload=b""):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return self.payload

    targets = json.dumps([
        {"type": "page", "id": "keeper-1", "url": CDP_KEEPER_URL},
        {"type": "page", "id": "boss-1", "url": "https://www.zhipin.com/"},
    ]).encode()

    def fake_urlopen(request, timeout):
        url = request if isinstance(request, str) else request.full_url
        calls.append((url, timeout))
        if url.endswith("/json/list"):
            return FakeResponse(targets)
        return FakeResponse()

    monkeypatch.setattr(session_module, "urlopen", fake_urlopen)
    session = BrowserSession(
        tmp_path / "profile", headless=False, cdp_url="http://127.0.0.1:9223"
    )

    session._ensure_visible_cdp_tab("https://www.zhipin.com/")

    # Stale keeper tabs are actively closed; no new keeper is ever created.
    assert any("/json/close/keeper-1" in url for url, _ in calls)
    assert any("/json/activate/boss-1" in url for url, _ in calls)
    assert not any("/json/new" in url for url, _ in calls)
    assert CDP_KEEPER_URL == "about:blank#autohunt-keeper"


async def test_cdp_recovery_rebinds_existing_target_without_creating_tab(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app.browser.session import CDP_KEEPER_URL

    class FakePage:
        def __init__(self, url):
            self.url = url
            self.timeout = None

        def is_closed(self):
            return False

        def set_default_timeout(self, timeout):
            self.timeout = timeout

    keeper = FakePage(CDP_KEEPER_URL)
    boss = FakePage("https://www.zhipin.com/")

    class FakeContext:
        def __init__(self):
            self.pages = [keeper]

        async def new_page(self):
            raise AssertionError("CDP recovery must not call BrowserContext.new_page")

    session = BrowserSession(
        tmp_path / "profile", headless=False, cdp_url="http://127.0.0.1:9223"
    )
    session.mode = BrowserMode.CDP.value
    session.context = FakeContext()
    session.browser = None

    monkeypatch.setattr(session, "_resolve_cdp_websocket", lambda: "ws://127.0.0.1:9223/devtools/browser/test")
    monkeypatch.setattr(
        session,
        "_ensure_visible_cdp_tab",
        lambda _url: (_ for _ in ()).throw(AssertionError("recovery must not create a CDP target")),
    )

    async def fake_connect():
        session.context.pages.append(boss)
        session.browser = SimpleNamespace(is_connected=lambda: True)
        return session.context

    monkeypatch.setattr(session, "_connect_cdp_with_retry", fake_connect)

    recovered = await session.recover_page("https://www.zhipin.com/")

    assert recovered is boss
    assert keeper in session.context.pages
    assert boss.timeout == 15_000

def test_stale_cdp_disconnect_does_not_poison_new_connection(tmp_path):
    session = BrowserSession(
        tmp_path / "profile", headless=False, cdp_url="http://127.0.0.1:9223"
    )
    session.connection_generation = 2

    session._mark_disconnected(1)
    assert session.disconnected.is_set() is False

    session._mark_disconnected(2)
    assert session.disconnected.is_set() is True

def test_cdp_startup_creates_target_when_no_site_tab_exists(tmp_path, monkeypatch):
    import app.browser.session as module

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self): return b"[]"

    monkeypatch.setattr(module, "urlopen", lambda *args, **kwargs: Response())
    session = BrowserSession(tmp_path / "profile", headless=False, cdp_url="http://127.0.0.1:9223")
    created = []
    monkeypatch.setattr(session, "_create_cdp_tab", created.append)
    session._ensure_visible_cdp_tab("http://127.0.0.1:8080/")
    assert created == ["http://127.0.0.1:8080/"]
