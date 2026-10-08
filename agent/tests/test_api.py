from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.dependencies import job_store
from app.main import app


def test_health_is_public() -> None:
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_api_requires_configured_key() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/memory")
    assert response.status_code in {401, 503}


def test_delivery_run_can_be_read_by_stable_run_id() -> None:
    job_id, _ = job_store.upsert_job({
        "source": "MockJobSite",
        "source_url": "http://127.0.0.1:8080/job.html?id=api-run",
        "external_id": "api-run",
        "title": "API Run Test",
        "company": "AutoHunt Test",
        "location": "深圳",
        "salary": "20-30K",
        "description": "durable run endpoint",
        "tags": ["Python"],
        "score": 80,
        "decision": "REVIEW",
        "reasoning": "test",
        "stage": "review",
    })
    run = job_store.create_delivery_run("delivery_api_stable_run", job_id)
    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/deliveries/{run.id}",
            headers={"X-AutoHunt-Key": settings.api_key or ""},
        )
    assert response.status_code == 200
    assert response.json()["id"] == run.id


def test_current_conversation_endpoint_returns_redacted_ordered_history() -> None:
    job_store.add_conversation_message("user", "联系我 user@example.com")
    job_store.add_conversation_message("assistant", "已经收到")
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/conversations/current?limit=2",
            headers={"X-AutoHunt-Key": settings.api_key or ""},
        )
    assert response.status_code == 200
    messages = response.json()["messages"]
    assert [item["role"] for item in messages] == ["user", "assistant"]
    assert "user@example.com" not in messages[0]["content"]


def test_resume_original_api_preserves_bytes_and_delete_clears_record() -> None:
    content = (
        b"Original candidate resume. Python FastAPI React Docker PostgreSQL. "
        b"Built reliable AI applications and browser automation workflows."
    )
    headers = {"X-AutoHunt-Key": settings.api_key or ""}
    with TestClient(app) as client:
        uploaded = client.post(
            "/api/v1/resumes",
            headers=headers,
            files={"file": ("api-resume.txt", content, "text/plain")},
        )
        assert uploaded.status_code == 201
        resume_id = uploaded.json()["resume"]["id"]

        original = client.get(f"/api/v1/resumes/{resume_id}/original", headers=headers)
        assert original.status_code == 200
        assert original.content == content

        activated = client.post(f"/api/v1/resumes/{resume_id}/activate", headers=headers)
        assert activated.status_code == 200
        assert activated.json()["resume"]["id"] == resume_id
        assert activated.json()["candidate_state"]["profile"]["resume_id"] == resume_id

        deleted = client.delete(f"/api/v1/resumes/{resume_id}", headers=headers)
        assert deleted.status_code == 200
        assert deleted.json()["deleted_id"] == resume_id
        assert client.get(f"/api/v1/resumes/{resume_id}/original", headers=headers).status_code == 404


def test_readme_material_upload_returns_project_proposal_and_updates_skills() -> None:
    readme = """# AutoHunt 智能求职 Agent

使用 Python、FastAPI、Next.js、TypeScript、Playwright 和 SQLite 构建。
实现职位搜索、匹配、人工 MFA 接管、投递状态机与结果去重。
""".encode("utf-8")
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/materials",
            headers={"X-AutoHunt-Key": settings.api_key or ""},
            data={"kind": "project"},
            files={"file": ("README.MD", readme, "text/markdown")},
        )
    assert response.status_code == 201
    payload = response.json()
    assert payload["material"]["filename"] == "README.MD"
    assert payload["resume_proposal"] is not None
    assert payload["resume_proposal"]["operations"]
    assert "Python" in payload["candidate_state"]["profile"]["skills"]
