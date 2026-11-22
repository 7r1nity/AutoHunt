"""Actual mock HTML + Playwright, with every network request intercepted.

Only synthetic candidate messages are sent to in-memory route doubles; no local
server, real conversation, resume, model or application is read or changed.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace as NS
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

from app.delivery.mock_site_adapter import MockJobSiteAdapter
from app.delivery.outreach import build_outreach
from app.delivery.automation import DeliveryAutomationService
from app.jobs.store import JobStore
from evaluation.outreach_contract import document, vacancy


class GroundedBrowserMessages(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.context = await self.browser.new_context(service_workers='block')
        await self.context.add_init_script("localStorage.setItem('userId','321');localStorage.setItem('userRole','applicant')")
        self.messages, self.writes, self.checkpoints = [], [], []
        self.read_failure = False
        self.lost_write_response = False
        self.html = Path(__file__).resolve().parents[3] / 'mock-site' / 'frontend'
        await self.context.route('**/*', self.intercept)
        self.page = await self.context.new_page()
        self.url = 'http://127.0.0.1:8080/job.html?id=9000123'
        await self.page.goto(self.url, wait_until='domcontentloaded')
        await self.page.locator('#job-hero h1').wait_for()
        self.adapter = MockJobSiteAdapter()
        self.draft = build_outreach(vacancy(), document())

    async def asyncTearDown(self):
        await self.context.close()
        await self.browser.close()
        await self.playwright.stop()

    async def intercept(self, route):
        target = urlsplit(route.request.url)
        if target.netloc != '127.0.0.1:8080':
            await route.abort()
            return
        assets = {'/job.html': ('job.html', 'text/html'), '/static/js/api.js': ('js/api.js', 'application/javascript'),
                  '/static/css/style.css': ('css/style.css', 'text/css')}
        if target.path in assets:
            file, mime = assets[target.path]
            await route.fulfill(content_type=mime + '; charset=utf-8', body=(self.html / file).read_text(encoding='utf-8'))
            return
        if target.path.startswith('/api/jobs/'):
            data = {'id': 9000123, 'title': '合成数据分析师', 'company_name': '合成雇主', 'city': '深圳',
                    'category': '数据分析', 'department': '合成部门', 'employer_id': 987, 'salary_min': 15,
                    'salary_max': 20, 'experience_required': '不限', 'education': '本科', 'headcount': 1,
                    'description': vacancy()['description'], 'skills': ['Python', 'SQL'], 'requirements': []}
        elif target.path in {'/api/resumes/mine', '/api/applications/mine'}:
            data = []
        elif target.path == '/api/messages':
            if route.request.method == 'POST':
                body = route.request.post_data_json
                self.writes.append(body)
                data = {'id': len(self.messages) + 1, **body, 'sender_id': 321, 'sender_role': 'applicant',
                        'created_at': '2026-01-01T00:00:00Z'}
                self.messages.append(data)
                if self.lost_write_response:
                    await route.abort()
                    return
            elif self.read_failure:
                await route.fulfill(status=503, body='synthetic unavailable')
                return
            else:
                data = list(self.messages)
        else:
            await route.abort()
            return
        await route.fulfill(content_type='application/json', body=json.dumps(data, ensure_ascii=False))

    async def prepare(self):
        return await self.adapter.prepare(self.page, self.url, outreach_message=self.draft['message'],
            before_communication=lambda: self.checkpoints.append('communication_started'))

    async def test_exact_bound_message_is_sent_once_through_real_composer(self):
        observation = await self.prepare()
        self.assertTrue(observation.waiting_for_reply)
        self.assertEqual([item['content'] for item in self.writes], [self.draft['message']])
        self.assertNotIn('希望了解更多岗位信息', self.writes[0]['content'])
        await self.prepare()
        await self.page.reload(wait_until='domcontentloaded')
        await self.prepare()
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.checkpoints, ['communication_started'])

    async def test_already_open_empty_chat_still_sends_frozen_message(self):
        await self.page.evaluate('openChat(false)')
        await self.prepare()
        self.assertEqual([item['content'] for item in self.writes], [self.draft['message']])

    async def test_refiltered_search_does_not_revoke_or_regreet_waiting_hr_delivery(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            store = JobStore(root / 'jobs.db')
            doc = document()
            ident = store.upsert_job(vacancy(9000123))[0]
            service = DeliveryAutomationService(store, root, headless=True, cdp_url='synthetic-only',
                resume_workspace=NS(read_document=lambda: doc))
            preview = await service.create_preview(ident)
            store.authorize_delivery_run(preview.id, 'synthetic-waiting-hr')
            self.draft['message'] = store.delivery_outreach(preview.id)
            await self.prepare()
            store.update_delivery_run('synthetic-waiting-hr', status='waiting_for_reply', checkpoint='waiting_for_reply')
            store.update_job_filter_result(ident, {'status': 'fail', 'digest': 'unrelated-new-search'})
            with store._connect() as db:
                db.execute('UPDATE jobs SET score=12 WHERE id=?', (ident,))
            reopened = JobStore(root / 'jobs.db')
            reopened.assert_delivery_facts(ident, preview.id)
            self.draft['message'] = reopened.delivery_outreach(preview.id)
            await self.page.reload(wait_until='domcontentloaded')
            await self.prepare()
            self.assertEqual(len(self.writes), 1)
            self.assertEqual(self.checkpoints, ['communication_started'])
            self.assertEqual(reopened.get_delivery_run('synthetic-waiting-hr').status, 'waiting_for_reply')

    async def test_existing_manual_conversation_is_not_replaced_or_greeted_again(self):
        self.messages.append({'id': 41, 'sender_id': 321, 'sender_role': 'applicant',
                              'content': '合成的既有人工沟通', 'created_at': '2026-01-01T00:00:00Z'})
        await self.prepare()
        self.assertEqual(self.writes, [])
        self.assertEqual(self.checkpoints, [])

    async def test_message_history_failure_never_sends_another_greeting(self):
        self.read_failure = True
        with self.assertRaises(Exception):
            await self.prepare()
        self.assertEqual(self.writes, [])
        self.assertEqual(self.checkpoints, [])

    async def test_lost_send_response_is_reconciled_by_reading_without_replaying(self):
        self.lost_write_response = True
        await self.prepare()
        self.assertEqual(len(self.writes), 1)
        await self.prepare()
        self.assertEqual(len(self.writes), 1)

    async def test_account_changed_before_send_is_not_allowed(self):
        await self.page.locator('#chat-input').evaluate("node => node.addEventListener('input', () => localStorage.setItem('userId','777'))")
        with self.assertRaisesRegex(ValueError, '账号发生变化'):
            await self.prepare()
        self.assertEqual(self.writes, [])
        self.assertEqual(self.checkpoints, [])


if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(__import__(__name__, fromlist=['*'])))
    output = Path(__file__).parent / 'results' / 'outreach-browser' / datetime.now(UTC).strftime('%Y%m%dT%H%M%S')
    output.mkdir(parents=True, exist_ok=True)
    (output / 'result.json').write_text(json.dumps({'tests': result.testsRun, 'passed': result.wasSuccessful(),
        'failures': len(result.failures), 'errors': len(result.errors), 'external_model_calls': 0,
        'real_recruitment_writes': 0}, ensure_ascii=False), encoding='utf-8')
    raise SystemExit(not result.wasSuccessful())
