"""Real browser checks against an isolated local annotation database only."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).parents[2]
OUTPUT = ROOT / 'evaluation/results/human-annotation-browser'


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        env = {**os.environ, 'AUTOHUNT_ANNOTATION_DB': str(Path(temp) / 'isolated.sqlite.db')}
        with (OUTPUT / 'server.log').open('w', encoding='utf-8') as log:
            process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'evaluation.human_annotation.web:app', '--host', '127.0.0.1', '--port', str(port)], cwd=ROOT, env=env, stdout=log, stderr=log)
            try:
                url = f'http://127.0.0.1:{port}'
                for _ in range(100):
                    if process.poll() is not None:
                        raise RuntimeError('Isolated server exited; see server.log')
                    try:
                        if httpx.get(url + '/api/status', trust_env=False, timeout=1).status_code == 200:
                            break
                    except httpx.RequestError:
                        pass
                    time.sleep(.1)
                else:
                    raise RuntimeError('Isolated server not ready')
                checks = []
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(channel='chrome', headless=True)
                    context = browser.new_context(viewport={'width': 1440, 'height': 1050}, reduced_motion='reduce')
                    page = context.new_page()
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    external = []
                    page.on('request', lambda request: external.append(request.url) if not request.url.startswith(url) else None)
                    page.goto(url)
                    expect(page.locator('.queue-item')).to_have_count(64)
                    expect(page.locator('#candidate-lines .doc-line')).to_have_count(7)
                    expect(page.locator('#job-lines .doc-line')).to_have_count(6)
                    expect(page.locator('#reference-results')).to_be_hidden()
                    checks.append('64 blinded calibration tasks render with original documents')
                    first = page.locator('#pair-id').inner_text()
                    page.locator('[data-source=candidate][data-line=c2]').click()
                    page.locator('[data-source=job][data-line=j2]').click()
                    page.locator('[data-relevance="0"]').click()
                    page.locator('[data-eligibility=fail]').click()
                    page.locator('#notes').fill('年限不满足；相关技能不能抵消硬要求。')
                    page.locator('#save').click()
                    expect(page.locator('#save-status')).to_have_text('草稿已保存')
                    page.reload()
                    expect(page.locator('[data-relevance="0"]')).to_have_attribute('aria-pressed', 'true')
                    expect(page.locator('#notes')).to_have_value('年限不满足；相关技能不能抵消硬要求。')
                    expect(page.locator('#evidence-count')).to_have_text('2 段')
                    checks.append('zero grade, evidence and notes survive reload')
                    page.locator('#complete').click()
                    expect(page.locator('#pair-id')).not_to_have_text(first)
                    expect(page.locator('#progress-text')).to_have_text('已完成 1 / 64')
                    checks.append('complete advances once and updates persisted progress')
                    page.locator('#notes').fill('输入数字 123 和 qwe 不应改标签')
                    page.locator('#notes').press('1')
                    expect(page.locator('[data-relevance="0"]')).to_have_attribute('aria-pressed', 'false')
                    page.locator('#save').click()
                    expect(page.locator('#save-status')).to_have_text('草稿已保存')
                    page.locator('#sample-number').click()
                    page.keyboard.press('2')
                    expect(page.locator('[data-relevance="1"]')).to_have_attribute('aria-pressed', 'true')
                    page.keyboard.press('e')
                    expect(page.locator('[data-eligibility=unknown]')).to_have_attribute('aria-pressed', 'true')
                    page.locator('[data-source=candidate][data-line=c3]').click()
                    page.locator('[data-source=job][data-line=j3]').click()
                    page.locator('#save').click()
                    expect(page.locator('#save-status')).to_have_text('草稿已保存')
                    checks.append('keyboard shortcuts work outside note inputs')
                    page.screenshot(path=str(OUTPUT / 'desktop.png'), full_page=True)
                    with page.expect_download() as download:
                        page.locator('#export-open').click()
                        page.locator('a[href="/api/export?format=json"]').click()
                    export_path = OUTPUT / 'isolated-export.json'
                    download.value.save_as(str(export_path))
                    data = json.loads(export_path.read_text(encoding='utf-8'))
                    assert len(data['reviews']) == 2 and data['reference_count'] == 0
                    page.locator('[data-close=export-dialog]').click()
                    checks.append('JSON download contains saved reviews and no fabricated reference labels')
                    page.locator('#compare').click()
                    expect(page.locator('#reference-results')).to_be_visible()
                    expect(page.locator('#reference-warning')).to_contain_text('未导入 Jev')
                    checks.append('reference view is opt-in and records exposure')
                    for width, height, name in [(1024, 900, 'tablet'), (390, 844, 'mobile')]:
                        page.set_viewport_size({'width': width, 'height': height})
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), f'Horizontal overflow at {width}'
                        page.locator('#queue-toggle').click()
                        expect(page.locator('#queue-panel')).to_be_visible()
                        page.locator('#queue-toggle').click()
                        page.screenshot(path=str(OUTPUT / f'{name}.png'), full_page=True)
                    checks.append('tablet and mobile layouts have no horizontal overflow and usable sample navigation')
                    page.set_viewport_size({'width': 1440, 'height': 1050})
                    page.locator('#scope').select_option('all')
                    expect(page.locator('.queue-item')).to_have_count(300)
                    page.locator('#state').select_option('submitted')
                    expect(page.locator('.queue-item')).to_have_count(1)
                    page.locator('#state').select_option('flagged')
                    expect(page.locator('#empty')).to_be_visible()
                    checks.append('scope and status filters, including empty state')
                    assert not errors, errors
                    assert not external, external
                    checks.append('no JavaScript errors or external network requests')
                    browser.close()
                summary = {'passed': len(checks), 'checks': checks, 'models_called': 0, 'recruitment_operations': 0, 'isolated_database': True}
                (OUTPUT / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            finally:
                process.terminate()
                process.wait(timeout=15)


if __name__ == '__main__':
    main()
