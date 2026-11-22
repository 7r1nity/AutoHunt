"""Synthetic recruiter-opening contracts; no model API or live recruitment writes."""
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
from unittest.mock import AsyncMock

from app.core.models import ResumeDocumentView, ResumeEntryView
from app.delivery.automation import DeliveryAutomationService
from app.delivery.outreach import OutreachService, build_outreach
from app.jobs.store import JobStore
from app.llm.base import LLMResult
from app.memory.resume_workspace import resume_document_digest
from evaluation.search_delivery_contract import job


def document():
    return ResumeDocumentView(id='synthetic-resume', version=2, skills=['Python', 'SQL'],
        summary='使用Python和SQL完成业务数据分析。',
        entries=[ResumeEntryView(id='synthetic-project', kind='project', title='合成数据分析项目',
            summary='使用Python清洗数据，使用SQL构建指标并完成验收报告。')])


def vacancy(index=1, **changes):
    return {**job(index), 'title': '合成数据分析师',
            'description': '使用Python和SQL进行业务分析；与业务团队沟通数据口径。', **changes}


class GroundedMessages(unittest.IsolatedAsyncioTestCase):
    def test_rendered_message_contains_only_provided_candidate_and_jd_excerpts(self):
        doc, target = document(), vacancy()
        draft = build_outreach(target, doc)
        self.assertIn('SQL', draft['message'])
        self.assertEqual(draft['resume_digest'], resume_document_digest(doc))
        self.assertEqual({item['source'] for item in draft['evidence']}, {'resume', 'jd'})
        for citation in draft['evidence']:
            source = target['description'] if citation['source'] == 'jd' else doc.entries[0].summary
            self.assertIn(citation['quote'], source)
        self.assertNotIn('五年', draft['message'])
        self.assertNotIn('已确认简历', draft['message'])
        self.assertLessEqual(len(draft['message']), 600)

    def test_different_jd_produces_different_frozen_opening(self):
        first = build_outreach(vacancy(), document())
        second = build_outreach(vacancy(title='合成财务分析师', description='负责现金流分析与财务建模。'), document())
        self.assertNotEqual(first['message'], second['message'])
        self.assertNotEqual(first['job_digest'], second['job_digest'])

    def test_contacts_and_full_resume_are_not_in_model_input_or_message(self):
        doc = document().model_copy(update={'raw_content': 'synthetic@example.invalid\n手机：13800138000\nprivate-only-marker'})
        draft = build_outreach(vacancy(), doc)
        self.assertNotIn('13800138000', draft['message'])
        self.assertNotIn('example.invalid', draft['message'])
        self.assertNotIn('private-only-marker', draft['message'])

    async def test_model_only_selects_from_minimized_evidence(self):
        doc = document().model_copy(update={'raw_content': 'never-send-whole-original'})
        llm = NS(generate=AsyncMock(return_value=LLMResult(text='{"jd_index":0,"resume_index":0}', provider='synthetic', model='synthetic')))
        draft = await OutreachService(llm).draft(vacancy(), doc)
        self.assertEqual(draft['method'], 'model_selected')
        request = llm.generate.await_args.args[0]
        self.assertNotIn('never-send-whole-original', request.user_message)
        self.assertLess(len(request.user_message), 2000)
        self.assertIn(draft['evidence'][0]['quote'], vacancy()['description'])

    async def test_generated_or_out_of_range_claims_fall_back_to_grounded_message(self):
        for raw in ('{"message":"我有五年经理经验"}', '{"jd_index":99,"resume_index":0}', 'not json'):
            with self.subTest(raw=raw):
                llm = NS(generate=AsyncMock(return_value=LLMResult(text=raw, provider='synthetic', model='synthetic')))
                draft = await OutreachService(llm).draft(vacancy(), document())
                self.assertEqual(draft['method'], 'evidence_template')
                self.assertNotIn('五年经理', draft['message'])

    async def test_model_failure_and_timeout_keep_valid_template(self):
        for error in (RuntimeError('synthetic failure'), asyncio.TimeoutError()):
            llm = NS(generate=AsyncMock(side_effect=error))
            draft = await OutreachService(llm).draft(vacancy(), document())
            self.assertEqual(draft['method'], 'evidence_template')
            self.assertTrue(draft['evidence'])

    async def test_actual_slow_model_is_bounded_and_falls_back(self):
        async def slow(_):
            await asyncio.sleep(60)
        llm = NS(generate=AsyncMock(side_effect=slow))
        draft = await OutreachService(llm, timeout_seconds=0.01).draft(vacancy(), document())
        self.assertEqual(draft['method'], 'evidence_template')
        self.assertEqual(llm.generate.await_count, 1)

    async def test_cancelled_drafting_is_not_retried_or_turned_into_a_send(self):
        llm = NS(generate=AsyncMock(side_effect=asyncio.CancelledError()))
        with self.assertRaises(asyncio.CancelledError):
            await OutreachService(llm).draft(vacancy(), document())
        self.assertEqual(llm.generate.await_count, 1)

    def test_empty_resume_does_not_generate_unsupported_candidate_claims(self):
        draft = build_outreach(vacancy(), ResumeDocumentView())
        self.assertEqual(draft['method'], 'minimal')
        self.assertNotIn('我的相关经历包括', draft['message'])


class PreviewBindings(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.jobs = JobStore(self.root / 'jobs.db')
        self.doc = document()
        self.ident = self.jobs.upsert_job(vacancy())[0]
        self.service = DeliveryAutomationService(self.jobs, self.root, headless=True, cdp_url='synthetic-only',
            resume_workspace=NS(read_document=lambda: self.doc))

    def tearDown(self):
        gc.collect()

    async def test_async_preview_freezes_message_without_starting_a_delivery(self):
        preview = await self.service.create_preview(self.ident)
        self.assertTrue(preview.outreach_message)
        self.assertEqual(preview.outreach_method, 'evidence_template')
        self.assertEqual(self.jobs.delivery_outreach(preview.id), preview.outreach_message)
        self.assertEqual(self.jobs.all_delivery_runs(), [])

    async def test_frozen_message_survives_store_reopen_without_model_regeneration(self):
        preview = await self.service.create_preview(self.ident)
        reopened = JobStore(self.root / 'jobs.db')
        self.assertEqual(reopened.delivery_outreach(preview.id), preview.outreach_message)

    async def test_changing_message_after_preview_invalidates_integrity(self):
        preview = await self.service.create_preview(self.ident)
        with self.jobs._connect() as db:
            row = db.execute('SELECT payload_json FROM delivery_previews WHERE id=?', (preview.id,)).fetchone()
            payload = json.loads(row['payload_json'])
            payload['outreach_message'] = 'not the approved opening'
            db.execute('UPDATE delivery_previews SET payload_json=? WHERE id=?', (json.dumps(payload), preview.id))
        with self.assertRaisesRegex(ValueError, '完整性'):
            self.jobs.assert_delivery_facts(self.ident, preview.id)

    async def test_new_resume_version_cannot_reuse_old_preview_binding(self):
        preview = await self.service.create_preview(self.ident)
        with self.assertRaisesRegex(ValueError, '已变化'):
            self.jobs.authorize_delivery_run(preview.id, 'synthetic-run', resume_document_id=self.doc.id,
                resume_version=self.doc.version + 1, resume_digest=resume_document_digest(self.doc))

    async def test_resume_changed_during_drafting_does_not_save_stale_preview(self):
        old = self.doc
        async def change_resume(*_):
            self.doc = old.model_copy(update={'version': old.version + 1})
            return build_outreach(vacancy(), old)
        self.service.outreach.draft = change_resume
        with self.assertRaisesRegex(ValueError, '期间主简历发生变化'):
            await self.service.create_preview(self.ident)

    async def test_jd_changes_invalidate_frozen_evidence_before_communication(self):
        preview = await self.service.create_preview(self.ident)
        self.jobs.upsert_job(vacancy(description='合成岗位的新职责要求已经变化，不复用先前的沟通证据。'))
        with self.assertRaisesRegex(ValueError, '已变化'):
            self.jobs.assert_delivery_facts(self.ident, preview.id)

    async def test_one_preview_can_only_issue_one_bound_delivery(self):
        preview = await self.service.create_preview(self.ident)
        self.jobs.authorize_delivery_run(preview.id, 'synthetic-run', resume_document_id=self.doc.id,
            resume_version=self.doc.version, resume_digest=resume_document_digest(self.doc))
        with self.assertRaises(ValueError):
            self.jobs.authorize_delivery_run(preview.id, 'synthetic-run-2')

    async def test_business_filter_exception_is_per_job_preview_not_global(self):
        outside = self.jobs.upsert_job(vacancy(2, filter_status='fail'))[0]
        with self.assertRaises(ValueError):
            await self.service.create_preview(outside)
        preview = await self.service.create_preview(outside, allow_outside_search=True)
        self.assertTrue(preview.allow_outside_search)
        self.assertTrue(preview.exception_reasons)
        self.jobs.assert_delivery_facts(outside, preview.id)
        with self.assertRaises(ValueError):
            self.jobs.assert_delivery_facts(self.ident, preview.id)
        with self.assertRaises(ValueError):
            await self.service.create_preview(outside)

    async def test_business_filter_exception_does_not_allow_removed_jobs(self):
        outside = self.jobs.upsert_job(vacancy(3, filter_status='fail'))[0]
        preview = await self.service.create_preview(outside, allow_outside_search=True)
        with self.jobs._connect() as db:
            db.execute("UPDATE jobs SET removed_at='synthetic removal' WHERE id=?", (outside,))
        with self.assertRaisesRegex(ValueError, '移除'):
            self.jobs.assert_delivery_facts(outside, preview.id)

    async def test_business_filter_exception_does_not_authorize_changed_job(self):
        outside = self.jobs.upsert_job(vacancy(4, filter_status='fail'))[0]
        preview = await self.service.create_preview(outside, allow_outside_search=True)
        self.jobs.upsert_job(vacancy(4, filter_status='fail', description='该岗位的薪资和职责现已变化，需要新的一次单岗范围确认。'))
        with self.assertRaises(ValueError):
            self.jobs.assert_delivery_facts(outside, preview.id)

    def enqueue(self, *, stamp='synthetic-stamp'):
        binding = {'resume_document_id': self.doc.id, 'resume_version': self.doc.version,
                   'resume_digest': resume_document_digest(self.doc)}
        self.jobs.enqueue_mock_deliveries([self.jobs.get_job(self.ident)], session_id='synthetic-session',
            resume_stamp=stamp, binding=binding, min_score=85,
            scope={'min_score': 85, 'source_task_id': 'old-search'},
            scope_evidence={self.ident: {'status': 'pass'}})
        return self.jobs.mock_delivery_queue()[0]

    async def test_old_waiting_hr_scope_survives_changed_search_score_and_filter_cache(self):
        item = self.enqueue()
        frozen = self.jobs.assert_delivery_queue_facts(item)
        preview = await self.service.create_preview(self.ident, eligibility_snapshot=frozen)
        self.jobs.authorize_delivery_run(preview.id, 'synthetic-waiting', **item['binding'])
        self.jobs.update_delivery_run('synthetic-waiting', status='waiting_for_reply', checkpoint='waiting_for_reply')
        self.jobs.update_job_filter_result(self.ident, {'status': 'fail', 'digest': 'a-new-search-scope'})
        with self.jobs._connect() as db:
            db.execute('UPDATE jobs SET score=?,qualification_evidence=? WHERE id=?',
                (10, json.dumps({'status': 'fail', 'resume_digest': resume_document_digest(self.doc)}), self.ident))
        reopened = JobStore(self.root / 'jobs.db')
        self.assertEqual(reopened.assert_delivery_queue_facts(reopened.mock_delivery_queue()[0])['score'], 88)
        reopened.assert_delivery_facts(self.ident, preview.id)
        self.assertEqual(reopened.delivery_outreach(preview.id), preview.outreach_message)
        self.assertEqual(reopened.get_delivery_run('synthetic-waiting').status, 'waiting_for_reply')

    async def test_queued_scope_can_create_preview_after_unrelated_refilter(self):
        item = self.enqueue()
        self.jobs.update_job_filter_result(self.ident, {'status': 'unknown', 'digest': 'new-query'})
        preview = await self.service.create_preview(self.ident, eligibility_snapshot=item['eligibility_snapshot'])
        self.jobs.authorize_delivery_run(preview.id, 'synthetic-run-frozen', **item['binding'])
        self.jobs.assert_delivery_facts(self.ident, preview.id)
        with self.assertRaises(ValueError):
            await self.service.create_preview(self.ident)  # A new scope cannot borrow old eligibility.

    def coordinator(self):
        from app.workflow.mock_coordinator import MockWorkflowCoordinator
        candidate = NS(read=lambda: NS(profile=NS(resume_id=self.doc.id)))
        return MockWorkflowCoordinator(self.jobs, NS(worker=None), self.service, candidate,
            NS(read_document=lambda: self.doc))

    async def test_real_coordinator_dispatch_preserves_frozen_scope_after_other_search(self):
        item = self.enqueue(stamp=f'{self.doc.id}:{self.doc.id}:{self.doc.version}')
        self.jobs.update_job_filter_result(self.ident, {'status': 'fail', 'digest': 'new-search'})
        with self.jobs._connect() as db:
            db.execute('UPDATE jobs SET score=10 WHERE id=?', (self.ident,))
        async def authorized(preview_id, **binding):
            binding.pop('monitor_reply')
            self.jobs.authorize_delivery_run(preview_id, 'synthetic-coordinator-run', **binding)
            state = self.jobs.update_delivery_run('synthetic-coordinator-run', status='waiting_for_reply', checkpoint='waiting_for_reply')
            return None, None, None, state
        self.service.authorize_and_start = AsyncMock(side_effect=authorized)
        original_preview = self.service.create_preview
        self.service.create_preview = AsyncMock(side_effect=original_preview)
        coordinator = self.coordinator()
        await coordinator.process_delivery_queue()
        self.service.authorize_and_start.assert_awaited_once()
        self.assertEqual(self.service.create_preview.await_args.kwargs['eligibility_snapshot'], item['eligibility_snapshot'])
        self.assertEqual(self.jobs.mock_delivery_queue()[0]['status'], 'dispatched')
        await coordinator.process_delivery_queue()
        self.service.authorize_and_start.assert_awaited_once()  # Waiting must not re-greet.
        self.assertEqual(self.jobs.get_delivery_run('synthetic-coordinator-run').status, 'waiting_for_reply')

    async def test_real_coordinator_rejects_actual_jd_drift_before_sending(self):
        self.enqueue(stamp=f'{self.doc.id}:{self.doc.id}:{self.doc.version}')
        self.jobs.upsert_job(vacancy(description='真实岗位职责已经改变，先前投递判定不可沿用。'))
        self.service.authorize_and_start = AsyncMock()
        await self.coordinator().process_delivery_queue()
        self.service.authorize_and_start.assert_not_awaited()
        self.assertEqual(self.jobs.mock_delivery_queue(active_only=False)[0]['status'], 'stale')

    def test_new_search_candidates_do_not_join_existing_delivery_queue(self):
        self.enqueue()
        self.jobs.upsert_job(vacancy(21))
        self.jobs.upsert_job(vacancy(22))
        self.assertEqual([item['job_id'] for item in self.jobs.mock_delivery_queue()], [self.ident])

    def test_changed_raw_jd_or_tags_still_invalidates_a_frozen_queue(self):
        item = self.enqueue()
        original = self.jobs.get_job(self.ident)
        for field, value in (('description', 'changed actual JD'), ('source_tags', '["new source tag"]'),
                             ('source_url', original['source_url'] + '&other=1'), ('salary', '50-60K'),
                             ('title', 'changed actual role'), ('company', 'changed actual company')):
            with self.subTest(field=field):
                with self.jobs._connect() as db:
                    db.execute(f'UPDATE jobs SET {field}=? WHERE id=?', (value, self.ident))
                with self.assertRaisesRegex(ValueError, '原始事实'):
                    self.jobs.assert_delivery_queue_facts(item)
                with self.jobs._connect() as db:
                    db.execute(f'UPDATE jobs SET {field}=? WHERE id=?', (original[field], self.ident))

    def test_cancelled_queue_cannot_use_its_unchanged_frozen_snapshot(self):
        item = self.enqueue()
        self.jobs.update_mock_delivery(self.ident, 'cancelled', 'synthetic user withdrawal')
        with self.assertRaisesRegex(ValueError, '取消'):
            self.jobs.assert_delivery_queue_facts(item)

    def test_queue_scope_binding_threshold_and_digest_cannot_be_rewritten(self):
        item = self.enqueue()
        for field, value in (('min_score', 0), ('session_id', 'other'), ('resume_stamp', 'other'),
                             ('scope', {}), ('scope_evidence', {'allow_outside_search': True})):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.jobs.assert_delivery_queue_facts({**item, field: value})
        changed = json.loads(json.dumps(item))
        changed['eligibility_snapshot']['score'] = 100
        canonical = self.jobs._canonical_snapshot(changed['eligibility_snapshot'])
        changed['eligibility_digest'] = hashlib.sha256(canonical.encode()).hexdigest()
        with self.assertRaisesRegex(ValueError, '持久队列'):
            self.jobs.assert_delivery_queue_facts(changed)

    def test_legacy_queue_without_new_snapshot_remains_conservative(self):
        self.enqueue()
        old_hash = self.jobs.review_fingerprint(self.jobs.get_job(self.ident))
        with self.jobs._connect() as db:
            db.execute('UPDATE mock_delivery_queue SET eligibility_json=NULL,eligibility_digest=NULL,job_hash=? WHERE job_id=?',
                       (old_hash, self.ident))
        legacy = self.jobs.mock_delivery_queue()[0]
        self.assertIsNone(self.jobs.assert_delivery_queue_facts(legacy))
        self.jobs.update_job_filter_result(self.ident, {'status': 'fail', 'digest': 'different'})
        with self.assertRaisesRegex(ValueError, '旧队列'):
            self.jobs.assert_delivery_queue_facts(legacy)

    async def test_legacy_preview_without_new_snapshot_keeps_old_fingerprint_check(self):
        preview = await self.service.create_preview(self.ident)
        with self.jobs._connect() as db:
            row = db.execute('SELECT payload_json FROM delivery_previews WHERE id=?', (preview.id,)).fetchone()
            old = json.loads(row[0])
            old.pop('job_facts_fingerprint')
            old.pop('eligibility_snapshot')
            canonical = self.jobs._canonical_snapshot(old)
            db.execute('UPDATE delivery_previews SET payload_json=?,payload_digest=? WHERE id=?',
                (canonical, hashlib.sha256(canonical.encode()).hexdigest(), preview.id))
        self.jobs.assert_delivery_facts(self.ident, preview.id)
        self.jobs.update_job_filter_result(self.ident, {'status': 'fail', 'digest': 'different'})
        with self.assertRaises(ValueError):
            self.jobs.assert_delivery_facts(self.ident, preview.id)


if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(__import__(__name__, fromlist=['*'])))
    output = Path(__file__).parent / 'results' / 'outreach' / datetime.now(UTC).strftime('%Y%m%dT%H%M%S')
    output.mkdir(parents=True, exist_ok=True)
    (output / 'result.json').write_text(json.dumps({'tests': result.testsRun, 'failures': len(result.failures),
        'errors': len(result.errors), 'passed': result.wasSuccessful(), 'external_model_calls': 0}, ensure_ascii=False), encoding='utf-8')
    raise SystemExit(not result.wasSuccessful())
