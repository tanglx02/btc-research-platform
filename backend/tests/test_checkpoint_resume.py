# -*- coding: utf-8 -*-
"""断点续传与任务恢复测试。

长期运行的系统会不断被中断：断网、进程重启、机器掉电、API 限额。
这里的验收点是「中断之后继续跑，不得重复劳动、也不得丢数据」：
  * checkpoint 必须持久化到数据库（进程重启后依然可读）；
  * 从头重来的代价要避免 —— cursor 必须记录到最后一批成功的位置；
  * 重新同步（reset）必须真的能清零，不能因为实现里写了 max() 而清不掉；
  * 失败状态要留痕，下次启动时知道上一次是失败而非成功。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from app.collectors.base import BaseCollector
from app.db.base import dispose_engine, get_session_factory
from app.db.migrate import init_db
from app.db.repo import candle_coverage, upsert_candles

from helpers import build_candles


class DummyCollector(BaseCollector):
    task_name = "unit_backfill"
    category = "ohlcv"

    async def execute(self) -> dict:  # pragma: no cover - 本用例只验证采集骨架
        return {"ok": True, "rows": 0}


def ts_at(date_str: str) -> int:
    return int(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def day_str(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


@pytest.mark.asyncio
async def test_checkpoint_defaults_to_start(tmp_db_path):
    await init_db()
    checkpoint = await DummyCollector().get_checkpoint()
    assert checkpoint["exists"] is False
    assert checkpoint["cursor_ts"] == 0
    assert checkpoint["status"] == "idle"


@pytest.mark.asyncio
async def test_checkpoint_advances_and_survives_restart(tmp_db_path):
    """checkpoint 写库后，即使重建 engine（模拟进程重启）也还能读回来。"""
    await init_db()
    collector = DummyCollector()
    await collector.save_checkpoint(cursor_ts=ts_at("2020-03-01"), cursor_date="2020-03-01",
                                    status="running", rows_delta=500)
    await collector.save_checkpoint(cursor_ts=ts_at("2020-07-15"), cursor_date="2020-07-15",
                                    status="running", rows_delta=300)

    checkpoint = await DummyCollector().get_checkpoint()
    assert checkpoint["cursor_date"] == "2020-07-15"
    assert checkpoint["total_rows"] == 800

    # 模拟进程重启：清掉 engine 单例后仍要从数据库读回进度
    await dispose_engine()
    after_restart = await DummyCollector().get_checkpoint()
    assert after_restart["exists"] is True
    assert after_restart["cursor_ts"] == ts_at("2020-07-15")
    assert after_restart["total_rows"] == 800


@pytest.mark.asyncio
async def test_cursor_does_not_go_backwards_on_out_of_order_write(tmp_db_path):
    """乱序回写不能把进度倒退（否则已抓过的数据会被重复抓取）。"""
    await init_db()
    collector = DummyCollector()
    await collector.save_checkpoint(cursor_ts=ts_at("2021-05-01"), status="running")
    await collector.save_checkpoint(cursor_ts=ts_at("2020-01-01"), status="running")
    assert (await collector.get_checkpoint())["cursor_ts"] == ts_at("2021-05-01")


@pytest.mark.asyncio
async def test_reset_really_clears_progress(tmp_db_path):
    """重新同步必须真清零 —— 否则 --reset 看起来执行成功却仍从旧断点继续。"""
    await init_db()
    collector = DummyCollector()
    await collector.save_checkpoint(cursor_ts=ts_at("2022-09-09"), status="failed",
                                    rows_delta=9000, error="boom")
    assert (await collector.get_checkpoint())["status"] == "failed"

    await collector.reset_checkpoint()
    checkpoint = await collector.get_checkpoint()
    assert checkpoint["cursor_ts"] == 0, "reset 未真正清零，--reset 会失效"
    assert checkpoint["status"] == "idle"
    assert checkpoint["last_error"] is None


@pytest.mark.asyncio
async def test_failure_state_is_recorded_for_next_run(tmp_db_path):
    await init_db()
    collector = DummyCollector()
    await collector.save_checkpoint(cursor_ts=ts_at("2023-03-03"), status="failed",
                                    error="provider quota exceeded")
    checkpoint = await collector.get_checkpoint()
    assert checkpoint["status"] == "failed"
    assert "quota" in (checkpoint["last_error"] or "")


@pytest.mark.asyncio
async def test_resume_after_interruption_writes_only_missing_part(tmp_db_path):
    """端到端演练：抓一半中断 → 重启后从断点继续 → 总量不丢不重。"""
    await init_db()
    factory = get_session_factory()
    first_half = build_candles(100)
    async with factory() as session:
        await upsert_candles(session, first_half)

    collector = DummyCollector()
    await collector.save_checkpoint(cursor_ts=first_half[-1]["ts"],
                                    cursor_date=day_str(first_half[-1]["ts"]),
                                    status="failed", rows_delta=100, error="模拟中断")

    # —— 模拟进程重启 ——
    await dispose_engine()
    factory = get_session_factory()

    checkpoint = await DummyCollector().get_checkpoint()
    assert checkpoint["status"] == "failed"
    resume_from = checkpoint["cursor_ts"]
    assert resume_from == first_half[-1]["ts"]

    # 从断点后一天继续抓
    extension_start = datetime.fromtimestamp(resume_from + 86400, tz=timezone.utc)
    second_half = build_candles(50, start=extension_start)
    async with factory() as session:
        await upsert_candles(session, second_half)
    await collector.save_checkpoint(cursor_ts=second_half[-1]["ts"], status="done",
                                    rows_delta=len(second_half))

    async with factory() as session:
        coverage = await candle_coverage(session, "BTC", "1d")
        count = (await session.execute(text("SELECT COUNT(*) FROM candles"))).scalar()

    # 中途中断既没有造成重复行，也没有丢数据
    assert count == coverage["count"] == 150
    assert (await collector.get_checkpoint())["total_rows"] == 150


@pytest.mark.asyncio
async def test_re_running_completed_batch_does_not_duplicate(tmp_db_path):
    """同一批数据重复入库（调度器反复触发）必须覆盖而不是叠加。"""
    await init_db()
    factory = get_session_factory()
    batch = build_candles(60)
    async with factory() as session:
        await upsert_candles(session, batch)
    async with factory() as session:
        await upsert_candles(session, batch)
    async with factory() as session:
        count = (await session.execute(text("SELECT COUNT(*) FROM candles"))).scalar()
    assert count == 60
