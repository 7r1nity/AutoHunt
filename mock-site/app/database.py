from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

DATABASE_PATH = Path(__file__).resolve().parents[1] / "job_site.db"
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///" + DATABASE_PATH.as_posix())
IS_SQLITE = DATABASE_URL.startswith("sqlite")

# SQLite 高并发配置：
# - WAL 模式：读写不互相阻塞，适合大量读 + 批量写
# - busy_timeout：写锁繁忙时排队等待，避免立即报 database is locked
# - synchronous=NORMAL：在 WAL 下兼顾性能与安全
if IS_SQLITE:
    engine = create_async_engine(
        DATABASE_URL,
        echo=False,
        connect_args={"timeout": 30},
    )
else:
    # PostgreSQL 等生产数据库使用连接池，支撑更高并发
    engine = create_async_engine(
        DATABASE_URL,
        echo=False,
        pool_size=20,
        max_overflow=40,
        pool_pre_ping=True,
        pool_recycle=1800,
    )


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record):
    if not IS_SQLITE:
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


class Base(DeclarativeBase):
    pass


SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncSession:
    async with SessionLocal() as session:
        yield session


async def create_tables() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
