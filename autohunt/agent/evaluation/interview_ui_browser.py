"""Synthetic interview UI acceptance on an isolated temporary Next.js server.

No user env, resume, history or live Agent endpoint is used. All browser API
requests are intercepted. This does not evaluate a real model or delivery.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import traceback
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, build_opener

from playwright.async_api import Error as PlaywrightError, async_playwright
from evaluation.job_page_fixture import job_page_response


ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / 'frontend'


def free_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def link_dependencies(source: Path, target: Path):
    if os.name == 'nt':
        result = subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(target), str(source)],
                                capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError('Cannot create the isolated dependency junction: ' + result.stderr.strip())
    else:
        target.symlink_to(source, target_is_directory=True)


def copy_source(destination: Path):
    destination.mkdir()
    for directory in ('app', 'components', 'lib'):
        for source in (FRONTEND / directory).rglob('*'):
            if source.is_file() and source.suffix in {'.ts', '.tsx', '.css', '.svg'}:
                target = destination / source.relative_to(FRONTEND)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
    for filename in ('package.json', 'tsconfig.json', 'next-env.d.ts', 'next.config.ts'):
        shutil.copyfile(FRONTEND / filename, destination / filename)
    if list(destination.rglob('.env*')):
        raise RuntimeError('The isolated frontend must not contain environment files')


def wait_server(origin: str, process):
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('Isolated Next.js exited during startup; inspect frontend.log')
        try:
            with opener.open(origin, timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.5)
    raise TimeoutError('Isolated Next.js did not become ready')


def stop_server(process):
    if process.poll() is not None:
        return
    if os.name == 'nt':
        # Only the process tree created by this runner; no unrelated services.
        subprocess.run(['taskkill.exe', '/PID', str(process.pid), '/T', '/F'], capture_output=True)
    else:
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if os.name != 'nt':
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def report_fixture():
    return {'id': 'synthetic-interview', 'resume_id': 'synthetic-resume', 'resume_document_id': 'synthetic-document',
        'resume_version': 3, 'resume_digest': 'synthetic-digest', 'job_id': 17, 'job_title': '合成算法工程师',
        'company': '合成星海科技', 'source_url': 'https://example.invalid/job/17', 'jd_hash': 'synthetic-jd',
        'created_at': '2026-01-01T08:00:00Z', 'focus': '项目落地', 'generation_mode': 'template',
        'provider': 'mock', 'model': 'synthetic', 'summary': '合成面试准备总结', 'notice': '模板建议，不是招聘方实际问题。',
        'questions': [{'id': 'q1', 'category': 'project', 'question': '请说明你在知识检索项目中的真实贡献。',
            'followups': ['你如何核对检索结果的质量？'], 'preparation': ['准备真实数据、评价方法和你负责的模块。'],
            'jd_quote': '负责知识检索系统的开发与评测。', 'resume_quote': '参与知识检索项目并整理评测集。',
            'candidate_source': '项目经历', 'assessment': 'evidenced'}], 'stale': False, 'stale_reasons': []}


async def exercise(origin: str, output: Path):
    posts, errors, checks, read_queries = [], [], [], []
    reports = []
    delayed = asyncio.Event()
    second_request = asyncio.Event()
    job = {'id': 17, 'role': '合成算法工程师', 'company': '合成星海科技', 'location': '深圳', 'salary': '20-30K',
           'source': '合成模拟站', 'score': 88, 'stage': 'review', 'mode': '人工确认', 'tags': ['检索'],
           'reason': '合成证据', 'posted': '2026-01-01', 'source_url': 'https://example.invalid/job/17'}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(service_workers='block', viewport={'width': 1440, 'height': 1100})

        async def intercept(route):
            nonlocal reports
            target = urlsplit(route.request.url)
            if target.netloc != urlsplit(origin).netloc:
                await route.abort(); return
            if not target.path.startswith('/api/'):
                await route.continue_(); return
            status, payload = 503, {'detail': 'Synthetic UI test: live APIs are disabled'}
            if target.path == '/api/agent/interview-preparations':
                status = 200
                if route.request.method == 'POST':
                    posts.append(route.request.post_data_json)
                    if len(posts) == 1:
                        created = report_fixture()
                        reports = [created]
                        payload = created
                    else:
                        second_request.set()
                        await delayed.wait()
                        payload = reports[0]
                else:
                    read_queries.append(parse_qs(target.query))
                    payload = reports
            elif target.path == '/api/agent/jobs': status, payload = 200, job_page_response([job], target.query)
            elif target.path == '/api/agent/runs': status, payload = 200, []
            elif target.path == '/api/agent/conversations':
                status, payload = 200, [{'id': 'default', 'title': '合成会话', 'created_at': '2026-01-01T00:00:00Z'}]
            elif target.path in {'/api/agent/chat', '/api/agent/resumes', '/api/agent/materials', '/api/agent/learning-plans'}:
                status, payload = 200, []
            elif target.path in {'/api/agent/browser', '/api/agent/delivery'}:
                status, payload = 200, {'status': 'idle', 'detail': '合成空闲'}
            elif target.path == '/api/agent/stats':
                status, payload = 200, {'review': 1, 'applying': 0, 'waiting': 0, 'interview': 0, 'indeterminate': 0,
                    'discovered': 1, 'stored': 1, 'duplicates': 0, 'matched': 1}
            try:
                await route.fulfill(status=status, content_type='application/json', body=json.dumps(payload, ensure_ascii=False))
            except PlaywrightError:
                if target.path != '/api/agent/interview-preparations' or route.request.method != 'POST' or len(posts) < 2:
                    raise

        await context.route('**/*', intercept)
        page = await context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            # Wait for a client-effect request: DOMContentLoaded alone can precede
            # React hydration, so an early click on the SSR nav can be lost.
            async with page.expect_response(lambda response: urlsplit(response.url).path == '/api/agent/conversations'):
                await page.goto(origin, wait_until='domcontentloaded', timeout=90_000)
            await page.get_by_role('button', name=re.compile(r'^申请看板')).click()
            await page.get_by_role('button', name=re.compile(r'^查看 合成算法工程师')).click()
            await page.get_by_role('button', name='预测面试问题', exact=True).click()
            dialog = page.get_by_role('dialog', name='预测面试问题')
            await dialog.wait_for()
            await dialog.get_by_text('这个岗位还没有当前简历的面试准备报告。', exact=False).wait_for()
            await dialog.get_by_label('关注方向（可选）').fill('项目落地')
            await dialog.get_by_role('button', name='生成本岗位面试准备', exact=True).click()
            await dialog.get_by_text('已保存模板准备建议。', exact=True).wait_for()
            assert posts == [{'job_id': 17, 'focus': '项目落地'}], posts
            checks.append('exact_job_id_and_focus')
            await dialog.get_by_role('heading', name='请说明你在知识检索项目中的真实贡献。').wait_for()
            await dialog.get_by_text('查看问题依据', exact=True).click()
            await dialog.get_by_text('负责知识检索系统的开发与评测。', exact=True).wait_for()
            assert await dialog.get_by_role('link', name='打开岗位原文 ↗').get_attribute('href') == 'https://example.invalid/job/17'
            checks.append('template_questions_evidence_and_clickable_source')
            reports[0] = {**reports[0], 'stale': True, 'stale_reasons': ['resume_changed'],
                          'source_url': 'javascript:alert(1)'}
            await dialog.get_by_role('button', name='刷新已保存报告', exact=True).click()
            await dialog.get_by_text('简历或 JD 已有变化，这份报告使用旧快照。请按当前简历重新生成。', exact=True).wait_for()
            assert await dialog.locator('a[href^="javascript:"]').count() == 0
            assert await dialog.get_by_role('link', name='打开岗位原文 ↗').count() == 0
            checks += ['stale_snapshot_visible', 'unsafe_source_url_not_linked']
            await dialog.screenshot(path=str(output / 'desktop.png'))
            await page.set_viewport_size({'width': 390, 'height': 844})
            await dialog.screenshot(path=str(output / 'mobile.png'))
            assert await dialog.evaluate('el => el.scrollWidth <= el.clientWidth + 1')
            checks.append('mobile_dialog_no_horizontal_overflow')
            await dialog.get_by_role('button', name='按当前简历重新生成', exact=True).click()
            await asyncio.wait_for(second_request.wait(), timeout=10)
            await dialog.get_by_role('button', name='停止等待', exact=True).click()
            await dialog.get_by_text('已停止等待。后台生成可能仍在继续', exact=False).wait_for()
            refreshed = report_fixture()
            refreshed.update(id='synthetic-background-result', summary='后台生成完成后可刷新核对。',
                             generation_mode='model', created_at='2026-01-01T08:01:00Z')
            reports = [refreshed, report_fixture()]
            delayed.set()
            await dialog.get_by_role('button', name='刷新已保存报告', exact=True).click()
            await dialog.get_by_text('后台生成完成后可刷新核对。', exact=True).wait_for()
            assert len(posts) == 2, posts
            checks.append('stop_waiting_then_read_saved_result_without_reissue')
            assert read_queries and all(query.get('job_id') == ['17'] for query in read_queries), read_queries
            checks.append('saved_reports_read_bound_to_exact_job')
            await page.keyboard.press('Escape')
            await dialog.wait_for(state='detached')
            await page.get_by_role('dialog', name='职位详情').wait_for()
            checks.append('escape_returns_to_job_details')
            assert not errors, errors
            checks.append('no_browser_runtime_errors')
            return {'passed': True, 'checks': checks, 'post_arguments': posts, 'read_queries': read_queries, 'page_errors': errors,
                    'coverage': 'Synthetic UI only: no model, user data, search, communication or application.'}
        except Exception:
            return {'passed': False, 'checks': checks, 'post_arguments': posts, 'page_errors': errors,
                    'error': traceback.format_exc(), 'body': await page.locator('body').inner_text()}
        finally:
            delayed.set()
            await browser.close()


def main():
    node = shutil.which('node')
    next_cli = FRONTEND / 'node_modules' / 'next' / 'dist' / 'bin' / 'next'
    if not node or not next_cli.is_file():
        raise RuntimeError('Install the frontend dependencies before running this isolated UI check')
    output = Path(__file__).parent / 'results' / 'interview-ui' / datetime.now(UTC).strftime('%Y%m%dT%H%M%S.%fZ')
    output.mkdir(parents=True)
    temporary = tempfile.TemporaryDirectory(prefix='autohunt-interview-ui-')
    temp_root = Path(temporary.name)
    source = temp_root / 'frontend'
    junctions = []
    server = None
    result = {'passed': False}
    try:
        copy_source(source)
        for dependency_root, target in ((FRONTEND / 'node_modules', source / 'node_modules'),
                                        (ROOT / 'node_modules', temp_root / 'node_modules')):
            link_dependencies(dependency_root, target)
            junctions.append(target)
        port = free_port()
        origin = f'http://127.0.0.1:{port}'
        env = {key: value for key, value in os.environ.items()
               if not key.startswith('AUTOHUNT_') and not key.endswith(('_API_KEY', '_TOKEN'))}
        env.update(NEXT_TELEMETRY_DISABLED='1', AUTOHUNT_BACKEND_URL='http://127.0.0.1:9',
                   AUTOHUNT_BACKEND_TOKEN='synthetic-interview-ui-only')
        options = {'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0)} if os.name == 'nt' else {'start_new_session': True}
        with (output / 'frontend.log').open('wb') as log:
            server = subprocess.Popen([node, str(next_cli), 'dev', '--webpack', '--hostname', '127.0.0.1', '--port', str(port)],
                                      cwd=source, env=env, stdout=log, stderr=log, **options)
            wait_server(origin, server)
            result = asyncio.run(exercise(origin, output))
    except Exception:
        result = {'passed': False, 'error': traceback.format_exc()}
    finally:
        if server is not None:
            stop_server(server)
        for target in reversed(junctions):
            if os.name == 'nt':
                os.rmdir(target)
            else:
                target.unlink()
        temporary.cleanup()
        (output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'output': str(output), **result}, ensure_ascii=False))
    if not result['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
