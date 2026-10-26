"""Corpus-bound annotation persistence. No production state or model clients."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from evaluation.jev_labels.corpus import digest, load


class Conflict(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


class AnnotationStore:
    def __init__(self, path: Path):
        self.data = load()
        self.sha = digest(self.data)
        self.pairs = {p['id']: p for p in self.data['pairs']}
        self.candidates = {c['id']: c for c in self.data['candidates']}
        self.jobs = {j['id']: j for j in self.data['jobs']}
        # Same development calibration subset as the existing Jev preparation.
        design = {d['pair_id']: d for d in self.data['design_intents']}
        self.calibration = set()
        for candidate in self.candidates:
            pool = [p for p in self.pairs.values() if p['candidate_id'] == candidate and p['split'] == 'development']
            for kind, count in [('close', 2), ('transfer', 1), ('depth', 1), ('ownership', 1), ('tenure', 1), ('missing', 1), ('untrusted', 1)]:
                selected = [p for p in sorted(pool, key=lambda p: p['id']) if design[p['id']]['case_family'] == kind][:count]
                self.calibration.update(p['id'] for p in selected)
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS reviews (
                    pair_id TEXT NOT NULL, slot TEXT NOT NULL, revision INTEGER NOT NULL,
                    body TEXT NOT NULL, PRIMARY KEY (pair_id,slot));
                CREATE TABLE IF NOT EXISTS exposures (
                    pair_id TEXT NOT NULL, reviewer TEXT NOT NULL, first_seen TEXT NOT NULL,
                    PRIMARY KEY (pair_id,reviewer));
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, at TEXT NOT NULL, pair_id TEXT NOT NULL,
                    kind TEXT NOT NULL, body TEXT NOT NULL);
            ''')
            row = db.execute("SELECT value FROM metadata WHERE key='dataset_sha256'").fetchone()
            if row and row[0] != self.sha:
                raise ValueError('标注数据库属于另一版数据集，请使用单独数据库。')
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('dataset_sha256',?)", (self.sha,))

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA foreign_keys=ON')
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def pair(self, pair_id):
        if pair_id not in self.pairs:
            raise KeyError(pair_id)
        return self.pairs[pair_id]

    @staticmethod
    def _review(db, pair_id, slot):
        row = db.execute('SELECT body FROM reviews WHERE pair_id=? AND slot=?', (pair_id, slot)).fetchone()
        return json.loads(row[0]) if row else None

    def review(self, pair_id, slot):
        self.pair(pair_id)
        with self.connection() as db:
            return self._review(db, pair_id, slot)

    def queue(self, scope, slot, state='all', query=''):
        with self.connection() as db:
            reviews = {r['pair_id']: json.loads(r['body']) for r in db.execute('SELECT pair_id,body FROM reviews WHERE slot=?', (slot,))}
        rows = []
        counters = {'total': 0, 'submitted': 0, 'draft': 0, 'skipped': 0, 'flagged': 0}
        for index, p in enumerate(self.pairs.values(), 1):
            if scope == 'calibration' and p['id'] not in self.calibration:
                continue
            if scope in ('development', 'test') and p['split'] != scope:
                continue
            c, j = self.candidates[p['candidate_id']], self.jobs[p['job_id']]
            r = reviews.get(p['id'], {})
            status = r.get('status', 'unlabeled')
            counters['total'] += 1
            if status in counters:
                counters[status] += 1
            if r.get('flagged'):
                counters['flagged'] += 1
            if state == 'pending' and status == 'submitted':
                continue
            if state == 'submitted' and status != 'submitted':
                continue
            if state == 'flagged' and not r.get('flagged'):
                continue
            if query.casefold() not in f"{p['id']} {c['role']} {j['title']} {j['text']}".casefold():
                continue
            rows.append({'id': p['id'], 'number': index, 'role': c['role'], 'split': p['split'], 'status': status, 'flagged': bool(r.get('flagged'))})
        return {'items': rows, 'counts': counters}

    def task(self, pair_id, slot, reviewer):
        p = self.pair(pair_id)
        c, j = self.candidates[p['candidate_id']], self.jobs[p['job_id']]
        # Explicit allowlist: never serialize author design labels or model outputs.
        review = self.review(pair_id, slot)
        occupied = bool(review and review['reviewer'] != reviewer)
        return {'id': pair_id, 'split': p['split'], 'occupied': occupied,
                'candidate': {k: c[k] for k in ('id', 'role', 'lines')},
                'job': {k: j[k] for k in ('id', 'title', 'lines')},
                'review': None if occupied else review}

    def save(self, pair_id, value):
        p = self.pair(pair_id)
        reviewer = value['reviewer'].strip()
        if not reviewer:
            raise ValueError('请填写标注员名称。')
        evidence = []
        for e in value['evidence']:
            source = e['source']
            document = self.candidates[p['candidate_id']] if source == 'candidate' else self.jobs[p['job_id']]
            line_id = e['line_id']
            if line_id not in document['lines'] or line_id in ('c0', 'j0'):
                raise ValueError('证据必须是材料或 JD 中可核对的正文段落。')
            item = {'source': source, 'line_id': line_id, 'quote': document['lines'][line_id]}
            if item not in evidence:
                evidence.append(item)
        if value['status'] == 'submitted' and (value['relevance'] is None or value['eligibility'] is None):
            raise ValueError('完成标注前，请选择匹配等级与硬条件结论。')
        warnings = []
        if value['status'] == 'submitted':
            if {e['source'] for e in evidence} != {'candidate', 'job'}:
                warnings.append('缺少材料或 JD 原文证据')
            if value['relevance'] == 2 and value['eligibility'] != 'pass':
                warnings.append('高匹配与硬条件结论需要复核')
            if value['slot'] == 'adjudicated' and not value['notes'].strip():
                raise ValueError('仲裁请填写结论理由。')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            old = self._review(db, pair_id, value['slot'])
            if (old['revision'] if old else 0) != value['expected_revision']:
                raise Conflict('此条已在另一页面更新。你的修改尚未覆盖它，请复制笔记后重新加载。')
            if old and old['reviewer'] != reviewer:
                raise Conflict('此标注槽已有另一位标注员的记录，请切换标注槽或使用原标注员名称。')
            a, b = self._review(db, pair_id, 'a'), self._review(db, pair_id, 'b')
            other = b if value['slot'] == 'a' else a
            if value['slot'] in ('a', 'b') and other and other['reviewer'] == reviewer:
                raise ValueError('独立双人标注需要不同标注员名称；单人可只填写 A。')
            if value['slot'] == 'adjudicated':
                if not all(r and r['status'] == 'submitted' for r in (a, b)):
                    raise ValueError('请先完成 A、B 两份标注，再进行仲裁。')
                if reviewer in {a['reviewer'], b['reviewer']}:
                    raise ValueError('仲裁请使用独立于 A、B 的标注员名称。')
            exposed = bool(db.execute('SELECT 1 FROM exposures WHERE pair_id=? AND reviewer=?', (pair_id, reviewer)).fetchone())
            record = {k: value[k] for k in ('slot', 'relevance', 'eligibility', 'notes', 'status')}
            record.update(pair_id=pair_id, reviewer=reviewer, evidence=evidence, revision=(old['revision'] if old else 0)+1,
                          reference_exposed=exposed or bool(old and old.get('reference_exposed')),
                          flagged=value['flagged'] or bool(warnings), warnings=warnings, updated_at=now())
            if value['slot'] == 'adjudicated':
                record['based_on_revisions'] = {'a': a['revision'], 'b': b['revision']}
            body = json.dumps(record, ensure_ascii=False)
            db.execute('INSERT INTO reviews VALUES (?,?,?,?) ON CONFLICT(pair_id,slot) DO UPDATE SET revision=excluded.revision,body=excluded.body', (pair_id, value['slot'], record['revision'], body))
            db.execute('INSERT INTO events(at,pair_id,kind,body) VALUES (?,?,?,?)', (now(), pair_id, 'save', body))
        return record

    def compare(self, pair_id, reviewer):
        self.pair(pair_id)
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR IGNORE INTO exposures VALUES (?,?,?)', (pair_id, reviewer, now()))
            db.execute('INSERT INTO events(at,pair_id,kind,body) VALUES (?,?,?,?)', (now(), pair_id, 'reference_reveal', json.dumps({'reviewer': reviewer}, ensure_ascii=False)))
            return {'a': self._review(db, pair_id, 'a'), 'b': self._review(db, pair_id, 'b'),
                    'adjudicated': self._review(db, pair_id, 'adjudicated'),
                    'model_reference': None, 'notice': '未导入 Jev 标注。此操作记录为已查看他人标注；后续保存会保留该事实。'}

    def export(self):
        with self.connection() as db:
            reviews = [json.loads(r[0]) for r in db.execute('SELECT body FROM reviews ORDER BY pair_id,slot')]
            exposures = [dict(r) for r in db.execute('SELECT * FROM exposures ORDER BY pair_id,reviewer')]
            events = [dict(r) for r in db.execute('SELECT * FROM events ORDER BY id')]
        by_pair = {}
        for r in reviews:
            by_pair.setdefault(r['pair_id'], {})[r['slot']] = r
        reference_labels = []
        for pid, slots in by_pair.items():
            a, b, adjudicated = (slots.get(s) for s in ('a', 'b', 'adjudicated'))
            valid = lambda r: r and r['status'] == 'submitted' and not r['flagged'] and {e['source'] for e in r['evidence']} == {'candidate', 'job'}
            chosen, method = None, None
            if valid(adjudicated) and a and b and adjudicated['based_on_revisions'] == {'a': a['revision'], 'b': b['revision']}:
                chosen, method = adjudicated, 'human_adjudicated'
            elif valid(a) and valid(b) and a['reviewer'] != b['reviewer'] and not a['reference_exposed'] and not b['reference_exposed'] and (a['relevance'], a['eligibility']) == (b['relevance'], b['eligibility']):
                chosen, method = a, 'human_double_agreement'
            if chosen:
                p = self.pairs[pid]
                reference_labels.append({'pair_id': pid, 'candidate_id': p['candidate_id'], 'job_id': p['job_id'], 'split': p['split'], 'relevance': chosen['relevance'], 'eligibility': chosen['eligibility'], 'method': method})
        return {'schema_version': 1, 'dataset_version': self.data['version'], 'dataset_sha256': self.sha,
                'exported_at': now(), 'reviewer_identity': 'self_reported_not_authenticated',
                'rubric': 'relevance:0=明显不匹配,1=部分匹配,2=高度匹配; eligibility:pass/fail/unknown',
                'total_pairs': len(self.pairs), 'reviews': reviews, 'reference_labels': reference_labels,
                'reference_count': len(reference_labels), 'exposures': exposures, 'events': events}
