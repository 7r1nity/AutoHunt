"""Search -> enriched persistence -> queue -> follow-up scope, entirely offline.

Scripted verdicts exercise application contracts, not real model accuracy. No
user database, resume, network, browser, contact or submission is used.
"""
from __future__ import annotations

import asyncio
import ast
import copy
import gc
import io
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from app.browser.automation import BrowserAutomationService
from app.core.agent_tools import AgentToolRegistry
from app.core.job_filters import FILTER_POLICY_VERSION, evaluate_filter, filter_facts_digest, validate_filter
from app.core.models import BrowserSearchRequest, BrowserTaskStatus, JobAnalysis, ResumeDocumentView
from app.growth.requests import resume_stamp
from app.jobs.store import JobStore
from app.llm.base import LLMResult
from app.runtime.context import RunContext, current_run
from app.runtime.dialogue import apply_operation
from app.runtime.memory_context import compile_context
from app.workflow.delivery_scope import materialize_delivery_scope
from app.workflow.mock_coordinator import MockWorkflowCoordinator
from app.workflow.planning import RequestDraft, plan_request, validate_draft
from app.workflow.result_sources import ResultSourceClarification
from app.workflow.revision import RevisionDraft, edited_plan
from app.security.scope_changes import delivery_scope_change

TRACES = []


def leaf(text):
    return {"op": "condition", "text": text}


def scope():
    return {"id": "synthetic-scope", "session_id": "default", "resume_stamp": "stamp",
            "task_id": "search", "min_score": 85, "filters": leaf("深圳正式全职，月薪至少14000元")}


def planned(args):
    return RequestDraft.model_validate({"steps": [{"id": "deliver", "tool": "mock_delivery_start",
        "arguments": args, "user_requested": True}],
        "goals": [{"quote": "补投", "disposition": "steps", "step_ids": ["deliver"]}]})


class Scope(unittest.TestCase):
    def resolve(self, args, previous=None, **kw):
        return materialize_delivery_scope(args, scope() if previous is None else previous,
            session_id=kw.pop("session_id", "default"), stamp=kw.pop("stamp", "stamp"), **kw)

    def test_omitted_filters_and_score_are_inherited(self):
        self.assertEqual(self.resolve({}), {"filters": validate_filter(scope()["filters"]).model_dump(mode="json"), "min_score": 85,
            "source_task_id": "search", "scope_mode": "new"})

    def test_followup_cannot_lower_score_or_clear_filters_by_omission(self):
        for args in ({"min_score": 88}, {"filters": None}, {"min_score": 0, "filters": None}):
            result = self.resolve(args)
            self.assertEqual(result["filters"]["text"], scope()["filters"]["text"])
            self.assertGreaterEqual(result["min_score"], 85)

    def test_new_filter_is_intersection_not_replacement(self):
        extra = leaf("有住房补贴")
        result = self.resolve({"filters": extra})
        self.assertEqual(result["filters"]["op"], "all")
        self.assertEqual([c["text"] for c in result["filters"]["children"]], [scope()["filters"]["text"], extra["text"]])

    def test_copied_scope_is_idempotent(self):
        args = self.resolve({"filters": scope()["filters"]})
        self.assertEqual(self.resolve(args), args)

    def test_single_selection_is_not_a_batch_retry(self):
        for args in ({"selector": "第七个"}, {"target_step_id": "chosen"}):
            self.assertEqual(self.resolve(args), args)

    def test_explicit_new_scope_can_replace_old_constraints(self):
        args = {"scope_mode": "new", "min_score": 80, "filters": leaf("实习岗位")}
        self.assertEqual(self.resolve(args), args)

    def test_new_search_does_not_reuse_old_delivery_scope(self):
        self.assertEqual(self.resolve({}, new_search=True), {})
        self.assertEqual(self.resolve({"source_task_id": "another-search"}), {"source_task_id": "another-search"})

    def test_changed_resume_or_session_asks_without_sending(self):
        for kw in ({"stamp": "changed"}, {"session_id": "other"}):
            with self.assertRaises(ResultSourceClarification):
                self.resolve({}, **kw)

    def test_snapshot_survives_dialogue_persistence_and_context_projection(self):
        state = apply_operation({}, {"op": "delivery_scope", "scope": scope()})
        state = apply_operation(state, {"op": "begin", "message": "还有两个88分的未投递"})
        self.assertEqual(compile_context(state, {})["delivery_scope"], scope())
        state = apply_operation(state, {"op": "reset"})
        self.assertIsNone(state["delivery_scope"])

    def test_model_new_flag_cannot_prove_old_filters_are_retained(self):
        result = delivery_scope_change({"scope_mode": "new", "min_score": 85}, scope(),
            session_id="default", stamp="stamp", task_id="search")
        self.assertTrue(result)
        self.assertIn("原有筛选条件", " ".join(result["reasons"]))

    def test_actual_subset_not_flag_controls_whether_confirmation_is_needed(self):
        args = self.resolve({"min_score": 88, "filters": leaf("有补贴")})
        self.assertIsNone(delivery_scope_change(args, scope(), session_id="default", stamp="stamp"))
        args["min_score"] = 80
        self.assertIsNotNone(delivery_scope_change(args, scope(), session_id="default", stamp="stamp"))

    def test_salary_narrowing_is_provable_but_alternative_expansion_is_not(self):
        salary = lambda n: {"op": "condition", "text": f"月薪至少{n}", "salary": {"unit": "monthly", "minimum": n}}
        previous = {**scope(), "filters": {"op": "all", "children": [leaf("正式"), salary(15000)]}}
        for amount, needs_confirmation in ((20000, False), (15000, False), (14000, True)):
            args = {"min_score": 85, "source_task_id": "search", "filters": {"op": "all", "children": [leaf("正式"), salary(amount)]}}
            self.assertEqual(bool(delivery_scope_change(args, previous, session_id="default", stamp="stamp")), needs_confirmation)
        args["filters"] = {"op": "any", "children": [previous["filters"], leaf("实习")]}
        self.assertIsNotNone(delivery_scope_change(args, previous, session_id="default", stamp="stamp"))

    def test_uncertain_paraphrase_requires_confirmation_not_silent_approval(self):
        previous = {**scope(), "filters": leaf("正式岗位")}
        self.assertIsNotNone(delivery_scope_change({"min_score": 85, "source_task_id": "search", "filters": leaf("全职工作")},
            previous, session_id="default", stamp="stamp"))

    def test_structural_subset_proof_has_no_boolean_false_positive(self):
        import itertools
        a, b, c = [leaf(x) for x in "ABC"]
        trees = [a, b, c, {"op": "not", "children": [a]},
            {"op": "all", "children": [a, b]}, {"op": "any", "children": [a, b]},
            {"op": "all", "children": [a, {"op": "any", "children": [b, c]}]},
            {"op": "any", "children": [{"op": "all", "children": [a, b]}, c]}]
        def truth(node, values):
            if node["op"] == "condition": return values[node["text"]]
            children = [truth(x, values) for x in node["children"]]
            return not children[0] if node["op"] == "not" else all(children) if node["op"] == "all" else any(children)
        for old, new in itertools.product(trees, repeat=2):
            change = delivery_scope_change({"min_score": 85, "source_task_id": "search", "filters": new},
                {**scope(), "filters": old}, session_id="default", stamp="stamp")
            if change is None:
                for bits in itertools.product((False, True), repeat=3):
                    values = dict(zip("ABC", bits))
                    self.assertFalse(truth(new, values) and not truth(old, values), (old, new, values))


class Evidence(unittest.IsolatedAsyncioTestCase):
    async def test_generated_tags_never_enter_judgment_or_invalidate_cache(self):
        facts = {"title": "数据分析师", "description": "当前岗位为正式全职，面向应届毕业生。", "tags": ["SQL"]}
        requests = []
        async def judge(request):
            requests.append(json.loads(request.user_message))
            return LLMResult(json.dumps({"conditions": [{"index": 0, "status": "pass", "reason": "全职",
                "evidence": [{"field": "description", "quote": "当前岗位为正式全职"}]}]}), "scripted", "offline")
        prior = await evaluate_filter(judge, leaf("正式全职"), facts)
        stored = {**facts, "source_tags": '["SQL"]', "tags": ["SQL", "虚构模型技能"]}
        result = await evaluate_filter(judge, leaf("正式全职"), stored, prior_evidence=prior)
        self.assertEqual(result["status"], "pass")
        self.assertTrue(result["conditions"][0]["reused"])
        self.assertEqual(len(requests), 1)
        self.assertEqual(filter_facts_digest(facts), filter_facts_digest(stored))
        self.assertNotIn("虚构模型技能", json.dumps(requests, ensure_ascii=False))

    async def test_real_jd_changes_and_policy_upgrades_require_recheck(self):
        facts = {"title": "数据分析师", "description": "本岗位是全职。", "tags": []}
        prior = {"policy_version": FILTER_POLICY_VERSION, "facts_digest": filter_facts_digest(facts),
            "conditions": [{"text": "全职", "status": "pass", "mode": "require", "evidence": [{"field": "description", "quote": "本岗位是全职"}]}]}
        cases = [("description", "旧描述：本岗位是全职。现改为实习。"), ("salary", "200元/天"),
                 ("location", "武汉"), ("company", "新雇主"), ("source_tags", ["实习"])]
        for field, value in cases:
            calls = []
            async def unknown(request):
                calls.append(request)
                return LLMResult('{"conditions":[{"index":0,"status":"unknown","evidence":[]}]}', "scripted", "offline")
            result = await evaluate_filter(unknown, leaf("全职"), {**facts, field: value}, prior_evidence=prior)
            self.assertEqual((result["status"], len(calls)), ("unknown", 1), field)
        legacy = {k: v for k, v in prior.items() if k != "policy_version"}
        result = await evaluate_filter(unknown, leaf("全职"), facts, prior_evidence=legacy)
        self.assertEqual(result["status"], "unknown")

    async def test_new_conditions_do_not_reuse_old_verdict(self):
        facts = {"description": "本岗位是全职。"}
        prior = {"policy_version": FILTER_POLICY_VERSION, "facts_digest": filter_facts_digest(facts),
            "conditions": [{"text": "全职", "status": "pass", "evidence": [{"field": "description", "quote": "本岗位是全职"}]}]}
        model = AsyncMock(return_value=LLMResult('{"conditions":[{"index":0,"status":"unknown","evidence":[]}]}', "scripted", "offline"))
        result = await evaluate_filter(model, leaf("全职且不外包"), facts, prior_evidence=prior)
        self.assertEqual(result["status"], "unknown")
        model.assert_awaited_once()

    async def test_missing_legacy_source_tags_are_not_reconstructed_from_analysis(self):
        async def judge(request):
            self.assertEqual(json.loads(request.user_message)["facts"]["tags"], "")
            return LLMResult('{"conditions":[{"index":0,"status":"unknown","evidence":[]}]}', "scripted", "offline")
        result = await evaluate_filter(judge, leaf("有资质证书"),
            {"description": "未说明资质", "source_tags": None, "tags": ["模型推断出的资质"]})
        self.assertEqual(result["status"], "unknown")

    async def test_employment_axes_and_unknown_case_matrix(self):
        cases = [("招聘应届生，当前为正式全职，签订劳动合同。", "regular", "campus", "pass"),
                 ("校招岗位，性质未披露。", "unknown", "campus", "unknown"),
                 ("应届生实习，当前为实习协议，之后有转正机会。", "internship", "campus", "fail"),
                 ("毕业实习，当前为实习协议。", "internship", "campus", "fail"),
                 ("全职，接受有实习经历的应届生。", "regular", "campus", "pass"),
                 ("面向社会招聘，性质未披露，月薪20K。", "unknown", "experienced", "unknown")]
        for industry in ("软件", "金融", "医疗", "外贸", "制造"):
            for description, kind, channel, verdict in cases:
                async def judge(request):
                    self.assertIn("校招/应届/社招属于招聘对象", request.instructions)
                    e = [{"field": "description", "quote": description}]
                    return LLMResult(json.dumps({"conditions": [{"index": 0, "status": verdict, "evidence": e}],
                        "employment": {"kind": {"value": kind, "evidence": e},
                                       "hiring_channel": {"value": channel, "evidence": e}}}), "scripted", "offline")
                result = await evaluate_filter(judge, leaf("正式全职"), {"title": industry + "合成岗位", "description": description})
                self.assertEqual(result["status"], verdict)
                self.assertEqual(result["employment"]["kind"]["value"], kind)
                self.assertEqual(result["employment"]["hiring_channel"]["value"], channel)
                TRACES.append({"industry": industry, "description": description, "result": result})

    async def test_invalid_employment_citation_does_not_create_known_fact(self):
        async def judge(_):
            return LLMResult(json.dumps({"conditions": [{"index": 0, "status": "unknown"}],
                "employment": {"kind": {"value": "regular", "evidence": [{"field": "description", "quote": "不存在的全职证据"}]}}}), "scripted", "offline")
        result = await evaluate_filter(judge, leaf("正式全职"), {"description": "校招，性质未披露。"})
        self.assertEqual(result["employment"]["kind"]["value"], "unknown")
        self.assertEqual(result["status"], "unknown")

    async def test_malformed_optional_classification_does_not_poison_valid_conditions(self):
        async def judge(_):
            return LLMResult(json.dumps({"conditions": [{"index": 0, "status": "pass", "evidence": [{"field": "description", "quote": "当前全职"}]}],
                "employment": {"kind": {"value": {"bad": "type"}}}}), "scripted", "offline")
        result = await evaluate_filter(judge, leaf("正式全职"), {"description": "当前全职"})
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["employment"]["kind"]["value"], "unknown")

    async def test_campus_label_cannot_waive_internship_daily_floor(self):
        expression = {"op": "all", "children": [leaf("当前为实习岗位"),
            {"op": "condition", "text": "实习日薪至少300元", "salary": {"unit": "daily", "minimum": 300, "applies_to": "internship"}}]}
        description = "招聘类型：应届/校招。当前签订实习协议，有转正机会。"
        async def judge(_):
            e = [{"field": "description", "quote": "当前签订实习协议"}]
            return LLMResult(json.dumps({"conditions": [{"index": 0, "status": "pass", "evidence": e}],
                "employment": {"kind": {"value": "internship", "evidence": e}}}), "scripted", "offline")
        for salary, expected in (("200-400元/天", "fail"), ("300-500元/天", "pass"), ("15-20K", "unknown"), ("面议", "unknown")):
            result = await evaluate_filter(judge, expression, {"description": description, "salary": salary})
            self.assertEqual(result["status"], expected, salary)
            self.assertEqual(result["employment"]["kind"]["value"], "internship")


class PersistedFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = JobStore(Path(self.temp.name) / "jobs.db")
        self.store.create_task("search", "mock", "合成分析师", resume_id="synthetic-resume")
        self.store.save_search_request("search", {"conversation_id": "default"})
        self.calls = []
        async def model(request):
            self.calls.append(request)
            if len(self.calls) > 2:
                return LLMResult('{"conditions":[{"index":0,"status":"unknown"}]}', "scripted", "changed-verdict")
            return LLMResult(json.dumps({"conditions": [{"index": 0, "status": "pass", "reason": "明确全职",
                "evidence": [{"field": "description", "quote": "当前正式全职岗位"}]}]}), "scripted", "offline")
        self.browser = object.__new__(BrowserAutomationService)
        self.browser.store, self.browser.llm = self.store, NS(generate=model)
        self.document = ResumeDocumentView(id="synthetic-doc", version=1)
        self.candidate = NS(read=lambda: NS(profile=NS(resume_id="synthetic-resume")))
        self.resume = NS(read_document=lambda: self.document)
        self.coordinator = MockWorkflowCoordinator(self.store, self.browser,
            NS(supports=lambda _: True), self.candidate, self.resume)
        self.coordinator._ensure_applicant_bound = AsyncMock(return_value="bound")
        self.registry = AgentToolRegistry(jobs=self.store, browser=self.browser,
            candidate_state=self.candidate, resume_workspace=self.resume, memory=None, mock_workflow=self.coordinator)
        self.events = []
        self.context = RunContext("synthetic-run", asyncio.current_task(), lambda kind, data: self.events.append((kind, data)), lambda _: None,
            dialogue={"tasks": {"browser_id": "search"}}, user_message="投递符合条件的岗位")
        self.token = current_run.set(self.context)

    async def asyncTearDown(self):
        current_run.reset(self.token)
        self.coordinator = self.registry = self.browser = self.store = None
        gc.collect()
        self.temp.cleanup()

    async def seed(self,qualification_statuses=("pass","pass")):
        request = BrowserSearchRequest(query="合成分析师", source="mock", filters=leaf("正式全职"), result_limit=2)
        for i in (1, 2):
            detail = {"title": "校招分析师", "company": f"合成公司{i}", "salary": "19-23K", "location": "深圳",
                "description": "面向应届生，当前正式全职岗位，签订劳动合同。", "tags": ["SQL"], "external_id": str(i)}
            await self.browser._check_filters("search", detail, request)
            from app.memory.resume_workspace import resume_document_digest
            analysis = JobAnalysis(score=88, decision="APPLY", matched_skills=["Python"], missing_skills=["Graph RAG"], reasoning="合成评分",
                qualification={"status":qualification_statuses[i-1],"assessed":True,"requirements":[],"resume_digest":resume_document_digest(self.document)})
            self.browser._persist_analysis("search", detail, analysis, f"http://127.0.0.1:8080/job.html?id={i}", source="MockJobSite", request=request)
        self.store.update_task("search", status=BrowserTaskStatus.COMPLETED)

    async def test_search_enrichment_then_queue_has_no_second_model_verdict(self):
        self.context.owner = asyncio.current_task()
        await self.seed()
        result = await self.registry._mock_delivery_start({"filters": leaf("正式全职"), "min_score": 85})
        self.assertEqual(result["queued"], 2)
        self.assertEqual(len(self.calls), 2)
        for row in self.store.mock_delivery_queue():
            data = self.store.get_job(row["job_id"])
            self.assertEqual(json.loads(data["source_tags"]), ["SQL"])
            self.assertIn("Graph RAG", json.loads(data["tags"]))
            self.assertTrue(row["scope_evidence"]["conditions"][0]["reused"])
        self.assertEqual(len(result["scope_checks"]), 2)
        TRACES.append({"flow": "search-enrich-persist-queue", "result": result, "model_calls": len(self.calls)})

    async def test_partial_upsert_keeps_raw_tags_and_screening_evidence(self):
        self.context.owner = asyncio.current_task()
        await self.seed()
        before = self.store.get_job(1)
        partial = {k: v for k, v in before.items() if k not in {"source_tags", "filter_status", "filter_evidence"}}
        partial["tags"] = ["Python"]
        self.store.upsert_job(partial)
        after = self.store.get_job(1)
        self.assertEqual(after["source_tags"], before["source_tags"])
        self.assertEqual(after["filter_evidence"], before["filter_evidence"])
        verdict = await evaluate_filter(self.browser.llm.generate, leaf("正式全职"), after,
            prior_evidence=json.loads(after["filter_evidence"]))
        self.assertTrue(verdict["conditions"][0]["reused"])
        self.assertEqual(len(self.calls), 2)

    async def test_explicit_null_empty_and_replacement_tags_are_not_omission(self):
        self.context.owner = asyncio.current_task()
        await self.seed()
        row = self.store.get_job(1)
        row["filter_evidence"] = json.loads(row["filter_evidence"])
        for value, expected in ((None, None), ([], "[]"), (["SQL", "Excel"], '["SQL", "Excel"]')):
            self.store.upsert_job({**row, "tags": ["Python"], "source_tags": value})
            self.assertEqual(self.store.get_job(1)["source_tags"], expected)

    async def test_partial_upsert_with_changed_jd_cannot_reuse_old_evidence(self):
        self.context.owner = asyncio.current_task()
        await self.seed()
        row = self.store.get_job(1)
        partial = {k: v for k, v in row.items() if k not in {"source_tags", "filter_evidence", "filter_status"}}
        partial["description"] += "以上是旧模板，实际以实习协议为准。"
        partial["tags"] = ["Python"]
        self.store.upsert_job(partial)
        updated = self.store.get_job(1)
        result = await evaluate_filter(self.browser.llm.generate, leaf("正式全职"), updated,
            prior_evidence=json.loads(updated["filter_evidence"]))
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(result["conditions"][0].get("reused"))
        self.assertEqual(len(self.calls), 3)

    async def test_contact_import_uses_partial_upsert_without_erasing_evidence(self):
        self.context.owner = asyncio.current_task()
        await self.seed()
        row = self.store.get_job(1)
        self.coordinator.candidate = NS(read=lambda: NS(profile=NS(skills=[]), preferences=NS(target_roles=NS(values=[]))))
        remote = {"title": row["title"], "company_name": row["company"], "city": row["location"],
            "salary_min": 19, "salary_max": 23, "description": row["description"], "skills": ["Python"]}
        ident, inserted = self.coordinator._import_contacted_job(1, remote)
        self.assertEqual((ident, inserted), (1, False))
        updated = self.store.get_job(1)
        self.assertEqual(updated["source_tags"], row["source_tags"])
        self.assertEqual(updated["filter_evidence"], row["filter_evidence"])

    async def test_manual_http_import_body_preserves_raw_tags_in_isolated_store(self):
        self.context.owner = asyncio.current_task()
        await self.seed()
        row = self.store.get_job(1)
        remote = {"title": row["title"], "company_name": row["company"], "city": row["location"],
            "salary_min": 19, "salary_max": 23, "description": row["description"], "skills": ["Python"]}
        # Execute the actual endpoint body with a scripted transport, without
        # importing the application module (which owns the user's live stores).
        from fastapi import HTTPException, Query
        from app.core.models import StoredJobView
        source = Path(__file__).parents[1] / "app/api/application.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "import_mock_site_job")
        fn.decorator_list = []
        ns = {"asyncio": asyncio, "json": json, "Query": Query, "HTTPException": HTTPException,
              "StoredJobView": StoredJobView, "job_store": self.store,
              "urlopen": lambda *a, **kw: io.BytesIO(json.dumps(remote).encode())}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), str(source), "exec"), ns)
        result = await ns["import_mock_site_job"](1, "gated")
        self.assertEqual(result.id, 1)
        updated = self.store.get_job(1)
        self.assertEqual(updated["source_tags"], row["source_tags"])
        self.assertEqual(updated["filter_evidence"], row["filter_evidence"])

    async def test_followup_and_manual_selection_keep_previous_batch_scope(self):
        self.context.owner = asyncio.current_task()
        await self.seed()
        await self.registry._mock_delivery_start({"filters": leaf("正式全职"), "min_score": 85})
        remembered = copy.deepcopy(self.context.dialogue["delivery_scope"])
        self.context.user_message = "还有两个88分的未投递"
        draft = validate_draft(planned({"min_score": 88}), self.registry, self.context.user_message,
            dialogue=self.context.dialogue, conditions={}, stamp=resume_stamp(self.document, self.candidate))
        args = next(s["arguments"] for s in draft["steps"] if s["tool"] == "mock_delivery_start")
        self.assertEqual(args["filters"]["text"], "正式全职")
        self.assertEqual(args["source_task_id"], "search")
        self.assertEqual(args["min_score"], 88)
        self.assertEqual(args["scope_mode"], "new")
        # A later scope pointer change must not rewrite already compiled args.
        self.context.dialogue["delivery_scope"] = {**remembered, "filters": leaf("别的条件")}
        self.assertEqual(self.registry._delivery_arguments(args), args)
        self.assertEqual(self.registry._delivery_arguments({"selector": "第七个"}), {"selector": "第七个"})

    async def test_missing_raw_tags_invalidates_frozen_receipt_before_scope_evaluation(self):
        self.context.owner = asyncio.current_task()
        await self.seed()
        with self.store._connect() as db:
            db.execute("UPDATE jobs SET source_tags=NULL")
        before = len(self.calls)
        result = await self.coordinator.queue_deliveries(session_id="default", task_id="search", min_score=85, filters=leaf("正式全职"))
        self.assertEqual((result["queued"], result["insufficient"]), (0, 2))
        self.assertEqual(result["reason"], "insufficient_evidence")
        self.assertEqual(result["scope_checks"], [])
        self.assertEqual(len(self.calls), before)  # Changed facts require fresh search evidence first.
        self.assertEqual(self.store.mock_delivery_queue(), [])

    async def test_planner_sees_scope_and_materializes_omitted_conditions(self):
        self.context.owner = asyncio.current_task()
        self.context.dialogue = apply_operation({}, {"op": "delivery_scope", "scope": scope()})
        async def generated(request):
            body = json.loads(request.user_message)
            self.assertEqual(body["dialogue"]["delivery_scope"], scope())
            self.assertIn("补投、漏投", request.instructions)
            return LLMResult(planned({"min_score": 88}).model_dump_json(), "scripted", "offline")
        result = await plan_request(NS(generate=generated), self.registry, "还有两个88分的未投递",
            dialogue=self.context.dialogue, conditions={}, stamp="stamp")
        args = next(s["arguments"] for s in result["steps"] if s["tool"] == "mock_delivery_start")
        self.assertEqual(args["filters"]["text"], scope()["filters"]["text"])

    async def test_revision_addition_freezes_scope_before_authorization(self):
        self.context.owner = asyncio.current_task()
        self.context.dialogue = apply_operation({}, {"op": "delivery_scope", "scope": scope()})
        base = validate_draft(planned({"scope_mode": "new", "min_score": 85}), self.registry, "合成批次",
            dialogue={}, conditions={}, stamp="stamp")
        base["steps"][0]["status"] = "completed"
        base["status"] = "running"
        changed = edited_plan(base, RevisionDraft(plan_id=base["id"], expected_revision=base["revision"],
            add_steps=[{"id": "extra", "tool": "mock_delivery_start", "arguments": {"min_score": 88}, "user_requested": True}],
            goals=[{"quote": "补投未完成岗位", "disposition": "steps", "step_ids": ["extra"]}]),
            self.registry, stamp="stamp", session_id="default", message="补投未完成岗位")
        target = next(s for s in changed["steps"] if s["id"] == "extra")
        self.assertEqual(target["arguments"]["filters"]["text"], scope()["filters"]["text"])
        self.assertEqual(target["arguments"]["source_task_id"], "search")
        from app.security.action_grants import action_binding
        expected = action_binding("mock_delivery_start", target["arguments"], session_id="default", requested=True)
        self.assertEqual(target["authority_binding"], expected)

    async def test_compiled_action_cannot_inherit_a_later_unrelated_scope(self):
        self.context.owner = asyncio.current_task()
        self.context.dialogue = apply_operation({}, {"op": "delivery_scope", "scope": scope()})
        self.context.action_authority = {"evidence_task_id": "search"}
        self.assertEqual(self.registry._delivery_arguments({"min_score": 85}), {"min_score": 85})

    async def test_campus_without_employment_evidence_stays_pending_off_board(self):
        self.context.owner = asyncio.current_task()
        async def unknown(_):
            return LLMResult('{"conditions":[{"index":0,"status":"unknown","reason":"只有校招，未披露岗位性质","evidence":[]}]}', "scripted", "offline")
        self.browser.llm.generate = unknown
        request = BrowserSearchRequest(query="分析师", source="mock", filters=leaf("正式全职"), result_limit=2)
        detail = {"title": "校招分析师", "company": "合成公司", "location": "深圳", "salary": "20-25K",
            "description": "校招，岗位性质未披露。", "tags": [], "external_id": "3"}
        await self.browser._check_filters("search", detail, request)
        analysis = JobAnalysis(score=88, decision="APPLY", matched_skills=[], missing_skills=[], reasoning="合成")
        counts = self.browser._persist_analysis("search", detail, analysis,
            "http://127.0.0.1:8080/job.html?id=3", source="MockJobSite", request=request)
        self.assertEqual(counts, (0, 0, 0))
        report = self.store.search_report("search")
        self.assertEqual(report["pending_verification"], 1)
        self.assertEqual(self.store.list_jobs(), [])
        self.assertEqual(self.store.mock_delivery_queue(), [])


def main():
    import sys
    log = io.StringIO()
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    folder = Path(__file__).parent / "results" / "delivery-evidence" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    folder.mkdir(parents=True)
    (folder / "tests.log").write_text(log.getvalue(), encoding="utf-8")
    summary = {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
        "external_model_calls": 0, "browser_actions": 0, "classification_matrix_cases": 30,
        "scope": "Offline application contracts; not live model accuracy/browser E2E"}
    (folder / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (folder / "traces.jsonl").write_text("".join(json.dumps(t, ensure_ascii=False) + "\n" for t in TRACES), encoding="utf-8")
    print(log.getvalue()); print(json.dumps({"output": str(folder), **summary}))
    return int(not result.wasSuccessful())


if __name__ == "__main__":
    raise SystemExit(main())
