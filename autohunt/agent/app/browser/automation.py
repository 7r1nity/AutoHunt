from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import uuid
from pathlib import Path
from urllib.parse import quote_plus, urlparse
from urllib.request import Request, urlopen

from playwright.async_api import Error as PlaywrightError, Page

from .boss_adapter import BossAdapter
from .zhaopin_adapter import ZhaopinAdapter
from .lock import BrowserExecutionLock, BrowserLockBusy
from .mock_site_adapter import MOCK_SITE_ORIGIN, MockJobSiteSearchAdapter
from .screening import (
    DETAIL_DECISIONS,
    MATCH_CONCURRENCY,
    clean_title,
    detail_request_from_card,
    match_job,
    pre_screen_cards,
)
from .session import BrowserSession
from ..core.models import BrowserMode, BrowserSearchRequest, BrowserTaskStatus, BrowserTaskView, JobAnalysis, JobAnalysisRequest
from ..core.job_filters import evaluate_filter
from ..delivery.mock_site_adapter import MockJobSiteAdapter as MockDeliveryAdapter
from ..jobs.analysis import KNOWN_SKILLS, analyze_job
from ..jobs.candidate_facts import match_facts
from ..jobs.store import JobStore
from ..llm.manager import LLMManager
from ..memory.candidate_state import CandidateStateStore
from ..memory.resume_workspace import ResumeWorkspaceStore, resume_document_digest
from ..memory.stores import MemoryStore
from ..runtime.resilience import WatchLimits, WatchdogExpired, guarded_operation


TERMINAL_STATUSES = {
    BrowserTaskStatus.COMPLETED,
    BrowserTaskStatus.FAILED,
    BrowserTaskStatus.STOPPED,
}


class DailySubmissionDisabled(RuntimeError):
    """The scheduled run has no current automatic resume-submission authority."""


class BrowserAutomationService:
    """Serialized browser worker with fail-closed human takeover boundaries."""

    def __init__(
        self, store: JobStore, memory: MemoryStore, candidate_state: CandidateStateStore,
        llm: LLMManager, data_root: Path, *, headless: bool,
        cdp_url: str = "http://127.0.0.1:9222", resume_workspace: ResumeWorkspaceStore | None = None,
    ) -> None:
        self.store = store
        self.memory = memory
        self.candidate_state = candidate_state
        self.llm = llm
        self.data_root = data_root
        self.profile_dir = data_root / "private" / "browser-profile"
        self.evidence_dir = data_root / "private" / "browser-evidence"
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.headless = headless
        self.cdp_url = cdp_url
        self.current_id: str | None = None
        self.worker: asyncio.Task | None = None
        self.resume_access_check = False
        self.lifecycle_lock = asyncio.Lock()
        self.gate = asyncio.Event()
        self.gate.set()
        self.session: BrowserSession | None = None
        self.adapter = BossAdapter()
        self.mock_adapter = MockJobSiteSearchAdapter()
        self.mock_delivery_adapter = MockDeliveryAdapter()
        self.resume_workspace = resume_workspace
        self.watch_limits = WatchLimits.configured()

    async def start(self, request: BrowserSearchRequest, *, daily: bool = False,
                    task_id: str | None = None) -> BrowserTaskView:
        if daily and (request.source != "mock" or not request.dry_run or request.dry_run_simulate_reply):
            raise ValueError("每日任务仅支持本地模拟站，且不得模拟招聘方回复")
        async with self.lifecycle_lock:
            # A terminal DB state can be written just before a worker enters its
            # finally block. Never let a new run replace self.session until that
            # cleanup has actually finished.
            if self.worker and not self.worker.done():
                raise RuntimeError("已有浏览器任务正在启动、运行或清理，请稍候")
            current = self.current()
            if current.status not in TERMINAL_STATUSES | {BrowserTaskStatus.IDLE}:
                # A process restart cannot retain a live Playwright/CDP connection.
                if current.id:
                    self.store.event(current.id, "task_interrupted", "后端重启导致浏览器会话中断")
                    self.store.update_task(current.id, status=BrowserTaskStatus.STOPPED, detail="后端已重启；原浏览器任务已安全停止")
            task_id = task_id or uuid.uuid4().hex
            self.current_id = task_id
            self.resume_access_check = False
            self.gate.set()
            state = self.store.create_task(
                task_id,
                request.source,
                request.query,
                request.browser_mode.value,
                dry_run=request.dry_run,
                adapter_version=(self.mock_adapter.selector_version if request.source == "mock" else
                    (ZhaopinAdapter.selector_version if request.source == "zhaopin" else BossAdapter.selector_version)),
                resume_id=self.candidate_state.read().profile.resume_id,
            )
            saved_request = request.model_dump(mode="json")
            if daily:
                settings = self.store.mock_workflow_settings()
                # Internal checkpoint, not a user-controlled request field. A
                # later off/on toggle cannot grant this old run new authority.
                saved_request["_daily_policy"] = {
                    "auto_submit_epoch": settings["auto_submit_enabled_at"] if settings["auto_submit"] else None,
                }
            self.store.save_search_request(task_id, saved_request)
            source_label = {"mock": "模拟招聘网站", "boss": "BOSS直聘", "zhaopin": "智联招聘"}[request.source]
            self.store.event(task_id, "task_started", f"{source_label} 搜索：{request.query}")
            self.store.event(task_id, "search_constraints", "本次搜索条件（不修改持久化偏好）", sample={
                "queries": request.queries or [request.query], "locations": request.locations or [],
                "result_limit": request.result_limit, "salary_min": request.salary_min, "max_jobs": request.max_jobs,
                "result_groups": request.result_groups,
                "conversation_id": request.conversation_id, "revision": request.search_state_revision,
                "source": request.source, "filters": request.filters,
            })
            self.worker = asyncio.create_task(self._run_guarded(task_id, request), name=f"browser-search-{task_id[:8]}")
            return state

    async def continue_search(self, task_id: str, request: BrowserSearchRequest) -> BrowserTaskView:
        """Reattach the same read-only search and skip persisted job receipts."""
        async with self.lifecycle_lock:
            if self.worker and not self.worker.done():
                raise RuntimeError("浏览器正在执行或清理，未重放搜索")
            saved = self.store.search_recovery(task_id)["request"]
            if not saved:
                raise ValueError("旧搜索缺少可靠的请求检查点，请明确重新搜索")
            original = BrowserSearchRequest.model_validate(saved)
            ignored = {"browser_mode", "search_state_revision"}
            if (original.dry_run or request.dry_run or
                    original.model_dump(exclude=ignored) != request.model_dump(exclude=ignored)):
                raise ValueError("搜索条件、会话已变化或原任务含投递，不能重放为只读续接")
            state = self.store.get_task(task_id)
            if state.status == BrowserTaskStatus.COMPLETED:
                return state
            self.store.reconcile_search_counts(task_id)
            self.current_id = task_id
            self.resume_access_check = True
            self.gate.set()
            self.store.update_task(task_id, status=BrowserTaskStatus.STARTING,
                detail="正在接续原搜索并重新核验登录；已记录职位不会重复分析", takeover_reason=None)
            self.worker = asyncio.create_task(self._run_guarded(task_id, original), name=f"search-continue-{task_id}")
            return self.current()

    async def _run_guarded(self, task_id: str, request: BrowserSearchRequest) -> None:
        def observe():
            view = self.store.get_task(task_id)
            key = (view.status, view.checkpoint, view.current_url, view.detail,
                   view.discovered, view.stored, view.duplicates, view.matched,
                   view.funnel.model_dump_json(), view.dry_run_contacted, view.dry_run_submitted)
            return key, view.status in {BrowserTaskStatus.WAITING_FOR_USER, BrowserTaskStatus.PAUSED}
        try:
            await guarded_operation(self._run(task_id, request), observe, limits=self.watch_limits)
        except WatchdogExpired as exc:
            self.store.event(task_id, "watchdog_expired", str(exc), reason_code=exc.code)
            self.store.increment_funnel(task_id, funnel_errors=1)
            self.store.update_task(task_id, status=BrowserTaskStatus.FAILED, detail=str(exc),
                                   checkpoint=exc.code, takeover_reason=None)

    def current(self) -> BrowserTaskView:
        if self.current_id:
            try:
                return self.store.get_task(self.current_id)
            except KeyError:
                pass
        return self.store.latest_task()

    def can_start_search(self) -> bool:
        # A persisted task is history, not a live lease. start() reconciles stale
        # non-terminal records; a live worker (including cleanup) still blocks.
        return self.worker is None or self.worker.done()

    async def pause(self) -> BrowserTaskView:
        state = self.current()
        if state.status not in {BrowserTaskStatus.RUNNING, BrowserTaskStatus.STARTING}:
            return state
        self.gate.clear()
        self.store.event(state.id or "unknown", "task_paused", "用户暂停任务")
        return self.store.update_task(state.id, status=BrowserTaskStatus.PAUSED, detail="任务已暂停")

    async def resume(self) -> BrowserTaskView:
        state = self.current()
        if state.status not in {BrowserTaskStatus.PAUSED, BrowserTaskStatus.WAITING_FOR_USER}:
            raise ValueError(f"当前浏览器状态为 {state.status}，没有暂停或等待接管的任务，未执行恢复")
        if self.worker is None or self.worker.done():
            raise ValueError("浏览器任务已没有存活执行器，请重新搜索；未恢复旧任务")
        self.resume_access_check = True
        self.gate.set()
        source_label = {"mock": "模拟招聘网站", "boss": "BOSS直聘", "zhaopin": "智联招聘"}.get(state.source, state.source)
        self.store.event(state.id or "unknown", "task_resumed", f"用户确认继续；将重新绑定{source_label}页面")
        return self.store.update_task(state.id, status=BrowserTaskStatus.RUNNING, detail=f"正在重新检查登录状态和{source_label}页面", takeover_reason=None)

    async def stop(self) -> BrowserTaskView:
        state = self.current()
        if self.worker and not self.worker.done():
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        self.gate.set()
        if state.id and (state.status not in TERMINAL_STATUSES or state.takeover_reason):
            self.store.event(state.id, "task_stopped", "用户停止任务")
            self.store.increment_funnel(state.id, cancelled=1)
            self.store.event(
                state.id, "funnel_cancelled", "用户中断搜索",
                reason_code="user_cancelled",
            )
            state = self.store.update_task(
                state.id,
                status=BrowserTaskStatus.STOPPED,
                detail="任务已停止",
                takeover_reason=None,
            )
        # Stop means stop the task, not close the user's logged-in Chrome.
        # Keep the CDP attachment alive so the next search can reuse the exact
        # same browser and BOSS target without a detach/reattach cycle.
        return state

    async def shutdown(self) -> None:
        if self.worker and not self.worker.done():
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        await self._close_session()

    async def _close_session(self) -> None:
        if self.session:
            await self.session.close()
            self.session = None

    async def _run(self, task_id: str, request: BrowserSearchRequest) -> None:
        execution_lock = BrowserExecutionLock(self.data_root / "private" / "browser-session.lock", task_id)
        pending = []
        try:
            execution_lock.acquire()
            if request.source == "mock":
                await self._run_mock_site(task_id, request)
                return
            self.adapter = ZhaopinAdapter() if request.source == "zhaopin" else BossAdapter()
            self.store.update_task(task_id, status=BrowserTaskStatus.STARTING, detail="正在连接浏览器")
            # BOSS currently serves a persistent loading shell (and may later clear
            # the tab to about:blank) to a Playwright-launched managed profile.
            # Never silently downgrade AUTO to that surface: fail closed unless the
            # user explicitly selected managed mode for diagnostics.
            requested_mode = BrowserMode.CDP if request.browser_mode == BrowserMode.AUTO else request.browser_mode

            if self.session is None:
                self.session = BrowserSession(self.profile_dir, headless=self.headless, cdp_url=self.cdp_url)
            actual_mode = await self.session.reuse(requested_mode)
            session_reused = actual_mode is not None

            if requested_mode == BrowserMode.CDP and not session_reused:
                # Keep Playwright completely detached during login. BOSS can make
                # its login flow unstable merely because a CDP automation client
                # is attached, even when that client is not navigating the page.
                # ensure_cdp_browser launches the dedicated AutoHunt Chrome when
                # it is not running yet; ensure_cdp_window then briefly attaches
                # only to open/focus a visible BOSS tab and detaches again, so
                # the login phase itself never runs with a live CDP client.
                await self.session.ensure_cdp_browser(self.adapter.origin)
                self.store.event(task_id, "cdp_browser_ready", self.cdp_url)
                await self.session.ensure_cdp_window(self.adapter.origin)
                self.store.event(task_id, "cdp_window_ready", "HTTP-only tab activation completed")
                self.gate.clear()
                self.store.event(task_id, "user_takeover_required", "Chrome 已自动拉起；登录阶段尚未连接 Playwright")
                self.store.update_task(
                    task_id,
                    status=BrowserTaskStatus.WAITING_FOR_USER,
                    browser_mode=BrowserMode.CDP.value,
                    detail=f"已打开{self.adapter.label}；请在该窗口中完成登录，然后点击继续",
                    takeover_reason="等待用户独立完成登录、验证码或 MFA",
                    checkpoint="waiting_for_login_before_attach",
                    current_url=self.adapter.origin,
                )
                await self.gate.wait()
                self.store.event(task_id, "user_resume_received", "准备连接 Playwright CDP")
                self.store.update_task(
                    task_id,
                    status=BrowserTaskStatus.STARTING,
                    detail="用户确认登录完成，正在连接 Chrome CDP",
                    takeover_reason=None,
                    checkpoint="attaching_after_login",
                )

            if session_reused:
                await self.session.ensure_cdp_window(self.adapter.origin)
                self.store.event(task_id, "browser_session_reused", f"{actual_mode}; pages={len(self.session.pages())}")
            else:
                self.store.event(task_id, "browser_attach_started", requested_mode.value)
                actual_mode = await self.session.open(requested_mode)
                self.store.event(task_id, "browser_attach_ready", f"{actual_mode}; pages={len(self.session.pages())}")
            self.store.update_task(task_id, browser_mode=actual_mode, selector_version=self.adapter.selector_version, detail="已接管用户 Chrome" if actual_mode == "cdp" else "已打开 Agent 专用 Chromium")

            queries = [item.strip() for item in (request.queries or []) if item.strip()] or [request.query]
            locations = [item.strip() for item in (request.locations or []) if item.strip()]
            search_runs = [(query, city) for city in (locations or [None]) for query in queries][:12]
            # Login is a strict preflight. Navigating an unauthenticated session
            # directly to /web/geek/jobs makes BOSS bounce between search, login,
            # and home pages. Keep the browser stable on the homepage and wait for
            # the user before issuing search navigations.
            self.store.event(task_id, "login_preflight_started", f"pages={len(self.session.pages())}")
            page = await self.session.recover_page(self.adapter.origin)
            page = await self._wait_for_page_readiness(page, self.adapter.origin)
            page = await self._ensure_access(task_id, self.adapter.origin, "login_preflight", screenshot=True)
            self.store.event(task_id, "login_preflight_ready", page.url)
            await self._respect_pause()

            all_cards: list[dict] = []
            seen_urls: set[str] = set()
            for position, (query, city) in enumerate(search_runs, start=1):
                await self._respect_pause()
                if position > 1:
                    # Human rhythm between search rounds: rapid successive
                    # searches are a strong automation signal for risk control.
                    await self._paced_wait(4.0, 10.0)
                city_note = f"（城市筛选：{city}）" if city else ""
                self.store.update_task(task_id, detail=f"正在搜索第 {position}/{len(search_runs)} 个关键词：{query}{city_note}")
                self.store.event(task_id, "search_iteration_recover_started", self.session.lifecycle_snapshot())
                page_cards = await self._execute_search_round(task_id, query, city, position)
                valid_count = sum(1 for card in page_cards if self.adapter.safe_job_url(card.get("url", "")))
                self.store.increment_funnel(
                    task_id,
                    links_seen=len(page_cards),
                    cards_parsed=valid_count,
                    funnel_errors=len(page_cards) - valid_count,
                )
                if valid_count < len(page_cards):
                    self.store.event(
                        task_id, "funnel_parse_error",
                        f"{len(page_cards) - valid_count} 个职位卡片缺少可信链接",
                        reason_code="invalid_job_url",
                    )
                for card in page_cards:
                    url = card.get("url", "")
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        all_cards.append(card)
                self.store.update_task(
                    task_id,
                    discovered=len(all_cards),
                    detail=f"第 {position}/{len(search_runs)} 个搜索条件完成，累计发现 {len(all_cards)} 个职位",
                )
                self.store.event(task_id, "search_results", f"关键词「{query}」{city_note}发现 {len(page_cards)} 个职位链接")

            cards = all_cards[: request.max_jobs]
            self.store.update_task(
                task_id,
                discovered=len(all_cards),
                unique_jobs=len(all_cards),
                limited=max(0, len(all_cards) - len(cards)),
                detail=f"发现 {len(all_cards)} 个职位",
            )
            if len(all_cards) > len(cards):
                self.store.event(
                    task_id, "funnel_limited", f"仅处理前 {len(cards)} 个唯一职位",
                    reason_code="max_jobs_limit",
                    sample={"unique": len(all_cards), "processed": len(cards)},
                )
            self.store.event(task_id, "search_results", f"共发现 {len(all_cards)} 个职位链接")
            await self._record_checkpoint(task_id, "search_results", page, screenshot=True)
            cards = self._filter_salary_cards(task_id, cards, request)
            processed = self.store.search_recovery(task_id)["processed_urls"]
            cards = [card for card in cards if card.get("url") not in processed]
            if not cards:
                self.store.update_task(task_id, status=BrowserTaskStatus.COMPLETED, detail="登录有效，但页面中没有识别到职位；可能确实无结果或选择器需要更新")
                return

            candidate_skills, target_roles, excluded_keywords = self._candidate_match_context(queries)
            candidate_facts=match_facts(self.resume_workspace,self.candidate_state)
            previous_counts = self.store.get_task(task_id)
            stored, duplicates, matched = previous_counts.stored, previous_counts.duplicates, previous_counts.matched

            def _accumulate(deltas: tuple[int, int, int]) -> None:
                nonlocal stored, duplicates, matched
                d_stored, d_duplicates, d_matched = deltas
                stored += d_stored
                duplicates += d_duplicates
                matched += d_matched
                self.store.update_task(task_id, stored=stored, duplicates=duplicates, matched=matched)

            # Phase A: one batch pre-screen call on card data. SKIP jobs never
            # open a detail page; APPLY/REVIEW candidates go through Phase B.
            verdicts = await pre_screen_cards(
                self.llm.generate, cards, candidate_skills,
                target_roles=target_roles,
                excluded_keywords=excluded_keywords,
                search_queries=queries,
            )
            detail_indices = [i for i, verdict in enumerate(verdicts) if verdict["decision"] in DETAIL_DECISIONS]
            detail_set = set(detail_indices)
            self.store.increment_funnel(task_id, filtered=len(cards) - len(detail_indices))
            self.store.event(
                task_id, "job_prescreen",
                f"初筛完成：{len(cards)} 个职位中 {len(detail_indices)} 个需要读取详情页，跳过 {len(cards) - len(detail_indices)} 个",
            )

            for index, (card, verdict) in enumerate(zip(cards, verdicts)):
                if index in detail_set:
                    continue
                url = self.adapter.safe_job_url(card.get("url", ""))
                if not url:
                    continue
                if verdict["decision"] == "SKIP":
                    # Irrelevant or over-qualified jobs never reach the board;
                    # they would dilute it with results unrelated to the search.
                    self.store.event(
                        task_id, "job_skipped", f"{str(card.get('title') or '未知职位')[:40]}（初筛跳过，不入看板）",
                        reason_code="prescreen_skip",
                        sample={"title": str(card.get("title") or "未知职位")[:100], "reason": verdict["reasoning"][:200]},
                    )
                    self.store.mark_search_processed(task_id, url)
                    continue
                analysis = JobAnalysis(score=0, decision=verdict["decision"], matched_skills=[], missing_skills=[], reasoning=verdict["reasoning"])
                _accumulate(self._persist_analysis(task_id, self._detail_from_card(card), analysis, url, request=request))

            # Phase B: serial detail-page reads, with per-job LLM matches running
            # concurrently (bounded semaphore) so DeepSeek latency overlaps the
            # next navigation instead of adding to it.
            semaphore = asyncio.Semaphore(MATCH_CONCURRENCY)
            pending: list[tuple[dict, str, JobAnalysisRequest, asyncio.Task]] = []

            async def guarded_match(match_request: JobAnalysisRequest, detail: dict) -> JobAnalysis:
                async with semaphore:
                    return await self._match_and_filter(task_id, detail, match_request, request)

            try:
                for position, index in enumerate(detail_indices, start=1):
                    card = cards[index]
                    await self._respect_pause()
                    # Human rhythm between detail-page visits.
                    await self._paced_wait(1.0, 3.0)
                    url = self.adapter.safe_job_url(card.get("url", ""))
                    if not url:
                        self.store.increment_funnel(task_id, funnel_errors=1)
                        self.store.event(task_id, "job_read_failed", "职位链接不可信", reason_code="invalid_job_url")
                        continue
                    self.store.increment_funnel(task_id, visited=1)
                    self.store.update_task(task_id, detail=f"正在读取第 {position}/{len(detail_indices)} 个职位")
                    try:
                        page = await self.session.recover_page(url)
                        page = await self.session.navigate(url, page=page)
                        page = await self._ensure_access(task_id, url, f"job_{position}_access")
                        await self.adapter.wait_for_job_detail(page)
                        detail = await self.adapter.extract_job_detail(page, card)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self.store.increment_funnel(task_id, funnel_errors=1)
                        self.store.event(
                            task_id, "job_read_failed", type(exc).__name__,
                            reason_code="detail_read_failed",
                            sample={"title": str(card.get("title") or "未知职位")[:100]},
                        )
                        continue

                    description = detail["description"]
                    if len(description) < 30:
                        description = (
                            f"BOSS 职位：{detail['title']}；公司：{detail['company']}；"
                            f"地点：{detail['location']}；薪资：{detail['salary']}。"
                            f"详情页未返回完整 JD，需在自动申请看板中人工复核。{description}"
                        )
                    detail["description"] = description
                    match_request = detail_request_from_card(
                        card, description, candidate_skills,
                        target_roles=target_roles,
                        excluded_keywords=excluded_keywords,
                        search_queries=queries,
                        candidate_facts=candidate_facts,
                    )
                    pending.append((detail, url, match_request, asyncio.create_task(guarded_match(match_request, detail))))
            except asyncio.CancelledError:
                for _, _, _, task in pending:
                    task.cancel()
                await asyncio.gather(*(task for _, _, _, task in pending), return_exceptions=True)
                raise

            for detail, url, match_request, task in pending:
                await self._respect_pause()
                try:
                    analysis = await task
                except Exception:
                    analysis = analyze_job(match_request)
                _accumulate(self._persist_analysis(task_id, detail, analysis, url, request=request))

            self.store.update_task(task_id, status=BrowserTaskStatus.COMPLETED, detail=f"搜索完成：保存 {stored}，重复 {duplicates}，建议审核 {matched}", checkpoint="completed")
            self.store.event(task_id, "task_completed", f"保存 {stored} 个职位")
        except asyncio.CancelledError:
            raise
        except BrowserLockBusy as exc:
            self.store.increment_funnel(task_id, funnel_errors=1)
            self.store.event(task_id, "task_failed", "browser_session_lock_busy")
            self.store.update_task(task_id, status=BrowserTaskStatus.FAILED, detail=str(exc))
        except Exception as exc:
            self.store.increment_funnel(task_id, funnel_errors=1)
            snapshot = self.session.lifecycle_snapshot() if self.session else "no_session"
            self.store.event(task_id, "task_failed", f"{type(exc).__name__}: {str(exc)[:300]}; {snapshot}")
            self.store.update_task(task_id, status=BrowserTaskStatus.FAILED, detail=f"浏览器任务失败：{str(exc)[:180]}")
        finally:
            for _, _, _, work in pending:
                if not work.done():
                    work.cancel()
            await asyncio.gather(*(work for _, _, _, work in pending), return_exceptions=True)
            # Preserve the single CDP Chrome and its BOSS tab across tasks.
            # Browser shutdown is an application-lifecycle operation, never a
            # search-task cleanup operation.
            self.store.event(
                task_id,
                "browser_session_preserved",
                self.session.lifecycle_snapshot() if self.session else "no_session",
            )
            execution_lock.release()

    async def _run_mock_site(self, task_id: str, request: BrowserSearchRequest) -> None:
        """Run the simulator through its visible login and search UI."""
        self.store.update_task(task_id, status=BrowserTaskStatus.STARTING, detail="正在连接模拟招聘网站")
        requested_mode = BrowserMode.CDP if request.browser_mode == BrowserMode.AUTO else request.browser_mode
        if self.session is None:
            self.session = BrowserSession(self.profile_dir, headless=self.headless, cdp_url=self.cdp_url)
        actual_mode = await self.session.reuse(requested_mode)
        if actual_mode is None:
            if requested_mode == BrowserMode.CDP:
                await self.session.ensure_cdp_browser(MOCK_SITE_ORIGIN)
                await self.session.ensure_cdp_window(MOCK_SITE_ORIGIN)
            actual_mode = await self.session.open(requested_mode)
        else:
            await self.session.ensure_cdp_window(MOCK_SITE_ORIGIN)

        self.store.update_task(
            task_id,
            browser_mode=actual_mode,
            selector_version=self.mock_adapter.selector_version,
            detail="已接管模拟招聘网站浏览器会话",
        )
        page = await self.session.recover_page(MOCK_SITE_ORIGIN)
        if urlparse(page.url).path not in {"", "/"} or not page.url.startswith(MOCK_SITE_ORIGIN):
            page = await self.session.navigate(MOCK_SITE_ORIGIN, page=page)
        await page.locator("#keyword").wait_for(state="attached", timeout=10_000)

        page = await self._ensure_mock_access(task_id, MOCK_SITE_ORIGIN, "mock_login_preflight")

        applicant_id = await page.evaluate(
            "() => localStorage.getItem('userRole') === 'applicant' ? localStorage.getItem('userId') : null"
        )
        if str(applicant_id or "").isdigit():
            self.store.bind_mock_applicant(int(applicant_id))
        self.store.update_task(
            task_id,
            status=BrowserTaskStatus.RUNNING,
            detail="模拟招聘网站登录已确认，开始搜索职位",
            takeover_reason=None,
            checkpoint="mock_login_confirmed",
        )
        queries = [item.strip() for item in (request.queries or []) if item.strip()] or [request.query]
        locations = [item.strip() for item in (request.locations or []) if item.strip()]
        search_runs = [(query, city) for city in (locations or [None]) for query in queries][:12]
        all_cards: list[dict] = []
        seen_urls: set[str] = set()
        for position, (query, city) in enumerate(search_runs, start=1):
            await self._respect_pause()
            page = await self.session.recover_page(MOCK_SITE_ORIGIN)
            if urlparse(page.url).path not in {"", "/"}:
                page = await self.session.navigate(MOCK_SITE_ORIGIN, page=page)
            city_note = f"（城市：{city}）" if city else ""
            self.store.update_task(task_id, detail=f"正在模拟网站搜索 {position}/{len(search_runs)}：{query}{city_note}")
            cards = await self.mock_adapter.search(page, query, city, max_cards=request.max_jobs)
            valid_count = sum(1 for card in cards if self.mock_adapter.safe_job_url(card.get("url", "")))
            self.store.increment_funnel(
                task_id,
                links_seen=len(cards),
                cards_parsed=valid_count,
                funnel_errors=len(cards) - valid_count,
            )
            for card in cards:
                if card["url"] not in seen_urls:
                    seen_urls.add(card["url"])
                    all_cards.append(card)
            self.store.update_task(task_id, discovered=len(all_cards))
            self.store.event(task_id, "search_results", f"模拟网站关键词「{query}」{city_note}发现 {len(cards)} 个职位")

        cards = all_cards[: request.max_jobs]
        self.store.update_task(
            task_id,
            discovered=len(all_cards),
            unique_jobs=len(all_cards),
            limited=max(0, len(all_cards) - len(cards)),
            detail=f"模拟网站发现 {len(all_cards)} 个职位",
        )
        await self._record_checkpoint(task_id, "mock_search_results", page, (await self.mock_adapter.inspect(page)).as_dict())
        cards = self._filter_salary_cards(task_id, cards, request)
        processed = self.store.search_recovery(task_id)["processed_urls"]
        cards = [card for card in cards if card.get("url") not in processed]
        if not cards:
            self.store.update_task(task_id, status=BrowserTaskStatus.COMPLETED, detail="搜索完成，但模拟网站没有符合条件的职位")
            return

        candidate_skills, target_roles, excluded_keywords = self._candidate_match_context(queries)
        candidate_facts=match_facts(self.resume_workspace,self.candidate_state)
        verdicts = await pre_screen_cards(
            self.llm.generate,
            cards,
            candidate_skills,
            target_roles=target_roles,
            excluded_keywords=excluded_keywords,
            search_queries=queries,
        )
        filtered_count = sum(1 for verdict in verdicts if verdict["decision"] == "SKIP")
        self.store.increment_funnel(task_id, filtered=filtered_count)
        previous_counts = self.store.get_task(task_id)
        stored, duplicates, matched = previous_counts.stored, previous_counts.duplicates, previous_counts.matched
        dry_run_candidates: list[tuple[int, int]] = []
        for position, (card, verdict) in enumerate(zip(cards, verdicts), start=1):
            await self._respect_pause()
            if request.result_limit and matched >= request.result_limit:
                self.store.increment_funnel(task_id, limited=len(cards) - position + 1)
                break
            url = self.mock_adapter.safe_job_url(card.get("url", ""))
            if not url:
                self.store.increment_funnel(task_id, funnel_errors=1)
                self.store.event(task_id, "job_read_failed", "模拟职位链接不可信", reason_code="invalid_job_url")
                continue
            if verdict["decision"] == "SKIP":
                self.store.event(
                    task_id, "job_skipped", f"{str(card.get('title') or '未知职位')[:40]}（初筛跳过）",
                    reason_code="prescreen_skip",
                    sample={"title": str(card.get("title") or "未知职位")[:100], "reason": verdict["reasoning"][:200]},
                )
                self.store.mark_search_processed(task_id, url)
                continue
            self.store.increment_funnel(task_id, visited=1)
            self.store.update_task(task_id, detail=f"正在读取模拟职位 {position}/{len(cards)}")
            try:
                detail_page = await self.session.recover_page(url)
                detail_page = await self.session.navigate(url, page=detail_page)
                await self.mock_adapter.wait_for_job_detail(detail_page)
                detail = await self.mock_adapter.extract_job_detail(detail_page, card)
                if not self._salary_allowed(task_id, detail, request):
                    self.store.mark_search_processed(task_id, url)
                    continue
                match_request = detail_request_from_card(
                    card,
                    detail["description"],
                    candidate_skills,
                    target_roles=target_roles,
                    excluded_keywords=excluded_keywords,
                    search_queries=queries,
                    candidate_facts=candidate_facts,
                )
                try:
                    analysis = await self._match_and_filter(task_id, detail, match_request, request)
                except Exception:
                    analysis = analyze_job(match_request)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.store.increment_funnel(task_id, funnel_errors=1)
                self.store.event(
                    task_id, "job_read_failed", f"模拟职位读取失败：{type(exc).__name__}",
                    reason_code="detail_read_failed",
                    sample={"title": str(card.get("title") or "未知职位")[:100]},
                )
                detail = {
                    "title": card.get("title") or "未命名职位",
                    "company": card.get("company") or "未知公司",
                    "location": card.get("location") or "未注明",
                    "salary": card.get("salary") or "未注明",
                    "description": "模拟招聘网站职位详情读取失败，需在看板中人工复核。",
                    "tags": list(card.get("tags") or []),
                    "external_id": card.get("external_id"),
                }
                analysis = JobAnalysis(
                    score=0,
                    decision=verdict["decision"],
                    matched_skills=[],
                    missing_skills=[],
                    reasoning=verdict["reasoning"],
                )
            if "filter_status" not in detail:
                await self._check_filters(task_id, detail, request)
            delta = self._persist_analysis(task_id, detail, analysis, url, source="MockJobSite", request=request)
            stored += delta[0]
            duplicates += delta[1]
            matched += delta[2]
            job_id = self.store.job_id_for_source_url(url)
            if job_id is not None and analysis.score >= request.dry_run_min_score:
                stored_job = self.store.get_job(job_id)
                if delta[2] and stored_job.get("filter_status", "pass") == "pass" and stored_job.get("stage") == "review" and analysis.decision in {"APPLY", "REVIEW"}:
                    dry_run_candidates.append((analysis.score, job_id))
            self.store.update_task(task_id, stored=stored, duplicates=duplicates, matched=matched)

        if request.dry_run:
            selected_ids = [job_id for _, job_id in sorted(dry_run_candidates, reverse=True)[: request.dry_run_max_contacts]]
            await self._run_mock_delivery_dry_run(task_id, request, selected_ids)

        final_detail = self.store.get_task(task_id).detail if request.dry_run else f"模拟网站搜索完成：保存 {stored}，重复 {duplicates}，建议审核 {matched}"
        self.store.update_task(
            task_id,
            status=BrowserTaskStatus.COMPLETED,
            detail=final_detail,
            checkpoint="completed",
        )
        self.store.event(task_id, "task_completed", f"模拟网站保存 {stored} 个职位")

    def _check_daily_submission(self, policy: dict | None) -> None:
        if policy is None:
            return  # An explicit manual Dry Run has its own batch authorization.
        settings = self.store.mock_workflow_settings()
        epoch = policy.get("auto_submit_epoch")
        if not epoch or not settings["auto_submit"] or settings["auto_submit_enabled_at"] != epoch:
            raise DailySubmissionDisabled("本轮未获当前自动投递授权，已保留招聘方回复，简历待人工确认")

    async def _run_mock_delivery_dry_run(
        self,
        task_id: str,
        request: BrowserSearchRequest,
        job_ids: list[int],
    ) -> None:
        """Contact matched simulator jobs and submit only after an observed HR reply.

        An explicit Dry Run command is the batch authorization; scheduled runs
        additionally require the unchanged opt-in setting before binding/clicks. Each job
        still receives a separate preview and single-use capability before any
        browser-side action. Only the local simulator is accepted by the adapter.
        """
        saved_request = self.store.search_recovery(task_id)["request"] or {}
        daily_policy = saved_request.get("_daily_policy")
        counters = {
            "dry_run_candidates": len(job_ids),
            "dry_run_contacted": 0,
            "dry_run_replied": 0,
            "dry_run_submitted": 0,
            "dry_run_waiting_reply": 0,
            "dry_run_failed": 0,
        }
        report_lines: list[str] = []
        self.store.update_task(
            task_id,
            status=BrowserTaskStatus.RUNNING,
            detail=f"Dry Run：准备处理 {len(job_ids)} 个高匹配岗位",
            checkpoint="mock_dry_run_start",
            **counters,
        )
        if not job_ids:
            report = f"没有达到 {request.dry_run_min_score} 分且仍待审核的模拟岗位，本轮未发起沟通或投递。"
            self.store.update_task(task_id, dry_run_report=report, checkpoint="mock_dry_run_completed")
            self.store.event(task_id, "dry_run_completed", report)
            return

        for position, job_id in enumerate(job_ids, start=1):
            await self._respect_pause()
            job = self.store.get_job(job_id)
            label = f"{job['company']} / {job['title']}"
            submit_started = False
            submit_confirmed = False
            try:
                job_url = self.mock_delivery_adapter.normalize_job_url(str(job.get("source_url") or ""))
                if not job_url:
                    raise ValueError("职位不是受支持的本地模拟招聘网站地址")

                binding: dict[str, object] = {}
                if self.resume_workspace is not None:
                    document = self.resume_workspace.read_document()
                    binding = {
                        "resume_document_id": document.id,
                        "resume_version": document.version,
                        "resume_digest": resume_document_digest(document),
                    }
                outreach = None
                if self.resume_workspace is not None:
                    from ..delivery.outreach import build_outreach
                    outreach = build_outreach(job, document)
                preview = self.store.create_delivery_preview(job_id, **binding, **({'outreach': outreach} if outreach else {}))
                authorization_id, token, authorized_job_id, _ = self.store.authorize_delivery(
                    preview.id, **binding
                )
                if self.store.consume_delivery_authorization(authorization_id, token) != authorized_job_id:
                    raise RuntimeError("一次性投递授权与职位不匹配")
                self.store.event(task_id, "dry_run_authorized", f"{job_id}:{preview.payload_digest[:12]}")
                self.store.update_task(
                    task_id,
                    detail=f"Dry Run {position}/{len(job_ids)}：正在发起沟通 · {label}",
                    checkpoint="mock_dry_run_contacting",
                )

                page = await self.session.recover_page(job_url)
                page = await self.session.navigate(job_url, page=page)
                while True:
                    observation = await self.mock_delivery_adapter.prepare(page, job_url,
                        **({'outreach_message': outreach['message']} if outreach else {}))
                    if not observation.intervention_reason:
                        break
                    self.gate.clear()
                    self.store.update_task(
                        task_id,
                        status=BrowserTaskStatus.WAITING_FOR_USER,
                        detail="Dry Run 已暂停；请在模拟招聘网站完成人工登录或 MFA",
                        takeover_reason=observation.intervention_reason,
                        checkpoint="mock_dry_run_waiting_for_user",
                        current_url=page.url,
                    )
                    await self._wait_for_user_or_disconnect()
                    page = await self.session.recover_page(job_url)
                    page = await self.session.navigate(job_url, page=page)
                    self.store.update_task(
                        task_id,
                        status=BrowserTaskStatus.RUNNING,
                        detail=f"Dry Run：继续处理 {label}",
                        takeover_reason=None,
                    )

                counters["dry_run_contacted"] += 1
                self.store.event(task_id, "dry_run_contacted", label)

                if (
                    position == 1
                    and request.dry_run_simulate_reply
                    and not observation.hr_reply_text
                    and not observation.application_submitted
                ):
                    injected = await self._inject_mock_recruiter_reply(page, job, task_id)
                    if injected:
                        await page.reload(wait_until="domcontentloaded")
                        observation = await self.mock_delivery_adapter.prepare(page, job_url)

                if observation.application_submitted:
                    if observation.hr_reply_text:
                        self.store.mark_recruiter_reply(job_id, observation.hr_reply_text)
                        counters["dry_run_replied"] += 1
                    self.store.mark_delivery_completed(job_id, "Dry Run 检测到既有投递成功证据")
                    counters["dry_run_submitted"] += 1
                    report_lines.append(f"- {label}：页面已有投递成功证据")
                elif observation.hr_reply_text:
                    self.store.mark_recruiter_reply(job_id, observation.hr_reply_text)
                    counters["dry_run_replied"] += 1
                    self._check_daily_submission(daily_policy)
                    if self.resume_workspace is None:
                        raise ValueError("没有授权主简历，不能投递")
                    from ..delivery.resume_binding import mock_resume_payload
                    payload = mock_resume_payload(self.resume_workspace.read_document(),
                        document_id=binding.get("resume_document_id"), version=binding.get("resume_version"),
                        digest=binding.get("resume_digest"))
                    site_binding = await self.mock_delivery_adapter.bind_resume(page, payload,
                        applicant_id=self.store.mock_workflow_settings().get("applicant_id"))
                    mock_resume_payload(self.resume_workspace.read_document(),
                        document_id=binding.get("resume_document_id"), version=binding.get("resume_version"),
                        digest=binding.get("resume_digest"))
                    def before_submit_click():
                        nonlocal submit_started
                        self._check_daily_submission(daily_policy)
                        mock_resume_payload(self.resume_workspace.read_document(),
                            document_id=binding.get("resume_document_id"), version=binding.get("resume_version"),
                            digest=binding.get("resume_digest"))
                        self.store.update_task(task_id, checkpoint="mock_dry_run_submit_started")
                        submit_started = True
                    result = await self.mock_delivery_adapter.submit(page, binding=site_binding,
                        before_click=before_submit_click)
                    if result.confirmed:
                        submit_confirmed = True
                        self.store.mark_delivery_completed(job_id, f"Dry Run 已确认点击发送简历；招聘方回复：{observation.hr_reply_text}")
                        counters["dry_run_submitted"] += 1
                        report_lines.append(f"- {label}：检测到明确回复并已点击“发简历”")
                    else:
                        self.store.mark_outcome_indeterminate(job_id, result.detail)
                        counters["dry_run_failed"] += 1
                        report_lines.append(f"- {label}：提交结果不确定，已禁止自动重试")
                else:
                    self.store.mark_delivery_waiting_reply(
                        job_id,
                        "Dry Run 已发起沟通，等待招聘方明确回复",
                        mode="自动沟通·等待回复",
                    )
                    counters["dry_run_waiting_reply"] += 1
                    report_lines.append(f"- {label}：已发起沟通，等待回复，未发送简历")
            except DailySubmissionDisabled as exc:
                detail = str(exc)
                if submit_started:
                    self.store.mark_outcome_indeterminate(job_id, "提交途中自动投递授权失效，结果待核验，禁止自动重试")
                    counters["dry_run_failed"] += 1
                    report_lines.append(f"- {label}：提交途中授权失效，结果待核验，未自动重试")
                else:
                    self.store.mark_job_needs_review(job_id, detail)
                    report_lines.append(f"- {label}：已收到回复，自动投递未授权或已关闭，未点击发送简历")
                self.store.event(task_id, "daily_submission_withheld", f"{label}: {detail}")
            except asyncio.CancelledError:
                if submit_started and not submit_confirmed:
                    self.store.mark_outcome_indeterminate(job_id, "简历提交期间中断/超时，结果未知，禁止自动重试")
                raise
            except Exception as exc:
                counters["dry_run_failed"] += 1
                detail = str(exc)[:300]
                if submit_started:
                    self.store.mark_outcome_indeterminate(job_id, detail)
                else:
                    self.store.update_stage(job_id, "review")
                self.store.event(task_id, "dry_run_job_failed", f"{label}: {type(exc).__name__}: {detail}")
                report_lines.append(f"- {label}：执行失败，未自动重试")
            finally:
                self.store.update_task(task_id, **counters)

        report = "\n".join(report_lines)
        self.store.update_task(
            task_id,
            detail=(
                f"Dry Run 完成：联系 {counters['dry_run_contacted']}，"
                f"明确回复 {counters['dry_run_replied']}，投递 {counters['dry_run_submitted']}，"
                f"等待回复 {counters['dry_run_waiting_reply']}"
            ),
            checkpoint="mock_dry_run_completed",
            dry_run_report=report,
            **counters,
        )
        self.store.event(task_id, "dry_run_completed", self.store.get_task(task_id).detail)

    async def _inject_mock_recruiter_reply(self, page: Page, job: dict, task_id: str) -> bool:
        """Create one clearly labelled simulator-only HR reply for E2E coverage."""
        applicant_id = str(await page.evaluate("() => localStorage.getItem('userId') || ''"))
        external_id = str(job.get("external_id") or "")
        if not applicant_id.isdigit() or not external_id.isdigit():
            return False

        def send_reply() -> None:
            with urlopen(f"{MOCK_SITE_ORIGIN}/api/jobs/{external_id}", timeout=5) as response:
                remote_job = json.loads(response.read().decode("utf-8"))
            body = json.dumps({
                "recipient_id": int(applicant_id),
                "job_id": int(external_id),
                "content": "【Dry Run 测试招聘方回复】你的经历与岗位方向匹配，请发送简历。",
            }, ensure_ascii=False).encode("utf-8")
            request = Request(
                f"{MOCK_SITE_ORIGIN}/api/messages",
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "X-User-Id": str(remote_job["employer_id"]),
                    "X-User-Role": "employer",
                },
            )
            with urlopen(request, timeout=5) as response:
                if response.status != 201:
                    raise RuntimeError(f"模拟招聘方回复失败：HTTP {response.status}")

        try:
            await asyncio.to_thread(send_reply)
            self.store.event(task_id, "dry_run_mock_reply_injected", f"job:{job.get('id')}")
            return True
        except Exception as exc:
            self.store.event(task_id, "dry_run_mock_reply_failed", type(exc).__name__)
            return False
    async def _wait_for_search_results(self, task_id: str, search_url: str) -> Page:
        self.gate.clear()
        self.store.event(task_id, "user_takeover_required", "页面未返回职位列表")
        self.store.update_task(
            task_id,
            status=BrowserTaskStatus.WAITING_FOR_USER,
            detail="BOSS 搜索页仍是骨架屏或首页路由；请在浏览器中等待职位列表出现，必要时手动点击一次搜索，然后点击继续",
            takeover_reason="需要用户确认搜索结果页面已显示",
            checkpoint="waiting_for_search_results",
        )
        await self._wait_for_user_or_disconnect()
        page = await self.session.recover_page(search_url)
        # Do not navigate the deep link here: BOSS may redirect it back to '/'
        # and restart the exact loop the user just recovered from.
        page = await self._wait_for_page_readiness(page, search_url)
        return await self._ensure_access(task_id, search_url, "search_recovered")

    async def _paced_wait(self, low: float, high: float) -> None:
        """Human-like random pause to reduce automation fingerprints.

        Cancellation propagates through the sleep; callers run a
        _respect_pause() right after when browser movement follows.
        """
        await asyncio.sleep(random.uniform(low, high))

    async def _execute_search_round(self, task_id: str, query: str, city: str | None, position: int) -> list[dict]:
        """Submit one search round and return its cards.

        BOSS risk control may close the tab right after a submit. A lost tab
        gets a cooldown pause (let the risk score decay), a rebind, and ONE
        retry; a second loss degrades to the human-takeover path instead of
        failing the task on a dead page.
        """
        page = await self.session.recover_page(self.adapter.origin)
        self.store.event(task_id, "search_iteration_page_ready", f"{page.url}; {self.session.lifecycle_snapshot()}")
        if city:
            selected = await self.adapter.select_city(page, city)
            if not selected:
                # A failed city selection must never sink the whole search:
                # fold the city into the keywords so intent is still honored.
                self.store.event(
                    task_id, "city_filter_unavailable",
                    f"{self.adapter.label}城市筛选器不可用，将「{city}」加入检索，并在读取后核对实际工作地点",
                )
                query = f"{query} {city}"
            else:
                self.store.event(task_id, "city_filter_selected", city)

        desired_search_url = self.adapter.search_url(query)
        # Give the page a moment of human rhythm before the first interaction.
        await self._paced_wait(2.5, 6.0)
        await self._respect_pause()

        for attempt in range(2):
            try:
                page_cards, page = await self._submit_search_attempt(task_id, page, query, desired_search_url)
                break
            except PlaywrightError as exc:
                if not self._is_target_closed(exc):
                    raise
                if attempt == 1:
                    self.store.event(task_id, "search_tab_lost_final", f"{self.adapter.label}搜索标签页反复关闭；转入人工接管")
                    page = await self._wait_for_search_results(task_id, desired_search_url)
                    return await self.adapter.extract_search_cards(page)
                self.store.event(task_id, "search_tab_lost", f"{self.adapter.label}搜索标签页关闭；等待后重新绑定重试一次")
                await self._paced_wait(10.0, 20.0)
                await self._respect_pause()
                page = await self.session.recover_page(self.adapter.origin)
                if city:
                    await self.adapter.select_city(page, city)

        # Safety pass: readiness + access check, then the human takeover for
        # an empty results page.
        page = await self._wait_for_page_readiness(page, desired_search_url)
        search_url = page.url if page.url.startswith(self.adapter.origin) else desired_search_url
        page = await self._ensure_access(task_id, search_url, "search_access_checked", screenshot=(position == 1))
        await self._respect_pause()
        if not page_cards:
            page = await self._wait_for_search_results(task_id, search_url)
            page_cards = await self.adapter.extract_search_cards(page)
        return page_cards

    async def _submit_search_attempt(self, task_id: str, page: Page, query: str, desired_search_url: str) -> tuple[list[dict], Page]:
        """Submit one search: deep link first, then the visible form fallback.

        A direct navigation looks like a user typing an address and avoids the
        fill+click automation signature. When BOSS bounces the deep link home
        (or shows a wall), the visible form is used instead. Target-closed
        errors propagate so the caller can rebind and back off.
        """
        page = await self.session.navigate(desired_search_url, page=page)
        await self.adapter.wait_for_search_cards(page, timeout_ms=8_000)
        cards = await self.adapter.extract_search_cards(page)
        if cards:
            return cards, page
        # Deep link bounced (home/city page) — inspect for walls, then use the form.
        page = await self._ensure_access(task_id, page.url, "deep_link_bounced")
        await self._respect_pause()
        if not await self.adapter.submit_search(page, query):
            raise RuntimeError(f"已登录，但未找到可用的{self.adapter.label}搜索框")
        await self.adapter.wait_for_search_cards(page)
        return await self.adapter.extract_search_cards(page), page

    async def _respect_pause(self) -> None:
        await self._wait_for_user_or_disconnect()
        if self.resume_access_check:
            # A user confirmation opens the gate, not proof of authentication.
            # The worker rebinds and inspects the live page before another action.
            self.resume_access_check = False
            state = self.current()
            if state.source == "mock":
                await self._ensure_mock_access(state.id, state.current_url or MOCK_SITE_ORIGIN, "mock_resume_access")
            else:
                await self._ensure_access(state.id, state.current_url or self.adapter.origin, "resume_access", require_auth=True)

    async def _ensure_mock_access(self, task_id: str, target_url: str, checkpoint: str) -> Page:
        while True:
            if not self.session:
                raise RuntimeError("浏览器会话已经断开")
            page = await self.session.recover_page(target_url)
            if not page.url.startswith(MOCK_SITE_ORIGIN):
                page = await self.session.navigate(MOCK_SITE_ORIGIN, page=page)
            evidence = await self.mock_adapter.inspect(page)
            await self._record_checkpoint(task_id, checkpoint, page, evidence.as_dict())
            if evidence.authenticated_signal and not evidence.intervention_reason:
                self.resume_access_check = False
                self.store.event(task_id, "login_access_verified", "已重新核对模拟站求职者登录与验证状态")
                self.store.update_task(task_id, status=BrowserTaskStatus.RUNNING, takeover_reason=None,
                    detail="模拟招聘网站登录与验证状态已核对", checkpoint=checkpoint + "_verified")
                return page
            await self.mock_adapter.open_login_prompt(page)
            evidence = await self.mock_adapter.inspect(page)
            reason = evidence.intervention_reason or "需要使用求职者账号登录并完成 MFA"
            self.gate.clear()
            self.store.event(task_id, "user_takeover_required", reason)
            self.store.update_task(task_id, status=BrowserTaskStatus.WAITING_FOR_USER,
                detail="登录/验证状态尚未通过，请人工完成后再继续", takeover_reason=reason,
                checkpoint="mock_waiting_for_login", current_url=page.url)
            await self._wait_for_user_or_disconnect()

    async def _wait_for_user_or_disconnect(self) -> None:
        if not self.session or self.session.disconnected.is_set():
            raise RuntimeError("Chrome 已关闭或 CDP 连接已断开；请重新启动 Agent")
        gate_wait = asyncio.create_task(self.gate.wait())
        disconnect_wait = asyncio.create_task(self.session.disconnected.wait())
        done, pending = await asyncio.wait(
            {gate_wait, disconnect_wait},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if disconnect_wait in done and disconnect_wait.result():
            raise RuntimeError("Chrome 已关闭或 CDP 连接已断开；请重新启动 Agent")

    async def _wait_for_page_readiness(self, page: Page, target_url: str, timeout_seconds: int = 20) -> Page:
        """Wait for BOSS while recovering a tab replaced during SPA navigation.

        Polls at 500ms granularity so a fast render is detected promptly;
        exits as soon as intervention/cards/stable state is visible. A page
        closed mid-wait (BOSS replacing its tab) is re-bound and retried.
        """
        attempts = max(1, timeout_seconds * 2)
        for _ in range(attempts):
            try:
                if page.is_closed():
                    page = await self.session.recover_page(target_url)
                evidence = await self.adapter.inspect(page)
                if evidence.intervention_reason or evidence.card_count > 0 or not evidence.loading_visible:
                    return page
                await page.wait_for_timeout(500)
            except PlaywrightError as exc:
                if not self._is_target_closed(exc):
                    raise
                page = await self.session.recover_page(target_url)
        return page

    @staticmethod
    def _is_target_closed(exc: Exception) -> bool:
        message = str(exc).lower()
        return "target page, context or browser has been closed" in message or "targetclosederror" in message
    @staticmethod
    def _is_recoverable_search_shell(
        target_url: str,
        current_url: str,
        *,
        authenticated: bool,
        loading: bool,
    ) -> bool:
        """Recognize BOSS search SPA fallbacks at '/' or '/<city>/'.

        During a city switch BOSS can remove the account header, show only a
        skeleton, and temporarily return to e.g. '/shenzhen/'. That transition
        is recoverable as long as no login/CAPTCHA signal was detected.
        """
        target_path = urlparse(target_url).path.rstrip("/")
        current_path = urlparse(current_url).path.rstrip("/")
        root_or_city = current_path == "" or bool(re.fullmatch(r"/[a-z][a-z0-9-]*", current_path))
        return bool(
            target_path == "/web/geek/jobs"
            and root_or_city
            and (authenticated or loading)
        )

    async def _ensure_access(self, task_id: str, target_url: str, checkpoint: str, *, screenshot: bool = False, require_auth: bool = False) -> Page:
        restore_attempts = 0
        require_auth = require_auth or checkpoint == "login_preflight"
        while True:
            if not self.session:
                raise RuntimeError("浏览器会话已经断开")
            page = await self.session.recover_page(target_url)
            evidence = await self.adapter.inspect(page)
            await self._record_checkpoint(
                task_id, checkpoint, page, evidence.as_dict(),
                screenshot=screenshot or evidence.intervention_reason is not None,
            )
            reason = evidence.intervention_reason
            if not reason and require_auth and not evidence.authenticated_signal:
                reason = "页面尚无可核对的登录状态，请完成登录或等待页面加载后再继续"
            target_path = urlparse(target_url).path
            current_path = urlparse(page.url).path
            if not reason and target_path and target_path not in current_path:
                if self._is_recoverable_search_shell(
                    target_url, page.url,
                    authenticated=evidence.authenticated_signal,
                    loading=evidence.loading_visible,
                ):
                    self.store.event(
                        task_id,
                        "search_shell_detected",
                        "BOSS 搜索页停留在首页或城市路由的骨架屏；不再用深链接恢复",
                    )
                    return page
                if restore_attempts >= 1:
                    raise RuntimeError(f"BOSS 将目标页面重定向到 {page.url}；已停止自动恢复")
                restore_attempts += 1
                page = await self.session.navigate(target_url, page=page)
                try:
                    await page.wait_for_timeout(1_000)
                except PlaywrightError:
                    continue
                checkpoint = "target_page_restored"
                continue
            if not reason:
                if require_auth:
                    self.resume_access_check = False
                    self.store.event(task_id, "login_access_verified", "已重新核对页面登录与验证状态")
                    self.store.update_task(task_id, status=BrowserTaskStatus.RUNNING, takeover_reason=None,
                        detail="页面登录与验证状态已核对", checkpoint=checkpoint + "_verified")
                return page
            self.gate.clear()
            self.store.event(task_id, "user_takeover_required", reason)
            self.store.update_task(task_id, status=BrowserTaskStatus.WAITING_FOR_USER, detail="请在浏览器中完成登录/验证码/MFA；完成后回到 AutoHunt 点击继续", takeover_reason=reason, checkpoint="waiting_for_user")
            await self._wait_for_user_or_disconnect()
            page = await self.session.recover_page(target_url)
            try:
                await page.wait_for_timeout(1_000)
            except PlaywrightError:
                continue
            checkpoint = "user_takeover_resumed"

    async def _record_checkpoint(self, task_id: str, name: str, page: Page, evidence: dict | None = None, *, screenshot: bool = False) -> None:
        if evidence is None:
            evidence = (await self.adapter.inspect(page)).as_dict()
        screenshot_path: str | None = None
        if screenshot and self.session and self.session.mode == BrowserMode.CDP.value:
            # Chrome 151 currently terminates the entire remote-debugging
            # instance on Page.captureScreenshot (both Playwright's high-level
            # API and raw CDP). Preserve structured evidence and keep search
            # alive until capture can be moved to a Windows/GDI backend.
            self.store.event(
                task_id,
                "screenshot_skipped",
                "当前 CDP Chrome 执行截图会关闭浏览器；已保留 URL、页面状态和登录证据",
            )
            screenshot = False
        if screenshot:
            try:
                folder = self.evidence_dir / task_id
                folder.mkdir(parents=True, exist_ok=True)
                target = folder / f"{name}.png"
                await page.screenshot(path=str(target), full_page=False)
                screenshot_path = str(target.relative_to(self.data_root))
            except Exception:
                pass
        self.store.checkpoint(task_id, name, current_url=page.url, evidence=evidence, screenshot_path=screenshot_path)

    @staticmethod
    def _safe_boss_url(value: str) -> str | None:
        return BossAdapter.safe_job_url(value)

    def _candidate_match_context(self, search_queries: list[str]) -> tuple[list[str], list[str], list[str]]:
        """Merge explicit search intent, persisted preferences, and resume evidence."""
        memory = self.memory.read().lower()
        skills: list[str] = []
        target_roles = list(search_queries)
        excluded_keywords: list[str] = []
        try:
            state = self.candidate_state.read()
            skills.extend(state.profile.skills)
            target_roles.extend(state.preferences.target_roles.values)
            excluded_keywords.extend(state.preferences.excluded_keywords.values)
        except RuntimeError:
            # Searching remains available when encrypted candidate state is unavailable.
            pass
        skills.extend(skill for skill in KNOWN_SKILLS if skill in memory)

        def unique(values: list[str], limit: int) -> list[str]:
            seen: set[str] = set()
            output: list[str] = []
            for value in values:
                cleaned = str(value).strip()
                key = cleaned.casefold()
                if cleaned and key not in seen:
                    seen.add(key)
                    output.append(cleaned)
            return output[:limit]

        return unique(skills, 100), unique(target_roles, 50), unique(excluded_keywords, 50)

    def _candidate_skills(self) -> list[str]:
        return self._candidate_match_context([])[0]
    @staticmethod
    def _detail_from_card(card: dict) -> dict:
        """Build a storable job detail from card data alone (no detail-page visit)."""
        tags = list(card.get("tags") or [])
        description = (
            f"BOSS 职位：{card.get('title') or ''}；公司：{card.get('company') or ''}；"
            f"地点：{card.get('location') or ''}；薪资：{card.get('salary') or ''}。"
            f"初筛阶段未打开详情页，如需完整 JD 可在自动申请看板中人工复核。"
            f"{' '.join(tags)}"
        )
        return {
            "title": card.get("title") or "未命名职位",
            "company": card.get("company") or "未知公司",
            "location": card.get("location") or "未注明",
            "salary": card.get("salary") or "未注明",
            "description": description,
            "tags": tags,
        }

    def _filter_salary_cards(self, task_id: str, cards: list[dict], request: BrowserSearchRequest) -> list[dict]:
        return [card for card in cards if self._salary_allowed(task_id, card, request)]

    def _salary_allowed(self, task_id: str, detail: dict, request: BrowserSearchRequest) -> bool:
        from ..core.job_filters import partial_filter_status
        if partial_filter_status(request.filters, detail) != "fail":
            return True
        self.store.increment_funnel(task_id, filtered=1)
        self.store.event(task_id, "job_skipped", "薪资条件不符合（按完整条件树判断）", reason_code="salary_condition_failed",
                         sample={"title": detail.get("title", ""), "salary": detail.get("salary", ""), "salary_min": request.salary_min})
        return False

    async def _match_and_filter(self, task_id, detail, match_request, request):
        # A failed/cancelled sibling must not leave inference workers detached.
        async with asyncio.TaskGroup() as group:
            matching = group.create_task(match_job(self.llm.generate, match_request))
            group.create_task(self._check_filters(task_id, detail, request))
        return matching.result()

    async def _check_filters(self, task_id: str, detail: dict, request: BrowserSearchRequest):
        from ..core.result_groups import grouped_filter, group_verdicts
        detail["title"] = clean_title(detail.get("title") or "")
        detail["company"] = clean_title(detail.get("company") or "")
        detail["description"] = re.sub(r"\s+", " ", str(detail.get("description") or "")).strip()
        # Preserve scraped facts before matching enriches the display tags.
        detail.setdefault("source_tags", list(detail.get("tags") or []))
        if request.locations and not any(city in str(detail.get("location", "")) for city in request.locations):
            verdict = {"status": "unknown" if detail.get("location") in {None, "", "未注明", "未知"} else "fail", "conditions": [], "reason": "工作地点无法满足本轮城市条件"}
        else:
            verdict = await evaluate_filter(self.llm.generate, grouped_filter(request.filters, request.result_groups), detail)
        if request.result_groups:
            detail["result_group_evidence"] = group_verdicts(request.filters, request.result_groups, verdict)
        detail["filter_status"], detail["filter_evidence"] = verdict["status"], verdict
        if request.filters or request.result_groups or verdict["status"] != "pass":
            self.store.event(task_id, "filter_evaluated", f"{detail.get('title', '')}：{verdict['status']}",
                reason_code="semantic_filter_" + verdict["status"], sample=verdict)

    def _persist_analysis(self, task_id: str, detail: dict, analysis: JobAnalysis, url: str, *, source: str = "BOSS直聘", request: BrowserSearchRequest | None = None) -> tuple[int, int, int]:
        """Upsert one analyzed job; return (stored, duplicates, qualified)."""
        if request and request.source == "zhaopin":
            source = "智联招聘"
        if detail.get("filter_status") == "fail":
            existing = self.store.job_id_for_source_url(url)
            if existing is not None:
                self.store.update_job_filter_result(existing, detail.get("filter_evidence", {"status": "fail"}))
            self.store.increment_funnel(task_id, filtered=1)
            self.store.mark_search_processed(task_id, url)
            return (0, 0, 0)
        if request and (request.filters or request.result_groups) and "filter_status" not in detail:
            # An unread card is never proof of a semantic hard condition.
            detail["filter_status"] = "unknown"
        if detail.get("filter_status") == "unknown":
            analysis = analysis.model_copy(update={"decision": "REVIEW", "reasoning": "筛选条件待核实，禁止自动投递。" + analysis.reasoning})
        if request is not None:
            if request.result_limit and self.store.get_task(task_id).matched >= request.result_limit:
                self.store.increment_funnel(task_id, limited=1)
                return (0, 0, 0)
            if not self._salary_allowed(task_id, detail, request):
                self.store.mark_search_processed(task_id, url)
                return (0, 0, 0)
            if request.result_limit and analysis.decision not in {"APPLY", "REVIEW"}:
                self.store.increment_funnel(task_id, filtered=1)
                self.store.event(task_id, "job_skipped", analysis.reasoning, reason_code="match_skip")
                self.store.mark_search_processed(task_id, url)
                return (0, 0, 0)
        detail.update({
            "title": clean_title(detail.get("title") or ""),
            "company": clean_title(detail.get("company") or ""),
            "source": source,
            "source_url": url,
            "score": analysis.score,
            "decision": analysis.decision,
            "stage": "rejected" if analysis.decision == "SKIP" else "review",
            "reasoning": analysis.reasoning,
            "qualification_evidence":analysis.qualification or {"status":"unknown","assessed":False,
                "requirements":[],"resume_digest":match_facts(getattr(self,"resume_workspace",None),getattr(self,"candidate_state",None)).get("digest")},
            "source_tags": detail.get("source_tags", list(detail.get("tags") or [])),
            "tags": sorted(set(detail["tags"] + analysis.matched_skills + analysis.missing_skills))[:10],
        })
        if request is not None:
            admitted = self.store.admit_search_result(task_id, detail, limit=request.result_limit, groups=request.result_groups)
            self.store.mark_search_processed(task_id, url)
            if admitted["status"] in {"quota_full", "target_full"}:
                self.store.increment_funnel(task_id, limited=1)
            return admitted["counts"]
        job_id, inserted = self.store.upsert_job(detail)
        if request is not None and request.result_limit and self.store.job_touched_by_task(task_id, job_id):
            return (0, 0, 0)
        receipt_description = re.sub(r'\s+', ' ', detail.get('description', '')).strip()
        receipt = self.store._analysis_receipt({**detail, 'id': job_id, 'description': receipt_description,
            'external_id': str(detail['external_id']) if detail.get('external_id') is not None else None,
            'content_hash': hashlib.sha256(receipt_description.encode()).hexdigest()})
        self.store.event(
            task_id,
            "job_stored" if inserted else "job_duplicate",
            f"{detail['company']} / {detail['title']}",
            reason_code="stored" if inserted else "duplicate",
            sample={"job_id": job_id, "title": detail["title"], "company": detail["company"],
                "filter_status": detail.get("filter_status", "pass"),
                "filter_evidence": detail.get("filter_evidence", {}),
                **receipt,
                "filter_fingerprint": self.store.review_fingerprint(self.store.get_job(job_id))},
        )
        return (1 if inserted else 0, 0 if inserted else 1, 1 if analysis.decision in {"APPLY", "REVIEW"} and detail.get("filter_status", "pass") == "pass" else 0)
