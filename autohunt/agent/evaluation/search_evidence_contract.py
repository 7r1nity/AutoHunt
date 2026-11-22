"""Per-search evidence contracts: synthetic SQLite, no network or user history."""
from __future__ import annotations

import gc
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from app.core.job_filters import filter_digest, validate_filter
from app.jobs.store import JobStore
from evaluation.search_delivery_contract import job


class FrozenSearchEvidence(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'jobs.db'
        self.jobs = JobStore(self.path)
        self.digest = 'a' * 64
        self.new_task('original')
        self.target = {**job(1), 'salary': '28-35K', 'source_tags': ['SQL'],
            'qualification_evidence': {'status': 'pass', 'assessed': True, 'resume_digest': self.digest}}
        self.ident = self.admit('original', self.target)

    def tearDown(self):
        gc.collect()

    def new_task(self, ident):
        self.jobs.create_task(ident, 'mock', '合成数据分析师', resume_id='synthetic-resume')
        self.jobs.save_search_request(ident, {'conversation_id': 'synthetic-session'})
        self.jobs.update_task(ident, status='completed')

    def admit(self, task, target):
        return self.jobs.admit_search_result(task, target, limit=20, groups=None)['job_id']

    def evidence(self, **options):
        return self.jobs.resume_analysis_evidence('synthetic-resume', resume_digest=self.digest, **options)

    def replace_cache(self):
        self.jobs.update_job_filter_result(self.ident, {'status': 'fail', 'digest': 'other-scope'})
        with self.jobs._connect() as db:
            db.execute('UPDATE jobs SET score=10,qualification_evidence=? WHERE id=?',
                (json.dumps({'status': 'fail', 'resume_digest': self.digest}), self.ident))

    def test_new_search_cannot_erase_previous_score_filter_or_qualification_evidence(self):
        self.new_task('other-search')
        self.replace_cache()
        selected, gaps = self.evidence(task_id='original', min_score=85, salary_min=25000)
        self.assertEqual([item['id'] for item in selected], [self.ident])
        self.assertEqual(selected[0]['score'], 88)
        self.assertEqual(selected[0]['filter_status'], 'pass')
        self.assertEqual(json.loads(selected[0]['qualification_evidence'])['status'], 'pass')
        self.assertEqual(gaps['stale_filter'], 0)

    def test_other_search_cache_cannot_implicitly_remove_admitted_board_jobs_or_stats(self):
        self.new_task('other-search')
        for status in ('fail', 'unknown'):
            with self.subTest(status=status):
                self.jobs.update_job_filter_result(self.ident, {'status': status, 'digest': 'other-scope'})
                self.assertEqual([item.id for item in self.jobs.list_jobs()], [self.ident])
                self.assertEqual([item.id for item in self.jobs.list_jobs(stage='review')], [self.ident])
                self.assertEqual(self.jobs.stats().review, 1)

    def test_unadmitted_candidates_stay_hidden_while_explicit_refilter_and_remove_still_work(self):
        self.new_task('other-search')
        self.admit('other-search', job(2, status='unknown'))
        self.admit('other-search', job(3, status='fail'))
        self.assertEqual([item.id for item in self.jobs.list_jobs()], [self.ident])
        self.assertEqual(self.jobs.stats().review, 1)
        candidates = self.jobs.refilter_candidates('original', resume_id='synthetic-resume', session_id='synthetic-session')
        self.jobs.save_refilter_results('original', candidates, [{'status': 'fail'}], {})
        self.assertEqual(self.jobs.list_jobs(), [])
        self.assertEqual(self.jobs.stats().review, 0)
        new_id = self.admit('other-search', job(4))
        self.assertEqual([item.id for item in self.jobs.list_jobs()], [new_id])
        self.assertEqual(self.jobs.stats().review, 1)
        row = self.jobs.get_job(new_id)
        self.jobs.remove_selection([{'id': new_id, 'content_hash': self.jobs.review_fingerprint(row)}],
            session_id='synthetic-session', run_id='synthetic-removal')
        self.assertEqual(self.jobs.list_jobs(), [])
        self.assertEqual(self.jobs.stats().review, 0)

    def test_growth_scope_uses_frozen_score_not_new_matching_score(self):
        self.replace_cache()
        self.assertEqual(self.evidence(task_id='original', max_score=84)[0], [])
        self.assertEqual(len(self.evidence(task_id='original', max_score=89)[0]), 1)

    def test_summary_uses_its_original_score_without_stale_selection_binding(self):
        self.replace_cache()
        report = self.jobs.search_report('original')
        self.assertEqual(report['qualified_jobs'], 1)
        self.assertEqual(report['jobs'][0]['score'], 88)
        self.assertEqual(report['jobs'][0]['content_hash'], self.jobs.review_fingerprint(self.jobs.get_job(self.ident)))

    def test_actual_jd_drift_is_unknown_in_report_and_not_counted_as_success(self):
        self.jobs.upsert_job({**self.target, 'description': '原始岗位职责已变化，不得套用旧总结。'})
        report = self.jobs.search_report('original')
        self.assertEqual(report['jobs'], [])
        self.assertEqual(report['qualified_jobs'], 0)
        self.assertEqual(report['pending_verification'], 1)

    def test_reopen_preserves_search_evidence_and_does_not_mutate_global_cache(self):
        self.replace_cache()
        self.jobs = JobStore(self.path)
        self.assertEqual(len(self.evidence(task_id='original')[0]), 1)
        self.assertEqual(self.jobs.get_job(self.ident)['filter_status'], 'fail')
        self.assertEqual(self.jobs.get_job(self.ident)['score'], 10)

    def test_actual_jd_salary_original_tags_and_url_changes_invalidate_receipt(self):
        original = self.jobs.get_job(self.ident)
        for field, changed in (('description', '真实JD改变'), ('salary', '30-40K'),
                               ('source_tags', '["new original source tag"]'),
                               ('source_url', original['source_url'] + '&changed=1')):
            with self.subTest(field=field):
                with self.jobs._connect() as db:
                    db.execute(f'UPDATE jobs SET {field}=? WHERE id=?', (changed, self.ident))
                selected, gaps = self.evidence(task_id='original')
                self.assertEqual(selected, [])
                self.assertEqual(gaps['stale_filter'], 1)
                with self.jobs._connect() as db:
                    db.execute(f'UPDATE jobs SET {field}=? WHERE id=?', (original[field], self.ident))

    def test_resume_version_change_is_not_repaired_with_new_global_qualification_cache(self):
        self.digest = 'b' * 64
        with self.jobs._connect() as db:
            db.execute('UPDATE jobs SET qualification_evidence=? WHERE id=?',
                (json.dumps({'status': 'pass', 'resume_digest': self.digest}), self.ident))
        selected, gaps = self.evidence(task_id='original')
        self.assertEqual(selected, [])
        self.assertEqual(gaps['stale_filter'], 1)

    def test_resume_switch_and_removed_job_cannot_reuse_old_evidence(self):
        self.assertEqual(self.jobs.resume_analysis_evidence('other-resume', resume_digest=self.digest)[0], [])
        with self.jobs._connect() as db:
            db.execute("UPDATE jobs SET removed_at='synthetic-user-removal' WHERE id=?", (self.ident,))
        self.assertEqual(self.evidence(task_id='original')[1]['removed'], 1)

    def test_latest_failed_other_search_does_not_shadow_an_older_valid_search(self):
        self.new_task('other')
        self.jobs.event('other', 'job_refiltered', 'other search rejected the same job', sample={
            'job_id': self.ident, 'admission': 'not_admitted', 'filter_status': 'fail'})
        self.replace_cache()
        self.assertEqual([item['id'] for item in self.evidence()[0]], [self.ident])

    def test_unknown_candidate_receipt_overrides_only_its_own_search_admission(self):
        self.new_task('other')
        unknown = {'job_id': self.ident, 'filter_status': 'unknown', 'admission': 'pending_verification'}
        self.jobs.event('other', 'candidate_recorded', 'other search is uncertain', sample=unknown)
        self.assertEqual([item['id'] for item in self.evidence()[0]], [self.ident])
        self.assertEqual([item['id'] for item in self.evidence(task_id='original')[0]], [self.ident])
        self.jobs.event('original', 'candidate_recorded', 'same search now requires verification', sample=unknown)
        selected, gaps = self.evidence(task_id='original')
        self.assertEqual(selected, [])
        self.assertEqual(gaps['unknown_filter'], 1)

    def test_latest_same_task_refilter_replaces_the_old_admission(self):
        candidates = self.jobs.refilter_candidates('original', resume_id='synthetic-resume', session_id='synthetic-session')
        self.jobs.save_refilter_results('original', candidates, [{'status': 'fail'}], {})
        self.assertEqual(self.evidence(task_id='original')[0], [])

    def test_refilter_pass_writes_a_new_frozen_receipt(self):
        conditions = {'op': 'condition', 'text': '支持远程办公'}
        digest = filter_digest(validate_filter(conditions))
        candidates = self.jobs.refilter_candidates('original', resume_id='synthetic-resume', session_id='synthetic-session')
        self.jobs.save_refilter_results('original', candidates, [{'status': 'pass', 'digest': digest}], {'filters': conditions})
        self.replace_cache()
        selected, gaps = self.evidence(task_id='original')
        self.assertEqual(len(selected), 1)
        self.assertEqual(json.loads(selected[0]['filter_evidence'])['digest'], digest)
        self.assertEqual(gaps['stale_filter'], 0)

    def test_receipt_tamper_is_not_accepted(self):
        with self.jobs._connect() as db:
            row = db.execute("SELECT id,sample_json FROM browser_events WHERE event_type='job_stored'").fetchone()
            sample = json.loads(row['sample_json'])
            sample['analysis_receipt']['score'] = 100
            db.execute('UPDATE browser_events SET sample_json=? WHERE id=?', (json.dumps(sample), row['id']))
        self.assertEqual(self.evidence(task_id='original')[1]['stale_filter'], 1)

    def test_legacy_receipts_without_bound_resume_do_not_inherit_current_version(self):
        with self.jobs._connect() as db:
            row = db.execute("SELECT id,sample_json FROM browser_events WHERE event_type='job_stored'").fetchone()
            sample = json.loads(row['sample_json'])
            sample.pop('analysis_receipt')
            sample.pop('analysis_receipt_digest')
            db.execute('UPDATE browser_events SET sample_json=? WHERE id=?', (json.dumps(sample), row['id']))
        self.assertEqual(self.evidence(task_id='original')[0], [])
        self.assertEqual(len(self.jobs.resume_analysis_evidence('synthetic-resume', task_id='original')[0]), 1)
        self.replace_cache()
        self.assertEqual(self.jobs.resume_analysis_evidence('synthetic-resume', task_id='original')[0], [])

    def test_recording_an_old_event_does_not_backfill_a_current_resume_receipt(self):
        self.new_task('legacy-task')
        self.jobs.event('legacy-task', 'job_stored', 'replayed old event', sample={
            'job_id': self.ident, 'filter_status': 'pass',
            'filter_fingerprint': self.jobs.review_fingerprint(self.jobs.get_job(self.ident))})
        selected, gaps = self.evidence(task_id='legacy-task')
        self.assertEqual(selected, [])
        self.assertEqual(gaps['stale_filter'], 1)
        with self.jobs._connect() as db:
            sample = json.loads(db.execute("SELECT sample_json FROM browser_events WHERE task_id='legacy-task'").fetchone()[0])
        self.assertNotIn('analysis_receipt', sample)

    def test_legacy_browser_persistence_receipts_come_from_this_evaluation(self):
        from app.browser.automation import BrowserAutomationService
        from app.core.models import JobAnalysis
        self.new_task('browser-legacy')
        target = {**job(6), 'external_id': 6, 'description': '合成职位\n  使用SQL进行分析与结果验收。', 'tags': ['SQL']}
        target.pop('filter_status')
        target.pop('filter_evidence')
        analysis = JobAnalysis(score=91, decision='REVIEW', matched_skills=['SQL'], missing_skills=[],
            reasoning='本次独立评价', qualification={'status': 'pass', 'resume_digest': self.digest})
        BrowserAutomationService._persist_analysis(NS(store=self.jobs), 'browser-legacy', target, analysis,
            target['source_url'], source='MockJobSite')
        selected, gaps = self.evidence(task_id='browser-legacy', min_score=90)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]['score'], 91)
        self.assertEqual(gaps['stale_filter'], 0)

    def test_each_search_preserves_its_own_matching_score_and_new_jobs_do_not_leak(self):
        self.new_task('other')
        self.admit('other', {**self.target, 'score': 80})
        other = self.admit('other', {**job(2), 'qualification_evidence': self.target['qualification_evidence']})
        self.assertEqual(self.evidence(task_id='original', min_score=85)[0][0]['score'], 88)
        self.assertEqual(self.evidence(task_id='other', min_score=85)[0][0]['id'], other)
        self.assertEqual([row['id'] for row in self.evidence(task_id='original')[0]], [self.ident])


class FrozenGrowthEvidence(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from app.core.models import ResumeDocumentView
        from app.growth.plans import LearningPlanService
        from app.memory.resume_workspace import resume_document_digest
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.jobs = JobStore(Path(self.temp.name) / 'jobs.db')
        self.doc = ResumeDocumentView(id='synthetic-document', version=3, skills=['Python'], summary='使用Python进行数据分析与质量验收。')
        self.target = {**job(11), 'salary': '28-35K', 'score': 82,
            'description': '负责使用Python与SQL进行复杂数据分析，维护数据质量与业务指标口径，持续复盘交付效果并为管理团队提供可靠分析报告。',
            'qualification_evidence': {'status': 'pass', 'resume_digest': resume_document_digest(self.doc)}}
        self.jobs.create_task('growth-search', 'mock', '合成岗位', resume_id='synthetic-resume')
        self.jobs.update_task('growth-search', status='completed')
        self.ident = self.jobs.admit_search_result('growth-search', self.target, limit=20, groups=None)['job_id']
        from app.llm.base import LLMResult
        data = {'title': '合成数据分析成长计划', 'summary': '依据本轮已核实JD，补充SQL业务分析和数据质量验收的实践证据。',
            'gaps': [{'category': 'technical_skill', 'name': 'SQL业务分析', 'priority': 'high',
                'jd_quotes': [{'job_id': self.ident, 'quote': '使用Python与SQL进行复杂数据分析'}],
                'rationale': '简历中的Python分析证据尚未覆盖SQL业务口径验收。',
                'objective': '使用现有Python技能完成有依据的SQL指标质量验收。', 'estimated_hours': 12,
                'steps': ['搭建虚构订单数据并明确每个字段的数据含义。', '编写SQL聚合指标并核对重复和空值处理规则。', '输出指标核验报告并标注样本来源与限制。'],
                'practice': '交付虚构订单SQL指标仓库和可复现的数据验收报告。',
                'acceptance': ['每个指标可以通过固定输入重复运行得到相同结果。', '报告记录至少三个异常数据案例及其修复前后结果。']}]}
        self.llm = NS(generate=AsyncMock(return_value=LLMResult(text=json.dumps(data), provider='synthetic', model='scripted')))
        candidates = NS(read=lambda: NS(profile=NS(resume_id='synthetic-resume', resume_activated_at=None)))
        workspace = NS(read_document=lambda: self.doc, render_markdown=lambda doc: doc.summary)
        self.service = LearningPlanService(self.jobs, candidates, workspace, self.llm)

    def tearDown(self):
        gc.collect()

    async def test_real_growth_generation_uses_original_scored_jd_after_other_search(self):
        self.jobs.update_job_filter_result(self.ident, {'status': 'fail', 'digest': 'other-search'})
        with self.jobs._connect() as db:
            db.execute('UPDATE jobs SET score=96 WHERE id=?', (self.ident,))
        result = await self.service.generate(max_score=84, salary_min=25000, task_id='growth-search')
        self.assertEqual(result['jobs_analyzed'], 1)
        self.assertEqual(result['job_sources'][0]['score'], 82)
        self.assertEqual(result['resume_version'], 3)
        self.assertEqual(len(self.jobs.learning_plans('synthetic-resume')), 1)
        self.llm.generate.assert_awaited_once()

    async def test_changed_resume_explains_stale_evidence_instead_of_empty_results(self):
        self.doc = self.doc.model_copy(update={'version': 4})
        with self.assertRaisesRegex(ValueError, '简历版本或冻结判定证据已变化'):
            await self.service.generate(max_score=84, task_id='growth-search')
        self.llm.generate.assert_not_awaited()
        self.assertEqual(self.jobs.learning_plans('synthetic-resume'), [])


if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(__import__(__name__, fromlist=['*'])))
    output = Path(__file__).parent / 'results' / 'search-evidence' / datetime.now(UTC).strftime('%Y%m%dT%H%M%S')
    output.mkdir(parents=True, exist_ok=True)
    (output / 'result.json').write_text(json.dumps({'tests': result.testsRun, 'failures': len(result.failures),
        'errors': len(result.errors), 'passed': result.wasSuccessful(), 'external_model_calls': 0}), encoding='utf-8')
    raise SystemExit(not result.wasSuccessful())
