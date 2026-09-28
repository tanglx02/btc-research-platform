# -*- coding: utf-8 -*-
"""数据库引擎与会话。

支持三种数据库，方言差异集中在 `app.db.dialects`：

* SQLite      —— 零依赖，数据落在本地文件，适合单台机器使用
* PostgreSQL  —— 推荐生产/多机共享，可叠加 TimescaleDB 时序扩展
* MySQL       —— 复用已有 MySQL / MariaDB 实例

选择哪一种是**首次启动的安装向导**决定的，结果写进 `.env` 的 `DATABASE_URL`。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from ..core.config import Settings, get_settings
from ..core.logging import get_logger
from .dialects import detect_dialect, normalize_to_async

logger = get_logger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def normalize_url(url: str, settings: Settings | None = None) -> str:
    """把用户写的连接串整理成「绝对路径 + 异步驱动」的形态。

    两件事必须在这里做完，别的地方都不该重复判定：
    1. SQLite 的相对路径 -> 绝对路径（否则进程换个工作目录就找不到库文件了）；
    2. 任意方言 -> 对应异步驱动（``psycopg``/``pymysql`` 这类同步驱动会在这步被纠正）。
    """
    s = settings or get_settings()

    if detect_dialect(url) == "sqlite":
        _, _, tail = url.partition("://")
        # 「sqlite:///data/btc.db」与漏写一个斜杠的「sqlite://data/btc.db」
        # 实际都指向同一个文件，这里统一按路径处理，不区分 netloc。
        path = (tail or "").lstrip("/") or "data/btc.db"
        if path.startswith(":"):  # 内存库 :memory: 原样放行
            return f"sqlite+aiosqlite:///{path}"
        abs_path = s.abs_path(path)
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite+aiosqlite:///{abs_path.as_posix().lstrip('/')}"

    return normalize_to_async(url)


def get_engine(settings: Settings | None = None) -> AsyncEngine:
    global _engine
    s = settings or get_settings()
    if _engine is None:
        url = normalize_url(s.DATABASE_URL, s)
        dialect = detect_dialect(url)
        kwargs: dict[str, Any] = {"echo": s.DB_ECHO, "future": True}

        if dialect == "sqlite":
            # 长期任务（回填几年历史）读写会撞在一起，WAL + busy_timeout 是必须的：
            # 默认 journal 模式下「database is locked」几乎必然出现。
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
            if dialect == "mysql":
                # MySQL 服务端默认 8 小时断空闲连接，客户端侧主动回收更省事；
                # 另外必须显式 utf8mb4，否则中文标题会变成问号。
                kwargs.update({"pool_recycle": 28000})
            elif dialect == "postgresql":
                # asyncpg 不支持 statement_cache_size=0 之外的部分参数，这里只放通用的
                kwargs.update({"pool_timeout": 30})

            engine = create_async_engine(url, **kwargs)

        _engine = engine
        logger.event("db.engine_ready", dialect=dialect, url=redact_url(url))
    return _engine


def current_dialect(settings: Settings | None = None) -> str:
    """当前连接串对应的方言名（``sqlite`` / ``postgresql`` / ``mysql``）。"""
    s = settings or get_settings()
    try:
        return detect_dialect(s.DATABASE_URL)
    except ValueError:
        # 连接串还没配好（例如安装向导进行中）：按最保守的 SQLite 处理，
        # 总比让 getattr-style 的判定抛异常要好。
        return "sqlite"


async def reset_engine() -> None:
    """丢弃当前引擎与会话工厂，下一次请求按最新配置重建。

    安装向导完成时会改 `.env`；如果不重置，进程里那个旧引擎会一直指向旧数据库，
    表现出来就是「明明选了 PostgreSQL，界面上还是 SQLite」。
    """
    await dispose_engine()
    get_settings.cache_clear()


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
