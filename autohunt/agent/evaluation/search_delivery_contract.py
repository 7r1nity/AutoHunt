"""Isolated result-budget, quota, selection and one-click delivery contracts.

Temporary SQLite and scripted models only. No live recruitment/browser operations.
"""
from __future__ import annotations

import asyncio
import gc
import io
import json
import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from app.browser.automation import BrowserAutomationService
from app.core.agent_tools import AgentToolRegistry
from app.core.models import BrowserSearchRequest, JobAnalysis
from app.core.result_groups import group_verdicts, grouped_filter, normalize_groups
from app.core.job_filters import evaluate_filter
from app.jobs.store import JobStore
from app.llm.base import LLMResult
from app.runtime.context import RunContext, current_run
from app.runtime.dialogue import apply_operation
from app.runtime.search_state import SearchState, merge_search_state
from app.workflow.delivery_rules import build_rules
from app.workflow.rules import Observation, RuleEngine


GROUPS = [{"label": "深圳", "count": 5, "filters": {"op": "condition", "text": "工作地点在深圳"}},
          {"label": "上海", "count": 5, "filters": {"op": "condition", "text": "工作地点在上海"}}]


def job(index, *, status="pass", groups=None):
    return {"title": f"合成岗位{index}", "company": "合成雇主", "location": "深圳", "salary": "15-20K",
            "description": "合成职位用于隔离测试，招聘类型：正式；独立完成数据分析任务。", "tags": [],
            "external_id": str(index), "source_url": f"http://127.0.0.1:8080/job.html?id={index}",
            "source": "MockJobSite", "score": 88, "decision": "REVIEW", "reasoning": "合成匹配",
            "stage": "review", "filter_status": status, "filter_evidence": {"status": status},
            "result_group_evidence": groups or []}


class Admission(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = JobStore(Path(self.temp.name) / "jobs.db")
        self.store.create_task("search", "mock", "数据分析师", resume_id="synthetic-resume")

    def tearDown(self):
        gc.collect()  # SQLite context exits commit; collect connections before Windows cleanup.

    def admit(self, value, limit=10, groups=None):
        return self.store.admit_search_result("search", value, limit=limit, groups=groups)

    def test_unknowns_do_not_expand_board_or_budget(self):
        for i in range(35):
            self.assertEqual(self.admit(job(i + 1, status="unknown"))["counts"], (0, 0, 0))
        for i in range(3):
            self.admit(job(100 + i))
        report = self.store.search_report("search")
        self.assertEqual(len(self.store.list_jobs()), 3)
        self.assertEqual(report["qualified_jobs"], 3)
        self.assertEqual(report["pending_verification"], 35)
        self.assertEqual(len(report["jobs"]), 3)

    def test_plain_limits_never_overflow(self):
        for limit in (5, 10, 15):
            ident = f"run-{limit}"
            self.store.create_task(ident, "mock", "数据分析师")
            total = 0
            for index in range(35):
                result = self.store.admit_search_result(ident, job(1000 * limit + index), limit=limit, groups=None)
                total += result["counts"][0]
            self.assertEqual(total, limit)
            self.assertEqual(len(self.store.search_report(ident)["jobs"]), limit)

    def test_parallel_admissions_share_one_atomic_budget(self):
        with ThreadPoolExecutor(max_workers=8) as workers:
            results = list(workers.map(lambda i: self.admit(job(2000+i), limit=10), range(40)))
        self.assertEqual(sum(row["counts"][2] for row in results), 10)
        self.assertEqual(len(self.store.list_jobs()), 10)

    def test_skip_does_not_fill_unbounded_search(self):
        value = {**job(1), "decision": "SKIP", "stage": "rejected"}
        self.assertEqual(self.admit(value, limit=None)["counts"], (0, 0, 0))
        self.assertEqual(self.store.list_jobs(), [])

    def test_partial_refilter_cannot_keep_old_unchecked_slots(self):
        from app.core.job_filters import filter_digest
        for i in range(40):
            self.admit(job(2000+i), limit=40)
        candidates = [self.store.get_job(view.id) for view in self.store.list_jobs()[:30]]
        verdict = {"status": "pass", "conditions": [], "digest": filter_digest(None)}
        self.store.save_refilter_results("search", candidates, [verdict]*30, {"result_limit": 10, "filters": None})
        self.assertEqual(len(self.store.list_jobs()), 10)
        self.assertEqual(self.store.search_report("search")["qualified_jobs"], 10)
        self.assertEqual(self.store.search_report("search")["pending_verification"], 10)

    def test_private_direct_preview_creates_bound_run_once(self):
        ident, _ = self.store.upsert_job(job(1, status="unknown"))
        preview = self.store.create_delivery_preview(ident, allow_unverified=True, delivery_flow="direct",
            resume_document_id="synthetic", resume_version=1, resume_digest="synthetic-digest")
        args = dict(run_id="synthetic-run", resume_document_id="synthetic", resume_version=1,
                    resume_digest="synthetic-digest")
        _, _, _, run = self.store.authorize_delivery_run(preview.id, **args)
        self.assertIn("flow=direct", run.target_url)
        self.assertEqual(run.job_id, ident)
        with self.assertRaises(ValueError):
            self.store.authorize_delivery_run(preview.id, **{**args, "run_id": "second"})

    def test_gated_queue_removes_direct_source_mode_and_preserves_stages(self):
        value = job(1)
        value["source_url"] += "&flow=direct"
        ident, _ = self.store.upsert_job(value)
        preview = self.store.create_delivery_preview(ident, delivery_flow="gated",
            resume_document_id="synthetic", resume_version=1, resume_digest="synthetic-digest")
        _, _, _, run = self.store.authorize_delivery_run(preview.id, run_id="synthetic-run",
            resume_document_id="synthetic", resume_version=1, resume_digest="synthetic-digest")
        self.assertNotIn("flow=direct", run.target_url)
        self.assertEqual(self.store.get_job(ident)["source_url"], value["source_url"])
        self.assertEqual(self.store.get_job(ident)["stage"], "review")
        self.store.mark_delivery_waiting_reply(ident, "合成沟通确认")
        self.assertEqual(self.store.get_job(ident)["stage"], "applying")
        self.store.mark_recruiter_reply(ident, "合成HR回复")
        self.assertEqual(self.store.get_job(ident)["stage"], "applying")
        self.store.mark_delivery_completed(ident, "合成投递记录确认")
        self.assertEqual(self.store.get_job(ident)["stage"], "waiting")

    def test_private_direct_preview_rejects_changed_resume(self):
        ident, _ = self.store.upsert_job(job(1))
        preview = self.store.create_delivery_preview(ident, delivery_flow="direct",
            resume_document_id="synthetic", resume_version=1, resume_digest="synthetic-digest")
        with self.assertRaises(ValueError):
            self.store.authorize_delivery_run(preview.id, run_id="synthetic-run", resume_document_id="synthetic",
                resume_version=2, resume_digest="changed-digest")

    def test_shenzhen_and_shanghai_are_independent_quotas(self):
        for i in range(20):
            self.admit(job(i + 1, groups=[{"index": 0, "status": "pass"}, {"index": 1, "status": "fail"}]), groups=GROUPS)
        self.assertEqual(len(self.store.list_jobs()), 5)
        for i in range(20):
            self.admit(job(100 + i, groups=[{"index": 0, "status": "fail"}, {"index": 1, "status": "pass"}]), groups=GROUPS)
        self.assertEqual(len(self.store.list_jobs()), 10)

    def test_unknown_group_never_fills_quota(self):
        result = self.admit(job(1, groups=[{"index": 0, "status": "unknown"}]), groups=GROUPS)
        self.assertEqual(result["counts"], (0, 0, 0))

    def test_overlapping_groups_allocate_each_job_once(self):
        for index in range(15):
            self.admit(job(index + 1, groups=[{"index": 0, "status": "pass"}, {"index": 1, "status": "pass"}]), groups=GROUPS)
        self.assertEqual(len(self.store.search_report("search")["jobs"]), 10)

    def test_receipt_replay_uses_no_new_slot(self):
        first = self.admit(job(1))
        self.assertEqual(self.admit(job(1))["counts"], (0, 0, 0))
        self.assertEqual(first["counts"], (1, 0, 1))

    def test_pending_candidate_can_promote_without_duplicate(self):
        pending = self.admit(job(1, status="unknown"))
        promoted = self.admit(job(1))
        self.assertEqual(pending["job_id"], promoted["job_id"])
        self.assertEqual(promoted["counts"], (1, 0, 1))

    def test_existing_board_duplicate_counts_toward_target(self):
        self.store.upsert_job(job(1))
        self.assertEqual(self.admit(job(1), limit=1)["counts"], (0, 1, 1))
        self.assertEqual(self.admit(job(2), limit=1)["counts"], (0, 0, 0))

    def test_known_failure_cannot_be_waived(self):
        ident, _ = self.store.upsert_job(job(1, status="fail"))
        with self.assertRaises(ValueError):
            self.store.create_delivery_preview(ident, allow_unverified=True, delivery_flow="direct")

    def test_unknown_requires_explicit_single_job_scope(self):
        ident, _ = self.store.upsert_job(job(1, status="unknown"))
        with self.assertRaises(ValueError):
            self.store.create_delivery_preview(ident)
        preview = self.store.create_delivery_preview(ident, allow_unverified=True, delivery_flow="direct",
            resume_document_id="synthetic", resume_version=1, resume_digest="synthetic-digest")
        with self.store._connect() as db:
            payload = json.loads(db.execute("SELECT payload_json FROM delivery_previews WHERE id=?", (preview.id,)).fetchone()[0])
        self.assertIn("flow=direct", payload["target_url"])
        token_id, token, _, _ = self.store.authorize_delivery(preview.id,
            resume_document_id="synthetic", resume_version=1, resume_digest="synthetic-digest")
        self.assertEqual(self.store.consume_delivery_authorization(token_id, token), ident)
        with self.assertRaises(ValueError):
            self.store.consume_delivery_authorization(token_id, token)
        self.assertEqual(self.store.get_job(ident)["filter_status"], "unknown")

    def test_repeated_click_inserts_one_queue_item(self):
        ident, _ = self.store.upsert_job(job(1))
        kwargs = {"session_id": "default", "resume_stamp": "synthetic", "binding": {}, "min_score": 0,
                  "scope_evidence": {ident: {"delivery_flow": "direct"}}}
        self.assertEqual(self.store.enqueue_mock_deliveries([self.store.get_job(ident)], **kwargs), [ident])
        self.assertEqual(self.store.enqueue_mock_deliveries([self.store.get_job(ident)], **kwargs), [])
        self.assertEqual(self.store.mock_delivery_queue()[0]["scope_evidence"]["delivery_flow"], "direct")


class GroupState(unittest.TestCase):
    def test_binding_bootstrap_keeps_saved_quotas(self):
        from app.workflow.mock_coordinator import MockWorkflowCoordinator
        saved = SearchState(queries=["数据分析师"], result_groups=GROUPS).model_dump()
        request = MockWorkflowCoordinator._saved_search_request(NS(search_state_reader=lambda _: saved), "default")
        self.assertEqual(request.result_groups, normalize_groups(GROUPS))
        self.assertEqual(request.result_limit, 10)

    def test_history_reset_includes_candidate_pool(self):
        from app.runtime.history_reset import JOB_TABLES
        self.assertIn("search_candidates", JOB_TABLES)
    def test_group_total_and_incremental_state(self):
        state = SearchState(queries=["数据分析师"], result_limit=99, result_groups=GROUPS).model_dump()
        self.assertEqual(state["result_limit"], 10)
        same = merge_search_state(state, {"locations": ["深圳"]})
        self.assertEqual(same["result_groups"], normalize_groups(GROUPS))
        changed = merge_search_state(same, {"result_limit": 15})
        self.assertIsNone(changed["result_groups"])
        self.assertEqual(changed["result_limit"], 15)

    def test_request_derives_total(self):
        self.assertEqual(BrowserSearchRequest(query="数据分析师", result_groups=GROUPS).result_limit, 10)

    def test_allowance_is_not_a_quota_classifier(self):
        with self.assertRaises(ValueError):
            normalize_groups([{"label": "上海", "count": 5, "filters": {"op": "condition", "text": "工作地点在上海", "mode": "allow"}}])


class SelectionAndDispatch(unittest.IsolatedAsyncioTestCase):
    async def test_model_missing_selector_cannot_fall_back_to_batch(self):
        items = [{"id": 100 + i, "title": f"合成岗位{i}"} for i in range(40)]
        dialogue = apply_operation({}, {"op": "snapshot", "kind": "job", "topic": "applications",
            "items": items, "displayed": True, "source": "synthetic", "version": "synthetic-search"})
        workflow = NS(remember_authorization_scope=lambda *a, **kw: None, queue_deliveries=AsyncMock(return_value={"queued": 1}))
        registry = AgentToolRegistry(jobs=NS(get_job=lambda ident: {"id": ident}, review_fingerprint=JobStore.review_fingerprint), browser=None, candidate_state=None,
            resume_workspace=None, memory=None, mock_workflow=workflow)
        ctx = RunContext("synthetic", asyncio.current_task(), lambda *a: None, lambda *a: None,
                         dialogue=dialogue, user_message="自动投递第33个")
        token = current_run.set(ctx)
        try:
            await registry._mock_delivery_start({})
        finally:
            current_run.reset(token)
        self.assertEqual(workflow.queue_deliveries.call_args.kwargs["job_ids"], [132])

    async def test_group_semantics_share_one_evidence_call(self):
        calls = []
        async def generate(request):
            calls.append(request)
            criteria = json.loads(request.user_message)["conditions"]
            return LLMResult(json.dumps({"conditions": [{"index": item["index"], "status": "pass" if item["text"] == "工作地点在深圳" else "fail",
                "reason": "合成岗位事实", "evidence": [{"field": "description", "quote": "工作地点在深圳"}]} for item in criteria]}), "scripted", "synthetic")
        verdict = await evaluate_filter(generate, grouped_filter(None, GROUPS), {"description": "本次工作地点在深圳"})
        self.assertEqual(len(calls), 1)
        self.assertEqual([item["status"] for item in group_verdicts(None, GROUPS, verdict)], ["pass", "fail"])

    async def test_dispatch_not_coupled_to_hr_poll_cooldown(self):
        calls = []
        coordinator = NS(jobs=NS(mark_job_needs_review=lambda *a: None),
            check_reply_queue=AsyncMock(return_value={}), recover_failed_starts=AsyncMock(return_value={}),
            process_delivery_queue=AsyncMock(side_effect=lambda: calls.append("dispatch")),
            sync_once=AsyncMock(return_value={}), run_daily_if_due=AsyncMock(return_value={}))
        engine = RuleEngine(build_rules(coordinator), clock=lambda: 0)
        facts = Observation(facts={"queue_depth": 5, "delivery_idle": True, "browser_idle": True,
                                   "reply_candidates": [{"id": "no-reply", "observed_reply": ""}]})
        for _ in range(6):
            await engine.step(facts)
        self.assertEqual(len(calls), 6)


if __name__ == "__main__":
    import sys
    buffer = io.StringIO()
    suite = unittest.defaultTestLoader.loadTestsFromModule(__import__(__name__, fromlist=["*"]))
    if "--regression" in sys.argv:
        for module in ("delivery_scheduling_contract", "condition_refresh_contract", "command_recovery_contract",
                       "approval_contract", "reporting_contract", "presentation_contract", "history_path_contract", "background_polling_contract"):
            suite.addTests(unittest.defaultTestLoader.loadTestsFromName(f"evaluation.{module}"))
    result = unittest.TextTestRunner(stream=buffer, verbosity=2).run(suite)
    output = Path(__file__).parent / "results" / "search-delivery" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True, exist_ok=True)
    (output / "tests.log").write_text(buffer.getvalue(), encoding="utf-8")
    (output / "summary.json").write_text(json.dumps({"run": result.testsRun, "failures": len(result.failures),
        "errors": len(result.errors), "passed": result.wasSuccessful()}, indent=2), encoding="utf-8")
    print(buffer.getvalue())
    print(f"Log: {output}")
    raise SystemExit(0 if result.wasSuccessful() else 1)
