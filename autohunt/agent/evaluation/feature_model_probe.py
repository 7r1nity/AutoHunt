"""Fresh-consent synthetic model probe for outreach and interview modules.

Only the configured DeepSeek credential/settings are read. Each physical
request is reserved in one persistent ledger before transport, with SDK and
application transport retries disabled. No recruitment operation is executed.
"""
from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import json
from pathlib import Path
import tempfile
import time
from types import MethodType, SimpleNamespace as NS

from cryptography.fernet import Fernet

from app.core.models import ResumeDocumentView, ResumeEntryView
from app.core.security import redact
from app.delivery.outreach import OutreachService
from app.interviews.service import InterviewPreparationService
from evaluation.benchmark.common import CASE, PhysicalBudget, append, write
from evaluation.run import consented_provider


CEILING = 12


def fixtures():
    rows = [
        ('rag', '知识库应用工程师', ['Python', 'RAG', '检索评测'],
         '使用Python构建虚构文档检索与问答原型，比较检索结果并保存评测记录。',
         '负责企业知识库的检索与问答功能，使用Python维护数据处理流程。需要设计检索评测、分析错误案例并说明结果局限。与产品和业务人员澄清需求，持续核对服务质量。'),
        ('finance', '财务分析师', ['SQL', '财务分析', '现金流'],
         '使用SQL整理模拟财务数据，核对收入与现金流口径并制作分析报告。',
         '负责经营数据分析和月度财务报告，使用SQL核对收入与现金流。需要解释数据口径、异常与财务分析假设，保留可复核资料。与业务和财务团队协作推进预算分析。'),
        ('trade', '海外客户开发专员', ['商务英语', '客户开发', '询盘报价'],
         '整理虚构客户询盘，用商务英语编写报价说明并记录交付风险与跟进事项。',
         '负责海外客户开发和询盘报价，使用商务英语整理需求与报价资料。需要核对客户要求、出口交付期限和贸易条款，形成可追踪的跟进记录。与供应链团队沟通风险。'),
        ('quality', '机械质量工程师', ['质量检验', '测量记录', '异常分析'],
         '基于模拟零件测量记录整理偏差，编写质量检验清单并追踪异常处理结果。',
         '负责机械零件质量检验与测量记录核对，分析偏差并提出复核方案。需要制定检验清单、追踪异常与返工情况，解释测量误差和验收标准。与制造团队协作解决交付问题。'),
    ]
    return [{'id': ident, 'title': title, 'skills': skills, 'project': project, 'jd': jd}
            for ident, title, skills, project, jd in rows]


def corpus():
    return [{**row, 'feature': feature, 'case_id': feature + '-' + row['id']}
            for row in fixtures() for feature in ('outreach', 'interview')]


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def physical_provider(output: Path, limit: int):
    budget = PhysicalBudget(output / 'budget.json', limit, ceiling=CEILING)
    model = consented_provider(limit, [])

    async def completion(self, **kwargs):
        number = budget.reserve()
        started = time.perf_counter()
        entry = {'call': number, 'case': CASE.get(), 'endpoint': 'https://api.deepseek.com',
                 'request': redact(kwargs)}
        try:
            # No safe_retry wrapper; one SDK transport, max_retries=0.
            async with asyncio.timeout(40):
                response = await self.client.chat.completions.create(**kwargs)
            entry['response'] = redact(response.model_dump(mode='json'))
            return response
        except BaseException as error:
            entry['error_type'] = type(error).__name__
            entry['http_status'] = getattr(error, 'status_code', None)
            if getattr(error, 'body', None) is not None:
                entry['provider_error'] = redact(error.body)
            raise
        finally:
            entry['latency_ms'] = round((time.perf_counter() - started) * 1000, 2)
            append(output / 'model-calls.jsonl', entry)

    model._completion = MethodType(completion, model)
    model.budget = budget
    return model


def workspace(row):
    document = ResumeDocumentView(id='synthetic-master-' + row['id'], version=1, skills=row['skills'],
        raw_content='SYNTHETIC_RAW_RESUME_NOT_FOR_EXPORT\n手机：13800138000\n邮箱：synthetic@example.invalid',
        entries=[ResumeEntryView(id='project-' + row['id'], kind='project', title='虚构实践项目',
                                  summary=row['project'])])
    candidate = NS(read=lambda: NS(profile=NS(resume_id='synthetic-resume-' + row['id'], qualification_facts=[])))
    return document, NS(read_document=lambda: document), candidate


async def exercise(row, model):
    document, resumes, candidates = workspace(row)
    job = {'id': 101, 'title': row['title'], 'company': '完全虚构的演示企业', 'description': row['jd'],
           'source_url': 'http://127.0.0.1:8080/job.html?id=101', 'content_hash': fingerprint(row['jd'])}
    if row['feature'] == 'outreach':
        result = await OutreachService(model).draft(job, document)
        checks = {'valid_message': bool(result.get('message')) and len(result['message']) <= 600,
                  'jd_grounded': all(item['quote'] in row['jd'] for item in result.get('evidence', []) if item['source'] == 'jd'),
                  'resume_grounded': all(item['quote'] in '\n'.join([row['project'], *row['skills']])
                                         for item in result.get('evidence', []) if item['source'] == 'resume'),
                  'has_both_sources': {item['source'] for item in result.get('evidence', [])} == {'jd', 'resume'},
                  'not_full_resume_or_contacts': not any(value in result['message'] for value in
                      ('SYNTHETIC_RAW_RESUME_NOT_FOR_EXPORT', '13800138000', 'synthetic@example.invalid'))}
        return {'checks': checks, 'passed': all(checks.values()),
                'model_selection_completed': result['method'] == 'model_selected', 'result': result}
    with tempfile.TemporaryDirectory(prefix='autohunt-real-synthetic-interview-') as folder:
        jobs = NS(get_job=lambda ident: job if ident == 101 else (_ for _ in ()).throw(KeyError(ident)))
        service = InterviewPreparationService(jobs, candidates, resumes, model, Path(folder), Fernet.generate_key().decode())
        result = await service.generate(101, '根据真实提供的项目证据和岗位要求准备面试，不补写没有发生的经历。')
        evidence = {'project:project-' + row['id']: '虚构实践项目\n' + row['project'],
                    'skills': '、'.join(row['skills'])}
        checks = {'real_model_mode': result['generation_mode'] == 'model',
                  'question_count': 3 <= len(result['questions']) <= 8,
                  'jd_grounded': all(item['jd_quote'] in row['jd'] for item in result['questions']),
                  'resume_grounded': all(not item['resume_quote'] or
                                        item['resume_quote'] in evidence.get(item['candidate_source'], '')
                                        for item in result['questions']),
                  'followups_and_preparation': all(item['followups'] and item['preparation'] for item in result['questions']),
                  'not_full_resume_or_contacts': not any(value in json.dumps(result, ensure_ascii=False) for value in
                      ('SYNTHETIC_RAW_RESUME_NOT_FOR_EXPORT', '13800138000', 'synthetic@example.invalid')),
                  'not_stale': not service.get(result['id'])['stale'],
                  'encrypted_storage': row['project'].encode() not in next(service.directory.glob('*.json.enc')).read_bytes()}
        return {'checks': checks, 'passed': all(checks.values()), 'result': result}


async def run(args):
    if not args.models_consented or not 1 <= args.max_calls <= CEILING:
        raise ValueError('Fresh consent for synthetic DeepSeek requests and budget 1..12 required')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    rows = corpus()
    selected_rows = [row for row in rows if not args.cases or row['case_id'] in args.cases]
    metadata = {'dataset_hash': fingerprint(rows), 'limit': args.max_calls, 'endpoint': 'https://api.deepseek.com',
                'data': 'new fictional candidate projects, JD and focus only', 'recruitment_operations': 0}
    manifest = output / 'manifest.json'
    if manifest.exists() and json.loads(manifest.read_text(encoding='utf-8')) != metadata:
        raise ValueError('Cannot replace the corpus or budget of an existing consent ledger')
    write(manifest, metadata)
    results_file = output / 'results.json'
    previous = json.loads(results_file.read_text(encoding='utf-8')) if results_file.exists() else []
    latest = {row['case_id']: row for row in previous}
    completed = {ident for ident, row in latest.items() if not args.retry_failed or row['passed']}
    results = list(previous)
    model = physical_provider(output, args.max_calls)
    try:
        for row in selected_rows:
            if row['case_id'] in completed:
                continue
            if model.budget.state()['used'] >= args.max_calls:
                break
            token = CASE.set(row['case_id'])
            before = model.budget.state()['used']
            started = time.perf_counter()
            item = {'case_id': row['case_id'], 'feature': row['feature'], 'fictional_input': redact(row)}
            try:
                item.update(await exercise(row, model))
            except Exception as error:
                item.update(passed=False, error_type=type(error).__name__, error=redact(str(error)))
            finally:
                CASE.reset(token)
            item.update(physical_requests=model.budget.state()['used'] - before,
                        latency_ms=round((time.perf_counter() - started) * 1000, 2))
            results.append(item)
            write(results_file, results)
            print(json.dumps({key: item[key] for key in ('case_id', 'passed', 'physical_requests', 'latency_ms')}, ensure_ascii=False), flush=True)
            gc.collect()
    finally:
        await model.client.close()
    latest = {row['case_id']: row for row in results}
    latest_rows = list(latest.values())
    summary = {'physical_requests': model.budget.state()['used'], 'consented_limit': args.max_calls,
               'attempts_run': len(results), 'cases_run': len(latest_rows), 'cases_passed': sum(bool(row['passed']) for row in latest_rows),
               'cases_expected': len(selected_rows), 'unrun_cases': [row['case_id'] for row in selected_rows if row['case_id'] not in {r['case_id'] for r in results}],
               'features': {feature: {'cases': sum(row['feature'] == feature for row in latest_rows),
                   'passed': sum(row['feature'] == feature and row['passed'] for row in latest_rows)} for feature in ('outreach', 'interview')},
               'outreach_model_selected': sum(row['feature'] == 'outreach' and row.get('model_selection_completed', False) for row in results),
               'outreach_template_fallbacks': sum(row['feature'] == 'outreach' and not row.get('model_selection_completed', False) for row in results),
               'external_recruitment_operations': 0, 'searches': 0, 'submissions': 0,
               'measurement': 'Module correctness and evidence provenance; no human quality gold, planner accuracy or browser E2E claim.'}
    write(output / 'summary.json', summary)
    print(json.dumps({'output': str(output), **summary}, ensure_ascii=False), flush=True)
    return 0 if not summary['unrun_cases'] and all(latest[row['case_id']]['passed'] for row in selected_rows) else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--models-consented', action='store_true')
    parser.add_argument('--max-calls', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--retry-failed', action='store_true', help='Use the same remaining ledger; retain all failed attempts')
    parser.add_argument('--cases', nargs='+', choices=[row['case_id'] for row in corpus()])
    raise SystemExit(asyncio.run(run(parser.parse_args())))
