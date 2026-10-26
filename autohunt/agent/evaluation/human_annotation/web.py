"""Run: python -m uvicorn evaluation.human_annotation.web:app --host 127.0.0.1 --port 8011."""
from __future__ import annotations

import csv
import io
import os
import secrets
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from evaluation.jev_labels.corpus import ROOT, digest, load
from .store import AnnotationStore, Conflict

STATIC = Path(__file__).parent / 'static'
Slot = Literal['a', 'b', 'adjudicated']


class Evidence(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source: Literal['candidate', 'job']
    line_id: str = Field(min_length=2, max_length=12)


class Review(BaseModel):
    model_config = ConfigDict(extra='forbid')
    slot: Slot
    reviewer: str = Field(min_length=1, max_length=80)
    expected_revision: int = Field(ge=0)
    relevance: Literal[0, 1, 2] | None = None
    eligibility: Literal['pass', 'fail', 'unknown'] | None = None
    evidence: list[Evidence] = Field(default_factory=list, max_length=40)
    notes: str = Field(default='', max_length=10000)
    status: Literal['draft', 'submitted', 'skipped'] = 'draft'
    flagged: bool = False


class Reveal(BaseModel):
    reviewer: str = Field(min_length=1, max_length=80)


def create_app(path: Path | None = None):
    path = path or Path(os.environ.get('AUTOHUNT_ANNOTATION_DB') or ROOT / 'evaluation/results/human-annotations' / digest(load())[:12] / 'annotations.sqlite.db')
    store = AnnotationStore(path)
    app = FastAPI(title='AutoHunt 人工标注工作台', docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    token = secrets.token_urlsafe(32)

    @app.middleware('http')
    async def local_boundary(request: Request, call_next):
        # DNS rebinding and cross-site writes are rejected even when bound locally.
        if request.url.hostname not in ('127.0.0.1', 'localhost', '::1', 'testserver'):
            return JSONResponse({'detail': '仅支持本机访问。'}, status_code=403)
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            origin = request.headers.get('origin')
            if (origin and origin != str(request.base_url).rstrip('/')) or request.headers.get('sec-fetch-site') == 'cross-site' or request.headers.get('x-annotation-token') != token:
                return JSONResponse({'detail': '页面校验失效，请在本机刷新标注页后重试。'}, status_code=403)
            if int(request.headers.get('content-length', '0')) > 64000:
                return JSONResponse({'detail': '保存内容过大。'}, status_code=413)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        return response

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({'detail': '未找到该样本。'}, status_code=404)

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=409)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=422)

    @app.get('/')
    def index():
        return FileResponse(STATIC / 'index.html')

    @app.get('/api/status')
    def status():
        return {'token': token, 'dataset_version': store.data['version'], 'dataset_sha256': store.sha,
                'pairs': len(store.pairs), 'calibration': len(store.calibration), 'synthetic_only': True}

    @app.get('/api/tasks')
    def tasks(scope: Literal['calibration', 'development', 'test', 'all'] = 'calibration', slot: Slot = 'a',
              state: Literal['all', 'pending', 'submitted', 'flagged'] = 'all', q: str = Query(default='', max_length=200)):
        return store.queue(scope, slot, state, q)

    @app.get('/api/tasks/{pair_id}')
    def task(pair_id: str, slot: Slot = 'a', reviewer: str = Query(default='', max_length=80)):
        return store.task(pair_id, slot, reviewer.strip())

    @app.put('/api/reviews/{pair_id}')
    def save(pair_id: str, review: Review):
        return store.save(pair_id, review.model_dump())

    @app.post('/api/compare/{pair_id}')
    def compare(pair_id: str, value: Reveal):
        reviewer = value.reviewer.strip()
        if not reviewer:
            raise HTTPException(422, '请先填写标注员名称。')
        return store.compare(pair_id, reviewer)

    @app.get('/api/export')
    def export(format: Literal['json', 'csv'] = 'json'):
        data = store.export()
        headers = {'Content-Disposition': f'attachment; filename="autohunt-human-annotations.{format}"'}
        if format == 'json':
            return JSONResponse(data, headers=headers)
        stream = io.StringIO(newline='')
        fields = ['pair_id', 'slot', 'reviewer', 'relevance', 'eligibility', 'status', 'flagged', 'reference_exposed', 'revision', 'notes', 'evidence', 'dataset_sha256']
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        import json
        for r in data['reviews']:
            row = {k: r.get(k, '') for k in fields}
            row['evidence'] = json.dumps(r['evidence'], ensure_ascii=False)
            row['dataset_sha256'] = store.sha
            for key, val in row.items():
                if isinstance(val, str) and val.startswith(('=', '+', '-', '@', '\t', '\r', '\n')):
                    row[key] = "'" + val
            writer.writerow(row)
        return Response('\ufeff' + stream.getvalue(), media_type='text/csv; charset=utf-8', headers=headers)

    app.mount('/static', StaticFiles(directory=STATIC), name='static')
    return app


app = create_app()
