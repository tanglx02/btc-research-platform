# -*- coding: utf-8 -*-
"""行情采集器：实时 tick / 增量同步 / 历史回填（断点续传）/ 自动补洞。

历史数据的核心原则：
    「抓一次，永久保存在自己的数据库；以后看历史只读本地，不再依赖第三方。」
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select

from ..core.config import get_settings
from ..core.errors import AllProvidersFailedError
from ..core.logging import get_logger
from ..db.base import get_session_factory
from ..db.models import Candle, DataQuality, MarketPrice
from ..db.repo import insert_raw, upsert_candles, upsert_market_price
from ..providers.router import ResilientRouter, get_router
from ..providers.types import Candle as CandleDTO
from ..providers.types import DataCategory
from ..providers.validation import completeness_ratio, sanity_check_price
from .base import BaseCollector

logger = get_logger(__name__)

INTERVAL_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "12h": 43200,
    "1d": 86400, "1w": 604800,
}


class MarketCollector(BaseCollector):
    """BTC 现货行情采集。"""

    category = "market"

    def __init__(self, interval: str = "1d", router: ResilientRouter | None = None) -> None:
        """构造参数顺序：interval 在前，router 在后。

        历史坑：早期版本写成 `(router, interval)`，而调度器按 `MarketCollector("1d")` 调用，
        于是字符串被塞进 router，表现为 `'str' object has no attribute 'fetch_validated'`。
        除了统一为 interval 优先，这里再做一层防御校正 —— 长期运行的系统里，
        任何一处旧写法或第三方脚本误用都不应该让采集任务静默失败。
        """
        if not isinstance(interval, str):          # 旧写法：MarketCollector(router)
            interval, router = "1d", interval
        if isinstance(router, str):                # 反过来误传：router 位置拿到周期字符串
            interval, router = router, None
        self.s = get_settings()
        self.router = router or get_router()
        self.task_name = f"market_{interval}"
        self.interval = interval
        self.step = INTERVAL_SECONDS.get(interval, 86400)
        self.batch_days = self.s.BACKFILL_BATCH_DAYS

    # -------------------------------------------------------------- 实时 tick
    async def collect_tick(self, symbol: str = "BTC") -> dict[str, Any]:
        """抓取当前价格（带交叉验证）并落库。失败时保留最近一次可信数据，不写假数据。"""
        try:
            result = await self.router.fetch_validated(DataCategory.MARKET_PRICE)
        except AllProvidersFailedError as exc:
            logger.event("collect.tick_all_failed", attempts=len(exc.attempts))
            return {"ok": False, "reason": "all_providers_failed", "price": None}

        price = float(result.data)
        ok, reason = sanity_check_price(price)
        if not ok:
            logger.event("collect.tick_rejected", reason=reason, price=price)
            return {"ok": False, "reason": reason}

        factory = get_session_factory()
        async with factory() as session:
            await upsert_market_price(
                session,
                {
                    "symbol": symbol,
                    "source_id": result.provider,
                    "price": price,
                    "quote": result.meta.get("quote", "USD"),
                    "quality_status": result.quality.value,
                    "cross_validation": result.cross_validation,
                    "confidence": result.confidence,
                    "observation_time": result.observation_time or datetime.now(timezone.utc),
                    "fetch_time": datetime.now(timezone.utc),
                },
            )
            await insert_raw(
                session,
                provider=result.provider,
                endpoint=result.trace.endpoint if result.trace else "price",
                category=DataCategory.MARKET_PRICE.value,
                payload=result.trace.raw_payload if result.trace and result.trace.raw_payload else {"price": price},
                http_status=result.trace.http_status if result.trace else None,
                latency_ms=result.latency_ms,
            )
        await self.save_checkpoint(
            cursor_ts=int(time.time()),
            status="running",
            provider=result.provider,
        )
        return {
            "ok": True,
            "price": price,
            "provider": result.provider,
            "used_fallback": result.used_fallback,
            "quality": result.quality.value,
            "confidence": round(result.confidence, 3),
        }

    # -------------------------------------------------------------- 增量同步
    async def sync_incremental(self, symbol: str = "BTC", limit: int = 1000) -> dict[str, Any]:
        """从本地最新一根 K 线往后补齐。"""
        factory = get_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(func.max(Candle.ts)).where(
                    Candle.symbol == symbol, Candle.interval == self.interval
                )
            )
            latest_ts = int(result.scalar() or 0)

        now_ts = int(time.time())
        start_ts = (latest_ts - self.step * 3) if latest_ts else now_ts - self.step * limit
        rows = await self._fetch_and_store(symbol, start_ts * 1000, now_ts * 1000)
        return {"ok": True, "rows": rows, "task": self.task_name}

    # -------------------------------------------------------------- 历史回填
    async def backfill(
        self,
        symbol: str = "BTC",
        start_date: str | None = None,
        end_date: str | None = None,
        reset: bool = False,
    ) -> dict[str, Any]:
        """历史回填：断点续传。中途崩溃后再次调用会从 checkpoint 继续。"""
        if reset:
            await self.reset_checkpoint()

        checkpoint = await self.get_checkpoint()
        cursor = int(checkpoint.get("cursor_ts") or 0)

        begin = datetime.strptime(start_date or self.s.BACKFILL_START_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if cursor:
            # 从断点继续，往前留 1 根重叠，防止边界缺失
            begin = datetime.fromtimestamp(max(cursor - self.step, begin.timestamp()), tz=timezone.utc)
        end = (
            datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if end_date
            else datetime.now(timezone.utc)
        )

        total = 0
        uncovered: list[str] = []   # 明确记录「哪些时间窗口所有数据源都没有覆盖」
        cursor_dt = begin
        await self.save_checkpoint(cursor_ts=int(cursor_dt.timestamp()), status="running")

        while cursor_dt < end:
            batch_end = min(cursor_dt + timedelta(days=self.batch_days), end)
            window = f"{cursor_dt:%Y-%m-%d}~{batch_end:%Y-%m-%d}"
            try:
                rows = await self._fetch_and_store(
                    symbol, int(cursor_dt.timestamp()) * 1000, int(batch_end.timestamp()) * 1000
                )
                total += rows
                if not rows:
                    # 数据源本身没有该窗口的数据（例如 Binance 只有 2017 年之后）。
                    # 这是「确实没有」而不是「出错」：如实记录，绝不造假，继续往前走。
                    uncovered.append(window)
                    logger.event("collect.backfill_no_coverage", task=self.task_name, window=window)
            except AllProvidersFailedError as exc:
                # 「所有源都明确说没有这个窗口的数据」 -> 视为无覆盖，而非致命失败
                if self._all_empty(exc):
                    uncovered.append(window)
                    logger.event("collect.backfill_no_coverage", task=self.task_name, window=window)
                else:
                    # 真实故障：保留断点，等待下次重试（这正是断点续传的意义）
                    await self.save_checkpoint(
                        cursor_ts=int(cursor_dt.timestamp()),
                        status="failed",
                        error=f"全部数据源失败: {exc.message[:160]}",
                    )
                    logger.event("collect.backfill_interrupted", task=self.task_name, reason=exc.message)
                    return {
                        "ok": False,
                        "rows": total,
                        "stopped_at": cursor_dt.strftime("%Y-%m-%d"),
                        "reason": "all_providers_failed",
                        "uncovered_windows": uncovered,
                    }
            except Exception as exc:  # noqa: BLE001
                await self.save_checkpoint(cursor_ts=int(cursor_dt.timestamp()), status="failed", error=str(exc)[:200])
                return {
                    "ok": False,
                    "rows": total,
                    "stopped_at": cursor_dt.strftime("%Y-%m-%d"),
                    "error": str(exc)[:200],
                    "uncovered_windows": uncovered,
                }

            cursor_dt = batch_end
            await self.save_checkpoint(
                cursor_ts=int(cursor_dt.timestamp()),
                cursor_date=cursor_dt.strftime("%Y-%m-%d"),
                status="running",
                rows_delta=rows,
            )

        await self.save_checkpoint(
            cursor_ts=int(end.timestamp()), status="done", rows_delta=0,
            extra={"uncovered_windows": uncovered[-50:]},
        )
        return {
            "ok": True,
            "rows": total,
            "task": self.task_name,
            "until": end.strftime("%Y-%m-%d"),
            "uncovered_windows": uncovered,
            "note": "标记为无覆盖的时间窗口代表所有数据源都没有该区间数据，系统不会用假数据填补。",
        }

    @staticmethod
    def _all_empty(exc: AllProvidersFailedError) -> bool:
        """判断失败是否全部源于「数据源本身没有该窗口数据」（而非网络/鉴权故障）。"""
        attempts = exc.attempts or []
        if not attempts:
            return False
        empty_like = {
            "empty_data",          # 数据源明确没有该窗口数据
            "data_format_error",   # 返回格式变化（厂商改版）
            "not_configured",      # 没配 Key
            "circuit_open",        # 熔断中
            "range_unsupported",   # 该交易所不支持按区间拉历史
            "provider_unsupported",
        }
        return all(
            (a.get("failure_type") in empty_like) or (a.get("ok") is False and not a.get("failure_type"))
            for a in attempts
        ) and any(a.get("failure_type") == "empty_data" for a in attempts)

    # -------------------------------------------------------------- 内部实现
    async def _fetch_and_store(self, symbol: str, start_ms: int, end_ms: int) -> int:
        """按时间段抓取并写入，返回写入行数。"""
        span_seconds = max(1, (end_ms - start_ms) // 1000)
        limit = min(1500, int(span_seconds // self.step) + 5)

        result = await self.router.fetch(
            DataCategory.OHLCV,
            symbol=symbol,
            interval=self.interval,
            limit=limit,
            start_time=start_ms,
            end_time=end_ms,
        )
        candles: list[CandleDTO] = result.data
        if not candles:
            return 0

        now = datetime.now(timezone.utc)
        rows = []
        reference_price = await self._reference_price()
        for c in candles:
            ok_price, reason = sanity_check_price(c.close, reference=reference_price)
            if not ok_price:
                logger.event("collect.candle_rejected", reason=reason, ts=c.ts, price=c.close)
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "interval": self.interval,
                    "ts": c.ts,
                    "open": c.open,
                    "high": max(c.high, max(c.open, c.close)),
                    "low": min(c.low, min(c.open, c.close)) if c.low > 0 else min(c.open, c.close),
                    "close": c.close,
                    "volume": c.volume,
                    "quote_volume": c.quote_volume,
                    "trades": c.trades,
                    "source_id": result.provider,
                    "quality_status": result.quality.value,
                    "observation_time": datetime.fromtimestamp(c.ts, tz=timezone.utc),
                    "fetch_time": now,
                }
            )
        if not rows:
            return 0

        factory = get_session_factory()
        async with factory() as session:
            written = await upsert_candles(session, rows)
            await insert_raw(
                session,
                provider=result.provider,
                endpoint=f"ohlcv:{self.interval}",
                category=DataCategory.OHLCV.value,
                payload=[c.to_list() for c in candles][:2000],
                params={"start": start_ms, "end": end_ms, "interval": self.interval},
                latency_ms=result.latency_ms,
            )
        return written

    async def _reference_price(self) -> float | None:
        """用于脏数据防护的参考价（本地最近一次可信价格）。"""
        factory = get_session_factory()
        async with factory() as session:
            stmt = select(MarketPrice).order_by(MarketPrice.observation_time.desc()).limit(1)
            row = (await session.execute(stmt)).scalars().first()
            return row.price if row else None

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        mode = kwargs.get("mode", "tick")
        if mode == "backfill":
            return await self.backfill(
                start_date=kwargs.get("start_date"), end_date=kwargs.get("end_date"), reset=bool(kwargs.get("reset"))
            )
        if mode == "incremental":
            return await self.sync_incremental()
        return await self.collect_tick()


class GapRepairCollector(BaseCollector):
    """自动补洞：扫描本地缺口，尝试主源 -> 备用源逐个补齐。"""

    task_name = "gap_repair"
    category = "data_quality"

    def __init__(self, interval: str = "1d", router: ResilientRouter | None = None) -> None:
        # 与 MarketCollector 同样的防御校正，避免任何旧写法把字符串塞给 router
        if not isinstance(interval, str):
            interval, router = "1d", interval
        if isinstance(router, str):
            interval, router = router, None
        self.router = router or get_router()
        self.interval = interval
        self.step = INTERVAL_SECONDS.get(interval, 86400)

    async def find_gaps(self, symbol: str = "BTC") -> list[int]:
        """基于本地已有数据的时间连续性找缺失桶。"""
        factory = get_session_factory()
        async with factory() as session:
            rows_result = await session.execute(
                select(Candle.ts).where(Candle.symbol == symbol, Candle.interval == self.interval).order_by(Candle.ts)
            )
            existing = {int(ts) // self.step * self.step for ts in rows_result.scalars().all()}
        if not existing:
            return []
        start, end = min(existing), max(existing)
        return [ts for ts in range(start, end + self.step, self.step) if ts not in existing]

    async def repair(self, symbol: str = "BTC", max_gaps: int = 40) -> dict[str, Any]:
        gaps = await self.find_gaps(symbol)
        if not gaps:
            await self.save_checkpoint(cursor_ts=int(time.time()), status="done")
            return {"ok": True, "repaired": 0, "remaining": 0}

        targets = gaps[:max_gaps]
        repaired = 0
        failed: list[int] = []
        for gap_ts in targets:
            start_ms = gap_ts * 1000
            end_ms = (gap_ts + self.step) * 1000
            try:
                result = await self.router.fetch(
                    DataCategory.OHLCV,
                    symbol=symbol,
                    interval=self.interval,
                    limit=5,
                    start_time=start_ms,
                    end_time=end_ms,
                )
                rows = [
                    {
                        "symbol": symbol,
                        "interval": self.interval,
                        "ts": c.ts,
                        "open": c.open,
                        "high": c.high,
                        "low": c.low,
                        "close": c.close,
                        "volume": c.volume,
                        "quote_volume": c.quote_volume,
                        "trades": c.trades,
                        "source_id": result.provider,
                        "quality_status": result.quality.value,
                        "observation_time": datetime.fromtimestamp(c.ts, tz=timezone.utc),
                        "fetch_time": datetime.now(timezone.utc),
                    }
                    for c in result.data
                ]
                if rows:
                    factory = get_session_factory()
                    async with factory() as session:
                        repaired += await upsert_candles(session, rows)
                else:
                    failed.append(gap_ts)
            except AllProvidersFailedError:
                failed.append(gap_ts)

        await self.save_checkpoint(
            cursor_ts=int(time.time()), status="done" if not failed else "failed",
            rows_delta=repaired, extra={"failed_gaps": failed[:20]},
        )
        return {"ok": True, "repaired": repaired, "remaining": len(gaps) - len(targets), "failed": len(failed)}

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        return await self.repair(symbol=kwargs.get("symbol", "BTC"))


class DataQualityScanner(BaseCollector):
    """数据质量扫描：计算完整性、写入 data_quality 表。"""

    task_name = "data_quality_scan"
    category = "data_quality"

    async def scan(self, symbol: str = "BTC", interval: str = "1d") -> dict[str, Any]:
        factory = get_session_factory()
        async with factory() as session:
            rows_result = await session.execute(
                select(Candle.ts).where(Candle.symbol == symbol, Candle.interval == interval)
            )
            timestamps = sorted(int(t) for t in rows_result.scalars().all())
        if not timestamps:
            return {"ok": True, "completeness": 0.0, "days": 0, "missing": 0}

        step = INTERVAL_SECONDS.get(interval, 86400)
        existing = {t // step * step for t in timestamps}
        expected = len(range(min(existing), max(existing) + step, step))
        actual = len(existing)
        completeness = completeness_ratio(expected, actual)
        today_bucket = int(time.time()) // step * step

        async with factory() as session:
            session.add(
                DataQuality(
                    category=f"candles_{interval}",
                    date=today_bucket,
                    expected=expected,
                    actual=actual,
                    missing_count=max(0, expected - actual),
                    completeness=completeness,
                    status="ok" if completeness >= 0.99 else ("warn" if completeness >= 0.95 else "error"),
                    notes=f"本地 {interval} 序列覆盖 {min(existing)} ~ {max(existing)}",
                )
            )
            await session.commit()
        return {
            "ok": True,
            "completeness": round(completeness, 4),
            "days": actual,
            "missing": max(0, expected - actual),
            "start_ts": min(existing),
            "end_ts": max(existing),
        }

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        return await self.scan(kwargs.get("symbol", "BTC"), kwargs.get("interval", "1d"))
