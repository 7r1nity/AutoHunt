"""Interview preparation interface contracts; fictional material and temp stores.

No external model calls, browser actions or recruitment side effects are used.
"""
from __future__ import annotations

import asyncio
import gc
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest

from cryptography.fernet import Fernet

from app.core.models import ResumeDocumentView, ResumeEntryView
from app.growth.interview import InterviewPreparationService
from app.jobs.store import JobStore
from app.llm.base import LLMResult


JD = ('岗位负责经营数据分析与月度财务报告。需要使用SQL核对收入和现金流，说明数据口径与异常。'
      '与业务和财务团队协作，建立可复核的分析流程，及时解释模型假设及风险。')
PROJECT = '使用SQL核对模拟经营数据，按收入与现金流建立分析报告。'
WORK = '整理客户需求，协调交付期限并核对财务资料。'


class ScriptedModel:
    def __init__(self, answers=(), *, provider='deepseek', mutation=None, delay=0):
        self.answers = list(answers)
        self.calls = []
        self.settings_store = NS(read=lambda: {'provider': provider})
        self.mutation, self.delay = mutation, delay

    async def generate(self, request):
        self.calls.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.mutation:
            self.mutation()
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return LLMResult(text=answer, provider='deepseek', model='synthetic-interview-model')


def response(*questions):
    return json.dumps({'questions': questions}, ensure_ascii=False)


def question(category='project', jd_quote='需要使用SQL核对收入和现金流', source='project:financial', quote=PROJECT):
    return {'category': category, 'jd_quote': jd_quote, 'candidate_source': source, 'resume_quote': quote}


class InterviewContracts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='autohunt-interview-synthetic-')
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(gc.collect)
        self.root = Path(self.temp.name)
        self.jobs = JobStore(self.root / 'jobs.db')
        self.job_id, _ = self.jobs.upsert_job({'source': 'Synthetic', 'source_url': 'http://127.0.0.1:8080/job.html?id=73',
            'external_id': 'synthetic-73', 'title': '财务分析师', 'company': '虚构企业', 'location': '深圳',
            'salary': '18-25K', 'description': JD, 'tags': ['SQL', '财务分析'],
            'score': 82, 'decision': 'REVIEW', 'reasoning': 'Synthetic interview contract fixture'})
        self.profile = NS(resume_id='synthetic-resume', qualification_facts=[])
        self.candidates = NS(read=lambda: NS(profile=self.profile))
        self.document = ResumeDocumentView(raw_content='PRIVATE_FULL_RESUME_MUST_NOT_LEAVE 联系电话：13812345678 邮箱：private@example.invalid',
            version=2, skills=['SQL', '财务分析'], entries=[
                ResumeEntryView(id='financial', kind='project', title='模拟财务分析', summary=PROJECT, highlights=['建立口径核对清单。']),
                ResumeEntryView(id='work', kind='experience', title='虚构企业实践', summary=WORK),
            ])
        self.resumes = NS(read_document=lambda: self.document)
        self.key = Fernet.generate_key().decode()

    def service(self, model=None, **kwargs):
        return InterviewPreparationService(self.jobs, self.candidates, self.resumes,
            model or ScriptedModel(provider='mock'), self.root, self.key, **kwargs)

    def saved_paths(self):
        return list((self.root / 'private' / 'interviews').glob('*.json.enc'))

    async def test_mock_mode_has_no_model_calls_and_persists_encrypted_evidence_bound_report(self):
        model = ScriptedModel(provider='mock')
        service = self.service(model)
        report = await service.generate(self.job_id)
        self.assertEqual(model.calls, [])
        self.assertEqual(report['generation_mode'], 'template')
        self.assertIn('未使用真实模型', report['notice'])
        self.assertEqual(report['resume_version'], 2)
        self.assertFalse(report['stale'])
        self.assertGreaterEqual(len(report['questions']), 3)
        for item in report['questions']:
            self.assertIn(item['jd_quote'], JD)
            self.assertTrue(item['followups'])
            self.assertTrue(item['preparation'])
        ciphertext = self.saved_paths()[0].read_bytes()
        self.assertNotIn(PROJECT.encode(), ciphertext)
        self.assertEqual(self.service().get(report['id'])['questions'], report['questions'])

    async def test_real_model_selects_evidence_but_cannot_invent_question_prose(self):
        model = ScriptedModel([response(question())])
        report = await self.service(model).generate(self.job_id, '请重点准备项目方法与分析口径')
        self.assertEqual(report['generation_mode'], 'model')
        self.assertEqual(report['provider'], 'deepseek')
        self.assertIn(PROJECT, report['questions'][0]['question'])
        self.assertIn('原文核对', report['notice'])
        self.assertEqual(len(model.calls), 1)
        self.assertIn('JSON', model.calls[0].instructions)
        self.assertTrue(model.calls[0].json_output)

    async def test_model_receives_minimal_redacted_evidence_not_raw_resume_or_contacts(self):
        self.document.entries[0].highlights.append('联系电话：13812345678 邮箱：private@example.invalid')
        model = ScriptedModel([response(question())])
        await self.service(model).generate(self.job_id)
        outbound = model.calls[0].user_message
        self.assertNotIn('PRIVATE_FULL_RESUME_MUST_NOT_LEAVE', outbound)
        self.assertNotIn('13812345678', outbound)
        self.assertNotIn('private@example.invalid', outbound)
        self.assertNotIn('raw_content', outbound)
        self.assertIn('experience:work', outbound)

    async def test_invalid_evidence_is_corrected_once_without_persisting_an_invalid_report(self):
        model = ScriptedModel([response(question(quote='候选人拥有十年管理经验')), response(question())])
        report = await self.service(model).generate(self.job_id)
        self.assertEqual(len(model.calls), 2)
        self.assertNotIn('十年管理经验', json.dumps(report, ensure_ascii=False))
        self.assertEqual(len(self.saved_paths()), 1)

    async def test_repeated_invalid_jd_quote_fails_without_any_report(self):
        invalid = response(question(jd_quote='该职位要求取得不存在的认证'))
        model = ScriptedModel([invalid, invalid])
        with self.assertRaisesRegex(ValueError, '未保存报告'):
            await self.service(model).generate(self.job_id)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(self.saved_paths(), [])

    async def test_model_cannot_smuggle_free_form_claims_into_persisted_questions(self):
        payload = question()
        payload['question'] = '你在真实企业实现了百分之九十九的收益，请详细说明'
        model = ScriptedModel([response(payload), response(question())])
        report = await self.service(model).generate(self.job_id)
        self.assertNotIn('百分之九十九', json.dumps(report, ensure_ascii=False))
        self.assertEqual(len(model.calls), 2)

    async def test_project_question_requires_an_actual_project_or_experience_source(self):
        invalid = response(question(source='skills', quote='SQL'))
        model = ScriptedModel([invalid, invalid])
        with self.assertRaisesRegex(ValueError, '未保存报告'):
            await self.service(model).generate(self.job_id)
        self.assertEqual(self.saved_paths(), [])

    async def test_timeout_is_bounded_and_does_not_create_partial_report(self):
        model = ScriptedModel([response(question())], delay=1)
        with self.assertRaisesRegex(ValueError, '超时'):
            await self.service(model, timeout_seconds=0.01).generate(self.job_id)
        self.assertEqual(self.saved_paths(), [])

    async def test_provider_failure_does_not_automatically_replay_network_calls(self):
        model = ScriptedModel([RuntimeError('synthetic provider outage')])
        with self.assertRaisesRegex(ValueError, '暂不可用'):
            await self.service(model).generate(self.job_id)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(self.saved_paths(), [])

    async def test_cancellation_propagates_without_report_or_second_attempt(self):
        model = ScriptedModel([response(question())], delay=2)
        task = asyncio.create_task(self.service(model).generate(self.job_id))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.saved_paths(), [])
        self.assertLessEqual(len(model.calls), 1)

    async def test_resume_change_during_generation_is_not_saved_as_current(self):
        def mutate():
            self.document = self.document.model_copy(update={'version': 3})
        model = ScriptedModel([response(question())], mutation=mutate)
        with self.assertRaisesRegex(ValueError, '生成期间简历或岗位已更新'):
            await self.service(model).generate(self.job_id)
        self.assertEqual(self.saved_paths(), [])

    async def test_resume_and_jd_updates_mark_existing_report_stale(self):
        service = self.service()
        report = await service.generate(self.job_id)
        self.document = self.document.model_copy(update={'version': 3})
        self.assertEqual(service.get(report['id'])['stale_reasons'], ['resume_changed'])
        changed = self.jobs.get_job(self.job_id)
        changed['description'] += '另需分析新的业务指标。'
        self.jobs.upsert_job(changed)
        self.assertCountEqual(service.get(report['id'])['stale_reasons'], ['resume_changed', 'job_changed'])

    async def test_other_resume_reports_are_not_returned_and_paths_cannot_escape_storage(self):
        service = self.service()
        report = await service.generate(self.job_id)
        self.profile.resume_id = 'different-synthetic-resume'
        self.assertEqual(service.list(), [])
        with self.assertRaises(KeyError):
            service.get(report['id'])
        with self.assertRaises(KeyError):
            service.get('../outside')

    async def test_corrupted_report_is_not_exposed_as_valid(self):
        service = self.service()
        report = await service.generate(self.job_id)
        self.saved_paths()[0].write_bytes(b'corrupt synthetic ciphertext')
        with self.assertRaisesRegex(ValueError, '无法解密'):
            service.get(report['id'])
        self.assertEqual(service.list(), [])

    async def test_per_job_history_is_filtered_before_global_limit(self):
        service = self.service()
        earliest = await service.generate(self.job_id)
        original = self.jobs.get_job(self.job_id)
        for index in range(21):
            ident, _ = self.jobs.upsert_job({**original, 'id': None, 'title': f'虚构分析岗位{index}',
                'external_id': f'list-limit-{index}', 'source_url': f'http://127.0.0.1:8080/job.html?id={1000+index}'})
            await service.generate(ident)
        self.assertEqual(len(service.list()), 20)
        self.assertEqual([report['id'] for report in service.list(job_id=self.job_id)], [earliest['id']])

    async def test_missing_resume_or_short_jd_is_rejected_before_model_call(self):
        model = ScriptedModel([response(question())])
        self.profile.resume_id = None
        with self.assertRaisesRegex(ValueError, '启用主简历'):
            await self.service(model).generate(self.job_id)
        self.profile.resume_id = 'synthetic-resume'
        changed = self.jobs.get_job(self.job_id)
        changed['description'] = 'SQL'
        self.jobs.upsert_job(changed)
        with self.assertRaisesRegex(ValueError, 'JD 原文'):
            await self.service(model).generate(self.job_id)
        self.assertEqual(model.calls, [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
