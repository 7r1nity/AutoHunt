"""Offline Jev contracts. HTTP is mocked; no labels are claimed as real Jev."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from evaluation.benchmark.common import PhysicalBudget
from . import corpus,protocol,runner

CONFIG={"api_key":"fixture-secret-never-real","model":"jev-1.14.0","timeout":1}


def response(request):
    answers={}
    for key,q in request["questions"].items():
        choice="1" if key=="relevance" else "partial" if key.endswith("alignment") else "unknown"
        if key.endswith("_evidence"): choice=next(iter(q["criteria"]))
        answers[key]={"type":"choice","choice":choice,"confidence":.95,
                      "probabilities":{k:float(k==choice) for k in q["criteria"]}}
    return {"model":"jev-1.14.0","answers":answers,"usage":{"input_tokens":100,"output_tokens":10}}


class DataContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.data=corpus.build()

    def test_frozen_data_and_shape(self):
        self.assertEqual(self.data,corpus.load())
        self.assertEqual([len(self.data[k]) for k in ("candidates","jobs","queries","pairs")],[15,300,15,300])
        self.assertEqual(len({p["id"] for p in self.data["pairs"]}),300)
        self.assertTrue(all(p["relevance"] is None and p["eligibility"] is None for p in self.data["pairs"]))

    def test_same_occupation_pools_have_multiple_design_levels_not_fake_labels(self):
        jobs={j["id"]:j for j in self.data["jobs"]}; candidates={c["id"]:c for c in self.data["candidates"]}
        intentions={d["pair_id"]:d for d in self.data["design_intents"]}
        for q in self.data["queries"]:
            self.assertEqual(len(set(q["pool"])),20)
            self.assertTrue(all(jobs[j]["family"]==candidates[q["candidate_id"]]["family"] for j in q["pool"]))
            rows=[p for p in self.data["pairs"] if p["candidate_id"]==q["candidate_id"]]
            self.assertEqual({intentions[p["id"]]["intended_relevance"] for p in rows},{None,0,1,2})

    def test_candidate_split_is_disjoint_and_source_spans_are_exact(self):
        groups={s:{p["candidate_id"] for p in self.data["pairs"] if p["split"]==s} for s in ("development","test")}
        self.assertFalse(groups["development"]&groups["test"])
        self.assertEqual([len(groups[s]) for s in ("development","test")],[8,7])
        for record in self.data["candidates"]+self.data["jobs"]:
            self.assertTrue(all(quote in record["text"] for quote in record["lines"].values()))

    def test_design_intents_scores_and_ids_never_cross_to_judge(self):
        p=self.data["pairs"][0]; r=protocol.request_for(self.data,p,CONFIG["model"])
        wire=json.dumps(r,ensure_ascii=False)
        for forbidden in ("design_intents","intended_relevance","case_family","human_gold",p["id"],p["candidate_id"],p["job_id"]):
            self.assertNotIn(forbidden,wire)
        changed=copy.deepcopy(self.data); changed["design_intents"]=[{"intended_relevance":999}]
        changed["pairs"][0].update(relevance=2,eligibility="pass",score=100)
        self.assertEqual(r,protocol.request_for(changed,p,CONFIG["model"]))
        self.assertEqual(set(r),{"model","state","questions"})

    def test_typed_response_and_source_evidence(self):
        p=self.data["pairs"][0]; request=protocol.request_for(self.data,p,CONFIG["model"])
        row=protocol.annotation(response(request),request,p)
        self.assertEqual(row["relevance"],1)
        self.assertFalse(row["human_gold"])
        self.assertTrue(all(e["quote"] in request["state"][e["source"]]["text"] for e in row["evidence"]))
        self.assertIn("candidate_boilerplate_evidence_needs_review",row["review_flags"])

    def test_malformed_or_invented_evidence_does_not_become_a_label(self):
        p=self.data["pairs"][0]; request=protocol.request_for(self.data,p,CONFIG["model"])
        for kind in ("choice","probabilities","confidence","missing","evidence","usage"):
            with self.subTest(kind=kind):
                raw=response(request)
                if kind=="choice": raw["answers"]["relevance"]["choice"]="highest"
                elif kind=="probabilities": raw["answers"]["relevance"]["probabilities"]["1"]=.2
                elif kind=="confidence": raw["answers"]["relevance"]["confidence"]=float("nan")
                elif kind=="missing": raw["answers"].pop("education")
                elif kind=="evidence": raw["answers"]["candidate_evidence"]["choice"]="c999"
                else: raw["usage"]["input_tokens"]=-1
                with self.assertRaises(ValueError): protocol.annotation(raw,request,p)

    def test_conflicts_are_reviewed_not_silently_relabelled(self):
        p=self.data["pairs"][0]; request=protocol.request_for(self.data,p,CONFIG["model"]); raw=response(request)
        raw["answers"]["relevance"].update(choice="2",probabilities={"0":0.,"1":0.,"2":1.})
        result=protocol.annotation(raw,request,p)
        self.assertEqual(result["relevance"],2)
        self.assertIn("high_relevance_without_verified_hard_conditions",result["review_flags"])
        self.assertEqual(result["status"],"needs_review")


class RunnerContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root=(corpus.ROOT/"evaluation/results/jev-contracts").resolve(); root.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix="case-",dir=root)
        self.path=Path(self.temp.name).resolve(); self.assertTrue(self.path.is_relative_to(root))
        self.called=[]
    async def asyncTearDown(self): self.temp.cleanup()

    def transport(self,status=200):
        def handle(req):
            self.called.append(req)
            self.assertEqual(str(req.url),runner.ENDPOINT)
            self.assertEqual(req.headers["Authorization"],"Bearer "+CONFIG["api_key"])
            return httpx.Response(status,json=response(json.loads(req.content)))
        return httpx.MockTransport(handle)

    async def test_missing_key_or_consent_spends_nothing(self):
        for consent,key,status in ((False,"present","awaiting_model_consent"),(True,"","awaiting_api_key")):
            result=await runner.annotate(self.path,{**CONFIG,"api_key":key},limit=1,consented=consent,transport=self.transport())
            self.assertEqual(result["status"],status); self.assertEqual(result["physical_requests"],0)
        self.assertFalse(self.called)

    async def test_success_is_journalled_and_resume_does_not_call_again(self):
        result=await runner.annotate(self.path,CONFIG,limit=1,consented=True,transport=self.transport())
        self.assertEqual(result["annotated_pairs"],1); self.assertEqual(len(self.called),1)
        self.assertNotIn(CONFIG["api_key"],(self.path/"model-calls.jsonl").read_text(encoding="utf-8"))
        (self.path/"annotations.json").unlink()  # Crash after response, before derived checkpoint.
        resumed=await runner.annotate(self.path,CONFIG,limit=1,consented=True,transport=self.transport())
        self.assertEqual(resumed["annotated_pairs"],1); self.assertEqual(len(self.called),1)
        self.assertFalse((self.path/"annotations.json").read_text(encoding="utf-8").find('"human_gold": true')>=0)

    async def test_transient_retry_counts_each_physical_request(self):
        def handle(req):
            self.called.append(req)
            return httpx.Response(529 if len(self.called)==1 else 200,json=response(json.loads(req.content)))
        async def no_wait(_): pass
        result=await runner.annotate(self.path,CONFIG,limit=2,consented=True,transport=httpx.MockTransport(handle),sleep=no_wait)
        self.assertEqual(result["physical_requests"],2); self.assertEqual(result["annotated_pairs"],1)

    async def test_authentication_and_schema_errors_stop_batch(self):
        for status in (401,403,422):
            output=self.path/str(status)
            result=await runner.annotate(output,CONFIG,limit=3,consented=True,transport=self.transport(status))
            self.assertEqual(result["physical_requests"],1); self.assertEqual(result["annotated_pairs"],0)
            self.assertTrue(result["status"].startswith("stopped_"))

    async def test_malformed_responses_have_bounded_failure_threshold(self):
        def handle(req): self.called.append(req); return httpx.Response(200,json={"model":CONFIG["model"],"answers":{}})
        result=await runner.annotate(self.path,CONFIG,limit=10,consented=True,transport=httpx.MockTransport(handle))
        self.assertEqual(result["physical_requests"],3); self.assertEqual(result["status"],"stopped_consecutive_failures")

    async def test_nonobject_response_and_model_change_are_not_labels(self):
        for name,raw in (("list",["bad"]),("version",{"model":"jev-other","answers":{}})):
            with self.subTest(name=name):
                def handle(req): return httpx.Response(200,json=raw)
                out=self.path/name
                result=await runner.annotate(out,CONFIG,limit=3,consented=True,transport=httpx.MockTransport(handle))
                self.assertEqual(result["annotated_pairs"],0)
                self.assertFalse(runner.restore(corpus.load(),out,CONFIG,.7))
                self.assertEqual(result["status"],"stopped_model_binding" if name=="version" else "stopped_consecutive_failures")

    async def test_manifest_budget_and_model_changes_cannot_reset_run(self):
        runner.prepare(self.path,CONFIG,2)
        for config,limit in (({**CONFIG,"model":"jev-1.13.0"},2),(CONFIG,3)):
            with self.assertRaises(ValueError): runner.prepare(self.path,config,limit)
        self.assertEqual(PhysicalBudget(self.path/"budget.json",2,ceiling=330).state()["used"],0)

    async def test_review_queue_is_blind_and_development_only(self):
        data,budget=runner.prepare(self.path,CONFIG,2)
        tasks=[json.loads(line) for line in (self.path/"human-calibration.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(tasks),64)
        self.assertTrue(all(row["split"]=="development" for row in tasks))
        self.assertTrue(all(row["reviewer_a"]["relevance"] is None for row in tasks))
        self.assertTrue(all("intended_relevance" not in row and "confidence" not in row for row in tasks))

    async def test_bad_result_path_and_exclusive_run(self):
        with self.assertRaises(ValueError): runner.result_path(corpus.ROOT/"data")
        with runner.lease(self.path):
            with self.assertRaises((RuntimeError,OSError)):
                with runner.lease(self.path): pass

    async def test_explicit_new_ceiling_does_not_expand_old_runner_default(self):
        with self.assertRaises(ValueError): PhysicalBudget(self.path/"old.json",301)
        budget=PhysicalBudget(self.path/"new.json",330,ceiling=330)
        self.assertEqual(budget.state()["limit"],330)

    async def test_proxy_endpoint_uses_eight_questions_and_preserves_failed_budget(self):
        old=await runner.annotate(self.path,CONFIG,limit=3,consented=True,transport=self.transport(401))
        self.assertEqual(old["physical_requests"],1)
        proxy={**CONFIG,"endpoint":runner.PROXY_ENDPOINT}
        def handle(req):
            self.assertEqual(str(req.url),runner.PROXY_ENDPOINT)
            body=json.loads(req.content)
            self.assertEqual(len(body["questions"]),8)
            self.assertNotIn("project_alignment",body["questions"])
            self.assertIn("Idempotency-Key",req.headers)
            return httpx.Response(200,json=response(body))
        result=await runner.annotate(self.path,proxy,limit=3,consented=True,transport=httpx.MockTransport(handle),endpoint_migration=True)
        self.assertEqual(result["physical_requests"],3)
        self.assertEqual(result["annotated_pairs"],2)
        history=json.loads((self.path/"manifest-revisions.jsonl").read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(history["physical_used_preserved"],1)
        self.assertEqual(history["old"]["endpoint"],runner.ENDPOINT)
        self.assertEqual(history["new"]["endpoint"],runner.PROXY_ENDPOINT)

    async def test_credits_stop_and_successful_labels_cannot_switch_endpoint(self):
        credits=await runner.annotate(self.path/"empty",CONFIG,limit=3,consented=True,transport=self.transport(402))
        self.assertEqual(credits["physical_requests"],1)
        self.assertEqual(credits["status"],"stopped_insufficient_credits")
        valid=self.path/"valid"
        await runner.annotate(valid,CONFIG,limit=1,consented=True,transport=self.transport())
        with self.assertRaises(ValueError): runner.migrate_endpoint(valid,{**CONFIG,"endpoint":runner.PROXY_ENDPOINT},1)

    async def test_prediction_input_is_sourced_and_partial_reference_has_no_metrics(self):
        from .evaluate import matcher_input,checked_labels
        data=corpus.load(); request=matcher_input(data["candidates"][0],data["jobs"][0])
        self.assertTrue(all(s in data["candidates"][0]["text"] for s in request.candidate_facts["documents"].values()))
        runner.prepare(self.path,CONFIG,1)
        budget=PhysicalBudget(self.path/"budget.json",1)
        runner.save_summary(data,self.path,budget,{},status="awaiting_api_key")
        with self.assertRaises(ValueError): checked_labels(data,self.path)


if __name__=="__main__": unittest.main()
