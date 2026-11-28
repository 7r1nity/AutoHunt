"""Global policy migration across real isolated receipts, board and wait records."""
from __future__ import annotations

import ast
import copy
import gc
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from app.core.job_filters import FILTER_POLICY_VERSION, filter_digest, filter_facts_digest, validate_filter
from app.core.models import BrowserSearchRequest, JobAnalysisRequest, ResumeDocumentView
from app.jobs.store import JobStore
from app.memory.resume_workspace import resume_document_digest
from app.runtime.filter_policy_results import refresh_filter_policy_results
from app.runtime.store import RunStore


TRACES = []


def legacy_filter():
    return {"op": "all", "children": [
        {"op": "condition", "text": "地点深圳"},
        {"op": "not", "children": [{"op": "condition", "text": "外包岗位"}]},
        {"op": "condition", "text": "月薪至少12000元", "salary": {"unit": "monthly", "minimum": 12000}},
        {"op": "not", "children": [{"op": "condition", "text": "实习岗位"}]}]}


class GlobalFilterResults(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.jobs = JobStore(Path(self.temp.name) / "jobs.db")
        self.runs = RunStore(Path(self.temp.name) / "runs.db")
        self.document = ResumeDocumentView(id="synthetic-document", version=1, raw_content="合成候选人，Python项目经验。")
        self.digest = resume_document_digest(self.document)
        self.candidate = NS(read=lambda: NS(profile=NS(resume_id="synthetic-resume")))
        self.workspace = NS(read_document=lambda: self.document)
        self.jobs.create_task("source", "mock", "合成工程师", resume_id="synthetic-resume")
        self.constraints = {"conversation_id": "default", "queries": ["Python工程师"], "locations": ["深圳"],
            "result_limit": 20, "source": "mock", "filters": legacy_filter()}
        self.jobs.save_search_request("source", self.constraints)
        self.jobs.event("source", "search_constraints", "合成旧搜索条件", sample=self.constraints)
        self.jobs.update_task("source", status="completed")
        self.runs.mutate_dialogue("default", {"op": "task", "kind": "browser_id", "id": "source", "status": "completed"})
        self.sources = [{"session_id": "default", "task_id": "source"}]

    async def asyncTearDown(self):
        TRACES.append({"test": self.id(), "visible": [j.id for j in self.jobs.list_jobs()],
            "messages": self.jobs.conversation_history(conversation_id="default", limit=20)})
        self.jobs = self.runs = self.workspace = self.candidate = None
        gc.collect()
        self.temp.cleanup()

    def seed(self, count=6, *, salary="20-25K", city="深圳", benefit=False, broken_city_quote=False):
        ids = []
        for index in range(count):
            row = {"source": "MockJobSite", "source_url": f"http://127.0.0.1:8080/job.html?id={index}",
                "external_id": str(index), "title": f"合成Python工程师{index}", "company": f"合成公司{index}",
                "location": city, "salary": salary, "description": "Python接口开发与质量检查，工作地点深圳。",
                "source_tags": ["Python"], "tags": ["Python"], "score": 90, "decision": "REVIEW", "reasoning": "合成分析",
                "stage": "review", "qualification_evidence": {"status": "unknown", "resume_digest": self.digest}}
            conditions = [
                {"index": 0, "text": "地点深圳", "mode": "require", "status": "pass", "evidence": [
                    {"field": "location", "quote": "不存在的地点" if broken_city_quote else city}]},
                {"index": 1, "text": "外包岗位", "mode": "require", "status": "fail" if index == 0 else "unknown", "evidence": []},
                {"index": 2, "text": "月薪至少12000元", "mode": "require", "salary": {"unit": "monthly", "minimum": 12000},
                    "status": "pass", "evidence": [{"field": "salary", "quote": salary}]},
                {"index": 3, "text": "实习岗位", "mode": "require", "status": "fail" if index == 0 else "unknown", "evidence": []}]
            if benefit:
                conditions.append({"index": 4, "text": "有住房补贴", "mode": "require", "status": "unknown", "evidence": []})
            expression = copy.deepcopy(self.constraints["filters"])
            row["filter_status"] = "pass" if index == 0 and not benefit else "unknown"
            row["filter_evidence"] = {"status": row["filter_status"], "conditions": conditions,
                "facts_digest": filter_facts_digest(row), "policy_version": FILTER_POLICY_VERSION,
                "digest": filter_digest(validate_filter(expression))}
            ids.append(self.jobs.admit_search_result("source", row, limit=20, groups=None)["job_id"])
        if ids:
            row = self.jobs.get_job(ids[0])
            self.runs.mutate_dialogue("default", {"op": "snapshot", "kind": "job", "topic": "applications",
                "source": "old-report", "version": "source", "displayed": True,
                "items": [{key: row[key] for key in ("id", "title", "company")} ]})
        return ids

    async def refresh(self):
        return await refresh_filter_policy_results(self.jobs, self.runs, self.candidate, self.workspace, self.sources)

    async def test_six_old_candidates_are_refiltered_and_five_become_visible(self):
        ids = self.seed()
        self.assertEqual([row.id for row in self.jobs.list_jobs()], ids[:1])
        result = await self.refresh()
        self.assertEqual(result["issues"], [])
        self.assertEqual((result["refreshed"][0]["pass"], result["refreshed"][0]["newly_visible"]), (6, 5))
        self.assertEqual({row.id for row in self.jobs.list_jobs()}, set(ids))
        self.assertEqual(len(self.jobs.all_delivery_runs()), 0)
        self.assertEqual(self.jobs.mock_delivery_queue(), [])
        self.assertEqual(self.jobs.search_report("source")["qualified_jobs"], 6)
        snapshots = self.runs.dialogue_state("default")["snapshots"]
        self.assertEqual([row["id"] for row in snapshots[-1]["items"]], ids)
        self.assertIn("本轮更新后的职位简表", self.jobs.conversation_history(conversation_id="default")[-1]["content"])
        TRACES.append({"test": self.id(), "result": result})

    async def test_existing_authorized_hr_wait_and_frozen_queue_survive(self):
        ids = self.seed()
        binding = {"resume_document_id": self.document.id, "resume_version": 1, "resume_digest": self.digest}
        self.jobs.enqueue_mock_deliveries([self.jobs.get_job(ids[0])], session_id="default", resume_stamp="synthetic:1",
            binding=binding, min_score=0)
        self.jobs.create_delivery_run("existing-wait", ids[0])
        self.jobs.update_delivery_run("existing-wait", status="waiting_for_reply", checkpoint="waiting_for_reply",
            detail="已沟通，等待回复", submitted=0)
        self.jobs.update_mock_delivery(ids[0], "dispatched", "合成等待", run_id="existing-wait")
        before = self.jobs.mock_delivery_queue(active_only=False)[0]
        await self.refresh()
        after = self.jobs.mock_delivery_queue(active_only=False)[0]
        self.assertEqual(after, before)
        self.assertIsNotNone(self.jobs.assert_delivery_queue_facts(after))
        wait = self.jobs.get_delivery_run("existing-wait")
        self.assertEqual((str(wait.status), wait.checkpoint, wait.submitted), ("waiting_for_reply", "waiting_for_reply", False))
        self.assertEqual(len(self.jobs.all_delivery_runs()), 1)

    async def test_migration_is_idempotent_and_preserves_old_audit(self):
        self.seed()
        first = await self.refresh()
        snapshots = self.runs.dialogue_state("default")["snapshots"]
        second = await self.refresh()
        self.assertEqual(len(second["skipped"]), 1)
        self.assertEqual(self.runs.dialogue_state("default")["snapshots"], snapshots)
        self.assertEqual(len(self.jobs.conversation_history(conversation_id="default")), 1)
        self.assertEqual(first["external_model_calls"], 0)
        self.assertIn("外包", json.dumps(self.jobs.search_recovery("source")["request"], ensure_ascii=False))

    async def test_remaining_salary_and_city_still_exclude(self):
        self.seed(1, salary="8-10K", city="武汉")
        result = await self.refresh()
        self.assertEqual(result["refreshed"][0]["fail"], 1)
        self.assertEqual(self.jobs.list_jobs(), [])

    async def test_unverified_remaining_benefit_is_not_promoted(self):
        self.constraints["filters"]["children"].append({"op": "condition", "text": "有住房补贴"})
        self.jobs.event("source", "search_constraints", "剩余福利条件", sample=self.constraints)
        self.seed(1, benefit=True)
        result = await self.refresh()
        self.assertEqual(result["refreshed"][0]["unknown"], 1)
        self.assertEqual(self.jobs.list_jobs(), [])

    async def test_changed_raw_facts_or_resume_cannot_reuse_old_evidence(self):
        ids = self.seed(2)
        row = self.jobs.get_job(ids[1])
        self.jobs.upsert_job({**row, "description": row["description"] + "岗位描述已修改。"})
        result = await self.refresh()
        self.assertEqual((result["refreshed"][0]["pass"], result["refreshed"][0]["unknown"]), (1, 1))

    async def test_wrong_session_does_not_change_candidates(self):
        ids = self.seed()
        self.sources[0]["session_id"] = "another-session"
        result = await self.refresh()
        self.assertEqual(len(result["skipped"]), 1)
        self.assertEqual([row.id for row in self.jobs.list_jobs()], ids[:1])

    async def test_fresh_install_without_sources_does_not_require_private_materials(self):
        def forbidden():
            raise AssertionError("empty installation must not request private profile or resume")
        result = await refresh_filter_policy_results(self.jobs, self.runs, NS(read=forbidden),
            NS(read_document=forbidden), [])
        self.assertEqual(result, {"refreshed": [], "skipped": [], "issues": [], "external_model_calls": 0})

    async def test_source_score_is_preserved_when_other_search_changed_global_cache(self):
        ids = self.seed(1)
        with self.jobs._connect() as db:
            db.execute("UPDATE jobs SET score=10 WHERE id=?", (ids[0],))
        await self.refresh()
        evidence, _ = self.jobs.resume_analysis_evidence("synthetic-resume", task_id="source", resume_digest=self.digest)
        self.assertEqual(evidence[0]["score"], 90)
        self.assertEqual(self.jobs.get_job(ids[0])["score"], 10)

    async def test_passed_refresh_removes_stale_application_block_note(self):
        ids = self.seed(1)
        with self.jobs._connect() as db:
            db.execute("UPDATE jobs SET reasoning=? WHERE id=?",
                ("筛选条件待核实，禁止自动投递。原技能匹配说明。", ids[0]))
        await self.refresh()
        self.assertEqual(self.jobs.get_job(ids[0])["reasoning"], "原技能匹配说明。")

    async def test_changed_resume_is_not_blessed_by_removing_categories(self):
        self.seed(1)
        self.document = self.document.model_copy(update={"version": 2, "raw_content": "已改变的合成简历。"})
        result = await self.refresh()
        self.assertEqual(result["refreshed"][0]["unknown"], 1)
        self.assertEqual(self.jobs.list_jobs(), [])

    async def test_actual_lifespan_migrates_before_workers_and_restores_chat(self):
        self.seed()
        source = Path(__file__).parents[1] / "app/api/application.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        fn = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan")
        fn.decorator_list = []
        events = []
        async def workers_start():
            self.assertEqual(len(self.jobs.list_jobs()), 6)
            events.append("workers-started")
        ns = {"__package__": "app.api", "FastAPI": object, "job_store": self.jobs, "run_store": self.runs,
            "candidate_state_store": self.candidate, "resume_workspace_store": self.workspace,
            "delivery_service": NS(reload_persisted_state=lambda: events.append("delivery-restored"), shutdown=AsyncMock()),
            "agent": NS(restore_history=lambda history: events.append(("history", len(history)))),
            "mock_workflow": NS(start=workers_start, stop=AsyncMock()),
            "agent_runtime": NS(start=workers_start, shutdown=AsyncMock()),
            "browser_service": NS(shutdown=AsyncMock())}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), str(source), "exec"), ns)
        async with asynccontextmanager(ns["lifespan"])(object()):
            self.assertEqual(events[0], "delivery-restored")
            self.assertIn(("history", 1), events)
            self.assertEqual(events.count("workers-started"), 2)

    async def test_open_quota_cannot_steal_city_quota_during_admission_or_refresh(self):
        originals = self.seed(8)
        groups = [{"label": "不限类别", "count": 3, "filters": None},
            {"label": "深圳范围", "count": 5, "filters": {"op": "condition", "text": "地点深圳"}}]
        self.jobs.create_task("group-source", "mock", "合成分组", resume_id="synthetic-resume")
        self.jobs.save_search_request("group-source", {"conversation_id": "default"})
        self.jobs.update_task("group-source", status="completed")
        verdicts = []
        for index, ident in enumerate(originals):
            row = self.jobs.get_job(ident)
            for key in ("tags", "source_tags", "qualification_evidence"):
                if isinstance(row.get(key), str):
                    row[key] = json.loads(row[key])
            row.update(source_url=f"http://127.0.0.1:8080/job.html?id=group-{index}", external_id=f"group-{index}",
                location="深圳" if index < 5 else "武汉", filter_status="pass", filter_evidence={"status": "pass"})
            row["result_group_evidence"] = [{"index": 0, "status": "pass"},
                {"index": 1, "status": "pass" if index < 5 else "fail"}]
            verdicts.append({"status": "pass", "conditions": [], "result_group_evidence": row["result_group_evidence"]})
            result = self.jobs.admit_search_result("group-source", row, limit=8, groups=groups)
            self.assertEqual(result["status"], "accepted")
        with self.jobs._connect() as db:
            counts = dict(db.execute("SELECT group_index,COUNT(*) FROM search_candidates WHERE task_id=? "
                "AND status='accepted' GROUP BY group_index", ("group-source",)))
        self.assertEqual(counts, {0: 3, 1: 5})
        candidates = self.jobs.refilter_candidates("group-source", resume_id="synthetic-resume", session_id="default")
        self.jobs.save_refilter_results("group-source", candidates, verdicts, {"result_limit": 8, "result_groups": groups})
        with self.jobs._connect() as db:
            counts = dict(db.execute("SELECT group_index,COUNT(*) FROM search_candidates WHERE task_id=? "
                "AND status='accepted' GROUP BY group_index", ("group-source",)))
        self.assertEqual(counts, {0: 3, 1: 5})


class DirectRequestPolicy(unittest.TestCase):
    def test_browser_model_projects_keywords_types_and_group_total(self):
        request = BrowserSearchRequest(query="Python实习工程师", queries=["应届Python工程师"],
            filters=legacy_filter(), result_groups=[{"label": "正式", "count": 5, "filters": {"op": "condition", "text": "正式岗位"}},
                {"label": "实习", "count": 5, "filters": {"op": "condition", "text": "实习岗位"}}])
        self.assertEqual(request.query, "Python工程师")
        self.assertEqual(request.result_limit, 10)
        self.assertIsNone(request.result_groups)
        self.assertEqual(len(validate_filter(request.filters).leaves()), 2)

    def test_direct_analysis_cannot_restore_disabled_excluded_keywords(self):
        request = JobAnalysisRequest(title="Python工程师", description="合成岗位描述。" * 10,
            excluded_keywords=["外包", "实习", "应届", "三年项目经验", "Java"])
        self.assertEqual(request.excluded_keywords, ["Java"])


def main():
    import sys
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    output = Path("evaluation/results/global-filter-results") / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    summary = {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
        "external_model_calls": 0, "real_recruitment_actions": 0}
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (output / "traces.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in TRACES), encoding="utf-8")
    print(json.dumps({"output": str(output), **summary}))
    return int(not result.wasSuccessful())


if __name__ == "__main__": raise SystemExit(main())
