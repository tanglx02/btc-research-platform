# -*- coding: utf-8 -*-
"""数据库迁移初始化。

SQLite：`create_all` 即可；PostgreSQL：可选把时序表升级为 TimescaleDB hypertable。
所有迁移可重复执行（幂等），不丢历史数据。
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ..core.config import get_settings
from ..core.logging import get_logger
from .base import get_engine
from .models import ALL_TABLES, Base

logger = get_logger(__name__)

HYPERTABLES = [("candles", "ts"), ("market_prices", "observation_time"), ("indicator_values", "ts")]


async def ensure_columns(engine: AsyncEngine | None = None) -> int:
    """增量补齐「模型里新增、但旧库中还没有」的列。

    长期维护场景下，升级版本新增字段时不应该要求用户重建数据库（那会丢掉所有历史数据）。
    这里对缺失的列执行 ALTER TABLE ADD COLUMN；SQLite 下新增列一律允许 NULL，避免影响已有行。
    """
    eng = engine or get_engine()
    added = 0
    async with eng.begin() as conn:
        for table in Base.metadata.sorted_tables:
            try:
                existing = {
                    row[1]
                    for row in await conn.execute(text(f"PRAGMA table_info({table.name})"))
                } if eng.dialect.name == "sqlite" else {
                    row[0]
                    for row in await conn.execute(
                        text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"),
                        {"t": table.name},
                    )
                }
            except Exception:  # noqa: BLE001 - 表还不存在时跳过，交给 create_all
                continue
            if not existing:
                continue
            for col in table.columns:
                if col.name in existing:
                    continue
                col_type = col.type.compile(eng.dialect)
                try:
                    await conn.execute(
                        text(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {col_type}')
                    )
                    added += 1
                except Exception as exc:  # noqa: BLE001
                    logger.event("db.add_column_failed", table=table.name,
                                 column=col.name, reason=str(exc)[:160])
    if added:
        logger.event("db.columns_added", count=added)
    return added


async def init_db(engine: AsyncEngine | None = None) -> None:
    """创建全部表结构（如不存在），并增量补齐新增列。"""
    eng = engine or get_engine()
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_columns(eng)
    logger.event("db.init_ok", tables=len(ALL_TABLES))


async def enable_timescaledb(engine: AsyncEngine | None = None) -> int:
    """PostgreSQL 可选：把时序表转为 hypertable。失败安全（无扩展时跳过）。"""
    s = get_settings()
    eng = engine or get_engine()
    if s.DATABASE_URL.startswith("sqlite"):
        return 0
    applied = 0
    async with eng.begin() as conn:
        try:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE"))
        except Exception as exc:  # noqa: BLE001 - 无扩展权限时安全跳过
            logger.event("db.timescaledb_skipped", reason=str(exc)[:200])
            return 0
        for table, time_col in HYPERTABLES:
            try:
                await conn.execute(
                    text(f"SELECT create_hypertable('{table}', '{time_col}', if_not_exists => TRUE, chunk_time_interval => 86400)")
                )
                applied += 1
            except Exception as exc:  # noqa: BLE001
                logger.event("db.hypertable_skipped", table=table, reason=str(exc)[:200])
    logger.event("db.timescaledb_applied", tables=applied)
    return applied


async def drop_all(engine: AsyncEngine | None = None) -> None:
    """危险操作：仅供测试环境重置使用。"""
    eng = engine or get_engine()
    s = get_settings()
    if s.is_production:
        raise RuntimeError("禁止在生产环境执行 drop_all")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def table_stats() -> dict[str, int]:
    """各表行数统计（数据质量中心使用）。"""
    from sqlalchemy import func, select

    from .base import get_session_factory

    counts: dict[str, int] = {}
    factory = get_session_factory()
    async with factory() as session:
        for model in ALL_TABLES:
            try:
                result = await session.execute(select(func.count()).select_from(model))
                counts[model.__tablename__] = int(result.scalar() or 0)
            except Exception:  # noqa: BLE001 - 单表统计失败不阻断
                counts[model.__tablename__] = -1
    return counts
