"""Condition allowance/alternative/refresh contracts; synthetic data only."""
from __future__ import annotations

import asyncio
import gc
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS

from app.core.agent_tools import AgentToolRegistry, MissingDeliveryEvidence
from app.core.job_filters import evaluate_filter, filter_digest, validate_filter
from app.core.models import BrowserTaskStatus
from app.jobs.store import JobStore
from app.llm.base import LLMResult
from app.runtime.context import RunContext, current_run
from app.workflow.execution import begin
from app.workflow.mock_coordinator import MockWorkflowCoordinator
from app.workflow.planning import RequestDraft, validate_draft

TRACES = []


def salary(unit, amount=None):
    return {"op": "condition", "text": "月薪至少15k" if unit == "monthly" else "薪资面议" if unit == "negotiable" else "实习日薪至少300元",
            "salary": {"unit": unit, **({"minimum": amount} if amount is not None else {})}}


def alternatives():
    return {"op": "any", "children": [
        {"op": "all", "children": [{"op": "condition", "text": "正式岗位"},
            {"op": "any", "children": [salary("monthly", 15000), salary("negotiable")]}]},
        {"op": "all", "children": [{"op": "condition", "text": "实习岗位"}, salary("daily", 300)]}]}


async def category_model(request):
    body = json.loads(request.user_message)
    description = body["facts"]["description"]
    known = "招聘类型：" in description
    internship = "招聘类型：实习" in description
    outputs = []
    for item in body["conditions"]:
        is_internship = item["text"] == "实习岗位"
        outputs.append({"index": item["index"], "status": "unknown" if not known else "pass" if internship == is_internship else "fail",
            "reason": "按当前岗位类型判断", "evidence": [{"field": "description", "quote": description}] if known else []})
    return LLMResult(json.dumps({"conditions": outputs}, ensure_ascii=False), "scripted", "condition-contract")


class Conditions(unittest.IsolatedAsyncioTestCase):
    async def test_allowance_never_requires_jd_acceptance(self):
        async def forbidden(_):
            raise AssertionError("allowance must not request JD acceptance evidence")
        for text in ("实习也可以", "接受校招", "小公司也可", "可接受跨行业", "允许远程", "短期合同也行"):
            expression = {"op": "all", "children": [salary("monthly", 15000), {"op": "condition", "text": text, "mode": "allow"}]}
            result = await evaluate_filter(forbidden, expression, {"salary": "28-33K"})
            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["conditions"][1]["reason"], "用户允许包含的范围，不是岗位必须满足的事实")
            TRACES.append({"kind": "allowance", "text": text, "result": result})

    async def test_alternative_branch_salary_and_employment_grid(self):
        cases = [("社招", "28-33K", "pass"), ("应届/校招", "15-20K", "pass"),
                 ("社招", "12-20K", "fail"), ("社招", "面议", "pass"),
                 ("实习", "300-500元/天", "pass"), ("实习", "200-500元/天", "fail"),
                 ("实习", "面议", "unknown"), ("实习", "15-20K", "fail"),
                 ("", "未披露", "unknown")]
        for industry in ("金融", "软件", "医疗", "外贸", "制造"):
            for employment, value, expected in cases:
                description = f"{industry}；招聘类型：{employment}" if employment else industry
                result = await evaluate_filter(category_model, alternatives(), {"salary": value, "description": description})
                self.assertEqual(result["status"], expected, (industry, employment, value))
                TRACES.append({"kind": "alternative", "industry": industry, "employment": employment, "salary": value, "expected": expected, "actual": result["status"]})

    async def test_intern_scope_cannot_bypass_daily_bound_with_monthly_salary(self):
        expression = alternatives()
        expression["children"][1]["children"][1]["salary"]["applies_to"] = "internship"
        for value in ("5-8K", "15-20K", "面议"):
            result = await evaluate_filter(category_model, expression, {"salary": value, "description": "招聘类型：实习"})
            self.assertEqual(result["status"], "unknown")


class Refresh(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.jobs = JobStore(Path(self.temp.name) / "jobs.db")
        self.profile = NS(resume_id="synthetic-resume")
        self.candidate = NS(read=lambda: NS(profile=self.profile))
        self.document = NS(id="synthetic-document", version=1)
        self.document.model_dump = lambda **_: {"id": self.document.id, "version": self.document.version}
        self.resume = NS(read_document=lambda: self.document)
        self.old = {"op": "all", "children": [salary("monthly", 15000), {"op": "condition", "text": "接受实习岗位"}]}
        self.new = {"op": "all", "children": [salary("monthly", 15000), {"op": "condition", "text": "实习也可", "mode": "allow"}]}
        self.task_id = "search-1"
        self.jobs.create_task(self.task_id, "mock", "分析师", resume_id=self.profile.resume_id)
        self.jobs.save_search_request(self.task_id, {"conversation_id": "default", "filters": self.old})
        self.jobs.update_task(self.task_id, status=BrowserTaskStatus.COMPLETED)
        self.ids = []
        for i in range(5):
            job_id, _ = self.jobs.upsert_job({"source": "MockJobSite", "external_id": str(i), "source_url": f"http://127.0.0.1:8080/job.html?id={i}",
                "title": "合成分析师", "company": f"合成公司{i}", "location": "深圳", "salary": "28-33K",
                "description": "招聘类型：社招。" + "合成JD职责和交付说明。" * 8,
                "score": 88, "decision": "REVIEW", "reasoning": "合成说明", "filter_status": "unknown",
                "filter_evidence": {"status": "unknown", "conditions": [], "digest": filter_digest(validate_filter(self.old))}})
            self.ids.append(job_id)
            job = self.jobs.get_job(job_id)
            self.jobs.event(self.task_id, "job_stored", "合成结果", sample={"job_id": job_id, "filter_status": "unknown",
                "filter_evidence": json.loads(job["filter_evidence"]), "filter_fingerprint": self.jobs.review_fingerprint(job)})
        async def no_model(_):
            raise AssertionError("numeric salary and allowance need no model")
        self.coordinator = object.__new__(MockWorkflowCoordinator)
        self.coordinator.jobs = self.jobs
        self.coordinator.candidate = self.candidate
        self.coordinator.resume = self.resume
        self.coordinator.authorization_scope = None
        self.coordinator.stopping_sessions = set()
        self.coordinator.stop_epochs = {}
        self.coordinator.wakeup = asyncio.Event()
        self.coordinator.delivery = NS(supports=lambda url: url.startswith("http://127.0.0.1:8080/"))
        async def bound(_): return "bound"
        self.coordinator._ensure_applicant_bound = bound
        self.registry = AgentToolRegistry(jobs=self.jobs, browser=NS(llm=NS(generate=no_model)), candidate_state=self.candidate,
            resume_workspace=self.resume, memory=None, learning_plans=NS(), mock_workflow=self.coordinator)
        self.events = []
        self.context = RunContext("synthetic-run", asyncio.current_task(), lambda kind,data: self.events.append({"kind":kind,**data}), lambda _: None,
            search_state={"queries": ["分析师"], "locations": [], "filters": self.new},
            dialogue={"tasks": {"browser_id": self.task_id}})
        from app.runtime.store import RunStore
        from app.runtime.manager import AgentRuntime
        self.runtime_store = RunStore(Path(self.temp.name) / "runtime.db")
        self.runtime_run, _ = self.runtime_store.create("default", "synthetic", "chat", {"message": "合成条件修订"})
        self.runtime = AgentRuntime(self.runtime_store, NS(tool_registry=self.registry, candidate_state=self.candidate), NS(), NS(), None, self.resume)
        original_emit = self.context.emit
        self.context.run_id = self.runtime_run["id"]
        self.context.emit = lambda kind,data: (original_emit(kind,data), self.runtime.emit(self.runtime_run["id"], kind,data))
        self.context.authorize_action = lambda *args: self.runtime.authorize_action(self.runtime_run["id"], *args)
        self.token = current_run.set(self.context)

    async def asyncTearDown(self):
        current_run.reset(self.token)
        gc.collect()
        self.temp.cleanup()

    def owned(self): self.context.owner = asyncio.current_task()

    async def test_five_high_scores_refresh_and_queue_in_isolated_store(self):
        self.owned()
        self.assertEqual(self.jobs.jobs_for_resume_analysis(self.profile.resume_id, task_id=self.task_id, min_score=85), [])
        result = await self.registry._job_results_refilter({})
        self.assertEqual((result["checked"], result["pass"]), (5, 5))
        evidence = self.jobs.jobs_for_resume_analysis(self.profile.resume_id, task_id=self.task_id, min_score=85)
        self.assertEqual({item["id"] for item in evidence}, set(self.ids))
        self.assertEqual(self.jobs.search_report(self.task_id)["qualified_jobs"], 5)
        queued = await self.coordinator.queue_deliveries(min_score=85, session_id="default", task_id=self.task_id)
        self.assertEqual(queued["queued"], 5)
        TRACES.append({"kind": "refresh_queue", "result": result, "queued_ids": queued["job_ids"], "events": self.events})

    async def test_unknown_evidence_reports_insufficient_not_completed_zero(self):
        self.owned()
        queued = await self.coordinator.queue_deliveries(min_score=85, session_id="default", task_id=self.task_id)
        self.assertEqual((queued["queued"], queued["reason"]), (0, "insufficient_evidence"))
        self.assertGreaterEqual(queued["insufficient"], 5)
        with self.assertRaises(MissingDeliveryEvidence):
            await self.registry._mock_delivery_start({"min_score": 85, "source_task_id": self.task_id})

    async def test_source_task_id_binds_only_that_search(self):
        self.owned()
        await self.registry._job_results_refilter({})
        second = "search-2"
        self.jobs.create_task(second, "mock", "分析师", resume_id=self.profile.resume_id)
        self.jobs.save_search_request(second, {"conversation_id": "default"})
        self.jobs.update_task(second, status=BrowserTaskStatus.COMPLETED)
        job_id, _ = self.jobs.upsert_job({"source": "MockJobSite", "external_id": "second",
            "source_url": "http://127.0.0.1:8080/job.html?id=second", "title": "合成分析师",
            "company": "合成二号公司", "location": "深圳", "salary": "30-35K",
            "description": "招聘类型：社招。" + "合成JD职责和交付说明。" * 8,
            "score": 92, "decision": "REVIEW", "reasoning": "合成说明", "filter_status": "pass"})
        job = self.jobs.get_job(job_id)
        self.jobs.event(second, "job_stored", "合成结果", sample={"job_id": job_id, "filter_status": "pass",
            "filter_fingerprint": self.jobs.review_fingerprint(job)})
        result = await self.registry._mock_delivery_start({"min_score": 85, "source_task_id": self.task_id})
        self.assertEqual(result["queued"], 5)
        self.assertNotIn(job_id, result["job_ids"])

    async def test_refresh_wrong_session_and_resume_rejected(self):
        self.owned()
        for resume_id, session_id in (("other-resume", "default"), (self.profile.resume_id, "other-session")):
            with self.assertRaises(ValueError):
                self.jobs.refilter_candidates(self.task_id, resume_id=resume_id, session_id=session_id)

    async def test_changed_job_aborts_atomic_refresh(self):
        rows = self.jobs.refilter_candidates(self.task_id, resume_id=self.profile.resume_id, session_id="default")
        verdict = {"status": "pass", "conditions": [], "digest": filter_digest(validate_filter(self.new))}
        self.jobs.update_job_filter_result(self.ids[-1], {"status": "fail"})
        with self.assertRaises(ValueError): self.jobs.save_refilter_results(self.task_id, rows, [verdict] * 5, {"filters": self.new})
        self.assertEqual(self.jobs.get_job(self.ids[0])["filter_status"], "unknown")

    async def test_stale_fingerprint_does_not_become_submission_evidence(self):
        self.owned()
        await self.registry._job_results_refilter({})
        self.jobs.update_job_filter_result(self.ids[0], {"status": "unknown", "conditions": []})
        ids = {job["id"] for job in self.jobs.jobs_for_resume_analysis(self.profile.resume_id, task_id=self.task_id, min_score=85)}
        self.assertEqual(ids, set(self.ids[1:]))

    async def test_partial_refresh_never_accepts_unrefreshed_old_pass(self):
        self.owned()
        await self.registry._job_results_refilter({})
        rows = self.jobs.refilter_candidates(self.task_id, resume_id=self.profile.resume_id, session_id="default")
        changed = salary("monthly", 30000)
        verdict = {"status": "fail", "conditions": [], "digest": filter_digest(validate_filter(changed))}
        self.jobs.save_refilter_results(self.task_id, rows[:1], [verdict], {"filters": changed})
        self.assertEqual(self.jobs.jobs_for_resume_analysis(self.profile.resume_id, task_id=self.task_id, min_score=85), [])
        report = self.jobs.search_report(self.task_id)
        self.assertEqual(report["qualified_jobs"], 0)
        self.assertEqual(report["pending_verification"], 4)

    async def test_planner_inserts_refresh_between_condition_write_and_delivery(self):
        self.owned()
        draft = RequestDraft.model_validate({"goals": [{"quote": "更新条件并投递", "disposition": "steps", "step_ids": ["conditions", "delivery"]}],
            "steps": [{"id": "conditions", "tool": "search_state_update", "arguments": {"filters": self.new}},
                      {"id": "delivery", "tool": "mock_delivery_start", "arguments": {"min_score": 85}, "user_requested": True}]})
        plan = validate_draft(draft, self.registry, "更新条件并投递", dialogue=self.context.dialogue,
            conditions=self.context.search_state, stamp="synthetic-resume:synthetic-document:1")
        delivery = next(s for s in plan["steps"] if s["id"] == "delivery")
        refresh = next(s for s in plan["steps"] if s["tool"] == "job_results_refilter")
        self.assertEqual(refresh["depends_on"], ["conditions"])
        self.assertEqual(delivery["evidence_step"], refresh["id"])
        response = await begin(plan, self.registry)
        self.assertEqual(plan["status"], "finished", response.message)
        self.assertEqual(delivery["status"], "completed", response.message)
        self.assertEqual(delivery["result"]["queued"], 5)


def main():
    suite = unittest.defaultTestLoader.loadTestsFromModule(__import__(__name__, fromlist=["*"]))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    root = Path("test-results/condition-refresh") / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    root.mkdir(parents=True)
    summary = {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
        "trace_cases": len(TRACES), "external_model_calls": 0, "browser_runs": 0, "real_submissions": 0}
    (root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (root / "traces.jsonl").write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in TRACES), encoding="utf-8")
    (root / "failures.json").write_text(json.dumps([{"test": str(test), "detail": detail} for test,detail in result.failures+result.errors], ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(root), **summary}))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__": raise SystemExit(main())
