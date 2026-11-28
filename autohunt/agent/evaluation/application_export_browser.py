"""Synthetic application-export UI acceptance; no model or recruitment actions.

Requires the local frontend (default port 3000). Every Agent API is fulfilled
locally, all writes/external hosts are blocked, and a fresh browser context owns
only synthetic fixtures. Downloads/results stay under ignored evaluation/results.
This verifies browser UI/downloads, not the real backend's export serialization.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import re
import traceback
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from playwright.async_api import async_playwright, expect
from evaluation.job_page_fixture import job_page_response


ROOT = Path(__file__).resolve().parent


def xlsx_fixture():
    """A valid, minimal spreadsheet made only from fixed synthetic strings."""
    output = io.BytesIO()
    parts = {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        "_rels/.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="合成申请" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>岗位</t></is></c><c r="B1" t="inlineStr"><is><t>地点</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>合成算法工程师</t></is></c><c r="B2" t="inlineStr"><is><t>深圳</t></is></c></row></sheetData></worksheet>',
    }
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as workbook:
        for name, text in parts.items():
            workbook.writestr(name, text.encode("utf-8"))
    return output.getvalue()


def proposal_fixture():
    return {"id": "synthetic-proposal", "document_id": "synthetic-document", "document_version": 1,
        "instruction": "补充合成技能", "reason": "根据虚构材料形成提案；此测试不会接受或写入。", "status": "pending",
        "operations": [{"id": "synthetic-operation", "operation": "add", "path": "/skills/-", "label": "新增技能：SQL",
            "old_value": None, "new_value": "SQL", "evidence_ids": ["synthetic-evidence"]}],
        "created_at": "2026-01-01T08:00:00Z", "decided_at": None}


async def exercise(origin, output):
    origin_parts = urlsplit(origin)
    if origin_parts.scheme != "http" or origin_parts.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Only a local HTTP frontend is permitted")
    checks, requests, exports, writes, external, errors, downloads = [], [], [], [], [], [], []
    mode = {"export": "normal", "proposal": False}
    release, pending = asyncio.Event(), asyncio.Event()
    excel = xlsx_fixture()
    csv_all = "\ufeff岗位,地点,状态\r\n合成算法工程师,深圳,待审核\r\n合成已移除岗位,上海,已移除\r\n".encode("utf-8")
    csv_board = "\ufeff岗位,地点,状态\r\n合成算法工程师,深圳,待审核\r\n".encode("utf-8")
    job = {"id": 17, "role": "合成算法工程师", "company": "合成星海科技", "location": "深圳", "salary": "20-30K",
        "source": "合成模拟站", "score": 88, "stage": "review", "mode": "人工确认", "tags": ["检索"],
        "reason": "仅合成证据", "posted": "2026-01-01", "source_url": "https://example.invalid/job/17"}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(service_workers="block", accept_downloads=True,
            viewport={"width": 1440, "height": 1000})

        async def intercept(route):
            request = route.request
            target = urlsplit(request.url)
            if (target.scheme, target.netloc) != (origin_parts.scheme, origin_parts.netloc):
                external.append({"method": request.method, "host": target.netloc})
                await route.abort()
                return
            if request.method not in {"GET", "HEAD"}:
                writes.append({"method": request.method, "path": target.path})
                await route.abort()
                return
            if not target.path.startswith("/api/"):
                await route.continue_()
                return
            # No Agent request reaches the running backend, including unknown GETs.
            requests.append(target.path)
            if target.path == "/api/agent/applications/export":
                query = parse_qs(target.query)
                entry = {"format": query.get("format", [None])[0], "scope": query.get("scope", [None])[0], "mode": mode["export"]}
                exports.append(entry)
                if mode["export"] == "error":
                    await route.fulfill(status=409, json={"detail": "合成导出冲突，请稍后重试。"})
                    return
                if mode["export"] == "loading":
                    pending.set()
                    await release.wait()
                assert entry["format"] in {"csv", "xlsx"} and entry["scope"] in {"all", "board"}, entry
                if entry["format"] == "xlsx":
                    body, filename = excel, "autohunt-applications-all.xlsx"
                    mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    disposition = f'attachment; filename="{filename}"'
                else:
                    body = csv_all if entry["scope"] == "all" else csv_board
                    filename = "申请历史_全部.csv" if entry["scope"] == "all" else "申请看板.csv"
                    mime = "text/csv; charset=utf-8"
                    disposition = "attachment; filename*=UTF-8''" + quote(filename)
                await route.fulfill(status=200, body=body,
                    headers={"Content-Type": mime, "Content-Disposition": disposition, "Cache-Control": "no-store"})
                return
            status, payload = 503, {"detail": "合成UI测试：真实Agent接口已禁用"}
            if target.path == "/api/agent/conversations":
                status, payload = 200, [{"id": "default", "title": "合成会话", "created_at": "2026-01-01T00:00:00Z"}]
            elif target.path == "/api/agent/chat":
                messages = [{"role": "assistant", "content": "合成简历修改提案", "created_at": "2026-01-01T08:00:00Z",
                    "response": {"resume_proposal": proposal_fixture()}}] if mode["proposal"] else []
                status, payload = 200, {"messages": messages}
            elif target.path == "/api/agent/jobs":
                status, payload = 200, job_page_response([job], target.query)
            elif target.path in {"/api/agent/runs", "/api/agent/resumes", "/api/agent/materials", "/api/agent/interview-preparations"}:
                status, payload = 200, []
            elif target.path in {"/api/agent/browser", "/api/agent/delivery"}:
                status, payload = 200, {"status": "idle", "detail": "合成空闲"}
            elif target.path == "/api/agent/stats":
                status, payload = 200, {"review": 1, "applying": 0, "waiting": 0, "interview": 0, "indeterminate": 0,
                    "discovered": 1, "stored": 1, "duplicates": 0, "matched": 1}
            await route.fulfill(status=status, content_type="application/json", body=json.dumps(payload, ensure_ascii=False))

        await context.route("**/*", intercept)
        page = await context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("download", lambda download: downloads.append(download.suggested_filename))

        async def open_page():
            # An effect request establishes React hydration before clicking nav.
            async with page.expect_response(lambda response: urlsplit(response.url).path == "/api/agent/conversations"):
                await page.goto(origin, wait_until="domcontentloaded", timeout=90_000)

        async def save_download(info, expected_bytes, expected_name):
            download = await info.value
            assert download.suggested_filename == expected_name, download.suggested_filename
            destination = output / expected_name
            await download.save_as(destination)
            assert destination.read_bytes() == expected_bytes, "Downloaded bytes differ from the synthetic export"

        try:
            await open_page()
            await page.get_by_role("button", name=re.compile(r"^Agent 对话")).click()
            await expect(page.locator(".chat-starters")).to_be_visible()
            assert await page.get_by_role("button", name=re.compile(r"成长|学习计划")).count() == 0
            assert not re.search(r"成长计划|学习计划", await page.locator("body").inner_text())
            checks.append("growth_navigation_and_empty_chat_shortcuts_removed")
            await page.get_by_role("button", name=re.compile(r"^申请看板")).click()
            scope = page.get_by_label("申请导出范围", exact=True)
            csv = page.get_by_role("button", name="下载 CSV", exact=True)
            xlsx = page.get_by_role("button", name="下载 Excel", exact=True)
            await expect(scope).to_have_value("all")
            async with page.expect_download() as info:
                await csv.click()
            await save_download(info, csv_all, "申请历史_全部.csv")
            assert exports[-1] == {"format": "csv", "scope": "all", "mode": "normal"}
            checks.append("default_all_csv_query_utf8_filename_and_exact_download_bytes")
            async with page.expect_download() as info:
                await xlsx.click()
            await save_download(info, excel, "autohunt-applications-all.xlsx")
            assert exports[-1] == {"format": "xlsx", "scope": "all", "mode": "normal"}
            checks.append("all_xlsx_query_and_exact_valid_workbook_bytes")
            await scope.select_option("board")
            async with page.expect_download() as info:
                await csv.click()
            await save_download(info, csv_board, "申请看板.csv")
            assert exports[-1] == {"format": "csv", "scope": "board", "mode": "normal"}
            checks.append("current_board_scope_query_and_exact_download_bytes")
            previous_downloads = len(downloads)
            mode["export"] = "error"
            await xlsx.click()
            await expect(page.locator('[aria-label="申请记录导出"]').get_by_role("alert")).to_have_text("合成导出冲突，请稍后重试。")
            await expect(xlsx).to_be_enabled()
            await expect(scope).to_be_enabled()
            await page.wait_for_timeout(150)
            assert len(downloads) == previous_downloads, downloads
            checks.append("http409_error_does_not_download_and_controls_recover")
            mode["export"] = "loading"
            before_requests = len(exports)
            async with page.expect_download() as info:
                await csv.click()
                await asyncio.wait_for(pending.wait(), timeout=10)
                await expect(scope).to_be_disabled()
                await expect(page.get_by_role("button", name="正在导出 CSV…", exact=True)).to_be_disabled()
                await expect(xlsx).to_be_disabled()
                await xlsx.evaluate("button => button.click()")
                assert len(exports) == before_requests + 1, exports
                release.set()
            await save_download(info, csv_board, "申请看板.csv")
            await expect(csv).to_be_enabled()
            checks.append("inflight_export_disables_scope_and_prevents_reentry")
            await page.set_viewport_size({"width": 390, "height": 844})
            assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), "Mobile page overflows horizontally"
            await page.screenshot(path=str(output / "mobile.png"))
            checks.append("390px_layout_has_no_page_horizontal_overflow")
            mode["proposal"] = True
            mode["export"] = "normal"
            await open_page()
            await page.get_by_role("button", name=re.compile(r"^Agent 对话")).click()
            await expect(page.get_by_text("主简历 v1 修改提案", exact=True)).to_be_visible()
            await expect(page.get_by_role("button", name="接受已选修改", exact=True)).to_be_visible()
            await expect(page.get_by_role("button", name="拒绝全部", exact=True)).to_be_visible()
            checks.append("resume_update_proposal_controls_remain_visible_without_writes")
            await page.get_by_role("button", name=re.compile(r"^申请看板")).click()
            await page.get_by_role("button", name=re.compile(r"^查看 合成算法工程师")).click()
            await page.get_by_role("button", name="预测面试问题", exact=True).click()
            dialog = page.get_by_role("dialog", name="预测面试问题")
            await expect(dialog).to_be_visible()
            await expect(dialog.get_by_role("button", name="生成本岗位面试准备", exact=True)).to_be_visible()
            await page.keyboard.press("Escape")
            checks.append("interview_preparation_entry_and_panel_remain_available")
            assert not [path for path in requests if "learning" in path or "growth" in path], requests
            checks.append("no_retired_learning_api_requests")
            assert not writes, writes
            assert not external, external
            assert not errors, errors
            checks.append("all_agent_apis_stubbed_no_writes_external_requests_or_runtime_errors")
            return {"passed": True, "checks": checks, "export_requests": exports, "downloaded_filenames": downloads,
                "blocked_writes": writes, "blocked_external_requests": external, "page_errors": errors,
                "real_model_calls": 0, "real_recruitment_actions": 0,
                "coverage": "Synthetic frontend navigation, binary downloads and error/loading contracts only; all Agent APIs stubbed, not backend export or recruitment E2E."}
        except Exception:
            return {"passed": False, "checks": checks, "error": traceback.format_exc(), "export_requests": exports,
                "downloaded_filenames": downloads, "blocked_writes": writes, "blocked_external_requests": external,
                "page_errors": errors, "synthetic_page_text": await page.locator("body").inner_text()}
        finally:
            release.set()
            await context.close()
            await browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:3000/")
    args = parser.parse_args()
    output = ROOT / "results/application-export-ui" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    result = asyncio.run(exercise(args.url, output))
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), **result}, ensure_ascii=False), flush=True)
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
