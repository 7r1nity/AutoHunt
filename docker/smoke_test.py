"""Isolated Compose acceptance: browser MFA, takeover, proxy and durable state.

Run only against a new Compose test project. This script never starts an Agent
search, sends recruitment messages, submits applications or calls a model.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener


MOCK_ORIGIN = "http://127.0.0.1:8080"
AGENT_ORIGIN = "http://127.0.0.1:8000"
FRONTEND_ORIGIN = "http://frontend:3000"
VIEWER_ORIGIN = "http://127.0.0.1:6080"
CDP_ORIGIN = "http://127.0.0.1:9223"
CONFIG_FILE = Path("/run/autohunt-config/runtime.env")
MARKER_NAME = ".docker-smoke.json"
STORAGE_KEY = "autohuntDockerSmoke"


def require_isolation(explicit: bool) -> None:
    if not explicit or os.environ.get("AUTOHUNT_DOCKER_TEST") != "1":
        raise RuntimeError("Use --isolated-test and AUTOHUNT_DOCKER_TEST=1 in a new Compose test project.")


def http_request(url: str, *, token: str | None = None, body: dict | None = None):
    headers = {"X-AutoHunt-Key": token} if token else {}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    request = Request(url, data=data, headers=headers)
    # An inherited HTTP proxy must not receive internal authentication headers.
    with build_opener(ProxyHandler({})).open(request, timeout=15) as response:
        raw = response.read()
        return response.status, raw


def http_json(url: str, **kwargs):
    status, raw = http_request(url, **kwargs)
    if status not in {200, 201}:
        raise AssertionError("Unexpected HTTP status")
    return json.loads(raw)


def read_runtime_config(path: Path = CONFIG_FILE) -> tuple[str, str]:
    raw = path.read_bytes()
    values = dict(line.split("=", 1) for line in raw.decode().splitlines()
                  if line and not line.startswith("#") and "=" in line)
    token = values.get("AUTOHUNT_API_KEY", "")
    if not token or not values.get("AUTOHUNT_DATA_KEY"):
        raise AssertionError("Generated runtime credentials are incomplete")
    return token, hashlib.sha256(raw).hexdigest()


def check_novnc_websocket(host: str = "127.0.0.1", port: int = 6080) -> None:
    """Check both the WebSocket upgrade and live VNC server's RFB greeting."""
    key = base64.b64encode(secrets.token_bytes(16)).decode()
    request = (f"GET /websockify HTTP/1.1\r\nHost: {host}:{port}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Origin: http://{host}:{port}\r\nSec-WebSocket-Key: {key}\r\n"
               "Sec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: binary\r\n\r\n")
    with socket.create_connection((host, port), timeout=10) as connection:
        connection.settimeout(10)
        connection.sendall(request.encode("ascii"))
        raw = b""
        while b"\r\n\r\n" not in raw:
            part = connection.recv(4096)
            if not part or len(raw) > 16_384:
                raise AssertionError("noVNC WebSocket handshake was incomplete")
            raw += part
        header, payload = raw.split(b"\r\n\r\n", 1)
        if not header.split(b"\r\n", 1)[0].startswith(b"HTTP/1.1 101"):
            raise AssertionError("noVNC WebSocket upgrade failed")
        expected = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        fields = {line.split(b":", 1)[0].strip().lower(): line.split(b":", 1)[1].strip()
                  for line in header.split(b"\r\n")[1:] if b":" in line}
        if fields.get(b"sec-websocket-accept") != expected:
            raise AssertionError("noVNC WebSocket upgrade signature did not match")
        while b"RFB " not in payload:
            part = connection.recv(4096)
            if not part or len(payload) > 16_384:
                raise AssertionError("noVNC did not connect to the visible browser desktop")
            payload += part


async def current_identity(page) -> dict:
    return await page.evaluate("""async () => {
      const response = await fetch('/api/auth/me', {headers: authHeaders()});
      if (!response.ok) throw new Error('Mock identity verification failed');
      return response.json();
    }""")


async def seed_browser_login(page, email: str, marker: str) -> str:
    from app.browser.mock_site_adapter import MockJobSiteSearchAdapter

    if await page.evaluate("localStorage.getItem('userId')"):
        raise RuntimeError("Refusing to replace an existing browser login; use a new isolated test project.")
    await page.locator("#login-button").click()
    await page.locator("#auth-email").fill(email)
    await page.locator("#auth-name").fill("Docker Synthetic Applicant")
    await page.locator("#auth-submit").click()
    await page.locator("#demo-code").filter(has_text=re.compile(r"\d{6}")).wait_for()
    code_match = re.search(r"\d{6}", await page.locator("#demo-code").inner_text())
    if code_match is None:
        raise AssertionError("The simulated MFA code was not displayed for human takeover")
    code = code_match.group()
    evidence = await MockJobSiteSearchAdapter().inspect(page)
    if not evidence.login_control_visible or evidence.authenticated_signal:
        raise AssertionError("The Agent adapter did not retain login takeover while MFA was open")

    wrong_code = ("0" if code[0] != "0" else "1") + code[1:]
    await page.locator("#auth-code").fill(wrong_code)
    async with page.expect_response(lambda response: response.url.endswith("/api/auth/mfa/verify")) as denied:
        await page.locator("#auth-submit").click()
    if (await denied.value).status != 400:
        raise AssertionError("An incorrect MFA code was accepted")
    if await page.evaluate("localStorage.getItem('userId')"):
        raise AssertionError("Failed MFA created an authenticated browser identity")

    await page.locator("#auth-code").fill(code)
    async with page.expect_response(lambda response: response.url.endswith("/api/auth/register")) as registered:
        await page.locator("#auth-submit").click()
    if (await registered.value).status != 200:
        raise AssertionError("MFA succeeded but simulated applicant registration failed")
    await page.wait_for_function("!!localStorage.getItem('userId') && !document.querySelector('#login-modal').classList.contains('open')")
    identity = await current_identity(page)
    if identity.get("email") != email or identity.get("role") != "applicant":
        raise AssertionError("The browser login is not the expected synthetic applicant")
    await page.evaluate("([key, value]) => localStorage.setItem(key, value)", [STORAGE_KEY, marker])
    # A fresh document checks persistent storage rather than retained JS variables.
    await page.reload(wait_until="domcontentloaded")
    return str(identity["id"])


async def check_browser(phase: str, state: dict) -> None:
    from app.browser.mock_site_adapter import MockJobSiteSearchAdapter
    from app.browser.session import BrowserSession

    root = Path(os.environ.get("AUTOHUNT_DATA_ROOT", "/var/lib/autohunt"))
    session = BrowserSession(root / "browser-profile", headless=False, cdp_url=CDP_ORIGIN)
    try:
        if await session.attach_existing_cdp() != "cdp" or session.context is None:
            raise AssertionError("Application BrowserSession could not attach to the visible Chromium")
        page = session.page_for_url(MOCK_ORIGIN)
        if page is None:
            page = await session.context.new_page()
        page.set_default_timeout(20_000)
        await page.goto(MOCK_ORIGIN, wait_until="domcontentloaded")
        await page.locator("#jobs .modern-job-card").first.wait_for()
        if phase == "seed":
            state["applicant_id"] = await seed_browser_login(page, state["email"], state["marker"])
        saved_marker = await page.evaluate("key => localStorage.getItem(key)", STORAGE_KEY)
        if saved_marker != state["marker"]:
            raise AssertionError("The visible browser profile did not retain its synthetic storage marker")
        identity = await current_identity(page)
        if str(identity.get("id")) != state["applicant_id"] or identity.get("email") != state["email"]:
            raise AssertionError("The original logged-in synthetic applicant was not restored")
        evidence = await MockJobSiteSearchAdapter().inspect(page)
        if not evidence.authenticated_signal or evidence.login_control_visible:
            raise AssertionError("The Agent adapter did not recognize the completed human login")
    finally:
        await session.close()


def check_services(token: str) -> None:
    health = http_json(MOCK_ORIGIN + "/api/health")
    if health != {"service": "autohunt-mock-site", "status": "ok"}:
        raise AssertionError("Loopback simulator relay did not reach the expected database service")
    direct = http_json(AGENT_ORIGIN + "/api/v1/automation/stats", token=token)
    proxied = http_json(FRONTEND_ORIGIN + "/api/agent/stats")
    status, workbench = http_request(FRONTEND_ORIGIN + "/")
    if status != 200 or b"AutoHunt" not in workbench:
        raise AssertionError("The production workbench HTML was not available")
    assets = re.findall(rb'src="(/_next/static/[^"<>]+\.js(?:\?[^"<>]*)?)"', workbench)
    if not assets:
        raise AssertionError("The production workbench did not reference its JavaScript assets")
    asset_status, _ = http_request(FRONTEND_ORIGIN + assets[0].decode().replace('&amp;', '&'))
    if asset_status != 200:
        raise AssertionError("The production workbench JavaScript asset was not available")
    for name in ("discovered", "stored", "applying"):
        if name not in direct or proxied.get(name) != direct[name]:
            raise AssertionError("The production frontend proxy did not reach the authenticated Agent")
    try:
        http_request(AGENT_ORIGIN + "/api/v1/automation/stats")
    except HTTPError as exc:
        if exc.code != 401:
            raise AssertionError("Unauthenticated backend access returned an unexpected status") from None
    else:
        raise AssertionError("The Agent backend accepted an unauthenticated request")
    status, viewer = http_request(VIEWER_ORIGIN + "/vnc.html")
    if status != 200 or b"novnc" not in viewer.lower():
        raise AssertionError("The noVNC browser takeover page was not available")
    check_novnc_websocket()


async def run(phase: str) -> dict:
    token, config_digest = read_runtime_config()
    root = Path(os.environ.get("AUTOHUNT_DATA_ROOT", "/var/lib/autohunt"))
    marker_file = root / MARKER_NAME
    started = time.monotonic()
    check_services(token)
    if phase == "seed":
        if marker_file.exists():
            raise RuntimeError("Test state already exists; use phase verify or a new isolated project.")
        sessions = http_json(AGENT_ORIGIN + "/api/v1/conversations", token=token)
        if any(session.get("id") != "default" for session in sessions):
            raise RuntimeError("Refusing a nonempty Agent conversation store; use a new isolated test project.")
        state = {"marker": secrets.token_hex(12), "email": "docker-" + secrets.token_hex(6) + "@example.invalid",
                 "config_digest": config_digest}
        await check_browser(phase, state)
        created = http_json(AGENT_ORIGIN + "/api/v1/conversations", token=token,
                            body={"title": "Docker isolated persistence check"})
        state["session_id"] = created["id"]
        marker_file.write_text(json.dumps(state), encoding="utf-8")
    else:
        state = json.loads(marker_file.read_text(encoding="utf-8"))
        if config_digest != state["config_digest"]:
            raise AssertionError("Restart regenerated the persistent API token or data encryption key")
        sessions = http_json(AGENT_ORIGIN + "/api/v1/conversations", token=token)
        if not any(session.get("id") == state["session_id"] for session in sessions):
            raise AssertionError("The Agent database did not preserve the synthetic conversation across restart")
        await check_browser(phase, state)
    checks = ["simulator_database_health", "authenticated_frontend_proxy", "production_workbench_html",
              "production_workbench_assets", "backend_authentication",
              "novnc_html", "novnc_websocket_vnc", "application_cdp_attach", "adapter_login_evidence"]
    checks += (["wrong_mfa_rejected", "correct_mfa_login", "synthetic_state_saved"] if phase == "seed" else
               ["browser_login_persistence", "agent_database_persistence", "credential_persistence"])
    return {"phase": phase, "status": "passed", "checks": checks,
            "seconds": round(time.monotonic() - started, 3), "external_model_calls": 0,
            "searches_started": 0, "applications_submitted": 0}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--isolated-test", action="store_true")
    parser.add_argument("--phase", choices=("seed", "verify"), default="seed")
    arguments = parser.parse_args()
    require_isolation(arguments.isolated_test)
    print(json.dumps(asyncio.run(run(arguments.phase)), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
