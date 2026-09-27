# -*- coding: utf-8 -*-
"""欧美所数据源：Coinbase、Kraken、Bitstamp、Bitfinex、Gemini。

多为 USD 计价，可作为 USDT 计价源的交叉验证对照：
当 CN 区同时抵扣、只有这些源可用时，系统仍可运行并标明「已降级为 USD 计价源」。
"""

from __future__ import annotations

from typing import Any

from ..types import Candle, DataCategory
from ._rest import RestMarketProvider


class CoinbaseProvider(RestMarketProvider):
    name = "coinbase"
    display_name = "Coinbase"
    homepage = "https://www.coinbase.com"
    base_url = "https://api.coinbase.com"
    categories = {DataCategory.MARKET_PRICE, DataCategory.MARKET_TICKER_24H, DataCategory.OHLCV}
    capabilities = {"price", "ticker_24h", "ohlcv"}
    symbol = "BTC-USD"
    quote = "USD"
    default_priority = 40
    region_hint = "cn-hostile"
    rate_limit_qps = 2.0
    interval_map = {"1m": "60", "5m": "300", "15m": "900", "1h": "3600", "1d": "86400"}
    ohlcv_max_limit = 300
    notes = "USD 计价；适合作为计价偏差的交叉验证对照。"

    async def _fetch_price_raw(self) -> tuple[float, dict[str, Any]]:
        data = await self.transport.get_json(f"/v2/prices/{self.symbol}/spot", label="price")
        amount = ((data or {}).get("data") or {}).get("amount")
        if amount is None:
            raise self._fail(f"返回格式异常: {str(data)[:120]}")
        return self._num(amount, "amount"), dict(data["data"])

    async def _fetch_ticker24h_raw(self) -> dict[str, Any]:
        data = await self.transport.get_json(f"/products/{self.symbol}/stats", label="ticker24h")
        return {
            "symbol": self.symbol,
            "last": self._num(data.get("last", 0), "last"),
            "open": self._num(data.get("open", 0), "open"),
            "high": self._num(data.get("high", 0), "high"),
            "low": self._num(data.get("low", 0), "low"),
            "volume_base": self._num(data.get("volume", 0), "volume"),
        }

    async def _fetch_ohlcv_raw(self, interval: str, limit: int, start_ms: int | None, end_ms: int | None) -> list[Candle]:
        params: dict[str, Any] = {"granularity": self.map_interval(interval)}
        if start_ms and end_ms:
            params["start"] = start_ms // 1000
            params["end"] = end_ms // 1000
        data = await self.transport.get_json(f"/products/{self.symbol}/candles", params=params, label="ohlcv")
        if not isinstance(data, list) or not data:
            raise self._fail("K 线返回为空", "empty_data")
        out: list[Candle] = []
        for row in data[: self._limit(limit)]:
            # [time, low, high, open, close, volume]
            out.append(
                Candle(
                    ts=int(row[0]),
                    open=self._num(row[3], "open"),
                    high=self._num(row[2], "high"),
                    low=self._num(row[1], "low"),
                    close=self._num(row[4], "close"),
                    volume=self._num(row[5], "volume"),
                    interval=interval,
                )
            )
        return sorted(out, key=lambda c: c.ts)


class KrakenProvider(RestMarketProvider):
    name = "kraken"
    display_name = "Kraken"
    homepage = "https://www.kraken.com"
    base_url = "https://api.kraken.com"
    categories = {DataCategory.MARKET_PRICE, DataCategory.MARKET_TICKER_24H, DataCategory.OHLCV}
    capabilities = {"price", "ticker_24h", "ohlcv"}
    symbol = "XBTUSD"
    quote = "USD"
    default_priority = 50
    region_hint = "cn-hostile"
    rate_limit_qps = 1.5
    interval_map = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "4h": "240", "1d": "1440", "1w": "10080"}
    ohlcv_max_limit = 720
    notes = "历史悠久，数据稳定性好，适合作为交叉验证源。"

    async def _fetch_price_raw(self) -> tuple[float, dict[str, Any]]:
        data = await self.transport.get_json("/0/public/Ticker", params={"pair": self.symbol}, label="price")
        result = (data or {}).get("result") or {}
        if not result:
            raise self._fail(f"返回格式异常: {str(data)[:120]}")
        row = next(iter(result.values()))
        return self._num(row["c"][0], "close"), dict(row)

    async def _fetch_ticker24h_raw(self) -> dict[str, Any]:
        data = await self.transport.get_json("/0/public/Ticker", params={"pair": self.symbol}, label="ticker24h")
        result = (data or {}).get("result") or {}
        row = next(iter(result.values()))
        last = self._num(row["c"][0], "close")
        open_p = self._num(row["o"], "open")
        change_pct = (last - open_p) / open_p * 100 if open_p else 0.0
        return {
            "symbol": self.symbol,
            "last": last,
            "open": open_p,
            "high": self._num(row.get("h")[1] if isinstance(row.get("h"), list) else row.get("h", 0), "high"),
            "low": self._num(row.get("l")[1] if isinstance(row.get("l"), list) else row.get("l", 0), "low"),
            "change_pct": change_pct,
            "volume_base": self._num(row.get("v")[1] if isinstance(row.get("v"), list) else row.get("v", 0), "volume"),
        }

    async def _fetch_ohlcv_raw(self, interval: str, limit: int, start_ms: int | None, end_ms: int | None) -> list[Candle]:
        params: dict[str, Any] = {"pair": self.symbol, "interval": self.map_interval(interval)}
        if start_ms:
            params["since"] = int(start_ms / 1000)
        data = await self.transport.get_json("/0/public/OHLC", params=params, label="ohlcv")
        result = (data or {}).get("result") or {}
        rows = None
        for key, value in result.items():
            if isinstance(value, list):
                rows = value
                break
        if not rows:
            raise self._fail("K 线返回为空", "empty_data")
        out: list[Candle] = []
        for row in rows[: self._limit(limit)]:
            # [time, open, high, low, close, vwap, volume, count]
            out.append(
                Candle(
                    ts=int(row[0]),
                    open=self._num(row[1], "open"),
                    high=self._num(row[2], "high"),
                    low=self._num(row[3], "low"),
                    close=self._num(row[4], "close"),
                    volume=self._num(row[6], "volume"),
                    interval=interval,
                )
            )
        return sorted(out, key=lambda c: c.ts)


class BitstampProvider(RestMarketProvider):
    name = "bitstamp"
    display_name = "Bitstamp"
    homepage = "https://www.bitstamp.net"
    base_url = "https://www.bitstamp.net"
    categories = {DataCategory.MARKET_PRICE, DataCategory.MARKET_TICKER_24H, DataCategory.OHLCV}
    capabilities = {"price", "ticker_24h", "ohlcv"}
    symbol = "btcusd"
    quote = "USD"
    default_priority = 60
    region_hint = "cn-hostile"
    rate_limit_qps = 1.5
    interval_map = {"1m": "60", "5m": "300", "15m": "900", "30m": "1800", "1h": "3600", "4h": "14400", "1d": "86400"}
    ohlcv_max_limit = 1000
    notes = "欧洲老牌交易所，适合作为备用交叉验证源。"

    async def _fetch_price_raw(self) -> tuple[float, dict[str, Any]]:
        data = await self.transport.get_json(f"/api/v2/ticker/{self.symbol}/", label="price")
        if "last" not in (data or {}):
            raise self._fail(f"返回格式异常: {str(data)[:120]}")
        return self._num(data["last"], "last"), dict(data)

    async def _fetch_ticker24h_raw(self) -> dict[str, Any]:
        data = await self.transport.get_json(f"/api/v2/ticker/{self.symbol}/", label="ticker24h")
        return {
            "symbol": self.symbol,
            "last": self._num(data.get("last", 0), "last"),
            "open": self._num(data.get("open", 0), "open"),
            "high": self._num(data.get("high", 0), "high"),
            "low": self._num(data.get("low", 0), "low"),
            "change_pct": self._num(data.get("percent", 0), "percent"),
            "volume_base": self._num(data.get("volume", 0), "volume"),
        }

    async def _fetch_ohlcv_raw(self, interval: str, limit: int, start_ms: int | None, end_ms: int | None) -> list[Candle]:
        params: dict[str, Any] = {"step": self.map_interval(interval), "limit": self._limit(limit)}
        # Bitstamp v2 支持 start / end（秒），必须传入才能得到请求窗口，而非最近 N 根。
        if start_ms:
            params["start"] = int(start_ms) // 1000
        if end_ms:
            params["end"] = int(end_ms) // 1000
        data = await self.transport.get_json(f"/api/v2/ohlc/{self.symbol}/", params=params, label="ohlcv")
        rows = ((data or {}).get("data") or {}).get("ohlc") or []
        if not rows:
            raise self._fail("K 线返回为空", "empty_data")
        out: list[Candle] = []
        for row in rows:
            out.append(
                Candle(
                    ts=int(row["timestamp"]),
                    open=self._num(row["open"], "open"),
                    high=self._num(row["high"], "high"),
                    low=self._num(row["low"], "low"),
                    close=self._num(row["close"], "close"),
                    volume=self._num(row["volume"], "volume"),
                    interval=interval,
                )
            )
        return sorted(out, key=lambda c: c.ts)


class BitfinexProvider(RestMarketProvider):
    name = "bitfinex"
    display_name = "Bitfinex"
    homepage = "https://www.bitfinex.com"
    base_url = "https://api.bitfinex.com"
    categories = {DataCategory.MARKET_PRICE, DataCategory.MARKET_TICKER_24H, DataCategory.OHLCV}
    capabilities = {"price", "ticker_24h", "ohlcv"}
    symbol = "tBTCUSD"
    quote = "USD"
    default_priority = 70
    region_hint = "cn-hostile"
    rate_limit_qps = 1.5
    interval_map = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h", "4h": "4h", "1d": "1D", "1w": "7D"}
    ohlcv_max_limit = 1000

    async def _fetch_price_raw(self) -> tuple[float, dict[str, Any]]:
        data = await self.transport.get_json(f"/v2/ticker/{self.symbol}", label="price")
        if not isinstance(data, list) or len(data) < 7:
            raise self._fail(f"返回格式异常: {str(data)[:120]}")
        return self._num(data[6], "last"), {"raw": list(data)}

    async def _fetch_ticker24h_raw(self) -> dict[str, Any]:
        data = await self.transport.get_json(f"/v2/ticker/{self.symbol}", label="ticker24h")
        last, volume, high, low = data[6], data[7], data[8], data[9]
        return {
            "symbol": self.symbol,
            "last": self._num(last, "last"),
            "high": self._num(high, "high"),
            "low": self._num(low, "low"),
            "volume_base": self._num(volume, "volume"),
        }

    async def _fetch_ohlcv_raw(self, interval: str, limit: int, start_ms: int | None, end_ms: int | None) -> list[Candle]:
        params = {"limit": self._limit(limit), "sort": 1}
        # Bitfinex v2 /hist 同为毫秒时间戳
        if start_ms:
            params["start"] = int(start_ms)
        if end_ms:
            params["end"] = int(end_ms)
        data = await self.transport.get_json(
            f"/v2/candles/trade:{self.map_interval(interval)}:{self.symbol}", params=params, label="ohlcv"
        )
        if not isinstance(data, list) or not data:
            raise self._fail("K 线返回为空", "empty_data")
        out: list[Candle] = []
        for row in data:
            # [MTS, OPEN, CLOSE, HIGH, LOW, VOLUME]
            out.append(
                Candle(
                    ts=int(row[0]) // 1000,
                    open=self._num(row[1], "open"),
                    high=self._num(row[3], "high"),
                    low=self._num(row[4], "low"),
                    close=self._num(row[2], "close"),
                    volume=self._num(row[5], "volume"),
                    interval=interval,
                )
            )
        return sorted(out, key=lambda c: c.ts)


class GeminiProvider(RestMarketProvider):
    name = "gemini"
    display_name = "Gemini"
    homepage = "https://www.gemini.com"
    base_url = "https://api.gemini.com"
    categories = {DataCategory.MARKET_PRICE}
    capabilities = {"price"}
    symbol = "btcusd"
    quote = "USD"
    default_priority = 80
    region_hint = "cn-hostile"
    rate_limit_qps = 1.2
    notes = "仅提供极简价格接口，作为最后一层备用。"

    async def _fetch_price_raw(self) -> tuple[float, dict[str, Any]]:
        data = await self.transport.get_json(f"/v1/pubticker/{self.symbol}", label="price")
        if "last" not in (data or {}):
            raise self._fail(f"返回格式异常: {str(data)[:120]}")
        return self._num(data["last"], "last"), dict(data)
