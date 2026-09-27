# -*- coding: utf-8 -*-
"""长时间运行稳定性测试（标记为 slow，CI 可用 `-m "not slow"` 跳过）。

长期跑才是真考验：一个每小时执行一次的调度任务，跑一年就是 8760 次。
这里模拟「反复重跑」会暴露的三类问题：
  1. 重复写入导致数据行数无上限增长（唯一约束没配对）；
  2. 每次重跑都产生微小差异，长时间累积成漂移；
  3. 内存/句柄泄漏导致越来越慢（这里至少保证运行时间与结果规模是线性、可控的）。
"""

from __future__ import annotations

import time

import pytest
from sqlalchemy import text

from app.db.base import get_session_factory
from app.db.migrate import init_db
from app.db.repo import candle_coverage, upsert_candles
from app.engines.indicators import IndicatorEngine

from helpers import build_candles


@pytest.mark.slow
@pytest.mark.asyncio
async def test_repeated_incremental_sync_is_stable(tmp_db_path):
    """同一批数据重复入库 20 次，行数必须保持不变。"""
    await init_db()
    factory = get_session_factory()
    batch = build_candles(200)

    for rounds in range(20):
        async with factory() as session:
            await upsert_candles(session, batch)
        async with factory() as session:
            count = (await session.execute(text("SELECT COUNT(*) FROM candles"))).scalar()
        assert count == 200, f"第 {rounds + 1} 轮重复同步后行数变成 {count}"


@pytest.mark.slow
@pytest.mark.asyncio
async def test_repeated_overlapping_batches_do_not_drift(tmp_db_path):
    """增量拉取天然会有重叠窗口（前后几根重复），不得造成数据漂移。"""
    await init_db()
    factory = get_session_factory()
    full = build_candles(300)

    # 模拟每轮带 30 根重叠窗口的分批写入
    step, overlap = 100, 30
    for start in range(0, len(full), step):
        chunk = full[max(0, start - overlap) : start + step]
        async with factory() as session:
            await upsert_candles(session, chunk)

    async with factory() as session:
        coverage = await candle_coverage(session, "BTC", "1d")
        close_sum = (await session.execute(text("SELECT SUM(close) FROM candles"))).scalar()

    assert coverage["count"] == 300
    assert close_sum == pytest.approx(sum(c["close"] for c in full), rel=1e-6)


@pytest.mark.slow
@pytest.mark.asyncio
async def test_indicator_cost_grows_roughly_linearly(tmp_db_path):
    """指标计算的耗时不应随样本量爆炸式增长（避免长期运行后单次计算拖垮服务）。"""
    await init_db()
    factory = get_session_factory()
    small = build_candles(500)
    large = build_candles(2000)
    async with factory() as session:
        await upsert_candles(session, small)
        await upsert_candles(session, large[500:])

    engine = IndicatorEngine()
    t0 = time.perf_counter()
    engine.compute(small, include_series=False)
    small_cost = time.perf_counter() - t0

    t1 = time.perf_counter()
    engine.compute(large, include_series=False)
    large_cost = time.perf_counter() - t1

    # 样本量 x4，耗时上限放宽到 x15（涵盖解释器抖动），但不允许爆炸
    assert large_cost < max(0.5, small_cost * 15), (
        f"指标计算耗时异常：500 根 {small_cost:.3f}s，2000 根 {large_cost:.3f}s"
    )
