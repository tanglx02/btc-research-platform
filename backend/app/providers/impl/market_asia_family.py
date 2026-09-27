# -*- coding: utf-8 -*-
"""亚太/离岸交易所数据源：OKX、火币 HTX、Bybit、Gate.io。

这些站点在国内网络环境下可达性不稳定（同一站点可能上午通、下午不通），
因此全部注册为备用源：主源失败时自动接管，恢复后自动归还优先级。
"""

from __future__ import annotations

from typing import Any

from ..types import Candle, DataCategory
from ._rest import RestMarketProvider


class _GateFamily(RestMarketProvider):
    """Gate.io v4 现货 + 合约（实测国内可达，优先级较高）。"""

    name = "gate"
    display_name = "Gate.io"
    homepage = "https://www.gate.io"
    base_url = "https://api.gateio.ws"
    categories = {DataCategory.MARKET_PRICE, DataCategory.MARKET_TICKER_24H, DataCategory.OHLCV}
    capabilities = {"price", "ticker_24h", "ohlcv"}
    symbol = "BTC_USDT"
    quote = "USDT"
    default_priority = 20
    region_hint = "cn-friendly"
    rate_limit_qps = 4.0
    interval_map = {
        "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h",
        "4h": "4h", "8h": "8h", "1d": "1d", "1w": "7d",
    }
    ohlcv_max_limit = 1000
    notes = "实测国内直连可达；同时提供合约 derivatives 数据。"

    async def _fetch_price_raw(self) -> tuple[float, dict[str, Any]]:
        data = await self.transport.get_json(
            "/api/v4/spot/tickers", params={"currency_pair": self.symbol}, label="price"
        )
        if not isinstance(data, list) or not data or "last" not in data[0]:
            raise self._fail(f"返回格式异常: {str(data)[:120]}")
        row = data[0]
        return self._num(row["last"], "last"), dict(row)

    async def _fetch_ticker24h_raw(self) -> dict[str, Any]:
        data = await self.transport.get_json(
            "/api/v4/spot/tickers", params={"currency_pair": self.symbol}, label="ticker24h"
        )
        if not isinstance(data, list) or not data:
            raise self._fail("返回空列表")
        row = data[0]
        last = self._num(row["last"], "last")
        change_pct = self._num(row.get("change_percentage", 0), "change_percentage")
        open_price = last / (1 + change_pct / 100) if change_pct != -100 else last
        return {
            "symbol": self.symbol,
            "last": last,
            "open": open_price,
            "high": self._num(row.get("high_24h", 0), "high_24h"),
            "low": self._num(row.get("low_24h", 0), "low_24h"),
            "change_pct": change_pct,
            "volume_base": self._num(row.get("base_volume", 0), "base_volume"),
            "volume_quote": self._num(row.get("quote_volume", 0), "quote_volume"),
        }

    async def _fetch_ohlcv_raw(self, interval: str, limit: int, start_ms: int | None, end_ms: int | None) -> list[Candle]:
        params: dict[str, Any] = {
            "currency_pair": self.symbol,
            "interval": self.map_interval(interval),
            "limit": self._limit(limit),
        }
        # Gate v4 支持 from/to（秒级 Unix 时间戳）；必须显式传入，否则永远只给最近 N 根。
        if start_ms:
            params["from"] = int(start_ms) // 1000
        if end_ms:
            params["to"] = int(end_ms) // 1000
        # 字段顺序: [ts, quote_volume, close, high, low, open, base_volume, finished]
        data = await self.transport.get_json("/api/v4/spot/candlesticks", params=params, label="ohlcv")
        if not isinstance(data, list) or not data:
            raise self._fail("K 线返回为空", "empty_data")
        out: list[Candle] = []
        for row in data:
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                continue
            ts = self._ts_ms(row[0])
            ts = ts // 1000 if ts > 10_000_000_000 else ts
            out.append(
                Candle(
                    ts=int(ts),
                    open=self._num(row[5], "open"),
                    high=self._num(row[3], "high"),
                    low=self._num(row[4], "low"),
                    close=self._num(row[2], "close"),
                    volume=self._num(row[6], "volume") if len(row) > 6 else 0.0,
                    quote_volume=self._num(row[1], "quote_volume"),
                    interval=interval,
                )
            )
        if not out:
            raise self._fail("K 线解析为空")
        return out


class _HuobiFamily(RestMarketProvider):
    """火币/HTX 现货（可达性波动大，作为备用）。"""

    name = "huobi"
    display_name = "HTX（火币）"
    homepage = "https://www.htx.com"
    base_url = "https://api.huobi.pro"
    categories = {DataCategory.MARKET_PRICE, DataCategory.MARKET_TICKER_24H, DataCategory.OHLCV}
    capabilities = {"price", "ticker_24h", "ohlcv"}
    symbol = "btcusdt"
    quote = "USDT"
    default_priority = 30
    region_hint = "cn-friendly"
    rate_limit_qps = 3.0
    interval_map = {
        "1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min",
        "1h": "60min", "4h": "4hour", "1d": "1day", "1w": "1week", "1M": "1mon",
    }
    ohlcv_max_limit = 2000
    # HTX 的 /market/history/kline 不提供时间区间参数（只能取最近 size 根），
    # 因此明确声明不支持区间拉取，避免历史回填拿到错误窗口的数据。
    ohlcv_supports_range = False
    notes = "实测可达性不稳定（时好时坏），作为备用源；不支持历史区间拉取，仅用于最新 K 线。"

    async def _fetch_price_raw(self) -> tuple[float, dict[str, Any]]:
        data = await self.transport.get_json(
            "/market/detail/merged", params={"symbol": self.symbol}, label="price"
        )
        if not isinstance(data, dict) or data.get("status") != "ok":
            raise self._fail(f"响应异常: {str(data)[:120]}")
        tick = data.get("tick", {})
        closes = tick.get("close")
        price = closes if not isinstance(closes, list) else closes[-1]
        return self._num(price, "close"), dict(tick)

    async def _fetch_ticker24h_raw(self) -> dict[str, Any]:
        data = await self.transport.get_json(
            "/market/detail/merged", params={"symbol": self.symbol}, label="ticker24h"
        )
        tick = data.get("tick", {}) or {}
        return {
            "symbol": self.symbol,
            "last": self._num(tick.get("close", 0), "close"),
            "open": self._num(tick.get("open", 0), "open"),
            "high": self._num(tick.get("high", 0), "high"),
            "low": self._num(tick.get("low", 0), "low"),
            "volume_base": self._num(tick.get("amount", 0), "amount"),
            "volume_quote": self._num(tick.get("vol", 0), "vol"),
            "trades": int(tick.get("count", 0) or 0),
        }

    async def _fetch_ohlcv_raw(self, interval: str, limit: int, start_ms: int | None, end_ms: int | None) -> list[Candle]:
        params: dict[str, Any] = {"symbol": self.symbol, "period": self.map_interval(interval), "size": self._limit(limit)}
        data = await self.transport.get_json("/market/history/kline", params=params, label="ohlcv")
        rows = (data or {}).get("data") if isinstance(data, dict) else None
        if not rows:
            raise self._fail("K 线返回为空", "empty_data")
        out: list[Candle] = []
        for row in rows:
            out.append(
                Candle(
                    ts=int(row["id"]),
                    open=self._num(row["open"], "open"),
                    high=self._num(row["high"], "high"),
                    low=self._num(row["low"], "low"),
                    close=self._num(row["close"], "close"),
                    volume=self._num(row["amount"], "amount"),
                    quote_volume=self._num(row["vol"], "vol"),
                    trades=int(row.get("count", 0) or 0),
                    interval=interval,
                )
            )
        return sorted(out, key=lambda c: c.ts)


class _OkxFamily(RestMarketProvider):
    """OKX v5（部分地区不可达，自动降级）。"""

    name = "okx"
    display_name = "OKX"
    homepage = "https://www.okx.com"
    base_url = "https://www.okx.com"
    categories = {DataCategory.MARKET_PRICE, DataCategory.MARKET_TICKER_24H, DataCategory.OHLCV}
    capabilities = {"price", "ticker_24h", "ohlcv"}
    symbol = "BTC-USDT"
    quote = "USDT"
    default_priority = 25
    region_hint = "global"
    rate_limit_qps = 3.0
    interval_map = {
        "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
        "1h": "1H", "4h": "4H", "12h": "12H", "1d": "1D", "1w": "1W",
    }
    ohlcv_max_limit = 300
    # OKX 的 after/before 是「反向分页」语义，与本框架「拉取 [start,end] 窗口」不同；
    # 在无法稳定验证的网络环境下保守声明不支持区间拉取，宁可不参与回填也不能写错数据。
    ohlcv_supports_range = False
    notes = "接口稳定但部分地区不可达；不可达时自动跳过。暂不参与历史区间回填。"

    async def _fetch_price_raw(self) -> tuple[float, dict[str, Any]]:
        data = await self.transport.get_json(
            "/api/v5/market/ticker", params={"instId": self.symbol}, label="price"
        )
        rows = (data or {}).get("data") or []
        if not rows or "last" not in rows[0]:
            raise self._fail(f"响应异常: {str(data)[:120]}")
        return self._num(rows[0]["last"], "last"), dict(rows[0])

    async def _fetch_ticker24h_raw(self) -> dict[str, Any]:
        data = await self.transport.get_json(
            "/api/v5/market/ticker", params={"instId": self.symbol}, label="ticker24h"
        )
        rows = (data or {}).get("data") or []
        if not rows:
            raise self._fail("返回空数据")
        row = rows[0]
        return {
            "symbol": self.symbol,
            "last": self._num(row.get("last", 0), "last"),
            "open": self._num(row.get("open24h", 0), "open24h"),
            "high": self._num(row.get("high24h", 0), "high24h"),
            "low": self._num(row.get("low24h", 0), "low24h"),
            "volume_base": self._num(row.get("vol24h", 0), "vol24h"),
            "volume_quote": self._num(row.get("volCcy24h", 0), "volCcy24h"),
            "observation_time": int(row.get("ts", 0)) // 1000 or None,
        }

    async def _fetch_ohlcv_raw(self, interval: str, limit: int, start_ms: int | None, end_ms: int | None) -> list[Candle]:
        params: dict[str, Any] = {"instId": self.symbol, "bar": self.map_interval(interval), "limit": self._limit(limit)}
        if start_ms:
            params["after"] = int(start_ms)
        data = await self.transport.get_json("/api/v5/market/candles", params=params, label="ohlcv")
        rows = (data or {}).get("data") or []
        if not rows:
            raise self._fail("K 线返回为空", "empty_data")
        out: list[Candle] = []
        for row in rows:
            # [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
            out.append(
                Candle(
                    ts=int(row[0]) // 1000,
                    open=self._num(row[1], "open"),
                    high=self._num(row[2], "high"),
                    low=self._num(row[3], "low"),
                    close=self._num(row[4], "close"),
                    volume=self._num(row[5], "volume"),
                    quote_volume=self._num(row[6], "quote_volume") if len(row) > 6 else 0.0,
                    interval=interval,
                )
            )
        return sorted(out, key=lambda c: c.ts)


class _BybitFamily(RestMarketProvider):
    """Bybit v5 现货。"""

    name = "bybit"
    display_name = "Bybit"
    homepage = "https://www.bybit.com"
    base_url = "https://api.bybit.com"
    categories = {DataCategory.MARKET_PRICE, DataCategory.MARKET_TICKER_24H, DataCategory.OHLCV}
    capabilities = {"price", "ticker_24h", "ohlcv"}
    symbol = "BTCUSDT"
    quote = "USDT"
    default_priority = 35
    region_hint = "cn-hostile"
    rate_limit_qps = 4.0
    interval_map = {
        "1m": "1", "5m": "5", "15m": "15", "30m": "30",
        "1h": "60", "4h": "240", "1d": "D", "1w": "W",
    }
    ohlcv_max_limit = 200
    notes = "部分地区不可达；作为备用链路。"

    async def _fetch_price_raw(self) -> tuple[float, dict[str, Any]]:
        data = await self.transport.get_json(
            "/v5/market/tickers", params={"category": "spot", "symbol": self.symbol}, label="price"
        )
        rows = ((data or {}).get("result") or {}).get("list") or []
        if not rows or "lastPrice" not in rows[0]:
            raise self._fail(f"响应异常: {str(data)[:120]}")
        return self._num(rows[0]["lastPrice"], "lastPrice"), dict(rows[0])

    async def _fetch_ticker24h_raw(self) -> dict[str, Any]:
        data = await self.transport.get_json(
            "/v5/market/tickers", params={"category": "spot", "symbol": self.symbol}, label="ticker24h"
        )
        rows = ((data or {}).get("result") or {}).get("list") or []
        if not rows:
            raise self._fail("返回空数据")
        row = rows[0]
        return {
            "symbol": self.symbol,
            "last": self._num(row.get("lastPrice", 0), "lastPrice"),
            "open": self._num(row.get("prevPrice24h", 0), "prevPrice24h"),
            "high": self._num(row.get("highPrice24h", 0), "highPrice24h"),
            "low": self._num(row.get("lowPrice24h", 0), "lowPrice24h"),
            "change_pct": self._num(row.get("price24hPcnt", 0), "price24hPcnt") * 100,
            "volume_base": self._num(row.get("volume24h", 0), "volume24h"),
            "volume_quote": self._num(row.get("turnover24h", 0), "turnover24h"),
        }

    async def _fetch_ohlcv_raw(self, interval: str, limit: int, start_ms: int | None, end_ms: int | None) -> list[Candle]:
        params: dict[str, Any] = {
            "category": "spot",
            "symbol": self.symbol,
            "interval": self.map_interval(interval),
            "limit": self._limit(limit),
        }
        if start_ms:
            params["start"] = int(start_ms)
        if end_ms:
            params["end"] = int(end_ms)
        data = await self.transport.get_json("/v5/market/kline", params=params, label="ohlcv")
        rows = ((data or {}).get("result") or {}).get("list") or []
        if not rows:
            raise self._fail("K 线返回为空", "empty_data")
        out: list[Candle] = []
        for row in rows:
            ts = self._ts_ms(row[0])
            ts = ts // 1000 if ts > 10_000_000_000 else ts
            out.append(
                Candle(
                    ts=int(ts),
                    open=self._num(row[1], "open"),
                    high=self._num(row[2], "high"),
                    low=self._num(row[3], "low"),
                    close=self._num(row[4], "close"),
                    volume=self._num(row[5], "volume"),
                    quote_volume=self._num(row[6], "quote_volume") if len(row) > 6 else 0.0,
                    interval=interval,
                )
            )
        return sorted(out, key=lambda c: c.ts)
