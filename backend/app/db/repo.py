# -*- coding: utf-8 -*-
"""通用仓储：批量 upsert、最近值查询、一致性封装。

所有写入都携带 source_id / provider / quality_status，确保可追溯。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.logging import get_logger
from .models import Candle, MarketPrice, RawMarketData

logger = get_logger(__name__)

CHUNK = 500


def dialect_insert(session: AsyncSession, model: Any):
    """按当前数据库方言返回对应的 INSERT 构造器（SQLite / PostgreSQL 均可 upsert）。"""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    bind = session.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        return pg_insert(model)
    return sqlite_insert(model)


async def upsert_candles(session: AsyncSession, rows: Sequence[dict[str, Any]]) -> int:
    """幂等写入 K 线（重复同步不会破坏历史数据）。

    冲突目标必须与模型 UniqueConstraint("symbol","interval","ts","source_id") 完全一致，
    因此这里用列名 `source_id`（不是 `provider`），并同时兼容 SQLite 与 PostgreSQL。
    """
    if not rows:
        return 0
    written = 0
    update_cols = {
        "open", "high", "low", "close", "volume",
        "quote_volume", "trades", "fetch_time", "updated_at", "quality_status",
    }
    for i in range(0, len(rows), CHUNK):
        chunk = rows[i : i + CHUNK]
        stmt = dialect_insert(session, Candle).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=["symbol", "interval", "ts", "source_id"],
            set_={name: getattr(stmt.excluded, name) for name in update_cols},
        )
        await session.execute(stmt)
        written += len(chunk)
    await session.commit()
    return written


async def upsert_rows(
    session: AsyncSession,
    model: Any,
    rows: Sequence[dict[str, Any]],
    index_elements: Sequence[str],
    update_cols: Sequence[str],
) -> int:
    """通用幂等写入。

    采集任务会被调度器反复执行（例如每小时一次），同一个观测点可能被重复抓取，
    因此所有批量写入都必须是 upsert 而不是 insert —— 否则第二次运行直接撞唯一约束而失败。

    冲突列必须与模型的 UniqueConstraint 完全一致（同名列，例如 `source_id`）。
    """
    if not rows:
        return 0
    written = 0
    # 冲突列本身不能被更新；同时过滤掉模型中不存在的列，
    # 避免调用方传入 updated_at 之类字段时把整个采集任务打挂。
    existing = set(model.__table__.columns.keys())
    conflict = [c for c in index_elements if c in existing]
    updatable = [c for c in update_cols if c not in set(conflict) and c in existing]

    dropped_conflict = [c for c in index_elements if c not in existing]
    dropped_cols: set[str] = set()
    if dropped_conflict:
        logger.event("db.upsert_conflict_key_missing", model=getattr(model, "__name__", "?"),
                     columns=",".join(dropped_conflict))

    for i in range(0, len(rows), CHUNK):
        # 逐行剔除模型里不存在的键：厂商字段改名、采集器版本不一致时，
        # 不能因为一行多余字段就让整批历史数据写不进去。
        clean_rows = []
        for row in rows[i : i + CHUNK]:
            extra = set(row) - existing
            if extra:
                dropped_cols |= extra
            clean_rows.append({k: v for k, v in row.items() if k in existing})
        if dropped_cols:
            logger.event("db.upsert_dropped_columns", model=getattr(model, "__name__", "?"),
                         columns=",".join(sorted(dropped_cols)[:20]))
        if not clean_rows or not clean_rows[0]:
            continue
        stmt = dialect_insert(session, model).values(clean_rows)
        if not updatable:
            # 没有可更新列时退化为「冲突即忽略」，同样保证幂等
            stmt = stmt.on_conflict_do_nothing(index_elements=conflict)
        else:
            stmt = stmt.on_conflict_do_update(
                index_elements=conflict,
                set_={name: getattr(stmt.excluded, name) for name in updatable},
            )
        await session.execute(stmt)
        written += len(clean_rows)
    await session.commit()
    return written


async def insert_raw(
    session: AsyncSession,
    *,
    provider: str,
    endpoint: str,
    category: str,
    payload: Any,
    params: dict[str, Any] | None = None,
    http_status: int | None = None,
    latency_ms: float = 0.0,
) -> None:
    """原始响应留档（失败不影响主流程）。"""
    try:
        import json

        text_payload = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, default=str)
        session.add(
            RawMarketData(
                provider=provider,
                endpoint=endpoint,
                category=category,
                request_params=params or {},
                payload=text_payload[:200000],
                http_status=http_status,
                latency_ms=latency_ms,
            )
        )
        await session.commit()
    except Exception as exc:  # noqa: BLE001 - 原始留档失败不应中断业务写入
        logger.event("db.raw_insert_failed", error=str(exc)[:200])
        await session.rollback()


async def upsert_market_price(session: AsyncSession, row: dict[str, Any]) -> None:
    stmt = dialect_insert(session, MarketPrice).values(row)
    stmt = stmt.on_conflict_do_update(
        index_elements=["symbol", "source_id", "observation_time"],
        set_={
            "price": stmt.excluded.price,
            "fetch_time": stmt.excluded.fetch_time,
            "updated_at": stmt.excluded.updated_at,
            "quality_status": stmt.excluded.quality_status,
            "cross_validation": stmt.excluded.cross_validation,
            "confidence": stmt.excluded.confidence,
        },
    )
    await session.execute(stmt)
    await session.commit()


async def latest_price(session: AsyncSession, symbol: str = "BTC") -> dict[str, Any] | None:
    """最近一次可信价格（无论来自哪个 Provider）——断网降级运行的基础。"""
    stmt = (
        select(MarketPrice)
        .where(MarketPrice.symbol == symbol)
        .order_by(MarketPrice.observation_time.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    row = result.scalars().first()
    if not row:
        return None
    return {
        "price": row.price,
        "provider": row.source_id,
        "quality_status": row.quality_status,
        "observation_time": row.observation_time.isoformat() if row.observation_time else None,
        "fetch_time": row.fetch_time.isoformat() if row.fetch_time else None,
        "confidence": row.confidence,
        "cross_validation": row.cross_validation,
    }


async def latest_candles(
    session: AsyncSession,
    symbol: str = "BTC",
    interval: str = "1d",
    limit: int = 500,
    before_ts: int | None = None,
) -> list[dict[str, Any]]:
    """读取本地历史 K 线 —— 查看历史优先读自己的数据库，不依赖第三方。"""
    stmt = (
        select(Candle)
        .where(Candle.symbol == symbol, Candle.interval == interval)
        .order_by(Candle.ts.desc())
        .limit(limit)
    )
    if before_ts:
        stmt = stmt.where(Candle.ts <= before_ts)
    result = await session.execute(stmt)
    rows = list(result.scalars())
    rows.reverse()
    return [
        {
            "ts": c.ts,
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
            "volume": c.volume,
            "provider": c.source_id,
            "quality_status": c.quality_status,
        }
        for c in rows
    ]


async def candle_coverage(session: AsyncSession, symbol: str = "BTC", interval: str = "1d") -> dict[str, Any]:
    """本地历史覆盖范围与缺口统计。"""
    stmt = select(
        func.min(Candle.ts), func.max(Candle.ts), func.count(Candle.id)
    ).where(Candle.symbol == symbol, Candle.interval == interval)
    result = await session.execute(stmt)
    min_ts, max_ts, count = result.first() or (None, None, 0)
    return {
        "symbol": symbol,
        "interval": interval,
        "count": int(count or 0),
        "start_ts": int(min_ts) if min_ts else None,
        "end_ts": int(max_ts) if max_ts else None,
        "expected_days": int((max_ts - min_ts) // 86400) + 1 if (min_ts and max_ts) else 0,
    }


async def purge_old_raw(session: AsyncSession, days: int) -> int:
    from datetime import timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    stmt = delete(RawMarketData).where(RawMarketData.fetch_time < cutoff)
    result = await session.execute(stmt)
    await session.commit()
    return int(result.rowcount or 0)


async def distinct_values(session: AsyncSession, model: Any, column: Any, limit: int = 100) -> list[Any]:
    result = await session.execute(select(column).distinct().limit(limit))
    return list(result.scalars())
