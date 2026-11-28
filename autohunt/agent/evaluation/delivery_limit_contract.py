"""Bounded batch queue contracts using isolated stores and synthetic evidence.

No model provider, browser, personal document, existing queue or recruitment
operation is used. Run: python -m evaluation.delivery_limit_contract
"""
from __future__ import annotations

import asyncio
import gc
import hashlib
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from app.core.models import BrowserTaskStatus, ResumeDocumentView
from app.jobs.store import JobStore
from app.memory.resume_workspace import resume_document_digest
from app.runtime.context import RunContext, current_run
from app.runtime.resilience import PreflightRefused
from app.workflow.mock_coordinator import MockWorkflowCoordinator


TRACES = []


class DeliveryLimitContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="autohunt-delivery-limit-")
        self.path = Path(self.temp.name) / "jobs.db"
        self.jobs = JobStore(self.path)
        self.document = ResumeDocumentView(id="synthetic-doc", version=1)
        self.candidate = NS(read=lambda: NS(profile=NS(resume_id="synthetic-resume")))
        self.resume = NS(read_document=lambda: self.document)
        self.model = AsyncMock(side_effect=AssertionError("No external or synthetic model call is needed"))
        self.coordinator = MockWorkflowCoordinator(self.jobs, NS(llm=NS(generate=self.model)),
            NS(supports=lambda url: url.startswith("http://127.0.0.1:8080/")), self.candidate, self.resume)
        self.coordinator._ensure_applicant_bound = AsyncMock(return_value="bound")
        self.events = []
        self.context = RunContext("synthetic-limit-run", asyncio.current_task(),
            lambda kind, data: self.events.append({"kind": kind, **data}), lambda _: None,
            session_id="default", dialogue={}, user_message="投递20个")
        self.token = current_run.set(self.context)
        self.make_search("current")

    async def asyncTearDown(self):
        current_run.reset(self.token)
        TRACES.append({"test": self.id(), "events": self.events})
        self.coordinator = self.jobs = None
        gc.collect()
        self.temp.cleanup()

    def make_search(self, task_id, *, session_id="default", resume_id="synthetic-resume"):
        self.jobs.create_task(task_id, "mock", "虚构算法岗位", resume_id=resume_id)
        self.jobs.save_search_request(task_id, {"conversation_id": session_id})
        self.jobs.update_task(task_id, status=BrowserTaskStatus.COMPLETED)

    def seed(self, count=1, *, task_id="current", score=90, qualification="pass", filter_status="pass",
             salary="25-30K", stage="review", start=0):
        ids = []
        for position in range(start, start + count):
            ident = f"{task_id}-{position}"
            job = {"source": "MockJobSite", "source_url": f"http://127.0.0.1:8080/job.html?id={task_id}-{position}",
                "external_id": ident, "title": f"合成算法岗位{ident}", "company": "虚构公司", "location": "深圳",
                "salary": salary, "description": "本岗位为正式全职。", "source_tags": ["Python"], "tags": ["Python"],
                "score": score, "decision": "REVIEW", "reasoning": "合成判定", "stage": stage,
                "filter_status": filter_status, "filter_evidence": {"status": filter_status, "conditions": []},
                "qualification_evidence": {"status": qualification, "assessed": True, "requirements": [],
                    "resume_digest": resume_document_digest(self.document)}}
            if qualification == "legacy":
                # Optional qualification advice may be absent while a valid
                # versioned search receipt still proves the exact resume used.
                job["qualification_evidence"] = None
                original_receipt = self.jobs._analysis_receipt
                def bound_receipt(row):
                    result = original_receipt(row)
                    result["analysis_receipt"]["resume_digest"] = resume_document_digest(self.document)
                    result["analysis_receipt_digest"] = hashlib.sha256(
                        self.jobs._canonical_snapshot(result["analysis_receipt"]).encode()).hexdigest()
                    return result
                with patch.object(self.jobs, "_analysis_receipt", bound_receipt):
                    receipt = self.jobs.admit_search_result(task_id, job, limit=None, groups=None)
            else:
                receipt = self.jobs.admit_search_result(task_id, job, limit=None, groups=None)
            ids.append(receipt["job_id"])
        return ids

    async def queue(self, **kwargs):
        self.context.owner = asyncio.current_task()
        result = await self.coordinator.queue_deliveries(session_id="default", task_id="current", limit=20, **kwargs)
        TRACES.append({"test": self.id(), "result": result})
        return result

    async def test_twenty_five_eligible_enqueue_exactly_twenty(self):
        ids = self.seed(25)
        result = await self.queue()
        self.assertEqual(result["queued"], 20)
        self.assertEqual(result["eligible_count"], 25)
        self.assertEqual(result["selected_job_ids"], ids[:20])
        self.assertEqual(result["shortfall"], 0)
        self.assertEqual(result["remaining_eligible"], 5)
        self.assertEqual(len(self.jobs.mock_delivery_queue()), 20)

    async def test_one_eligible_is_queued_and_nineteen_shortfall_reported(self):
        ids = self.seed()
        result = await self.queue()
        self.assertEqual(result["job_ids"], ids)
        self.assertEqual((result["requested_limit"], result["queued"], result["shortfall"]), (20, 1, 19))
        self.assertIn("尚差19个", result["message"])
        self.assertNotIn("请补充", result["message"])

    async def test_delivery_failure_reason_is_delivered_once_to_chat(self):
        from app.core.models import DeliveryTaskStatus
        ident = self.seed()[0]
        await self.queue()
        self.jobs.update_mock_delivery(ident, "dispatched", "合成启动", run_id="synthetic-failure")
        self.coordinator.browser.worker = self.coordinator.delivery.worker = None
        self.jobs.get_delivery_run = lambda _: NS(status=DeliveryTaskStatus.FAILED, submitted=False,
            detail="岗位已关闭，发送按钮不存在", job_id=ident)
        await self.coordinator.process_delivery_queue()
        await self.coordinator.process_delivery_queue()
        messages = self.jobs.conversation_history(conversation_id="default", limit=20)
        self.assertEqual(len(messages), 1)
        self.assertIn("岗位已关闭，发送按钮不存在", messages[0]["content"])
        self.assertIn("不会重试", messages[0]["content"])
        self.assertEqual(self.jobs.mock_delivery_queue(active_only=False)[0]["status"], "blocked")

    async def test_empty_round_reports_twenty_shortfall_without_binding_search(self):
        result = await self.queue()
        self.assertEqual(result["queued"], 0)
        self.assertEqual(result["shortfall"], 20)
        self.assertEqual(result["selected_job_ids"], [])
        self.coordinator._ensure_applicant_bound.assert_not_awaited()
        self.assertFalse(self.coordinator.wakeup.is_set())
        self.assertFalse(any(event["kind"] == "effect_checkpoint" for event in self.events))

    async def test_score_gate_precedes_limit_while_qualification_is_advisory(self):
        self.seed(8, score=60)
        self.seed(8, qualification="unknown", start=10)
        self.seed(8, qualification="fail", start=20)
        wanted = self.seed(25, score=95, start=30)
        result = await self.queue(min_score=85)
        self.assertEqual(result["selected_job_ids"], wanted[:20])
        self.assertEqual(result["eligible_count"], 41)
        self.assertEqual(result["queued"], 20)

    async def test_default_qualification_advisories_do_not_block_bounded_batches(self):
        expected = {}
        for index, status in enumerate(("fail", "unknown", "legacy")):
            for ident in self.seed(2, qualification=status, start=index * 10):
                expected[ident] = status
        result = await self.queue()
        self.assertEqual((result["queued"], result["eligible_count"], result["shortfall"]), (6, 6, 14))
        self.assertEqual((result["excluded"], result["unknown"], result["insufficient"]), (0, 0, 0))
        self.assertEqual({item["job_id"]: item["status"] for item in result["qualification_advisories"]}, expected)
        for row in self.jobs.mock_delivery_queue():
            advisory = row["scope_evidence"]["qualification_advisory"]
            self.assertEqual(advisory["status"], expected[row["job_id"]])
            frozen = row["eligibility_snapshot"]["qualification_evidence"]
            if expected[row["job_id"]] == "legacy":
                self.assertIn(frozen, (None, "null"))
            else:
                self.assertEqual(json.loads(frozen)["status"], expected[row["job_id"]])
            self.assertIsNotNone(self.jobs.assert_delivery_queue_facts(row))

    async def test_explicit_single_selection_needs_no_qualification_exception(self):
        ids = []
        for index, status in enumerate(("fail", "unknown", "legacy")):
            ids.extend(self.seed(1, qualification=status, start=index * 10))
        result = await self.coordinator.queue_deliveries(session_id="default", job_ids=ids,
            selection_evidence={"source": "ui_click"})
        self.assertEqual(result["job_ids"], ids)
        self.assertEqual(result["reason"], "queued")
        self.assertNotIn("outside_targets", result)
        self.assertEqual((result["excluded"], result["unknown"], result["insufficient"]), (0, 0, 0))

    async def test_explicit_user_salary_filter_still_excludes_qualification_advisories(self):
        for index, status in enumerate(("fail", "unknown", "legacy")):
            self.seed(1, qualification=status, salary="15-18K", start=index * 10)
        result = await self.queue(filters={"op": "condition", "salary": {"unit": "monthly", "minimum": 20000}})
        self.assertEqual((result["queued"], result["shortfall"], result["excluded"]), (0, 20, 3))
        self.assertEqual((result["unknown"], result["insufficient"]), (0, 0))
        self.coordinator._ensure_applicant_bound.assert_not_awaited()

    async def test_unknown_explicit_user_filter_still_requires_evidence(self):
        self.seed(1, qualification="fail")
        async def unknown(*args, **kwargs):
            return {"status": "unknown", "conditions": [{"status": "unknown", "reason": "用户明确条件缺少证据"}]}
        with patch("app.core.job_filters.evaluate_filter", unknown):
            result = await self.queue(filters={"op": "condition", "text": "用户明确要求的企业福利", "quote": "企业福利"})
        self.assertEqual((result["queued"], result["shortfall"], result["unknown"]), (0, 20, 1))
        self.assertEqual(result["insufficient"], 1)
        self.assertEqual(result["qualification_advisories"], [])
        self.coordinator._ensure_applicant_bound.assert_not_awaited()

    async def test_independent_salary_filter_applies_before_limit(self):
        self.seed(9, salary="15-18K")
        wanted = self.seed(25, salary="25-30K", start=10)
        expression = {"op": "condition", "salary": {"unit": "monthly", "minimum": 20000}}
        result = await self.queue(filters=expression)
        self.assertEqual(result["excluded"], 9)
        self.assertEqual(result["selected_job_ids"], wanted[:20])
        self.assertEqual(result["queued"], 20)
        self.model.assert_not_awaited()

    async def test_duplicate_history_is_skipped_before_limiting(self):
        ids = self.seed(25)
        original = self.jobs.latest_delivery_status_for_job
        self.jobs.latest_delivery_status_for_job = lambda ident: "completed" if ident in ids[:3] else original(ident)
        result = await self.queue()
        self.assertEqual(result["already_delivered"], 3)
        self.assertEqual(result["selected_job_ids"], ids[3:23])
        self.assertEqual(result["queued"], 20)

    async def test_existing_queue_is_not_reused_as_new_batch_slots(self):
        ids = self.seed(25)
        first = await self.queue()
        second = await self.queue()
        self.assertEqual(first["queued"], 20)
        self.assertEqual(second["queued"], 5)
        self.assertEqual(second["shortfall"], 15)
        self.assertEqual(second["job_ids"], ids[20:])
        self.assertEqual(len(self.jobs.mock_delivery_queue(active_only=False)), 25)

    async def test_other_round_never_fills_current_shortage(self):
        self.make_search("other")
        other = self.seed(30, task_id="other")
        wanted = self.seed(1)
        result = await self.queue()
        self.assertEqual(result["job_ids"], wanted)
        self.assertTrue(set(result["job_ids"]).isdisjoint(other))
        self.assertEqual(result["shortfall"], 19)

    async def test_order_uses_frozen_score_then_id(self):
        low = self.seed(3, score=85)
        high = self.seed(2, score=95, start=10)
        result = await self.queue()
        self.assertEqual(result["selected_job_ids"], high + low)

    async def test_explicit_order_is_preserved_and_capped(self):
        ids = self.seed(25)
        supplied = list(reversed(ids))
        result = await self.queue(job_ids=supplied, selection_evidence={"allow_unverified": True})
        self.assertEqual(result["selected_job_ids"], supplied[:20])
        self.assertEqual(result["queued"], 20)

    async def test_explicit_ids_cannot_escape_bound_round(self):
        self.make_search("other")
        other = self.seed(25, task_id="other")
        wanted = self.seed(2)
        result = await self.queue(job_ids=[*other, *wanted], selection_evidence={"allow_unverified": True})
        self.assertEqual(result["job_ids"], wanted)
        self.assertEqual(result["shortfall"], 18)
        self.assertEqual(result["insufficient"], 25)

    async def test_explicit_selection_cannot_bypass_unknown_user_filter(self):
        unknown = self.seed(3, filter_status="unknown")
        wanted = self.seed(1, start=10)
        result = await self.queue(job_ids=[*unknown, *wanted], selection_evidence={"allow_unverified": True})
        self.assertEqual(result["job_ids"], wanted)
        self.assertEqual(result["shortfall"], 19)
        self.assertEqual(result["insufficient"], 3)
        self.assertEqual(result["unknown"], 0)

    async def test_known_below_score_explicit_rows_are_not_called_unknown(self):
        low = self.seed(15, score=60)
        wanted = self.seed(25, score=95, start=20)
        result = await self.queue(min_score=85, job_ids=[*low, *wanted], selection_evidence={"bounded_batch": True})
        self.assertEqual(result["job_ids"], wanted[:20])
        self.assertEqual(result["eligible_count"], 25)
        self.assertEqual(result["no_match"], 15)
        self.assertEqual(result["insufficient"], 0)
        self.assertEqual(result["unknown"], 0)
        self.assertNotIn("因证据不足", result["message"])

    async def test_all_known_below_score_explicit_rows_report_no_match(self):
        low = self.seed(3, score=60)
        result = await self.queue(min_score=85, job_ids=low, selection_evidence={"bounded_batch": True})
        self.assertEqual(result["queued"], 0)
        self.assertEqual(result["shortfall"], 20)
        self.assertEqual(result["reason"], "no_match")
        self.assertEqual(result["no_match"], 3)
        self.assertEqual(result["insufficient"], 0)
        self.assertNotIn("缺少投递所需证据", result["message"])
        self.coordinator._ensure_applicant_bound.assert_not_awaited()

    async def test_other_round_matching_cache_cannot_revoke_current_receipt(self):
        ids = self.seed(1, score=95)
        job = self.jobs.get_job(ids[0])
        self.jobs.upsert_job({**job, "tags": ["Python"], "score": 10,
            "filter_status": "unknown", "filter_evidence": {"status": "unknown", "conditions": []},
            "qualification_evidence": {"status": "unknown", "resume_digest": resume_document_digest(self.document)}})
        result = await self.queue(min_score=85, job_ids=ids, selection_evidence={"bounded_batch": True})
        self.assertEqual(result["job_ids"], ids)
        self.assertEqual(result["unknown"], 0)
        queued = self.jobs.mock_delivery_queue()[0]
        self.assertEqual(queued["eligibility_snapshot"]["score"], 95)
        self.assertEqual(queued["eligibility_snapshot"]["filter_status"], "pass")

    async def test_changed_original_jd_invalidates_explicit_round_receipt(self):
        ids = self.seed(1, score=95)
        job = self.jobs.get_job(ids[0])
        self.jobs.upsert_job({**job, "tags": ["Python"], "description": job["description"] + "实际岗位现已调整。"})
        result = await self.queue(job_ids=ids, selection_evidence={"bounded_batch": True})
        self.assertEqual(result["queued"], 0)
        self.assertEqual(result["shortfall"], 20)
        self.assertEqual(result["reason"], "insufficient_evidence")
        self.assertEqual(self.jobs.mock_delivery_queue(), [])
        self.coordinator._ensure_applicant_bound.assert_not_awaited()

    async def test_limit_is_frozen_in_persisted_eligibility_and_survives_reload(self):
        ids = self.seed(25)
        result = await self.queue()
        reopened = JobStore(self.path)
        rows = reopened.mock_delivery_queue(active_only=False)
        self.assertEqual({row["job_id"] for row in rows}, set(ids[:20]))
        for row in rows:
            self.assertEqual(row["scope"]["limit"], 20)
            self.assertEqual(row["scope"]["source_task_id"], "current")
            self.assertEqual(row["scope"]["selected_job_ids"], result["selected_job_ids"])
            self.assertIsNotNone(reopened.assert_delivery_queue_facts(row))

    async def test_invalid_limits_are_rejected_before_binding_or_effects(self):
        for limit in (0, 201, -1, True, False, 20.0, "20"):
            with self.subTest(limit=limit), self.assertRaises(PreflightRefused):
                await self.coordinator.queue_deliveries(session_id="default", task_id="current", limit=limit)
        self.coordinator._ensure_applicant_bound.assert_not_awaited()
        self.assertEqual(self.jobs.mock_delivery_queue(), [])

    async def test_missing_or_foreign_round_does_not_guess_other_results(self):
        self.make_search("foreign", session_id="another-session")
        self.make_search("resume-changed", resume_id="another-resume")
        self.seed(25)
        for task in (None, "foreign", "resume-changed", "missing"):
            with self.subTest(task=task), self.assertRaises(PreflightRefused):
                await self.coordinator.queue_deliveries(session_id="default", task_id=task, limit=20)
        self.assertEqual(self.jobs.mock_delivery_queue(), [])
        self.coordinator._ensure_applicant_bound.assert_not_awaited()

    async def test_noncompleted_round_cannot_be_queued_or_start_binding_search(self):
        self.seed(25)
        for status in (BrowserTaskStatus.RUNNING, BrowserTaskStatus.FAILED, BrowserTaskStatus.STOPPED):
            self.jobs.update_task("current", status=status)
            with self.subTest(status=status), self.assertRaises(PreflightRefused):
                await self.queue()
            self.assertEqual(self.jobs.mock_delivery_queue(), [])
        self.coordinator._ensure_applicant_bound.assert_not_awaited()

    async def test_cancellation_during_binding_does_not_enqueue_selected_jobs(self):
        self.seed(25)
        async def interrupted(_):
            self.coordinator.stop_epochs["default"] = 1
            return "bound"
        self.coordinator._ensure_applicant_bound.side_effect = interrupted
        with self.assertRaises(PreflightRefused):
            await self.queue()
        self.assertEqual(self.jobs.mock_delivery_queue(), [])

    async def test_cancellation_during_filtering_prevents_binding_search(self):
        self.seed(1)
        async def interrupted(*args, **kwargs):
            self.coordinator.stop_epochs["default"] = 1
            return {"status": "pass", "conditions": []}
        with patch("app.core.job_filters.evaluate_filter", interrupted), self.assertRaises(PreflightRefused):
            await self.queue(filters={"op": "condition", "text": "合成条件"})
        self.coordinator._ensure_applicant_bound.assert_not_awaited()
        self.assertEqual(self.jobs.mock_delivery_queue(), [])

    async def test_resume_change_during_filtering_prevents_binding_search(self):
        self.seed(1)
        async def changed(*args, **kwargs):
            self.document = ResumeDocumentView(id="synthetic-doc", version=2)
            return {"status": "pass", "conditions": []}
        with patch("app.core.job_filters.evaluate_filter", changed), self.assertRaises(PreflightRefused):
            await self.queue(filters={"op": "condition", "text": "合成条件"})
        self.coordinator._ensure_applicant_bound.assert_not_awaited()
        self.assertEqual(self.jobs.mock_delivery_queue(), [])

    async def test_binding_wait_is_pending_not_a_final_shortage(self):
        self.seed(25)
        self.coordinator._ensure_applicant_bound.return_value = "waiting_resource"
        result = await self.queue()
        self.assertEqual(result["binding"], "waiting_resource")
        self.assertEqual(result["requested_limit"], 20)
        self.assertEqual(result["eligible_count"], 25)
        self.assertEqual(len(result["selected_job_ids"]), 20)
        self.assertIsNone(result["shortfall"])
        self.assertTrue(result["quantity_pending"])
        self.assertEqual(self.jobs.mock_delivery_queue(), [])

    async def test_cancellation_during_pending_binding_does_not_leave_false_wait(self):
        self.seed(1)
        async def interrupted(_):
            self.coordinator.stop_epochs["default"] = 1
            return "waiting_resource"
        self.coordinator._ensure_applicant_bound.side_effect = interrupted
        with self.assertRaises(PreflightRefused):
            await self.queue()
        self.assertEqual(self.jobs.mock_delivery_queue(), [])

    async def test_resume_change_during_pending_binding_does_not_leave_false_wait(self):
        self.seed(1)
        async def changed(_):
            self.document = ResumeDocumentView(id="synthetic-doc", version=2)
            return "waiting_resource"
        self.coordinator._ensure_applicant_bound.side_effect = changed
        with self.assertRaises(PreflightRefused):
            await self.queue()
        self.assertEqual(self.jobs.mock_delivery_queue(), [])

    async def test_resume_changes_during_binding_do_not_enqueue_selected_jobs(self):
        self.seed(25)
        async def changed(_):
            self.document = ResumeDocumentView(id="synthetic-doc", version=2)
            return "bound"
        self.coordinator._ensure_applicant_bound.side_effect = changed
        with self.assertRaises(PreflightRefused):
            await self.queue()
        self.assertEqual(self.jobs.mock_delivery_queue(), [])


def main():
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(
        __import__(__name__, fromlist=["*"])))
    output = Path("evaluation/results/delivery-limit") / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    summary = {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
               "external_model_calls": 0, "recruitment_actions": 0}
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (output / "traces.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in TRACES), encoding="utf-8")
    print(json.dumps({"output": str(output), **summary}))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
