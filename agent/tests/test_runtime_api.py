from fastapi.testclient import TestClient

from app.api import application
from app.core.config import settings
from app.main import app
from test_agent_runtime import make_runtime


def test_runtime_api_auth_sessions_idempotency_and_sse_resume(tmp_path, monkeypatch):
    runtime, _ = make_runtime(tmp_path)
    monkeypatch.setattr(application, "agent_runtime", runtime)
    monkeypatch.setattr(application, "run_store", runtime.store)
    monkeypatch.setattr(application, "job_store", runtime.jobs)
    headers = {"X-AutoHunt-Key": settings.api_key}
    with TestClient(app) as client:
        assert client.get("/api/v1/runs").status_code in {401, 403}
        session = client.post("/api/v1/conversations", json={"title": "独立会话"}, headers=headers).json()
        payload = {"conversation_id": session["id"], "request_id": "once", "message": "hello"}
        created = client.post("/api/v1/runs", json=payload, headers=headers)
        assert created.status_code == 202
        run_id = created.json()["id"]
        again = client.post("/api/v1/runs", json=payload, headers=headers)
        assert again.json()["id"] == run_id
        assert client.post("/api/v1/runs", json={**payload, "message": "different"}, headers=headers).status_code == 409
        events = client.get(f"/api/v1/runs/{run_id}/events", headers=headers)
        assert events.headers["content-type"].startswith("text/event-stream")
        assert "event: response" in events.text
        steps = client.get(f"/api/v1/runs/{run_id}/steps", headers=headers).json()
        assert not client.get(f"/api/v1/runs/{run_id}/events?after={steps[-1]['seq']}", headers=headers).text
        snapshot = client.get(f"/api/v1/runs/{run_id}", headers=headers).json()
        assert snapshot["status"] == "completed"
        assert "checkpoint" not in snapshot and "payload" not in snapshot
        history = client.get(f"/api/v1/conversations/current?conversation_id={session['id']}", headers=headers).json()
        assert len(history["messages"]) == 2
        assert client.get("/api/v1/conversations/current", headers=headers).json()["messages"] == []
        assert client.post("/api/v1/runs/missing/cancel", headers=headers).status_code == 404


def test_search_state_api_isolated_authenticated_and_reset(tmp_path, monkeypatch):
    from app.runtime.search_state import prepare_search_state, merge_search_state
    runtime, _ = make_runtime(tmp_path)
    monkeypatch.setattr(application, "agent_runtime", runtime)
    monkeypatch.setattr(application, "run_store", runtime.store)
    monkeypatch.setattr(application, "job_store", runtime.jobs)
    state = merge_search_state(prepare_search_state(None), {"queries": ["护士"], "locations": ["杭州"], "result_limit": 20})
    runtime.store.save_search_state("default", state)
    headers = {"X-AutoHunt-Key": settings.api_key}
    with TestClient(app) as client:
        path = "/api/v1/conversations/default/search-state"
        assert client.get(path).status_code in {401, 403}
        assert client.get(path, headers=headers).json()["queries"] == ["护士"]
        second = client.post("/api/v1/conversations", json={"title": "新会话"}, headers=headers).json()["id"]
        assert client.get(f"/api/v1/conversations/{second}/search-state", headers=headers).json()["result_limit"] is None
        assert client.get("/api/v1/conversations/missing/search-state", headers=headers).status_code == 404
        reset = client.post(f"{path}/reset", headers=headers)
        assert reset.status_code == 200 and reset.json()["result_limit"] is None
        assert reset.json()["queries"] == []
