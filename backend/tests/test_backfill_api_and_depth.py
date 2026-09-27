# -*- coding: utf-8 -*-
"""历史回填相关的回归测试。

三条用例各自对应一个真实发生过的缺陷 —— 都是在界面上表现为
「点了一下，显示成功（或 0 根），但什么也没发生」：

1. 请求体错位：``post(path, null, {...})`` 让后端收到字面量 JSON ``null`` → HTTP 422；
2. 过期断点吞掉指定范围：无条件 ``begin = max(cursor, begin)`` → while 一次不进，rows=0；
3. 参考价取错：拿「当前最新价」去校验「历史价格」，偏差超过 25% → 整批被判脏数据丢弃。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.collectors.market_collector import MarketCollector
from app.db.base import get_session_factory
from app.db.migrate import init_db
from app.db.models import Candle, MarketPrice
from app.db.repo import candle_coverage, latest_candles, upsert_candles

from helpers import build_candles


def _day(offset_days: int, base: str = "2024-01-01") -> int:
    base_dt = datetime.strptime(base, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int((base_dt + timedelta(days=offset_days)).timestamp())


@pytest.mark.asyncio
async def test_reference_price_prefers_local_neighbour_over_global_latest(tmp_db_path):
    """参考价必须按时间去取：回填 2024 年时要用 2024 年的价，不能用今天的价。

    以前只用「最新价」，于是 2024 年的 44,179 对上今天的 84,436（偏差 47%），
    全部被 sanity_check 判为脏数据丢弃 —— 界面显示「写入 0 根」，
    用户会以为数据源没数据，其实是系统自己拦下了。
    """
    await init_db()
    factory = get_session_factory()
    async with factory() as session:
        session.add(
            MarketPrice(
                symbol="BTC",
                source_id="unit",
                price=84436.0,               # 「当前」价：远高于 2024 年
                quality_status="ok",
                observation_time=datetime.now(timezone.utc),
                fetch_time=datetime.now(timezone.utc),
            )
        )
        await session.commit()

    collector = MarketCollector(interval="1d")

    # 本地还没有任何 K 线：2024 年那段时间没有参照 -> 不猜、返回 None（随后只做区间校验）
    assert await collector._reference_price(_day(0)) is None

    # 放进一根 2024 年的 K 线，再问同一时刻，必须拿到的是它的价格
    async with factory() as session:
        await upsert_candles(
            session,
            [
                {
                    "symbol": "BTC",
                    "interval": "1d",
                    "ts": _day(0),
                    "open": 44000.0,
                    "high": 44500.0,
                    "low": 43800.0,
                    "close": 44179.55,
                    "volume": 100.0,
                    "quote_volume": 0.0,
                    "trades": 0,
                    "source_id": "unit",
                    "quality_status": "ok",
                    "observation_time": datetime.fromtimestamp(_day(0), tz=timezone.utc),
                    "fetch_time": datetime.now(timezone.utc),
                }
            ],
        )

    assert await collector._reference_price(_day(0)) == pytest.approx(44179.55)
    # 不传时间（增量场景）仍然取最新价
    assert await collector._reference_price() == pytest.approx(84436.0)


@pytest.mark.asyncio
async def test_historical_batch_is_not_rejected_because_of_reference_price(tmp_db_path, monkeypatch):
    """端到端串起来：2024 年的真实价格必须能写进去，不能被最新价判成脏数据。"""
    await init_db()
    factory = get_session_factory()
    async with factory() as session:
        session.add(
            MarketPrice(
                symbol="BTC",
                source_id="unit",
                price=84436.0,
                quality_status="ok",
                observation_time=datetime.now(timezone.utc),
                fetch_time=datetime.now(timezone.utc),
            )
        )
        await session.commit()

    collector = MarketCollector(interval="1d")
    base_ts = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp())
    end_ms = (base_ts + 5 * 86400) * 1000

    from app.providers.types import Candle as CandleDTO
    from app.providers.types import DataCategory, ProviderResult

    dtos = [
        CandleDTO(
            ts=base_ts + i * 86400,
            open=44000.0 + i,
            high=44600.0 + i,
            low=43800.0 + i,
            close=44179.0 + i,
            volume=1000.0,
            quote_volume=0.0,
            trades=0,
        )
        for i in range(5)
    ]

    async def fake_fetch(category, **kwargs):  # noqa: ANN001
        return ProviderResult(
            data=dtos,
            provider="unit-source",
            category=DataCategory.OHLCV,
            latency_ms=1.0,
        )

    monkeypatch.setattr(collector.router, "fetch", fake_fetch)

    # 不传 reference_ts（模拟修复前的行为）：拿最新价 84436 比 44179 -> 全拒
    legacy_written = await collector._fetch_and_store("BTC", base_ts * 1000, end_ms)
    assert legacy_written == 0, "用最新价校验历史价格时应当全被拒 —— 这正是 bug 现场"

    # 传窗口时间（修复后的写法）：没有本地参照也照常写入，历史数据不再被误杀
    written = await collector._fetch_and_store("BTC", base_ts * 1000, end_ms, reference_ts=base_ts)
    assert written == 5


@pytest.mark.asyncio
async def test_same_timestamp_from_two_sources_is_read_once(tmp_db_path):
    """多源副本只用于交叉验证，读出来分析时必须去重。

    同一天两根 K 线会让图表出现重复横坐标，指标会变成在重复序列上算，结果全是错的。
    """
    await init_db()
    factory = get_session_factory()
    base = datetime(2024, 3, 1, tzinfo=timezone.utc)
    rows = []
    for source, price in (("gate", 61000.0), ("binance_vision", 61010.0)):
        for i in range(5):
            ts = int((base + timedelta(days=i)).timestamp())
            rows.append(
                {
                    "symbol": "BTC",
                    "interval": "1d",
                    "ts": ts,
                    "open": price,
                    "high": price + 100,
                    "low": price - 100,
                    "close": price + 1,
                    "volume": 10.0,
                    "quote_volume": 0.0,
                    "trades": 1,
                    "source_id": source,
                    "quality_status": "ok",
                    "observation_time": datetime.fromtimestamp(ts, tz=timezone.utc),
                    "fetch_time": datetime.fromtimestamp(ts, tz=timezone.utc),
                }
            )
    async with factory() as session:
        await upsert_candles(session, rows)

    async with factory() as session:
        candles = await latest_candles(session, "BTC", "1d", limit=10)
        coverage = await candle_coverage(session, "BTC", "1d")

    timestamps = [c["ts"] for c in candles]
    assert len(timestamps) == len(set(timestamps)), "同一时间点读出了多根 K 线"
    assert len(candles) == 5, f"期望 5 个时间点，实际 {len(candles)}"
    assert coverage["count"] == 5, "覆盖率不能按多源副本重复计数"


@pytest.mark.asyncio
async def test_reading_does_not_lose_days_when_duplicates_exist(tmp_db_path):
    """有副本时 limit=N 仍要读到 N 个时间点，而不是 N/副本数。"""
    await init_db()
    factory = get_session_factory()
    base = datetime(2023, 6, 1, tzinfo=timezone.utc)
    rows = []
    for source in ("gate", "binance_vision", "okx"):
        for i in range(10):
            ts = int((base + timedelta(days=i)).timestamp())
            rows.append(
                {
                    "symbol": "BTC",
                    "interval": "1d",
                    "ts": ts,
                    "open": 27000.0,
                    "high": 27100.0,
                    "low": 26900.0,
                    "close": 27050.0,
                    "volume": 1.0,
                    "quote_volume": 0.0,
                    "trades": 1,
                    "source_id": source,
                    "quality_status": "ok",
                    "observation_time": datetime.fromtimestamp(ts, tz=timezone.utc),
                    "fetch_time": datetime.fromtimestamp(ts, tz=timezone.utc),
                }
            )
    async with factory() as session:
        await upsert_candles(session, rows)

    async with factory() as session:
        candles = await latest_candles(session, "BTC", "1d", limit=6)

    assert len(candles) == 6, f"三份副本下 limit=6 应拿到 6 天，实际 {len(candles)}"
    assert [c["ts"] for c in candles] == sorted(c["ts"] for c in candles), "返回必须按时间升序"


def test_build_candles_helper_shape():
    """helpers.build_candles 仍是 candles 语义，避免后续用例被 helper 改动带偏。"""
    batch = build_candles(3)
    assert len(batch) == 3
    assert {"symbol", "interval", "ts", "close"} <= set(batch[0])
