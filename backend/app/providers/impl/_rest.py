# -*- coding: utf-8 -*-
"""REST 交易所 Provider 的公共工具（非数据源，autodiscover 会跳过 `_` 前缀模块）。

新增一个交易所只需：写分类 faculty + 三个 parse 方法，20~60 行代码即可接入，
不触碰任何业务代码。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from ...core.errors import ProviderError
from ..base import DataProvider
from ..types import Candle, DataCategory

# 统一 interval -> 交易所自有周期 的默认映射
STANDARD_INTERVALS = ("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w")


class RestMarketProvider(DataProvider):
    """REST 现货交易所通用实现骨架。"""

    symbol: str = "BTCUSDT"          # 该所 BTC 计价符号（美元/泰达）
    quote: str = "USDT"
    supports_price_via_ticker: bool = False
    ohlcv_max_limit: int = 1000
    interval_map: dict[str, str] = {}
    ohlcv_ascending: bool = True      # 返回顺序是否升序
    ohlcv_supports_range: bool = True  # 是否支持按时间区间拉取（历史回填依赖）

    # ------------------------------------------------------------------ helpers
    def map_interval(self, interval: str) -> str:
        mapped = self.interval_map.get(interval)
        if not mapped:
            raise ProviderError(
                f"{self.name} 不支持周期 {interval}",
                provider=self.name,
                failure_type="unsupported_input",
            )
        return mapped

    def _fail(self, msg: str, failure_type: str = "data_format_error") -> ProviderError:
        return ProviderError(msg, provider=self.name, failure_type=failure_type)

    @staticmethod
    def _num(v: Any, field_name: str) -> float:
        try:
            f = float(v)
        except (TypeError, ValueError) as exc:
            raise ProviderError(
                f"字段 {field_name} 不是数字: {v!r}", failure_type="data_format_error"
            ) from exc
        if f != f:  # NaN
            raise ProviderError(f"字段 {field_name} 为 NaN", failure_type="data_format_error")
        return f

    def _limit(self, limit: int) -> int:
        return max(1, min(int(limit), self.ohlcv_max_limit))

    @staticmethod
    def _ts_ms(v: Any) -> int:
        return int(float(v))

    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def sorted_candles(candles: Iterable[Candle]) -> list[Candle]:
        """按时间升序去重。

        注意：Candle 是可变 dataclass（不可哈希），不能用 `set()` 去重，
        必须按 ts 归并（同一根 K 线以后出现的为准）。
        """
        dedup: dict[int, Candle] = {}
        for candle in candles:
            dedup[int(candle.ts)] = candle
        return [dedup[ts] for ts in sorted(dedup)]

    # ------------------------------------------------------------------ 待实现
    async def _fetch_price_raw(self) -> tuple[float, dict[str, Any]]:
        raise NotImplementedError

    async def _fetch_ticker24h_raw(self) -> dict[str, Any]:
        raise NotImplementedError

    async def _fetch_ohlcv_raw(self, interval: str, limit: int, start_ms: int | None, end_ms: int | None) -> list[Candle]:
        raise NotImplementedError

    # ------------------------------------------------------------------ 通用出口
    async def fetch_price(self, symbol: str = "BTC", **_: Any):
        started = datetime.now(timezone.utc)
        price, extra = await self._fetch_price_raw()
        if price <= 0:
            raise self._fail("价格非法", "data_quality_error")
        return self.ok(
            price,
            self.category_price(),
            latency_ms=(datetime.now(timezone.utc) - started).total_seconds() * 1000,
            endpoint="price",
            raw=extra,
            quote=self.quote,
        )

    async def fetch_ticker_24h(self, symbol: str = "BTC", **_: Any):
        started = datetime.now(timezone.utc)
        data = await self._fetch_ticker24h_raw()
        for key in ("last", "open", "high", "low", "volume"):
            if key in data:
                data[key] = self._num(data[key], key)
        return self.ok(
            data,
            self.category_ticker(),
            latency_ms=(datetime.now(timezone.utc) - started).total_seconds() * 1000,
            endpoint="ticker_24h",
            raw=dict(data),
        )

    async def fetch_ohlcv(
        self,
        symbol: str = "BTC",
        interval: str = "1d",
        limit: int = 500,
        start_time: int | None = None,
        end_time: int | None = None,
        **_: Any,
    ):
        started = datetime.now(timezone.utc)

        # 历史回填依赖「按时间区间拉取」。若某个交易所不支持（很多站点的 /kline 只返回最近 N 根），
        # 必须明确报错跳过，绝不能返回错误的窗口 —— 否则回填会看似成功、实际覆盖错误区间。
        if (start_time is not None or end_time is not None) and not self.ohlcv_supports_range:
            raise ProviderError(
                f"{self.name} 不支持按时间区间拉取历史 K 线（仅能取最新 {limit} 根），不参与历史回填",
                provider=self.name,
                failure_type="range_unsupported",
            )

        candles = await self._fetch_ohlcv_raw(interval, limit, start_time, end_time)
        if not candles:
            raise self._fail("K 线返回为空", "empty_data")
        candles = [c for c in candles if c.close > 0 and c.high >= c.low]
        if not candles:
            raise self._fail("K 线数据质量校验失败", "data_quality_error")

        candles = self._clip_window(candles, start_time, end_time)
        if not candles:
            raise self._fail("过滤后窗口内无数据", "empty_data")

        return self.ok(
            self.sorted_candles(candles),
            DataCategory.OHLCV,
            latency_ms=(datetime.now(timezone.utc) - started).total_seconds() * 1000,
            endpoint="ohlcv",
            params={"interval": interval, "limit": limit},
            interval=interval,
            quote=self.quote,
        )

    def category_price(self) -> DataCategory:
        return DataCategory.MARKET_PRICE

    def category_ticker(self) -> DataCategory:
        return DataCategory.MARKET_TICKER_24H

    # ------------------------------------------------------------------ 窗口防护
    @staticmethod
    def _clip_window(candles: list[Candle], start_ms: int | None, end_ms: int | None) -> list[Candle]:
        """把返回值裁剪到请求窗口内，并对「明显忽略时间参数」的数据源做防御。

        这是回填正确性的最后一道闸：如果数据源没有按我们请求的时间区间返回（例如只给最近 N 根），
        与其把错的区间写进本地历史库，不如直接判定为数据错误。
        """
        lo = int(start_ms) // 1000 if start_ms else None
        hi = int(end_ms) // 1000 if end_ms else None

        if lo is not None and candles and max(c.ts for c in candles) < lo:
            raise ProviderError(
                "返回数据全部早于请求窗口起点（数据源未遵守时间参数）",
                failure_type="data_quality_error",
            )
        if hi is not None and candles and min(c.ts for c in candles) > hi:
            raise ProviderError(
                "返回数据全部晚于请求窗口终点（数据源未遵守时间参数）",
                failure_type="data_quality_error",
            )

        out = candles
        if lo is not None:
            out = [c for c in out if c.ts >= lo]
        if hi is not None:
            out = [c for c in out if c.ts <= hi]
        return out
