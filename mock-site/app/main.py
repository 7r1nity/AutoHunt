from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .database import SessionLocal, create_tables
from .routers import applications, auth, forms, jobs, messages, resumes, stats
from .seed import seed_if_empty

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_tables()
    async with SessionLocal() as db:
        await seed_if_empty(db)
    yield


app = FastAPI(
    title="模拟招聘网站",
    description="一个支持高并发访问的模拟招聘平台：职位发布、简历投递、聊天、表单。",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

@app.middleware("http")
async def fresh_frontend_assets(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/") or request.url.path == "/" or request.url.path.endswith(".html"):
        # UI and shared helpers must change together; never keep an old helper
        # behind a newly loaded page after local development updates.
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


app.include_router(auth.router, prefix="/api")
app.include_router(jobs.router, prefix="/api")
app.include_router(resumes.router, prefix="/api")
app.include_router(applications.router, prefix="/api")
app.include_router(messages.router, prefix="/api")
app.include_router(forms.router, prefix="/api")
app.include_router(stats.router, prefix="/api")

# 前端静态资源
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/job.html", include_in_schema=False)
async def job_page() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "job.html")


@app.get("/applicant.html", include_in_schema=False)
async def applicant_page() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "applicant.html")


@app.get("/employer.html", include_in_schema=False)
async def employer_page() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "employer.html")


@app.get("/messages.html", include_in_schema=False)
async def messages_page() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "messages.html")


@app.get("/employer-messages.html", include_in_schema=False)
async def employer_messages_page() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "employer-messages.html")


if __name__ == "__main__":
    import os
    import uvicorn

    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("app.main:app", host=host, port=port, reload=False)
