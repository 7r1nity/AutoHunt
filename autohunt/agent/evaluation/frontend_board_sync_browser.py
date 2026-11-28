"""Board synchronization through the production UI, with every API intercepted.

Run with the local frontend already serving static assets. The runner uses a
new temporary browser context for each case, synthetic jobs/events, no model,
no existing browser profile, and no recruitment or other business mutations.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import re
import time
import traceback
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright, expect


RESULTS = Path(__file__).parent / "results"
ROLE = "合成看板同步工程师"
OLD_ROLE = "过期快照不应显示的岗位"


def job(stage="review", **updates):
    return {"id": 501, "role": ROLE, "company": "虚构同步科技", "location": "深圳",
            "salary": "20-30K", "source": "MockJobSite",
            "source_url": "http://127.0.0.1:8080/job.html?id=synthetic-501",
            "score": 88, "stage": stage, "mode": "人工已授权" if stage != "review" else "人工确认",
            "tags": ["Python"], "reason": "仅用于隔离同步测试", "posted": "2026-01-01T00:00:00Z",
            "filter_status": "pass", **updates}


def delivery(status="waiting_for_reply", *, submitted=False, **updates):
    return {"id": "delivery_synthetic_board", "job_id": 501, "status": status,
            "submitted": submitted, "detail": "合成投递状态；没有真实招聘操作",
            "checkpoint": "waiting_for_reply" if not submitted else "confirmed",
            "recruiter_reply": None, "takeover_reason": None, **updates}


class Hold:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()


class Fixture:
    def __init__(self, origin, output, name):
        self.origin, self.output, self.name = origin, output, name
        self.allowed = urlsplit(origin)
        self.jobs = [job()]
        self.jobs_failure = None
        self.jobs_holds = deque()
        self.related_modes = {}
        self.related_hold = asyncio.Event()
        self.events = []
        self.changed = asyncio.Condition()
        self.event_served = asyncio.Condition()
        self.last_served_seq = 0
        self.sequence = 0
        self.run_status = "waiting_reply"
        self.run_session = "default"
        self.current_delivery = delivery()
        self.requests, self.violations, self.page_errors, self.checks = [], [], [], []
        self.job_snapshots = []
        self.jobs_reads = 0
        self.active_jobs_reads = 0
        self.maximum_parallel_jobs_reads = 0

    def run(self):
        return {"id": "synthetic-board-run", "session_id": self.run_session,
                "kind": "delivery", "status": self.run_status, "result": None,
                "error": None, "can_resume": True, "browser_id": None,
                "created_at": "2026-01-01T00:00:00Z"}

    async def publish(self, kind="delivery", *, task=None, session=None, duplicate=False):
        async with self.changed:
            if duplicate and self.events:
                event = copy.deepcopy(self.events[-1])
            else:
                self.sequence += 1
                event = {"seq": self.sequence, "kind": kind,
                         "data": {"task": copy.deepcopy(task or self.current_delivery),
                                  "status": self.run_status,
                                  "detail": "合成状态变化"}}
                if session is not None:
                    event["data"]["conversation_id"] = session
            self.events.append(event)
            self.changed.notify_all()
            return event["seq"]

    async def wait_served(self, seq):
        async with self.event_served:
            await asyncio.wait_for(self.event_served.wait_for(lambda: self.last_served_seq >= seq), 12)

    async def intercept(self, route):
        request, target = route.request, urlsplit(route.request.url)
        if (target.scheme, target.netloc) != (self.allowed.scheme, self.allowed.netloc):
            self.violations.append({"method": request.method, "host": target.netloc})
            await route.abort()
            return
        if not target.path.startswith("/api/"):
            if request.method != "GET":
                self.violations.append({"method": request.method, "path": target.path})
                await route.abort()
            else:
                await route.continue_()
            return
        self.requests.append({"method": request.method, "path": target.path, "query": target.query})
        if request.method != "GET":
            self.violations.append({"method": request.method, "path": target.path})
            await route.abort()
            return
        payload, code = {}, 200
        if target.path == "/api/agent/jobs":
            self.jobs_reads += 1
            self.active_jobs_reads += 1
            self.maximum_parallel_jobs_reads = max(self.maximum_parallel_jobs_reads, self.active_jobs_reads)
            snapshot, failure = copy.deepcopy(self.jobs), self.jobs_failure
            hold = self.jobs_holds.popleft() if self.jobs_holds else None
            self.job_snapshots.append({"read": self.jobs_reads, "failure": failure, "held": hold is not None,
                                       "rows": [{key: row.get(key) for key in ("id", "role", "stage")} for row in snapshot]})
            try:
                if hold:
                    hold.started.set()
                    await asyncio.wait_for(hold.release.wait(), 15)
                if failure == "network":
                    await route.abort()
                elif failure == "json":
                    await route.fulfill(content_type="application/json", body="{invalid-json")
                elif failure == "status":
                    await route.fulfill(status=503, json={"detail": "合成岗位列表暂时不可用"})
                elif failure == "shape":
                    await route.fulfill(json=[job("unconfirmed-client-invented-stage")])
                else:
                    await route.fulfill(json=snapshot)
            except PlaywrightError:
                # A superseded/unmounted request may already have been aborted.
                pass
            finally:
                self.active_jobs_reads -= 1
            return
        if target.path in {"/api/agent/browser", "/api/agent/delivery", "/api/agent/stats"}:
            mode = self.related_modes.get(target.path)
            if mode == "hang":
                await self.related_hold.wait()
            if mode == "network":
                await route.abort()
                return
            if mode == "json":
                await route.fulfill(content_type="application/json", body="{invalid-json")
                return
            if mode == "status":
                await route.fulfill(status=503, json={"detail": "合成关联接口失败"})
                return
            if target.path.endswith("/browser"):
                payload = {"id": None, "status": "idle", "detail": "合成空闲浏览器"}
            elif target.path.endswith("/delivery"):
                payload = self.current_delivery
            else:
                payload = {"review": 1, "applying": 0, "waiting": 0, "interview": 0,
                           "discovered": 1, "stored": 1, "duplicates": 0, "matched": 1}
        elif target.path == "/api/agent/runs/synthetic-board-run/events":
            cursor = int(parse_qs(target.query).get("after", ["0"])[0])
            async with self.changed:
                try:
                    await asyncio.wait_for(self.changed.wait_for(
                        lambda: any(event["seq"] > cursor for event in self.events)), 12)
                except TimeoutError:
                    pass
                # Deliberately replay the complete log; the real client must
                # ignore old sequence numbers and duplicate frames.
                body = "".join("data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
                               for event in self.events) or ": keep-alive\n\n"
            try:
                await route.fulfill(content_type="text/event-stream", body=body)
                async with self.event_served:
                    self.last_served_seq = max([self.last_served_seq, *[e["seq"] for e in self.events]])
                    self.event_served.notify_all()
            except PlaywrightError:
                pass
            return
        elif target.path == "/api/agent/runs/synthetic-board-run":
            payload = self.run()
        elif target.path == "/api/agent/runs":
            session = parse_qs(target.query).get("conversation_id", ["default"])[0]
            payload = [self.run()] if session == self.run_session else []
        elif target.path == "/api/agent/conversations":
            payload = [{"id": "default", "title": "隔离同步会话"}, {"id": "other", "title": "另一隔离会话"}]
        elif target.path == "/api/agent/chat":
            payload = {"messages": []}
        elif target.path.endswith("/search-state"):
            payload = {"queries": [], "locations": [], "result_limit": None, "salary_min": None,
                       "query_source": "synthetic", "revision": 1}
        elif target.path.endswith("/context"):
            payload = {"active_intent": "application", "revision": 1, "idle_expired": False,
                       "frames": {}, "pending": [], "tasks": {}, "trace": []}
        elif target.path in {"/api/agent/resumes", "/api/agent/materials", "/api/agent/documents"}:
            payload = []
        elif target.path == "/api/agent/llm-config":
            payload = {"provider": "mock", "model": "mock", "credential_configured": False}
        elif target.path == "/api/agent/mock-workflow":
            payload = {"enabled": False, "automatic_delivery": False}
        else:
            payload, code = {"detail": "隔离同步测试：未访问真实业务接口"}, 503
        await route.fulfill(status=code, json=payload)

    async def open(self, browser):
        self.context = await browser.new_context(service_workers="block", viewport={"width": 1480, "height": 1100})
        await self.context.route("**/*", self.intercept)
        self.page = await self.context.new_page()
        self.page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        async with self.page.expect_response(lambda response: urlsplit(response.url).path == "/api/agent/conversations"):
            await self.page.goto(self.origin, wait_until="domcontentloaded", timeout=90_000)
        await self.page.get_by_text("已保存 1 个职位", exact=True).wait_for(timeout=15_000)
        await self.open_board()

    async def open_board(self):
        await self.page.get_by_role("button", name=re.compile(r"^申请看板")).click()
        await expect(self.page.locator(".standalone-board .kanban-column")).to_have_count(4)
        await expect(self.page.get_by_role("button", name="刷新看板", exact=True)).to_be_enabled(timeout=15_000)

    async def stage(self, expected, *, role=ROLE):
        column = {"review": 0, "applying": 1, "waiting": 2, "interview": 3}[expected]
        board = self.page.locator(".standalone-board")
        await expect(board.locator(".kanban-column").nth(column).get_by_role("heading", name=role, exact=True)).to_be_visible(timeout=12_000)
        await expect(board.locator(".job-card")).to_have_count(1)

    async def refresh(self):
        await self.page.get_by_role("button", name="刷新看板", exact=True).click()

    async def finish(self):
        self.related_hold.set()
        for hold in self.jobs_holds:
            hold.release.set()
        assert not self.violations, self.violations
        assert not self.page_errors, self.page_errors
        assert not [request for request in self.requests if request["method"] != "GET"]
        self.checks.append("all_business_requests_read_only_and_intercepted")
        report = {"name": self.name, "passed": True, "checks": self.checks,
                  "jobs_reads": self.jobs_reads, "maximum_parallel_jobs_reads": self.maximum_parallel_jobs_reads,
                  "events": self.events, "job_snapshots": self.job_snapshots,
                  "requests": self.requests, "violations": self.violations, "page_errors": self.page_errors}
        (self.output / (self.name + ".json")).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        await self.context.close()
        return {key: report[key] for key in ("name", "passed", "checks", "jobs_reads", "maximum_parallel_jobs_reads")}


async def event_transitions(f, browser):
    await f.open(browser)
    await f.stage("review")
    f.jobs = [job("applying")]
    seq = await f.publish()
    await f.wait_served(seq)
    await f.stage("applying")
    f.checks.append("delivery_sse_updates_review_to_waiting_hr_without_page_reload")
    # A reply event alone is not proof that the resume was submitted.
    f.current_delivery = delivery("running", recruiter_reply="请发送简历")
    seq = await f.publish()
    await f.wait_served(seq)
    await f.stage("applying")
    await expect(f.page.locator(".kanban-column").nth(2).locator(".job-card")).to_have_count(0)
    f.checks.append("hr_reply_does_not_invent_submission_before_backend_receipt")
    f.jobs = [job("waiting", reason="后端已核实简历投递")]
    f.current_delivery = delivery("completed", submitted=True)
    f.run_status = "completed"
    seq = await f.publish()
    await f.wait_served(seq)
    await f.stage("waiting")
    f.checks.append("confirmed_backend_submission_updates_delivered_column")


async def related_failure(f, browser, modes):
    f.related_modes = modes
    f.jobs = [job("applying")]
    await f.open(browser)
    await f.stage("applying")
    f.checks.append("jobs_render_independently_of_hung_failed_or_invalid_related_endpoints")
    assert any(request["path"] in modes for request in f.requests)


async def stale_response(f, browser):
    await f.open(browser)
    await f.stage("review")
    first, second = Hold(), Hold()
    f.jobs_holds.extend((first, second))
    f.jobs = [job("review", role=OLD_ROLE)]
    await f.page.evaluate("""() => {
      window.__boardOldVisible = false;
      const target = document.querySelector('.standalone-board');
      window.__boardWatcher = new MutationObserver(() => {
        if (target.textContent.includes('过期快照不应显示的岗位')) window.__boardOldVisible = true;
      });
      window.__boardWatcher.observe(target, {subtree:true, childList:true, characterData:true});
    }""")
    await f.refresh()
    await asyncio.wait_for(first.started.wait(), 8)
    f.jobs = [job("applying")]
    seq = await f.publish()
    await f.wait_served(seq)
    first.release.set()
    await asyncio.wait_for(second.started.wait(), 8)
    await expect(f.page.get_by_role("heading", name=OLD_ROLE, exact=True)).to_have_count(0)
    assert not await f.page.evaluate("window.__boardOldVisible")
    f.checks.append("event_invalidates_inflight_snapshot_before_it_can_publish_old_review")
    second.release.set()
    await f.stage("applying")
    assert f.maximum_parallel_jobs_reads == 1, f.maximum_parallel_jobs_reads
    assert not await f.page.evaluate("window.__boardOldVisible")
    f.checks.append("one_followup_read_publishes_current_stage_without_parallel_jobs_requests")


async def return_to_board(f, browser):
    await f.open(browser)
    await f.stage("review")
    await f.page.get_by_role("button", name=re.compile(r"^Agent 对话")).click()
    f.jobs = [job("applying")]
    await f.open_board()
    await f.stage("applying")
    f.checks.append("returning_to_board_refreshes_without_waiting_for_periodic_poll")


async def foreground_refresh(f, browser):
    await f.open(browser)
    await f.stage("review")
    await f.page.evaluate("""() => {
      Object.defineProperty(document, 'hidden', {configurable:true, value:true});
      Object.defineProperty(document, 'visibilityState', {configurable:true, value:'hidden'});
      document.dispatchEvent(new Event('visibilitychange'));
    }""")
    f.jobs = [job("applying")]
    await f.page.evaluate("""() => {
      Object.defineProperty(document, 'hidden', {configurable:true, value:false});
      Object.defineProperty(document, 'visibilityState', {configurable:true, value:'visible'});
      document.dispatchEvent(new Event('visibilitychange'));
    }""")
    await f.stage("applying")
    f.checks.append("visibility_return_refreshes_current_backend_snapshot")
    await expect(f.page.get_by_role("button", name="刷新看板", exact=True)).to_be_enabled()
    f.jobs = [job("waiting")]
    await f.page.evaluate("window.dispatchEvent(new Event('focus'))")
    await f.stage("waiting")
    f.checks.append("window_focus_refreshes_current_backend_snapshot")


async def duplicate_event(f, browser):
    await f.open(browser)
    f.jobs = [job("applying")]
    seq = await f.publish()
    await f.wait_served(seq)
    await f.stage("applying")
    await f.publish(duplicate=True)
    seq = await f.publish("status")
    await f.wait_served(seq)
    await expect(f.page.get_by_role("button", name="刷新看板", exact=True)).to_be_enabled()
    await f.stage("applying")
    f.checks.append("duplicate_sse_frames_keep_one_card_without_business_mutation")


async def previous_session_event(f, browser):
    await f.open(browser)
    await f.stage("review")
    await f.page.get_by_role("button", name=re.compile(r"^Agent 对话")).click()
    selector = f.page.get_by_role("combobox", name="当前会话", exact=True)
    async with f.page.expect_response(lambda response: urlsplit(response.url).path == "/api/agent/chat"
                                     and "conversation_id=other" in response.url):
        await selector.select_option("other")
    await expect(selector).to_have_value("other")
    foreign_detail = "上一会话延迟事件不应污染当前会话"
    seq = await f.publish(task=delivery("running", detail=foreign_detail))
    await f.wait_served(seq)
    await f.open_board()
    await f.page.evaluate("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    await expect(f.page.get_by_text(foreign_detail, exact=True)).to_have_count(0)
    await f.stage("review")
    f.checks.append("delayed_previous_session_delivery_event_does_not_replace_current_view")


async def stale_indicator(f, browser, failure):
    f.jobs = [job("applying")]
    await f.open(browser)
    await f.stage("applying")
    f.jobs_failure = failure
    await f.refresh()
    indicator = f.page.get_by_role("status", name="看板同步状态", exact=True)
    await expect(indicator).to_contain_text("看板同步失败，显示上次结果")
    await f.stage("applying")
    f.checks.append("failed_jobs_read_keeps_last_snapshot_and_explicit_stale_indicator")
    f.jobs_failure = None
    f.jobs = [job("waiting")]
    await f.refresh()
    await f.stage("waiting")
    await expect(indicator).to_contain_text("已同步")
    f.checks.append("successful_retry_clears_stale_indicator_using_backend_status")


async def authoritative_empty_list(f, browser):
    f.jobs = [job("applying")]
    await f.open(browser)
    await f.stage("applying")
    f.jobs = []
    await f.refresh()
    await expect(f.page.locator(".standalone-board .job-card")).to_have_count(0)
    await expect(f.page.get_by_role("status", name="看板同步状态", exact=True)).to_contain_text("已同步")
    f.checks.append("valid_empty_backend_list_removes_ghost_cards_without_becoming_stale_error")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--origin", default="http://127.0.0.1:3000")
    args = parser.parse_args()
    target = urlsplit(args.origin)
    if target.scheme != "http" or target.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Only a local frontend origin is allowed")
    output = RESULTS / "frontend-board-sync" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    frontend = Path(__file__).parents[2] / "frontend"
    source_files = [frontend / relative for relative in (
        "app/page.tsx", "lib/agent-runtime.ts", "lib/use-agent-runtime.ts",
        "lib/use-job-board.ts", "lib/latest-snapshot.ts")]
    source_hashes = {str(path.relative_to(frontend)): hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in source_files}
    summary = {"passed": False, "model_calls": 0, "real_recruitment_actions": 0,
               "cases": [], "source_sha256": source_hashes,
               "scope": "Production UI with synthetic intercepted APIs/SSE; not model accuracy or real-site E2E"}
    definitions = [
        ("delivery-event-transitions", event_transitions, ()),
        ("browser-hang-stats-json-delivery-status", related_failure,
         ({"/api/agent/browser": "hang", "/api/agent/stats": "json", "/api/agent/delivery": "status"},)),
        ("delivery-hang-browser-json-stats-network", related_failure,
         ({"/api/agent/delivery": "hang", "/api/agent/browser": "json", "/api/agent/stats": "network"},)),
        ("event-during-inflight-jobs", stale_response, ()),
        ("return-to-board", return_to_board, ()),
        ("visibility-and-focus", foreground_refresh, ()),
        ("duplicate-events", duplicate_event, ()),
        ("previous-session-event", previous_session_event, ()),
        ("jobs-status-failure-recovery", stale_indicator, ("status",)),
        ("jobs-json-failure-recovery", stale_indicator, ("json",)),
        ("jobs-shape-failure-recovery", stale_indicator, ("shape",)),
        ("authoritative-empty-list", authoritative_empty_list, ()),
    ]
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            for name, action, extra in definitions:
                fixture = Fixture(args.origin, output, name)
                started = time.monotonic()
                try:
                    await action(fixture, browser, *extra)
                    result = await fixture.finish()
                    result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
                    summary["cases"].append(result)
                    print(json.dumps({"case": name, "passed": True, "checks": len(result["checks"])}, ensure_ascii=False), flush=True)
                except BaseException:
                    fixture.related_hold.set()
                    for hold in fixture.jobs_holds:
                        hold.release.set()
                    failure = {"name": name, "passed": False, "error": traceback.format_exc(),
                               "checks": fixture.checks, "requests": fixture.requests,
                               "events": fixture.events, "job_snapshots": fixture.job_snapshots,
                               "page_errors": fixture.page_errors, "violations": fixture.violations}
                    if hasattr(fixture, "page"):
                        failure["page_text"] = await fixture.page.locator("body").inner_text()
                        await fixture.page.screenshot(path=str(output / (name + "-failure.png")))
                    summary["cases"].append(failure)
                    print(json.dumps({"case": name, "passed": False, "error": failure["error"]}, ensure_ascii=False), flush=True)
                    if hasattr(fixture, "context"):
                        await fixture.context.close()
        finally:
            await browser.close()
    changes = [str(path.relative_to(frontend)) for path in source_files
               if hashlib.sha256(path.read_bytes()).hexdigest() != source_hashes[str(path.relative_to(frontend))]]
    summary.update(passed=all(case["passed"] for case in summary["cases"]) and not changes,
                   case_count=len(summary["cases"]),
                   source_changed_during_run=changes,
                   checks=sum(len(case["checks"]) for case in summary["cases"]))
    (output / "result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), **{key: summary[key] for key in ("passed", "case_count", "checks", "model_calls", "real_recruitment_actions")}}), flush=True)
    return int(not summary["passed"])


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
