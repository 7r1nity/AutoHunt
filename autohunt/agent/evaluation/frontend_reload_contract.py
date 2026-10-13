"""Isolated real-browser frontend reload smoke test, NOT recruitment E2E.

Run from agent/: .venv/Scripts/python.exe -m evaluation.frontend_reload_contract
Requires the local frontend only. Blocks every Agent API and external request;
never loads private history, attaches CDP, or invokes recruitment actions.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright


async def run(url: str, seconds: float) -> dict:
    origin = urlsplit(url)
    if origin.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Only a local frontend is permitted")
    frontend = Path(__file__).resolve().parents[2] / "frontend"
    package = json.loads((frontend / "package.json").read_text(encoding="utf-8"))
    assert "--webpack" in package["scripts"]["dev"], "Dev startup must select Webpack"
    result = {"test": "frontend_reload_contract", "seconds": seconds,
              "navigations": 0, "navigation_urls": [], "document_requests": 0,
              "blocked_agent_requests": 0,
              "blocked_external_requests": 0, "page_errors": [], "hmr_errors": []}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(service_workers="block")

            async def intercept(route):
                target = urlsplit(route.request.url)
                if (target.scheme, target.netloc) != (origin.scheme, origin.netloc):
                    result["blocked_external_requests"] += 1
                    await route.abort()
                elif target.path.startswith("/api/"):
                    result["blocked_agent_requests"] += 1
                    await route.fulfill(status=503, content_type="application/json",
                                        body='{"detail":"Isolated frontend fixture: backend unavailable"}')
                else:
                    await route.continue_()

            await context.route("**/*", intercept)
            page = await context.new_page()
            # Next's history.replaceState can emit framenavigated without loading
            # a document. Count actual document creation and network requests too.
            await page.add_init_script("""
                if (location.protocol === 'http:' || location.protocol === 'https:') {
                    const key = 'autohunt-smoke-document-count';
                    sessionStorage.setItem(key, String(Number(sessionStorage.getItem(key) || 0) + 1));
                }
            """)

            def navigation(frame):
                if frame == page.main_frame and frame.url != "about:blank":
                    result["navigations"] += 1
                    result["navigation_urls"].append(frame.url)

            def request_started(request):
                if request.is_navigation_request() and request.frame == page.main_frame:
                    result["document_requests"] += 1

            def console(message):
                if any(term in message.text.lower() for term in
                       ("turbopack", "fast refresh had to", "next.js package not found")):
                    result["hmr_errors"].append(message.text[:400])

            page.on("framenavigated", navigation)
            page.on("request", request_started)
            page.on("pageerror", lambda error: result["page_errors"].append(str(error)[:400]))
            page.on("console", console)
            response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            result["http_status"] = response.status if response else None
            await page.wait_for_timeout(seconds * 1000)
            result["title"] = await page.title()
            result["document_initializations"] = await page.evaluate(
                "Number(sessionStorage.getItem('autohunt-smoke-document-count') || 0)")
            result["passed"] = (result["http_status"] == 200
                                and result["document_requests"] == 1
                                and result["document_initializations"] == 1
                                and result["blocked_agent_requests"] > 0
                                and not result["page_errors"] and not result["hmr_errors"])
        finally:
            await browser.close()
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:3000/")
    parser.add_argument("--seconds", type=float, default=40)
    args = parser.parse_args()
    if not 10 <= args.seconds <= 300:
        parser.error("Observation must be between 10 and 300 seconds")
    result = asyncio.run(run(args.url, args.seconds))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    output = Path(__file__).parent / "results" / "frontend-reload" / stamp
    output.mkdir(parents=True, exist_ok=True)
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    print(f"Log: {output / 'result.json'}")
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
