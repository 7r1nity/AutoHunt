"""Offline HTTP/storage contracts; no external services or production state."""
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from .store import AnnotationStore
from .web import create_app


class HumanAnnotationContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'review.sqlite.db'
        self.app = create_app(self.path)
        self.store = self.app.state.store
        self.client = TestClient(self.app)
        self.token = self.client.get('/api/status').json()['token']
        self.headers = {'x-annotation-token': self.token}
        self.pair = next(iter(self.store.pairs))

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def payload(self, **updates):
        value = dict(slot='a', reviewer='Alice', expected_revision=0, relevance=1, eligibility='unknown',
                     evidence=[{'source': 'candidate', 'line_id': 'c2'}, {'source': 'job', 'line_id': 'j2'}],
                     notes='经验相近，负责人经历需核实。', status='submitted', flagged=False)
        value.update(updates)
        return value

    def save(self, **updates):
        return self.client.put(f'/api/reviews/{self.pair}', headers=self.headers, json=self.payload(**updates))

    def task(self, **query):
        return self.client.get(f'/api/tasks/{self.pair}', params={'reviewer': 'Alice', **query})

    def test_counts_and_default_calibration(self):
        for scope, total in [('calibration', 64), ('development', 160), ('test', 140), ('all', 300)]:
            result = self.client.get('/api/tasks', params={'scope': scope}).json()
            self.assertEqual(total, result['counts']['total'])
            self.assertEqual(total, len(result['items']))
        self.assertTrue(all(self.store.pairs[p]['split'] == 'development' for p in self.store.calibration))

    def test_blind_response_does_not_leak_designs_or_other_labels(self):
        self.save()
        encoded = self.client.get('/api/tasks').text + self.task(slot='b').text
        for key in ('intended_relevance', 'design_intents', 'case_family', 'model_reference', 'Alice', '经验相近'):
            self.assertNotIn(key, encoded)
        hidden = self.task(reviewer='Bob').json()
        self.assertTrue(hidden['occupied'])
        self.assertIsNone(hidden['review'])

    def test_save_evidence_quotes_come_from_corpus(self):
        result = self.save().json()
        self.assertEqual(1, result['revision'])
        self.assertEqual('unknown', result['eligibility'])
        p = self.store.pair(self.pair)
        self.assertEqual(self.store.candidates[p['candidate_id']]['lines']['c2'], result['evidence'][0]['quote'])
        self.assertEqual(result, self.task().json()['review'])

    def test_zero_label_is_preserved_and_survives_restart(self):
        self.assertEqual(200, self.save(relevance=0, eligibility='fail').status_code)
        reopened = AnnotationStore(self.path)
        self.assertEqual(0, reopened.review(self.pair, 'a')['relevance'])

    def test_cas_conflict_does_not_overwrite(self):
        self.save()
        response = self.save(notes='overwrite')
        self.assertEqual(409, response.status_code)
        self.assertNotEqual('overwrite', self.store.review(self.pair, 'a')['notes'])

    def test_draft_skip_and_missing_labels(self):
        self.assertEqual(422, self.save(relevance=None).status_code)
        self.assertEqual(200, self.save(relevance=None, eligibility=None, status='draft').status_code)
        self.assertEqual(200, self.save(expected_revision=1, relevance=None, status='skipped').status_code)

    def test_invalid_evidence_and_injection_rejected(self):
        for evidence in ([{'source': 'job', 'line_id': 'c2'}], [{'source': 'candidate', 'line_id': 'c0'}], [{'source': 'job', 'line_id': 'j2', 'quote': 'made up'}]):
            self.assertEqual(422, self.save(evidence=evidence).status_code)
        self.assertEqual(404, self.client.put('/api/reviews/missing', headers=self.headers, json=self.payload()).status_code)

    def test_missing_evidence_and_inconsistent_grade_are_review_flags(self):
        result = self.save(relevance=2, eligibility='fail', evidence=[]).json()
        self.assertTrue(result['flagged'])
        self.assertEqual(2, len(result['warnings']))
        self.assertEqual(0, self.store.export()['reference_count'])

    def test_independent_reviewer_names_and_slot_ownership(self):
        self.save()
        self.assertEqual(422, self.save(slot='b').status_code)
        self.assertEqual(409, self.save(reviewer='Bob', expected_revision=1).status_code)
        self.assertEqual(200, self.save(slot='b', reviewer='Bob').status_code)

    def test_reference_reveal_is_logged_and_cannot_be_blind_again(self):
        self.save()
        result = self.client.post(f'/api/compare/{self.pair}', headers=self.headers, json={'reviewer': 'Bob'})
        self.assertIsNone(result.json()['model_reference'])
        self.assertEqual(200, self.save(slot='b', reviewer='Bob').status_code)
        exported = self.store.export()
        self.assertEqual(0, exported['reference_count'])
        self.assertTrue(next(r for r in exported['reviews'] if r['slot'] == 'b')['reference_exposed'])
        self.assertEqual(1, len(exported['exposures']))

    def test_double_agreement_and_adjudication_stale_revision(self):
        self.save()
        self.save(slot='b', reviewer='Bob')
        self.assertEqual(1, self.store.export()['reference_count'])
        self.assertEqual(422, self.save(slot='adjudicated', reviewer='Alice').status_code)
        result = self.save(slot='adjudicated', reviewer='Carol')
        self.assertEqual(200, result.status_code)
        self.assertEqual('human_adjudicated', self.store.export()['reference_labels'][0]['method'])
        self.save(slot='b', reviewer='Bob', expected_revision=1, relevance=0)
        self.assertEqual(0, self.store.export()['reference_count'])

    def test_adjudication_requires_two_completed_reviews(self):
        self.save()
        self.assertEqual(422, self.save(slot='adjudicated', reviewer='Carol').status_code)

    def test_export_provenance_and_csv_injection(self):
        self.save(notes='=HYPERLINK("bad")')
        response = self.client.get('/api/export?format=json')
        data = response.json()
        self.assertEqual(self.store.sha, data['dataset_sha256'])
        self.assertEqual('self_reported_not_authenticated', data['reviewer_identity'])
        self.assertEqual(0, data['reference_count'])
        self.assertIn('attachment', response.headers['Content-Disposition'])
        raw = self.client.get('/api/export?format=csv').content.decode('utf-8-sig')
        row = next(csv.DictReader(io.StringIO(raw)))
        self.assertTrue(row['notes'].startswith("'="))

    def test_local_request_protection_and_response_headers(self):
        self.assertEqual(403, self.client.put(f'/api/reviews/{self.pair}', json=self.payload()).status_code)
        self.assertEqual(403, self.client.put(f'/api/reviews/{self.pair}', headers={**self.headers, 'origin': 'https://elsewhere.test'}, json=self.payload()).status_code)
        self.assertEqual(403, self.client.get('/api/status', headers={'host': 'malicious.test'}).status_code)
        response = self.client.get('/')
        self.assertEqual(200, response.status_code)
        self.assertEqual('no-store', response.headers['cache-control'])
        self.assertIn("frame-ancestors 'none'", response.headers['content-security-policy'])

    def test_schema_and_queue_filters(self):
        self.assertEqual(422, self.save(relevance=7).status_code)
        self.assertEqual(422, self.save(notes='x' * 10001).status_code)
        self.save(flagged=True)
        queue = self.client.get('/api/tasks?scope=all&state=flagged').json()
        self.assertEqual([self.pair], [p['id'] for p in queue['items']])
        self.assertEqual(299, len(self.client.get('/api/tasks?scope=all&state=pending').json()['items']))

    def test_database_is_bound_to_frozen_corpus(self):
        with self.store.connection() as db:
            db.execute("UPDATE metadata SET value='incorrect' WHERE key='dataset_sha256'")
        with self.assertRaises(ValueError):
            AnnotationStore(self.path)


if __name__ == '__main__':
    unittest.main(verbosity=2)
