"""Metric denominator checks; synthetic traces, no model/browser requests."""
import unittest
import copy

from evaluation.benchmark.quantify import durations, execution_metrics, fraction
from evaluation.benchmark.planning import inherited_search_scope
from evaluation import delivery_evidence_contract as evidence
from evaluation.search_delivery_contract import job
from app.core.models import BrowserTaskStatus


class Metrics(unittest.TestCase):
    def test_unchanged_bound_search_is_a_scope_not_an_omitted_filter(self):
        search={"id":"search","tool":"job_search_start","task_id":"t1","depends_on":[],
                "arguments":{"queries":["analyst"],"locations":["Shenzhen"],"filters":{"op":"condition","text":"direct employment"}}}
        send={"id":"send","tool":"mock_delivery_start","depends_on":["search"],"evidence_step":"search","arguments":{"min_score":85}}
        plan={"steps":[search,send],"verified_conditions":copy.deepcopy(search["arguments"])}
        self.assertTrue(inherited_search_scope(plan,send))
        for changes in ({"source_task_id":"foreign"},{"selector":"first job"}):
            self.assertFalse(inherited_search_scope(plan,{**send,"arguments":changes}))
        different=copy.deepcopy(plan); different["steps"][0]["arguments"]["locations"]=["Shanghai"]
        self.assertFalse(inherited_search_scope(different,send))
        unbound=copy.deepcopy(plan); unbound["steps"][1]["depends_on"]=[]
        self.assertFalse(inherited_search_scope(unbound,unbound["steps"][1]))

    def test_undefined_denominator_is_not_perfect_success(self):
        self.assertIsNone(fraction(0,0)["rate"])
        self.assertNotIn("wilson95",fraction(0,0))

    def test_finite_perfect_sample_still_has_uncertainty(self):
        result=fraction(28,28)
        self.assertEqual(result["rate"],1)
        self.assertLess(result["wilson95"][0],.9)

    def test_duration_measures_mean_and_tail_separately(self):
        value=durations([100,100,1000])
        self.assertEqual(value["mean_ms"],400)
        self.assertEqual(value["p50_ms"],100)
        self.assertEqual(value["p95_ms"],1000)

    def test_protected_success_cannot_inflate_delivery_or_recovery(self):
        cases=[]; audited=[]
        for ident,scenario,positive,success,status in (
            ("ok","normal",True,True,"ok"),
            ("bad","normal",True,False,"awaiting_user"),
            ("wait","hr_no_reply",False,True,"ok"),
            ("recovered","submit_response_lost",True,True,"ok")):
            cases.append({"id":ident,"method":"agent","scenario":scenario,"latency_ms":100,
                "planning_ms":20,"agent_trace":{"events":[{"kind":"tool_finished","data":{
                    "tool":"mock_delivery_start","status":status,"duration_ms":1}}]}})
            audited.append({"id":ident,"positive_case":positive,"task_success":success,
                "post_attempts_above_one":0,"assertions":{"duplicate_application_records":0}})
        result=execution_metrics(cases,{"cases":audited},[{"id":"submit_response_lost","expect_recovery":True}])["agent"]
        self.assertEqual(result["task_success"]["rate"],3/4)
        self.assertEqual(result["end_to_end_delivery_success"]["rate"],2/3)
        self.assertEqual(result["raw_tool_success"]["rate"],3/4)
        self.assertEqual(result["protected_case_handling"]["total"],1)
        self.assertEqual(result["fault_recovery"]["total"],1)
        self.assertEqual(result["fault_recovery"]["rate"],1)


class SearchScope(unittest.IsolatedAsyncioTestCase):
    async def test_real_queue_without_duplicate_filters_excludes_other_search_jobs(self):
        fixture=evidence.PersistedFlow()
        await fixture.asyncSetUp()
        try:
            await fixture.seed()
            fixture.store.create_task("foreign-search","mock","synthetic",resume_id="synthetic-resume")
            foreign=fixture.store.admit_search_result("foreign-search",job(99),limit=10,groups=None)["job_id"]
            fixture.store.update_task("foreign-search",status=BrowserTaskStatus.COMPLETED)
            result=await fixture.coordinator.queue_deliveries(session_id="default",task_id="search",min_score=85)
            self.assertEqual(result["queued"],2)
            self.assertNotIn(foreign,result["job_ids"])
        finally:
            await fixture.asyncTearDown()


if __name__=="__main__": unittest.main()
