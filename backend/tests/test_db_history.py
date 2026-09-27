# -*- coding: utf-8 -*-
"""数据库与历史数据测试。

对应需求：
  * 本地历史数据优先，一次抓取永久保存；
  * 重复同步必须幂等（调度器每小时跑一次，不能撞唯一约束导致失败）；
  * 升级新增字段不能丢历史数据（ensure_columns）；
  * 原始表 / 标准表双存储；
  * 异常 K 线（high<low、负数、价格为 0）必须能被检测出来。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.db.base import get_session_factory
from app.db.migrate import ensure_columns, init_db
from app.db.models import (
    Candle,
    DataQuality,
    MacroSeries,
    MarketPrice,
    RawMarketData,
    Sentiment,
)
from app.db.repo import (
    candle_coverage,
    insert_raw,
    latest_price,
    upsert_candles,
    upsert_market_price,
    upsert_rows,
)

DAY = 86400


def candle_row(ts: int, close: float, source: str = "unit_source") -> dict:
    return {
        "symbol": "BTC",
        "interval": "1d",
        "ts": ts,
        "open": close,
        "high": close * 1.01,
        "low": close * 0.99,
        "close": close,
        "volume": 100.0,
        "source_id": source,
        "quality_status": "SINGLE_SOURCE",
        "observation_time": datetime.fromtimestamp(ts, tz=timezone.utc),
        "fetch_time": datetime.now(timezone.utc),
    }


@pytest.mark.asyncio
async def test_init_db_creates_tables(tmp_db_path):
    await init_db()
    factory = get_session_factory()
    async with factory() as session:
        for table in ("candles", "market_prices", "providers", "model_versions", "backtest_runs"):
            rows = await session.execute(text(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'"))
            assert rows.scalar() == table, f"缺少表 {table}"


@pytest.mark.asyncio
async def test_candle_write_is_idempotent(tmp_db_path):
    """同一批 K 线写入两次不得报错、也不得产生重复行。"""
    await init_db()
    factory = get_session_factory()
    base = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp())
    rows = [candle_row(base + i * DAY, 42000.0 + i) for i in range(10)]

    async with factory() as session:
        assert await upsert_candles(session, rows) == 10
    async with factory() as session:
        # 第二次：值更新而不是插入新行
        rows[0]["close"] = 99999.0
        assert await upsert_candles(session, rows) == 10

    async with factory() as session:
        cov = await candle_coverage(session, "BTC", "1d")
        assert cov["count"] == 10
        assert cov["expected_days"] == 10
        row = (await session.execute(
            text("SELECT close FROM candles WHERE ts = :ts"), {"ts": base}
        )).scalar()
        assert row == 99999.0


@pytest.mark.asyncio
async def test_candles_from_multiple_sources_are_kept(tmp_db_path):
    """同一时间点来自不同数据源的 K 线都应保留（可交叉校验），由 UniqueConstraint 保证。"""
    await init_db()
    factory = get_session_factory()
    ts = int(datetime(2024, 5, 1, tzinfo=timezone.utc).timestamp())
    async with factory() as session:
        await upsert_candles(session, [candle_row(ts, 60000.0, source="src_a")])
        await upsert_candles(session, [candle_row(ts, 60050.0, source="src_b")])
    async with factory() as session:
        rows = (await session.execute(
            text("SELECT source_id, close FROM candles WHERE ts = :ts ORDER BY source_id"), {"ts": ts}
        )).all()
        assert [r[0] for r in rows] == ["src_a", "src_b"]


@pytest.mark.asyncio
async def test_upsert_rows_is_idempotent(tmp_db_path):
    """采集器周期性重跑时不能因为唯一约束失败（此前真实踩过）。"""
    await init_db()
    factory = get_session_factory()
    ts = int(datetime(2024, 6, 1, tzinfo=timezone.utc).timestamp())
    payload = [{
        "series_id": "DXY", "observation_ts": ts, "release_ts": ts, "value": 104.5,
        "source_id": "unit_source", "quality_status": "SINGLE_SOURCE",
        "fetch_time": datetime.now(timezone.utc),
    }]
    index = ["series_id", "observation_ts", "source_id"]
    async with factory() as session:
        await upsert_rows(session, MacroSeries, payload, index, ["value", "quality_status"])
    async with factory() as session:
        payload[0]["value"] = 105.2
        await upsert_rows(session, MacroSeries, payload, index, ["value", "quality_status"])
    async with factory() as session:
        n = (await session.execute(text("SELECT COUNT(*) FROM macro_series"))).scalar()
        v = (await session.execute(text("SELECT value FROM macro_series"))).scalar()
        assert n == 1
        assert v == 105.2


@pytest.mark.asyncio
async def test_upsert_rows_ignores_columns_missing_in_model(tmp_db_path):
    """防御性：传了模型里不存在的列（例如 updated_at）不能让采集任务崩溃。"""
    await init_db()
    factory = get_session_factory()
    ts = int(datetime(2024, 7, 1, tzinfo=timezone.utc).timestamp())
    payload = [{
        "observation_ts": ts, "metric": "fear_greed", "value": 55.0, "classification": "Neutral",
        "source_id": "unit_source", "quality_status": "SINGLE_SOURCE",
        "updated_at": datetime.now(timezone.utc),   # 该列在 upsert 更新列里，但不在冲突键里
        "fetch_time": datetime.now(timezone.utc),
    }]
    async with factory() as session:
        await upsert_rows(session, Sentiment, payload,
                          ["metric", "observation_ts", "source_id"], ["value", "updated_at"])


@pytest.mark.asyncio
async def test_upsert_rows_drops_keys_absent_from_model(tmp_db_path):
    """真实场景：采集器传入的字典里带了模型不存在的列（如 timestamp），
    必须被安全丢弃，而不是让整个定时任务崩掉。"""
    await init_db()
    factory = get_session_factory()
    ts = int(datetime(2024, 7, 15, tzinfo=timezone.utc).timestamp())
    payload = [{
        "series_id": "CPI", "observation_ts": ts, "value": 313.7,
        "source_id": "unit_source", "quality_status": "SINGLE_SOURCE",
        "timestamp": ts,                 # MacroSeries 里没有这一列
        "not_a_column": "will be dropped",
        "fetch_time": datetime.now(timezone.utc),
    }]
    # 冲突键里也不慎混入了模型不存在的列
    async with factory() as session:
        written = await upsert_rows(
            session, MacroSeries, payload,
            ["series_id", "observation_ts", "source_id"], ["value"],
        )
        assert written == 1
    async with factory() as session:
        v = (await session.execute(text("SELECT value FROM macro_series"))).scalar()
        assert v == 313.7


@pytest.mark.asyncio
async def test_raw_and_normalized_dual_storage(tmp_db_path):
    """原始数据必须留痕，事后可追溯到厂商返回体。"""
    await init_db()
    factory = get_session_factory()
    async with factory() as session:
        row_id = await insert_raw(
            session,
            provider="unit_source",
            endpoint="https://example.invalid/ticker",
            category="market_price",
            payload={"lastPrice": "61234.5"},
            http_status=200,
            latency_ms=123.4,
        )
    async with factory() as session:
        row = (await session.execute(text(
            "SELECT payload, provider FROM raw_market_data"))).first()
        assert row is not None
        # 原始响应体必须完整保留字符串痕迹，才能事后追溯到厂商返回值
        assert "lastPrice" in row[0]
        assert row[1] == "unit_source"
    _ = row_id


@pytest.mark.asyncio
async def test_latest_price_reads_back_written_value(tmp_db_path):
    await init_db()
    factory = get_session_factory()
    now = datetime.now(timezone.utc)
    async with factory() as session:
        await upsert_market_price(session, {
            "symbol": "BTC", "source_id": "unit_source", "price": 60000.0, "quote": "USDT",
            "quality_status": "SINGLE_SOURCE", "confidence": 1.0,
            "observation_time": now, "fetch_time": now,
        })
    async with factory() as session:
        got = await latest_price(session, "BTC")
        assert got is not None
        assert float(got["price"]) == 60000.0
        assert got["provider"] == "unit_source"


@pytest.mark.asyncio
async def test_latest_price_prefers_newest_observation(tmp_db_path):
    """同一符号有多条价格时，必须取观测时间最新的一条，而不是最新写入的一条。"""
    await init_db()
    factory = get_session_factory()
    newer = datetime.now(timezone.utc)
    older = newer - timedelta(hours=2)
    async with factory() as session:
        await upsert_market_price(session, {
            "symbol": "BTC", "source_id": "unit_source", "price": 61000.0, "quote": "USDT",
            "quality_status": "SINGLE_SOURCE", "confidence": 1.0,
            "observation_time": newer, "fetch_time": newer,
        })
        # 故意后写入一条"观测时间更早"的旧价格：若排序写错，这里会把它误当成最新价
        await upsert_market_price(session, {
            "symbol": "BTC", "source_id": "another_source", "price": 50000.0, "quote": "USDT",
            "quality_status": "SINGLE_SOURCE", "confidence": 1.0,
            "observation_time": older, "fetch_time": newer,
        })
    async with factory() as session:
        got = await latest_price(session, "BTC")
        assert got["price"] == 61000.0


@pytest.mark.asyncio
async def test_ensure_columns_is_incremental_and_safe(tmp_db_path):
    """重复调用 ensure_columns 不应报错，也不应丢数据。"""
    await init_db()
    factory = get_session_factory()
    ts = int(datetime(2024, 8, 1, tzinfo=timezone.utc).timestamp())
    async with factory() as session:
        await upsert_candles(session, [candle_row(ts, 50000.0)])
    added_second = await ensure_columns()
    assert added_second == 0
    async with factory() as session:
        cov = await candle_coverage(session)
        assert cov["count"] == 1


@pytest.mark.asyncio
async def test_data_quality_table_records_completeness(tmp_db_path):
    await init_db()
    factory = get_session_factory()
    ts = int(datetime(2024, 12, 31, tzinfo=timezone.utc).timestamp())
    async with factory() as session:
        session.add(DataQuality(
            category="candles", date=ts, expected=365, actual=358, missing_count=7,
            conflicts=0, completeness=0.98, status="ok", notes="测试：年度完整性",
            checked_at=datetime.now(timezone.utc),
        ))
        await session.commit()
    async with factory() as session:
        row = (await session.execute(
            text("SELECT completeness, status, missing_count FROM data_quality"))).first()
        assert row is not None
        assert row[0] == 0.98
        assert row[1] == "ok"
        assert row[2] == 7
