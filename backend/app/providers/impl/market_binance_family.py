# -*- coding: utf-8 -*-
"""Binance 系列数据源。

三个独立 Provider（同名ancellus不耦合）：
- BinanceProvider        api.binance.com        （官方主域，部分地区不可达）
- BinanceVisionProvider  data-api.binance.vision（官方只读镜像，国内可达性最好 -> 默认主源）
- BinanceUSProvider      api.binance.us         （美国站，作为额外备源）

三者之一全部失效不影响其他 Provider；业务层完全无感知。
"""

from __future__ import annotations

import time
from typing import Any

from ..types import Candle, DataCategory
from ._rest import RestMarketProvider

BINANCE_INTERVALS = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "2h": "2h", "4h": "4h", "6h": "6h", "8h": "8h", "12h": "12h",
    "1d": "1d", "3d": "3d", "1w": "1w", "1M": "1M",
}


class _BinanceFamily(RestMarketProvider):
    """Binance REST v3 通用解析（各站点仅 base_url 不同）。"""

    categories = {DataCategory.MARKET_PRICE, DataCategory.MARKET_TICKER_24H, DataCategory.OHLCV}
    capabilities = {"price", "ticker_24h", "ohlcv"}
    interval_map = BINANCE_INTERVALS
    ohlcv_max_limit = 1000
    symbol: str = "BTCUSDT"
    quote: str = "USDT"

    async def _fetch_price_raw(self) -> tuple[float, dict[str, Any]]:
        data = await self.transport.get_json(
            "/api/v3/ticker/price", params={"symbol": self.symbol}, label="price"
        )
        if not isinstance(data, dict) or "price" not in data:
            raise self._fail(f"返回格式异常: {str(data)[:120]}")
        return self._num(data["price"], "price"), dict(data)

    async def _fetch_ticker24h_raw(self) -> dict[str, Any]:
        data = await self.transport.get_json(
            "/api/v3/ticker/24hr", params={"symbol": self.symbol}, label="ticker24h"
        )
        if not isinstance(data, dict) or "lastPrice" not in data:
            raise self._fail(f"返回格式异常: {str(data)[:120]}")
        return {
            "symbol": self.symbol,
            "last": self._num(data["lastPrice"], "lastPrice"),
            "open": self._num(data.get("openPrice", 0), "openPrice"),
            "high": self._num(data.get("highPrice", 0), "highPrice"),
            "low": self._num(data.get("lowPrice", 0), "lowPrice"),
            "change_pct": self._num(data.get("priceChangePercent", 0), "priceChangePercent"),
            "volume_base": self._num(data.get("volume", 0), "volume"),
            "volume_quote": self._num(data.get("quoteVolume", 0), "quoteVolume"),
            "trades": int(data.get("count", 0) or 0),
            "observation_time": int(data.get("closeTime", 0) or 0) or None,
        }

    async def _fetch_ohlcv_raw(self, interval: str, limit: int, start_ms: int | None, end_ms: int | None) -> list[Candle]:
        params: dict[str, Any] = {"symbol": self.symbol, "interval": self.map_interval(interval), "limit": self._limit(limit)}
        if start_ms:
            params["startTime"] = int(start_ms)
        if end_ms:
            params["endTime"] = int(end_ms)
        data = await self.transport.get_json("/api/v3/klines", params=params, label="ohlcv")
        if not isinstance(data, list) or not data:
            raise self._fail("K 线返回为空", "empty_data")
        out: list[Candle] = []
        for row in data:
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                continue
            out.append(
                Candle(
                    ts=int(row[0]) // 1000,
                    open=self._num(row[1], "open"),
                    high=self._num(row[2], "high"),
                    low=self._num(row[3], "low"),
                    close=self._num(row[4], "close"),
                    volume=self._num(row[5], "volume"),
                    quote_volume=self._num(row[7], "quote_volume") if len(row) > 7 else 0.0,
                    trades=int(row[8]) if len(row) > 8 else 0,
                    interval=interval,
                )
            )
        if not out:
            raise self._fail("K 线解析结果为空", "data_format_error")
        return out

    @staticmethod
    def now_ms() -> int:
        return int(time.time() * 1000)


class BinanceVisionProvider(_BinanceFamily):
    """data-api.binance.vision —— Binance 官方只读数据镜像。

    实测中国大陆可达性最好，因此作为默认主源。
    """

    name = "binance_vision"
    display_name = "Binance Vision（官方只读镜像）"
    homepage = "https://data-api.binance.vision"
    base_url = "https://data-api.binance.vision"
    default_priority = 10
    region_hint = "cn-friendly"
    rate_limit_qps = 4.0
    notes = "实测国内直连可达；提供 2017 年至今现货 K 线，是历史回填主力源。"


class BinanceProvider(_BinanceFamily):
    name = "binance"
    display_name = "Binance 官方主站"
    homepage = "https://api.binance.com"
    base_url = "https://api.binance.com"
    default_priority = 15
    region_hint = "cn-hostile"
    rate_limit_qps = 4.0
    notes = "部分地区直连不可达；可通过代理或自动切换至 binance_vision。"


class BinanceUSProvider(_BinanceFamily):
    name = "binance_us"
    display_name = "Binance.US"
    homepage = "https://api.binance.us"
    base_url = "https://api.binance.us"
    quote = "USD"
    default_priority = 45
    symbol = "BTCUSD"
    interval_map = {k: v for k, v in BINANCE_INTERVALS.items()}
    region_hint = "cn-hostile"
    notes = "美国站，计价为 USD，作为额外备用路。"
