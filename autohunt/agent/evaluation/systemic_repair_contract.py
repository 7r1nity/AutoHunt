"""Frozen failure contracts; synthetic state/scripted models, ZERO external calls."""
from __future__ import annotations

import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from app.core.agent_tools import AgentToolRegistry
from app.core.job_filters import FilterExpression,filter_schema,validate_filter,combine
from app.core.job_targets import resolve_job_targets,TargetClarification
from app.core.models import JobAnalysis,JobAnalysisRequest,ResumeDocumentView,ResumeEntryView
from app.browser.screening import match_job
from app.jobs.candidate_facts import match_facts,verify_requirements,qualification_status
from app.jobs.store import JobStore
from app.llm.base import LLMResult
from app.runtime.dialogue import apply_operation,resolve_reference
from app.workflow.planning import plan_request
from app.workflow.revision import RevisionDraft,edited_plan
from app.workflow.scope_review import proposals
from evaluation.stateful_scenarios import Harness,STAMP
from evaluation.search_delivery_contract import job


def monthly(value=15000):
    return {"op":"condition","text":f"月薪至少{value}元","salary":{"unit":"monthly","minimum":value}}


def negotiable():
    return {"op":"condition","text":"面议","salary":{"unit":"negotiable"}}


def fixture_model(plan,verdict=None):
    calls=[]
    async def generate(request):
        calls.append(request)
        value=verdict if request.context=="条件语义核对，不产生执行授权" else plan
        if isinstance(value,Exception): raise value
        return LLMResult(json.dumps(value,ensure_ascii=False),"fixture","scripted-no-network")
    return NS(generate=generate,calls=calls)


class FilterContracts(unittest.TestCase):
    def test_schema_runtime_structure_parity(self):
        import jsonschema
        leaf=monthly()
        examples=[leaf,{"op":"all","text":"说明","children":[leaf]},
            {"op":"not","text":"不要外包","children":[{"op":"condition","text":"外包"}]},
            {"op":"any","label":"正式或实习","children":[leaf,negotiable()]},
            {"op":"not","children":[]},{"op":"not","children":[leaf,leaf]},
            {"op":"condition","text":""},{"op":"condition","text":"有效","children":[leaf]},
            {"op":"all","children":[leaf],"mode":"allow"},{"op":"all","children":[leaf],"salary":{"unit":"negotiable"}}]
        schema=filter_schema()
        for item in examples:
            with self.subTest(item=item):
                accepted=True
                try: validate_filter(item)
                except ValueError: accepted=False
                self.assertEqual(jsonschema.Draft202012Validator(schema).is_valid(item),accepted)
                try:
                    AgentToolRegistry.validate_arguments(schema,item)
                    registry=True
                except ValueError: registry=False
                self.assertEqual(registry,accepted)

    def test_group_annotation_is_retained_without_changing_truth(self):
        tree={"op":"any","text":"正式或实习","children":[monthly(),negotiable()]}
        expression=validate_filter(tree)
        self.assertEqual(expression.label,"正式或实习")
        self.assertEqual(expression.text,"")
        self.assertEqual(len(expression.leaves()),2)
        for first in ("pass","fail","unknown"):
            for second in ("pass","fail","unknown"):
                self.assertEqual(combine(expression,iter([first,second])),combine(validate_filter({**tree,"text":""}),iter([first,second])))

    def test_nullable_filter_and_cached_schema_isolation(self):
        AgentToolRegistry.validate_arguments({**filter_schema(),"type":["object","null"]},None)
        one=filter_schema(); one["properties"]["text"]["maxLength"]=1
        self.assertEqual(filter_schema()["properties"]["text"]["maxLength"],400)


class ScopeContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.h=Harness(self.temp.name)
    async def asyncTearDown(self):
        await self.h.close(); self.temp.cleanup()

    async def test_initial_unrequested_exception_is_corrected_then_recompiled(self):
        original={"steps":[{"id":"search","tool":"job_search_start","arguments":{"queries":["Python后端"],"filters":{"op":"any","children":[monthly(),negotiable()]}},"user_requested":True}]}
        model=fixture_model(original,{"scopes":[{"step_id":"search","status":"correct","changes":{"filters":monthly()},"reason":"用户没有允许面议"}]})
        with self.h.context("找15份Python后端工作，月薪15k以上"):
            result=await plan_request(model,self.h.registry,"找15份Python后端工作，月薪15k以上",dialogue={},conditions={},stamp=STAMP)
        self.assertEqual(len(model.calls),2)
        search=next(s for s in result["steps"] if s["tool"]=="job_search_start")
        self.assertEqual([leaf.salary.unit for leaf in validate_filter(search["arguments"]["filters"]).leaves()],["monthly"])
        self.assertEqual(result["verified_conditions"]["filters"],search["arguments"]["filters"])
        self.assertEqual(result["scope_review"]["status"],"reviewed")

    async def test_semantic_unavailable_waits_only_affected_branch_without_repair_loop(self):
        original={"steps":[{"id":"search","tool":"job_search_start","arguments":{"queries":["Python后端"],"filters":monthly()},"user_requested":True},
            {"id":"stats","tool":"job_stats_read","arguments":{},"user_requested":True}]}
        model=fixture_model(original,RuntimeError("synthetic unavailable"))
        with self.h.context("找Python后端月薪15k以上，另外查看职位统计"):
            result=await plan_request(model,self.h.registry,"找Python后端月薪15k以上，另外查看职位统计",dialogue={},conditions={},stamp=STAMP)
        self.assertEqual(len(model.calls),2)
        self.assertEqual(next(s for s in result["steps"] if s["id"]=="search")["status"],"waiting_input")
        self.assertEqual(next(s for s in result["steps"] if s["id"]=="stats")["status"],"pending")

    async def test_reviewer_cannot_change_tool_or_target(self):
        original={"steps":[{"id":"search","tool":"job_search_start","arguments":{"queries":["Python后端"],"filters":monthly()},"user_requested":True}]}
        model=fixture_model(original,{"scopes":[{"step_id":"search","status":"correct","changes":{"queries":["金融分析师"]},"reason":"malformed reviewer"}]})
        with self.h.context():
            result=await plan_request(model,self.h.registry,"找Python后端月薪15k以上",dialogue={},conditions={},stamp=STAMP)
        search=result["steps"][0]
        self.assertEqual(search["arguments"]["queries"],["Python后端"])
        self.assertEqual(search["status"],"waiting_input")

    async def test_count_patch_preserves_scope_without_another_semantic_call(self):
        self.h.install([{"id":"search","tool":"job_search_start","arguments":{"queries":["Python后端"],"filters":monthly(),"result_limit":15},
                         "status":"pending","effect":"external","depends_on":[],"command_quote":"","scope_quote":"",
                         "missing_slots":[],"questions":[],"slot_evidence":{}}])
        draft={"operation":"revise","revision":{"plan_id":"model-invented-id","expected_revision":8,
            "updates":[{"step_id":"search","arguments":{"result_limit":10}}]}}
        model=fixture_model(draft)
        with self.h.context("改成10份其他不变"):
            result=await plan_request(model,self.h.registry,"改成10份其他不变",dialogue={},conditions={},stamp=STAMP)
            updated=edited_plan(self.h.plan,RevisionDraft.model_validate(result["revision"]),self.h.registry,stamp=STAMP,session_id="default",message="改成10份其他不变")
        self.assertEqual(len(model.calls),1)
        self.assertEqual(updated["steps"][0]["arguments"]["filters"],validate_filter(monthly()).model_dump(mode="json"))
        self.assertEqual(updated["steps"][0]["arguments"]["result_limit"],10)

    async def test_real_concurrent_plan_change_is_not_overwritten(self):
        self.h.install([{"id":"search","tool":"job_search_start","arguments":{"queries":["Python后端"]},
                         "status":"pending","effect":"external","depends_on":[],"command_quote":"","scope_quote":"",
                         "missing_slots":[],"questions":[],"slot_evidence":{}}])
        async def generate(request):
            self.h.plan["revision"]+=1
            self.h.store.save_plan(self.h.run["id"],self.h.plan,expected_revision=0)
            return LLMResult(json.dumps({"operation":"revise","revision":{"updates":[{"step_id":"search","arguments":{"result_limit":10}}]}}),"fixture","concurrent")
        from app.workflow.validation import PlanValidationError
        with self.h.context("改成10份"):
            with self.assertRaises(PlanValidationError) as caught:
                await plan_request(NS(generate=generate),self.h.registry,"改成10份",dialogue={},conditions={},stamp=STAMP)
        self.assertEqual(caught.exception.issue.code,"concurrent_plan_update")
        self.assertEqual(self.h.plan["revision"],1)

    async def test_semantic_deadline_does_not_issue_another_request(self):
        from app.workflow.scope_review import review
        from app.workflow.planning import RequestDraft
        draft=RequestDraft.model_validate({"steps":[{"id":"s","tool":"job_search_start","arguments":{"filters":monthly()}}]})
        model=fixture_model({})
        _,metadata,waits=await review(model,draft,message="月薪15k以上",live_plan=None,conditions={},timeout_seconds=.001)
        self.assertFalse(model.calls)
        self.assertEqual(metadata["status"],"unavailable")
        self.assertIn("s",waits)

    async def test_review_observes_range_overlap_witness_instead_of_point_only(self):
        from app.workflow.scope_review import salary_examples
        args={"filters":monthly()}
        args["filters"]["salary"]["range_mode"]="overlap"
        overlap=salary_examples(args)[0]
        self.assertEqual(overlap["acceptance"][-1]["status"],"pass")
        args["filters"]["salary"]["range_mode"]="contained"
        contained=salary_examples(args)[0]
        self.assertEqual(contained["acceptance"][-1]["status"],"fail")

    async def test_retrying_unavailable_review_rechecks_scope_even_when_arguments_unchanged(self):
        from app.workflow.planning import RequestDraft
        prior={"scope_review":{"waits":{"s":"服务暂不可用"}},"steps":[{"id":"s","tool":"job_search_start","arguments":{"filters":monthly()}}]}
        draft=RequestDraft.model_validate({"operation":"revise","revision":{"updates":[{"step_id":"s","arguments":{}}]}})
        self.assertEqual(proposals(draft,prior)[0]["changes"],{"filters":monthly()})


class TargetContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.jobs=JobStore(Path(self.temp.name)/"jobs.db")
        self.ids=[self.jobs.upsert_job({**job(i),"title":f"虚构Python后端工程师{i}","company":f"虚构公司{i}"})[0] for i in (1,2)]
        self.dialogue=apply_operation(None,{"op":"snapshot","topic":"applications","kind":"job","source":"fixture",
            "items":[{"id":i,"title":f"虚构Python后端工程师{j}","company":f"虚构公司{j}"} for j,i in enumerate(self.ids,1)]})
    async def asyncTearDown(self): self.temp.cleanup()

    async def test_ordinal_title_company_and_structured_reference_have_same_identity(self):
        selectors=["第1个","第1个 虚构Python后端工程师1","第1个 虚构公司1","第1个 虚构Python后端工程师1 虚构公司1",
            {"ordinal":1,"title":"虚构Python后端工程师1","company":"虚构公司1"}]
        for selector in selectors:
            result=await resolve_job_targets(selector,dialogue=self.dialogue,jobs=self.jobs)
            self.assertEqual([r["id"] for r in result["items"]],[self.ids[0]])

    async def test_structured_conflict_and_stale_snapshot_cannot_switch_jobs(self):
        for selector in ({"ordinal":1,"company":"虚构公司2"},{"ordinal":1,"snapshot_id":"stale"}):
            with self.assertRaises(TargetClarification):
                await resolve_job_targets(selector,dialogue=self.dialogue,jobs=self.jobs)

    async def test_structured_reference_keeps_user_ordinal_and_snapshot_evidence(self):
        with self.assertRaises(TargetClarification):
            await resolve_job_targets({"ordinal":1},dialogue=self.dialogue,jobs=self.jobs,instruction="投递第2个")
        for item in self.dialogue["snapshots"][-1]["items"]:
            item["content_hash"]=self.jobs.review_fingerprint(self.jobs.get_job(item["id"]))
        self.jobs.upsert_job({**job(1),"title":"虚构Python后端工程师1","company":"虚构公司1","description":"岗位描述已经变更，需要重新核对；这里是新的职位证据。"})
        with self.assertRaises(TargetClarification):
            await resolve_job_targets({"ordinal":1},dialogue=self.dialogue,jobs=self.jobs)

    async def test_semantic_fallback_cannot_override_locked_ordinal(self):
        async def malicious(request):
            return LLMResult(json.dumps({"status":"resolved","matches":[{"ordinal":2,"evidence":[{"field":"company","quote":"虚构公司2"}]}]},ensure_ascii=False),"fixture","scripted")
        with self.assertRaises(TargetClarification):
            await resolve_job_targets("第1个 与第二家公司有关",dialogue=self.dialogue,jobs=self.jobs,generate=malicious)


class MatchingContracts(unittest.IsolatedAsyncioTestCase):
    def request(self):
        return JobAnalysisRequest(title="虚构后端工程师",description="【合成JD】必须硕士学历和五年工作经验。负责后端接口设计与质量检查。",
            candidate_facts={"document_id":"fixture","digest":"fixed-digest","documents":{"education":"学历：本科","years_experience":"相关工作经验1年"}})
    async def test_default_qualification_failure_is_advisory_without_changing_match(self):
        async def generate(request):
            return LLMResult(json.dumps({"score":99,"decision":"APPLY","matched_skills":[],"missing_skills":[],"reasoning":"synthetic",
                "requirements":[{"name":"学历","status":"fail","job_quote":"必须硕士学历","candidate_evidence":[{"field":"education","quote":"学历：本科"}]}]},ensure_ascii=False),"fixture","scripted")
        result=await match_job(generate,self.request())
        self.assertEqual(result.qualification["status"],"fail")
        self.assertEqual(result.score,99)
        self.assertEqual(result.decision,"APPLY")
        self.assertEqual(result.qualification["policy"],"advisory")

    async def test_invented_or_missing_candidate_evidence_stays_unknown(self):
        analysis=JobAnalysis(score=99,decision="APPLY",matched_skills=[],missing_skills=[],reasoning="test",requirements=[
            {"name":"学历","status":"pass","job_quote":"必须硕士学历","candidate_evidence":[{"field":"education","quote":"硕士"}]}])
        result=verify_requirements(analysis,self.request(),assessed=True)
        self.assertEqual(result.qualification["status"],"unknown")
        self.assertEqual(qualification_status({"qualification_evidence":json.dumps(result.qualification)},"fixed-digest"),"unknown")

    async def test_projection_excludes_raw_resume_identity_and_unquoted_profile(self):
        document=ResumeDocumentView(raw_content="姓名：虚构人\n电话：13800138000\n学历：本科\n其他未批准内容",entries=[
            ResumeEntryView(id="project",kind="project",title="虚构项目",summary="实现接口；电话：13800138000")])
        profile=NS(qualification_facts=[{"field":"education","value":"本科","quote":"学历：本科"},
                                         {"field":"years_experience","value":6,"quote":"不存在的六年"}])
        result=match_facts(NS(read_document=lambda:document),NS(read=lambda:NS(profile=profile)))
        outbound=json.dumps(result,ensure_ascii=False)
        self.assertNotIn("13800138000",outbound)
        self.assertNotIn("其他未批准内容",outbound)
        self.assertNotIn("虚构人",outbound)
        self.assertNotIn("years_experience",result["documents"])
        self.assertIn("education",result["documents"])

    async def test_partial_upsert_preserves_qualification_and_changed_resume_invalidates_it(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            store=JobStore(Path(directory)/"jobs.db")
            evidence={"status":"fail","assessed":True,"resume_digest":"original-document"}
            ident,_=store.upsert_job({**job(1),"qualification_evidence":evidence})
            store.upsert_job(job(1))
            self.assertEqual(json.loads(store.get_job(ident)["qualification_evidence"]),evidence)
            self.assertEqual(qualification_status(store.get_job(ident),"original-document"),"fail")
            self.assertEqual(qualification_status(store.get_job(ident),"changed-document"),"unknown")

    async def test_actual_batch_queue_uses_user_scope_not_default_qualification_veto(self):
        from evaluation.delivery_evidence_contract import PersistedFlow
        fixture=PersistedFlow("test_search_enrichment_then_queue_has_no_second_model_verdict")
        await fixture.asyncSetUp()
        try:
            fixture.context.owner=asyncio.current_task()
            await fixture.seed(qualification_statuses=("fail","unknown"))
            result=await fixture.coordinator.queue_deliveries(session_id="default",task_id="search",min_score=85)
            self.assertEqual(result["queued"],2)
            self.assertEqual(result["insufficient"],0)
            self.assertEqual(result["no_match"],0)
            self.assertEqual(len(fixture.store.mock_delivery_queue()),2)
            # Widening the policy must not manufacture positive evidence.
            self.assertEqual([json.loads(fixture.store.get_job(i)["qualification_evidence"])["status"]
                             for i in (1,2)], ["fail","unknown"])
        finally:
            await fixture.asyncTearDown()


if __name__=="__main__":
    unittest.main()
