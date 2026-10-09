from __future__ import annotations

import json
import logging
import math
import time
import uuid
from collections import defaultdict, deque
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

from .security import redact, secure_compare


logger = logging.getLogger(__name__)


class HistoryMaintenanceMiddleware(BaseHTTPMiddleware):
    """Single-process local gate: no uploads/commands racing a history reset."""
    def __init__(self, app):
        super().__init__(app)
        self.writers = 0
        self.clearing = False

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        from ..runtime.history_reset import RESET_PATH
        maintenance = request.method == "POST" and request.url.path == RESET_PATH
        mutation = request.method in {"POST", "PUT", "PATCH", "DELETE"}
        if request.url.path.startswith("/api/") and self.clearing:
            return JSONResponse({"detail": "正在清除历史，请稍后再操作"}, status_code=409)
        if maintenance and self.writers:
            return JSONResponse({"detail": "正在上传或处理操作，请等待完成后再清理"}, status_code=409)
        if maintenance:
            self.clearing = True
        elif mutation:
            self.writers += 1
        try:
            return await call_next(request)
        finally:
            if maintenance:
                self.clearing = False
            elif mutation:
                self.writers -= 1


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("X-Request-ID", uuid.uuid4().hex)
        request.state.request_id = request_id[:64]
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cache-Control"] = "no-store"
        return response


class BodyLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, max_bytes: int) -> None:
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        length = request.headers.get("content-length")
        if length:
            try:
                if int(length) > self.max_bytes:
                    return JSONResponse({"detail": "Request body too large"}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "Invalid Content-Length header"}, status_code=400)
        return await call_next(request)


class ApiKeyMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, api_key: str | None) -> None:
        super().__init__(app)
        self.api_key = api_key

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not request.url.path.startswith("/api/"):
            return await call_next(request)
        if not self.api_key:
            return JSONResponse({"detail": "Server API key is not configured"}, status_code=503)
        if not secure_compare(self.api_key, request.headers.get("X-AutoHunt-Key")):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, requests_per_minute: int, *, read_requests_per_minute: int = 600,
                 api_key: str | None = None) -> None:
        super().__init__(app)
        self.limit = requests_per_minute
        self.read_limit = read_requests_per_minute
        self.api_key = api_key
        self.history: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        client = request.client.host if request.client else "unknown"
        authenticated = bool(self.api_key and secure_compare(self.api_key, request.headers.get("X-AutoHunt-Key")))
        # Dashboard polling must not exhaust the budget for uploads, chat or
        # confirmation. Only authenticated API reads get the larger budget;
        # invalid credentials cannot claim it just by sending a header.
        lane = ("read" if request.method in {"GET", "HEAD"} else "write") if (
            authenticated and request.url.path.startswith("/api/")
        ) else "anonymous"
        limit = self.read_limit if lane == "read" else self.limit
        now = time.monotonic()
        bucket = self.history[(client, lane)]
        while bucket and bucket[0] <= now - 60:
            bucket.popleft()
        if len(bucket) >= limit:
            retry_after = max(1, math.ceil(60 - (now - bucket[0]))) if bucket else 60
            return JSONResponse({"detail": "请求过于频繁，请稍后重试", "retry_after": retry_after},
                                status_code=429, headers={"Retry-After": str(retry_after)})
        bucket.append(now)
        return await call_next(request)


class AuditMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, log_path: Path) -> None:
        super().__init__(app)
        self.log_path = log_path
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        started = time.perf_counter()
        response = await call_next(request)
        event = redact({
            "timestamp": datetime.now(UTC).isoformat(),
            "request_id": getattr(request.state, "request_id", "unknown"),
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "client": request.client.host if request.client else "unknown",
        })
        try:
            with self.log_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            # A stale file can have foreign ACLs after tests or elevated runs. Preserve
            # auditing in a process-local fallback without making actions look failed.
            fallback = self.log_path.with_name("events-local.jsonl")
            try:
                with fallback.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            except OSError:
                logger.exception("Could not write audit event")
        return response
