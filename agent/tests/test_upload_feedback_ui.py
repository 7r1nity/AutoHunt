"""Opt-in upload UI regression. All API calls are intercepted; no files are persisted."""
import os
from urllib.parse import urlparse

import pytest
from playwright.sync_api import expect, sync_playwright


pytestmark = pytest.mark.skipif(
    os.getenv("AUTOHUNT_RESUME_UI") != "1", reason="Requires a running AutoHunt frontend"
)


@pytest.mark.parametrize("kind", ["resume", "material"])
def test_upload_failure_is_visible_next_to_file(kind):
    posts = []
    errors = []

    def api(route):
        path = urlparse(route.request.url).path
        if path.endswith(("/resumes", "/materials")):
            if route.request.method == "POST":
                posts.append(path)
                route.fulfill(status=503, json={"detail": "AutoHunt backend is unavailable"})
            else:
                route.fulfill(json=[])
        elif path.endswith("/chat"):
            route.fulfill(json={"messages": []})
        elif path.endswith("/candidate-state"):
            route.fulfill(status=503, json={"detail": "No state"})
        else:
            route.fulfill(status=503, json={"detail": "Not part of upload test"})

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.route("**/api/agent/**", api)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(os.getenv("AUTOHUNT_FRONTEND_URL", "http://127.0.0.1:3000"))
        page.get_by_role("button", name="简历").click()
        button = page.get_by_role("button", name="上传简历" if kind == "resume" else "上传材料")
        with page.expect_file_chooser() as chooser:
            button.click()
        filename = "sample-resume.txt" if kind == "resume" else "sample-project.md"
        chooser.value.set_files({"name": filename, "mimeType": "text/plain", "buffer": b"test content"})
        feedback = page.locator(".material-upload-feedback")
        expect(feedback).to_contain_text("Agent 后端不可用")
        expect(page.locator(".material-row").filter(has_text=filename)).to_contain_text("失败")
        assert len(posts) == 1
        assert posts[0].endswith("/resumes" if kind == "resume" else "/materials")
        assert not errors
        browser.close()
