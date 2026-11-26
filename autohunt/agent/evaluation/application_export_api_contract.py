"""Authenticated Harness export and retired-feature contracts in isolated state."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from app.runtime.dialogue import DialogueState, apply_operation, compact_state
from app.runtime.models import RunCreate
from app.runtime.retired_features import retire_growth_steps


class RetiredFeatureContracts(unittest.TestCase):
    def test_new_run_schema_rejects_retired_kind(self):
        with self.assertRaises(ValueError):
            RunCreate(request_id="synthetic", kind="learning_plan", focus="synthetic")

    def test_old_dialogue_remains_readable_without_reactivating_removed_actions(self):
        saved = {"active_intent": "learning", "learning_request": {"message": "synthetic"},
                 "frames": {"learning": {}, "search": {}}, "tasks": {"browser_id": "search-kept"}}
        original = copy.deepcopy(saved)
        state = DialogueState.model_validate(saved)
        self.assertEqual(state.active_intent, "general")
        self.assertNotIn("learning_request", state.model_dump())
        self.assertNotIn("learning", state.frames)
        self.assertEqual(state.tasks["browser_id"], "search-kept")
        self.assertEqual(saved, original)
        updated = apply_operation(saved, {"op": "begin", "message": "查看搜索结果", "source": "synthetic"})
        self.assertEqual(updated["active_intent"], "search")
        self.assertNotIn("learning_request", compact_state(updated))

    def test_legacy_step_retirement_is_idempotent_and_preserves_other_receipts(self):
        checkpoint = {"browser_id": "search-kept", "plan": {"steps": [
            {"id": "search", "tool": "job_search_start", "status": "completed", "result": {"matched": 3}},
            {"id": "retired", "tool": "learning_plan_generate", "status": "pending", "depends_on": ["search"]},
            {"id": "apply", "tool": "mock_delivery_start", "status": "waiting_external", "task_id": "queue-kept"}]}}
        original = copy.deepcopy(checkpoint)
        changed, ids = retire_growth_steps(checkpoint)
        self.assertEqual(ids, ["retired"])
        self.assertEqual(changed["plan"]["steps"][1]["status"], "skipped")
        self.assertEqual(changed["plan"]["steps"][1]["failure"]["code"], "feature_removed")
        self.assertEqual(changed["plan"]["steps"][0], original["plan"]["steps"][0])
        self.assertEqual(changed["plan"]["steps"][2], original["plan"]["steps"][2])
        self.assertEqual(checkpoint, original)
        self.assertEqual(retire_growth_steps(changed), (changed, []))

    def test_completed_legacy_step_is_kept_as_history(self):
        checkpoint = {"plan": {"steps": [{"id": "old", "tool": "learning_plan_generate", "status": "completed", "result": {"id": "history"}}]}}
        self.assertEqual(retire_growth_steps(checkpoint), (checkpoint, []))

    def test_old_skipped_step_is_retired_instead_of_becoming_retryable(self):
        checkpoint = {"plan": {"steps": [{"id": "old", "tool": "learning_plan_generate", "status": "skipped"}]}}
        migrated, ids = retire_growth_steps(checkpoint)
        self.assertEqual(ids, ["old"])
        self.assertEqual(migrated["plan"]["steps"][0]["failure"]["code"], "feature_removed")
        self.assertEqual(retire_growth_steps(migrated), (migrated, []))


class ExportHTTPContracts(unittest.TestCase):
    def test_http_auth_binary_formats_scope_and_removed_routes(self):
        # App dependency construction belongs to a separate process with a new
        # data root. No lifespan is entered, so no worker/browser can be started.
        script = r'''
import csv, io, json
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from app.api.application import app
from app.core.dependencies import job_store, agent_tool_registry
from evaluation.application_export_contract import vacancy
key='synthetic-export-api-key'
for n in range(3): job_store.upsert_job(vacancy(n))
with job_store._connect() as db:
    db.execute("UPDATE jobs SET board_visible=0,removed_at='2026-11-26T00:00:00Z' WHERE id=(SELECT MIN(id) FROM jobs)")
    before=db.iterdump(); before='\n'.join(before)
client=TestClient(app)
assert client.get('/api/v1/applications/export').status_code==401
headers={'X-AutoHunt-Key':key}
for fmt in ('csv','xlsx'):
    response=client.get('/api/v1/applications/export',params={'format':fmt,'scope':'all'},headers=headers)
    assert response.status_code==200, response.text[:100]
    assert 'attachment;' in response.headers['content-disposition']
    assert response.headers['cache-control']=='no-store'
    assert response.headers['x-content-type-options']=='nosniff'
    if fmt=='csv':
        assert response.content.startswith(b'\xef\xbb\xbf')
        assert len(list(csv.DictReader(io.StringIO(response.content.decode('utf-8-sig')))))==3
    else:
        assert response.content.startswith(b'PK')
        book=load_workbook(io.BytesIO(response.content),read_only=True)
        assert len(list(book.active.values))==4
        book.close()
board=client.get('/api/v1/applications/export?format=csv&scope=board',headers=headers)
assert len(list(csv.DictReader(io.StringIO(board.content.decode('utf-8-sig')))))==2
for query in ('format=html','scope=other'):
    assert client.get('/api/v1/applications/export?'+query,headers=headers).status_code==422
for method,path in [('GET','/api/v1/learning-plans'),('POST','/api/v1/learning-plans'),('POST','/api/v1/learning-plans/old/gaps/old/complete')]:
    assert client.request(method,path,headers=headers,json={}).status_code==404
assert client.post('/api/v1/runs',headers=headers,json={'request_id':'synthetic','kind':'learning_plan','focus':'synthetic'}).status_code==422
assert all(t['function']['name']!='learning_plan_generate' for t in agent_tool_registry.schemas())
with job_store._connect() as db:
    assert before=='\n'.join(db.iterdump())
print(json.dumps({'checks':10,'external_model_calls':0,'business_actions':0,'passed':True}))
'''
        with tempfile.TemporaryDirectory() as root:
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith("AUTOHUNT_") and not key.endswith(("_API_KEY", "_TOKEN"))}
            from cryptography.fernet import Fernet
            env.update(AUTOHUNT_DATA_ROOT=root, AUTOHUNT_DATA_KEY=Fernet.generate_key().decode(),
                       AUTOHUNT_API_KEY="synthetic-export-api-key", AUTOHUNT_LLM_PROVIDER="mock",
                       AUTOHUNT_LLM_MODEL="mock-autohunt-v1", PYTHONUTF8="1")
            result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                                    env=env, capture_output=True, text=True, timeout=45)
            self.assertEqual(result.returncode, 0, result.stderr[-5000:])
            self.assertTrue(json.loads(result.stdout.strip().splitlines()[-1])["passed"])


if __name__ == "__main__":
    unittest.main()
