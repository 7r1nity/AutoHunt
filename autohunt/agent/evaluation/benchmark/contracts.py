"""Harness contracts and a zero-external-call browser/backend pilot."""
from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from app.llm.base import LLMResult
from .common import PhysicalBudget, BudgetExceeded, ranking_metrics
from .corpus import build, digest
from .analyze import tie_metrics
from .planning import salary_scope_exact, check_plan
from .audit import backend_assertions, expected_resume


class Contracts(unittest.TestCase):
    def test_corpus_counts_and_unique_pairs(self):
        data=build()
        self.assertEqual([len(data[k]) for k in ("resumes","jobs","pairs","planning","execution")],[30,100,450,20,24])
        self.assertEqual(len({p["id"] for p in data["pairs"]}),450)
        self.assertEqual(sum(q["split"]=="challenge" for q in data["queries"]),12)
        self.assertTrue(all(len(q["pool"])==len(set(q["pool"]))==15 for q in data["queries"]))
        self.assertTrue(all(p["human_review"]=="pending" for p in data["pairs"]))
        self.assertEqual(digest(data),digest(build()))

    def test_unknown_not_negative_hard_fact(self):
        rows=[p for p in build()["pairs"] if p["candidate_id"].endswith("-2")]
        self.assertTrue(rows); self.assertTrue(all(p["eligibility"]=="unknown" for p in rows))

    def test_metric_ideal_and_reversed_and_no_positive(self):
        self.assertEqual(ranking_metrics([2,1,0],[3,2,1],["pass"]*3)["ndcg_at_5"],1)
        self.assertLess(ranking_metrics([2,1,0],[1,2,3],["pass"]*3)["ndcg_at_5"],1)
        self.assertIsNone(ranking_metrics([0]*5,[1]*5,["fail"]*5)["ndcg_at_5"])
        self.assertIsNone(ranking_metrics([0]*5,[1]*5,["fail"]*5)["recall_at_5_pool"])

    def test_physical_budget_persists_and_cannot_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"budget.json"
            first=PhysicalBudget(path,2); self.assertEqual(first.reserve(),1)
            second=PhysicalBudget(path,2); self.assertEqual(second.reserve(),2)
            with self.assertRaises(BudgetExceeded): second.reserve()
            with self.assertRaises(ValueError): PhysicalBudget(path,3)

    def test_critical_safety_and_capability_cases_present(self):
        names={c["id"] for c in build()["execution"]}
        self.assertTrue({"wrong_account","captcha","resume_changed","duplicate_request","submit_response_lost","button_id_changed","extra_required_step"}<=names)

    def test_ties_do_not_gain_from_fixture_order(self):
        tied={"labels":[2,0,1,0,2,1],"scores":[50]*6,"eligibility":["pass"]*6}
        reversed_row={key:list(reversed(value)) for key,value in tied.items()}
        self.assertEqual(tie_metrics(tied),tie_metrics(reversed_row))
        self.assertAlmostEqual(tie_metrics(tied)["precision_at_5"],4/6)

    def test_typed_salary_is_equivalent_but_negotiable_is_not(self):
        from app.core.salary_conditions import normalized_search_filters
        tree=normalized_search_filters(None,15000)
        tree["salary"]["origin"]="model"
        self.assertTrue(salary_scope_exact({"salary_min":15000},15000))
        self.assertTrue(salary_scope_exact({"filters":tree},15000))
        self.assertFalse(salary_scope_exact({"filters":{"op":"any","children":[tree,{"op":"condition","text":"薪资面议","salary":{"unit":"negotiable"}}]}},15000))

    def test_final_database_checks_content_and_hr_order(self):
        expected=expected_resume()
        records={"applications":[{"applicant_id":3,"resume_id":5,"job_id":9,"created_at":"2026-10-26 01:00:01"}],
            "resumes":[{"id":5,"applicant_id":3,**expected}],
            "messages":[{"sender_role":"employer","recipient_id":3,"job_id":9,"created_at":"2026-10-26 01:00:00"}]}
        self.assertTrue(backend_assertions(records,expected)["resume_projection_matches_reference"])
        self.assertTrue(backend_assertions(records,expected)["hr_reply_before_submission"])
        records["resumes"][0]["summary"]="different document"
        self.assertFalse(backend_assertions(records,expected)["resume_projection_matches_reference"])
        records["messages"]=[]
        self.assertFalse(backend_assertions(records,expected)["hr_reply_before_submission"])

    def test_dependency_may_be_transitive(self):
        plan={"steps":[
            {"id":"search","tool":"job_search_start","arguments":{"result_limit":15,"salary_min":15000},"status":"waiting_external","depends_on":[]},
            {"id":"report","tool":"job_results_read","arguments":{},"status":"pending","depends_on":["search"]},
            {"id":"send","tool":"mock_delivery_start","arguments":{"min_score":85},"status":"pending","depends_on":["report"]}]}
        checks=check_plan(plan,{"tool":"mock_delivery_start","preserve_search":True,"min_score":85},{},None)
        self.assertTrue(all(checks.values()),checks)

    def test_paused_staged_revision_is_update_only(self):
        plan={"paused":True,"steps":[{"id":"s","tool":"job_search_start","arguments":{"queries":["Python后端"]},
            "staged_arguments":{"queries":["金融分析师"]},"status":"waiting_external"}]}
        self.assertTrue(all(check_plan(plan,{"tool":"search_state_update","no_search":True},{},None).values()))


async def pilot(output,cases):
    from .execution import run
    async def generate(request):
        return LLMResult(json.dumps({"steps":[{"id":"apply","tool":"mock_delivery_start","arguments":{"selector":"第1个"},"user_requested":True}],
            "goals":[],"questions":[]}),"scripted","harness-pilot")
    output=Path(output).resolve(); output.mkdir(parents=True,exist_ok=True)
    summary=await run(build(),output,NS(generate=generate),repetitions=1,cases=cases or ["normal"])
    print(json.dumps(summary,ensure_ascii=False))


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--pilot-output")
    parser.add_argument("--cases",nargs="*")
    args=parser.parse_args()
    if args.pilot_output: asyncio.run(pilot(args.pilot_output,args.cases))
    else: unittest.main(argv=[__file__])
