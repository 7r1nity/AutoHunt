from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.middleware import ApiKeyMiddleware, RateLimitMiddleware


def client_for_limits(read=120, write=3):
    app = FastAPI()
    app.add_middleware(ApiKeyMiddleware, api_key="test-key")
    app.add_middleware(RateLimitMiddleware, requests_per_minute=write,
                       read_requests_per_minute=read, api_key="test-key")

    @app.api_route("/api/test", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def endpoint():
        return {"ok": True}

    return TestClient(app)


KEY = {"X-AutoHunt-Key": "test-key"}


def test_dashboard_reads_do_not_starve_upload_or_confirmation():
    with client_for_limits() as client:
        # Reproduces the >60 polling requests seen in the local startup log.
        assert all(client.get("/api/test", headers=KEY).status_code == 200 for _ in range(100))
        for method in ("POST", "PUT", "DELETE"):
            assert client.request(method, "/api/test", headers=KEY).status_code == 200
        assert client.patch("/api/test", headers=KEY).status_code == 429


def test_reads_still_have_a_finite_limit_and_retry_after():
    with client_for_limits(read=2) as client:
        for _ in range(2):
            assert client.get("/api/test", headers=KEY).status_code == 200
        response = client.get("/api/test", headers=KEY)
        assert response.status_code == 429
        assert 1 <= int(response.headers["Retry-After"]) <= 60
        assert response.json()["retry_after"] == int(response.headers["Retry-After"])
        assert client.post("/api/test", headers=KEY).status_code == 200


def test_invalid_keys_cannot_claim_authenticated_read_budget():
    with client_for_limits() as client:
        for _ in range(3):
            assert client.get("/api/test", headers={"X-AutoHunt-Key": "wrong"}).status_code == 401
        assert client.get("/api/test").status_code == 429
        assert client.get("/api/test", headers=KEY).status_code == 200


def test_expired_window_recovers(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("app.core.middleware.time.monotonic", lambda: clock[0])
    with client_for_limits(read=1) as client:
        assert client.get("/api/test", headers=KEY).status_code == 200
        assert client.get("/api/test", headers=KEY).status_code == 429
        clock[0] += 60
        assert client.get("/api/test", headers=KEY).status_code == 200
