"""Measure public search/board limits against a 120-job goal, without user data.

The actual authenticated ASGI jobs route is called in an isolated subprocess.
Its store is a temporary SQLite file; lifespan workers are never started. The
frontend receives that exact route result through intercepted APIs in a new browser.
The legacy array route stays compatible; the board consumes all pages through
the new snapshot-bound route. This is surface coverage, not recruitment E2E.
"""
from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from cryptography.fernet import Fernet
from playwright.async_api import async_playwright, expect
from pydantic import ValidationError

from app.core.models import BrowserSearchRequest
from app.core.result_groups import normalize_groups
from app.jobs.store import JobStore
from app.runtime.search_state import SearchPatch, SearchState
from evaluation.frontend_board_sync_browser import Fixture


ROOT = Path(__file__).parents[1]
FRONTEND = ROOT.parent / "frontend"


def read_http_board(database, directory):
    script = r'''
import json,sys,time
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from app.api import application
from app.jobs.store import JobStore
application.job_store = JobStore(Path(sys.argv[1]))
with patch('httpx.AsyncClient.send',side_effect=AssertionError('No outbound HTTP permitted')):
    # No context-manager lifespan: no browser/model/background worker starts.
    client=TestClient(application.app)
    assert client.get('/api/v1/jobs').status_code==401
    started=time.perf_counter()
    response=client.get('/api/v1/jobs',headers={'X-AutoHunt-Key':'synthetic-scale-api-key'})
    assert response.status_code==200,response.text[:200]
    legacy_count=len(response.json())
    assert client.get('/api/v1/jobs/page').status_code==401
    pages=[];rows=[];offset=0;snapshot=None
    while True:
        params={'limit':100,'offset':offset}
        if snapshot is not None:params['snapshot']=snapshot
        response=client.get('/api/v1/jobs/page',params=params,headers={'X-AutoHunt-Key':'synthetic-scale-api-key'})
        assert response.status_code==200,response.text[:200]
        page=response.json();pages.append(page);rows.extend(page['items'])
        snapshot=page['snapshot']
        if page['next_offset'] is None:break
        offset=page['next_offset']
    assert len(rows)==pages[0]['total']==len({row['id'] for row in rows})
    result={'rows':rows,'pages':pages,'legacy_array_count':legacy_count,'elapsed_ms':round((time.perf_counter()-started)*1000,3),
            'authentication_checked':True,'full_asgi_dependency_assembly_tested':True,
            'lifespan_workers_started':False,'outbound_http_requests':0}
    client.close()
print(json.dumps(result,ensure_ascii=False))
'''
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("AUTOHUNT_") and not key.endswith(("_API_KEY", "_TOKEN"))}
    env.update(AUTOHUNT_DATA_ROOT=str(Path(directory) / "isolated-assembly"),
               AUTOHUNT_DATA_KEY=Fernet.generate_key().decode(),
               AUTOHUNT_API_KEY="synthetic-scale-api-key", AUTOHUNT_LLM_PROVIDER="mock",
               AUTOHUNT_LLM_MODEL="mock-autohunt-v1", PYTHONUTF8="1")
    outcome = subprocess.run([sys.executable, "-X", "utf8", "-c", script, str(database)],
                             cwd=ROOT, env=env, text=True, capture_output=True, timeout=60)
    if outcome.returncode:
        raise RuntimeError(outcome.stderr[-5000:])
    return json.loads(outcome.stdout.strip().splitlines()[-1])


def check_schema(name, factory):
    started = time.perf_counter()
    try:
        factory()
        return {"name": name, "accepted": True, "elapsed_ms": round((time.perf_counter() - started) * 1000, 3)}
    except (ValidationError, ValueError) as exc:
        details = exc.errors(include_url=False) if isinstance(exc, ValidationError) else [{"message": str(exc)}]
        return {"name": name, "accepted": False, "errors": details,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3)}


async def run(origin, target, output):
    paths = [ROOT / relative for relative in (
        "app/api/application.py", "app/jobs/store.py", "app/core/models.py",
        "app/core/result_groups.py", "app/core/filter_policy.py", "app/runtime/search_state.py")]
    paths += [FRONTEND / relative for relative in (
        "app/page.tsx", "app/api/agent/jobs/route.ts", "lib/use-job-board.ts", "lib/job-board-pages.ts", "lib/latest-snapshot.ts")]
    hashes = {str(p.relative_to(ROOT.parent)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    report = {"target": target, "engineering_passed": False, "single_search_and_full_board_goal_met": False,
              "external_model_calls": 0, "real_recruitment_actions": 0, "source_sha256": hashes,
              "scope": "Public schema, authenticated ASGI route with isolated SQLite, and frontend with intercepted APIs. Not real-model planning or recruitment E2E."}
    store = fixture = None
    try:
        report["search_boundaries"] = [
            check_schema("search_100", lambda: BrowserSearchRequest(query="Python工程师", max_jobs=200, result_limit=100)),
            check_schema(f"search_{target}", lambda: BrowserSearchRequest(query="Python工程师", max_jobs=200, result_limit=target)),
            check_schema(f"session_state_{target}", lambda: SearchState(queries=["Python工程师"], result_limit=target)),
            check_schema(f"search_patch_{target}", lambda: SearchPatch(result_limit=target)),
            check_schema(f"candidate_budget_{target}_without_result_quota",
                         lambda: BrowserSearchRequest(query="Python工程师", max_jobs=target)),
            check_schema("two_city_quotas_60_plus_60", lambda: normalize_groups([
                {"label": city, "count": 60, "filters": {"op": "condition", "text": "工作地点在" + city}}
                for city in ("深圳", "杭州")])),
        ]
        with tempfile.TemporaryDirectory(prefix="autohunt-scale-surface-") as directory:
            database = Path(directory) / "synthetic.db"
            store = JobStore(database)
            for index in range(target):
                store.upsert_job({"source": "MockJobSite", "source_url": f"http://127.0.0.1:8080/job.html?id={800000+index}",
                    "external_id": str(800000+index), "title": f"合成规模边界岗位{index+1}", "company": "虚构规模企业",
                    "location": "深圳", "salary": "20-30K", "description": "仅用于隔离规模测试的Python开发岗位。",
                    "tags": ["Python"], "source_tags": ["Python"], "score": 90, "decision": "REVIEW",
                    "reasoning": "合成记录，不是真实招聘", "stage": "review", "filter_status": "pass"})
            actual = await asyncio.to_thread(read_http_board, database, directory)
            payload = actual.pop("rows")
            pages = actual.pop("pages")
            stored = store.list_jobs(limit=200)
            assert len(stored) == target
            assert {item["id"] for item in payload} <= {item.id for item in stored}
            report["board_api"] = {"seeded_jobs": target, "returned_jobs": len(payload),
                                   "omitted_jobs": target-len(payload), "page_count": len(pages), **actual}
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                try:
                    fixture = Fixture(origin, output, "board-as-returned-by-public-api")
                    fixture.jobs = payload
                    fixture.context = await browser.new_context(service_workers="block", viewport={"width": 1480, "height": 1100})
                    async def intercept(route):
                        target_url = urlsplit(route.request.url)
                        if target_url.path == "/api/agent/jobs":
                            query = parse_qs(target_url.query)
                            assert route.request.method == "GET" and query.get("page") == ["true"]
                            offset = int(query.get("offset", [0])[0])
                            page = next(row for row in pages if row["offset"] == offset)
                            if offset:
                                assert query.get("snapshot") == [page["snapshot"]]
                            await route.fulfill(json=page)
                        else:
                            await fixture.intercept(route)
                    await fixture.context.route("**/*", intercept)
                    fixture.page = await fixture.context.new_page()
                    fixture.page.on("pageerror", lambda error: fixture.page_errors.append(str(error)))
                    started = time.perf_counter()
                    await fixture.page.goto(origin, wait_until="domcontentloaded", timeout=90_000)
                    await fixture.page.get_by_text(f"已保存 {len(payload)} 个职位", exact=True).wait_for(timeout=20_000)
                    await fixture.open_board()
                    await expect(fixture.page.locator(".standalone-board .job-card")).to_have_count(len(payload))
                    await expect(fixture.page.locator(".standalone-board .kanban-column").first.locator(".job-card")).to_have_count(len(payload))
                    fixture.checks.append("frontend_renders_exact_public_route_result_without_fabricating_missing_rows")
                    report["board_ui"] = {"rendered_cards": len(payload),
                                          "elapsed_ms": round((time.perf_counter()-started)*1000, 3),
                                          "full_target_visible": len(payload) == target,
                                          "business_mutations": 0}
                    await fixture.finish()
                    fixture = None
                finally:
                    if fixture is not None and hasattr(fixture, "context"):
                        await fixture.context.close()
                    await browser.close()
            store = None
            gc.collect()
        accepted = next(row["accepted"] for row in report["search_boundaries"] if row["name"] == f"search_{target}")
        report["single_search_and_full_board_goal_met"] = accepted and len(payload) == target
        assert report["single_search_and_full_board_goal_met"], "Public search/complete-board goal remains unmet"
        report["engineering_passed"] = True
    except BaseException:
        report["error"] = traceback.format_exc()
        store = None
        gc.collect()
    changes = [str(path.relative_to(ROOT.parent)) for path in paths
               if hashlib.sha256(path.read_bytes()).hexdigest() != hashes[str(path.relative_to(ROOT.parent))]]
    report["source_changed_during_run"] = changes
    if changes:
        report["engineering_passed"] = False
    (output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--origin", default="http://127.0.0.1:3000")
    parser.add_argument("--jobs", type=int, default=120, choices=[120, 200])
    args = parser.parse_args()
    # Fixture itself rejects all hosts except this origin; enforce local input too.
    parsed = urlsplit(args.origin)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("Only a local frontend origin is allowed")
    output = ROOT / "evaluation/results/delivery-scale-surface" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    report = asyncio.run(run(args.origin, args.jobs, output))
    print(json.dumps({"output": str(output), **{key: report.get(key) for key in (
        "engineering_passed", "single_search_and_full_board_goal_met", "board_api", "board_ui", "error")}}, ensure_ascii=False))
    return int(not report["engineering_passed"])


if __name__ == "__main__":
    raise SystemExit(main())
