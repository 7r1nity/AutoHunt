"""Synthetic evaluation fixture: empty database; no real accounts or seed data."""
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from .database import create_tables, engine
from .routers import auth, jobs, resumes, applications, messages, stats
FRONTEND_DIR = Path(__file__).resolve().parents[1] / "frontend"
@asynccontextmanager
async def lifespan(app):
    await create_tables()
    yield
    await engine.dispose()
app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
for module in (auth, jobs, resumes, applications, messages, stats):
    app.include_router(module.router, prefix="/api")
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
@app.get("/")
async def index():
    return FileResponse(FRONTEND_DIR / "index.html")
@app.get("/job.html")
async def job_page():
    return FileResponse(FRONTEND_DIR / "job.html")
