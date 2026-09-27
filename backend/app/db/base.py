# -*- coding: utf-8 -*-
"""数据库引擎与会话。

默认 SQLite（Windows / Linux 零依赖一键启动）；生产切换到 PostgreSQL(+TimescaleDB)
只需修改环境变量 `DATABASE_URL`。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from ..core.config import Settings, get_settings
from ..core.logging import get_logger

logger = get_logger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def normalize_url(url: str, settings: Settings | None = None) -> str:
    """把相对路径 SQLite URL 转成绝对路径，并创建目录。"""
    s = settings or get_settings()
    parsed = urlparse(url)

    if parsed.scheme.startswith("sqlite"):
        path = parsed.path or ""
        if parsed.netloc:  # sqlite:/// 形态 netloc 为空
            path = f"/{parsed.netloc}{path}"
        if not path:
            path = "/data/btc.db"
        abs_path = s.abs_path(path.lstrip("/"))
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        if not os.path.isabs(abs_path.as_posix()):
            abs_path = Path(abs_path.as_posix())
        return f"{parsed.scheme}:///{abs_path.as_posix().lstrip('/')}"

    if parsed.scheme.startswith("postgres"):
        # 统一使用 psycopg3 异步驱动
        return url.replace("postgres://", "postgresql+psycopg://").replace(
            "postgresql://", "postgresql+psycopg://"
        )

    return url


def get_engine(settings: Settings | None = None) -> AsyncEngine:
    global _engine
    s = settings or get_settings()
    if _engine is None:
        url = normalize_url(s.DATABASE_URL, s)
        kwargs: dict[str, Any] = {"echo": s.DB_ECHO, "future": True}

        if url.startswith("sqlite"):
            kwargs.update({"connect_args": {"check_same_thread": False, "timeout": 30}})
            engine = create_async_engine(url, **kwargs)

            @event.listens_for(engine.sync_engine, "connect")
            def _set_sqlite_pragma(dbapi_conn: Any, _: Any) -> None:
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()
        else:
            kwargs.update(
                {
                    "pool_size": s.DB_POOL_SIZE,
                    "max_overflow": s.DB_MAX_OVERFLOW,
                    "pool_pre_ping": True,
                    "pool_recycle": 3600,
                }
            )
            engine = create_async_engine(url, **kwargs)

        _engine = engine
        logger.event("db.engine_ready", url=redact_url(url))
    return _engine


def get_session_factory(settings: Settings | None = None) -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(settings), expire_on_commit=False, autoflush=False
        )
    return _session_factory


async def session_scope() -> AsyncIterator[AsyncSession]:
    """独立会话上下文（脚本/任务使用）。"""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def redact_url(url: str) -> str:
    """隐藏数据库 URL 中的口令。"""
    try:
        parsed = urlparse(url)
        if parsed.password:
            return url.replace(parsed.password, "***")
    except Exception:  # noqa: BLE001
        pass
    return url


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None


async def check_connection() -> bool:
    engine = get_engine()
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.event("db.connection_failed", error=str(exc))
        return False
