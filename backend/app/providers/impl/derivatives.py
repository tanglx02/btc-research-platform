# -*- coding: utf-8 -*-
"""衍生品数据源：资金费率 / 持仓量 / 多空比。

未配置或不可达时，状态为 NOT_CONFIGURED / OFFLINE，
对应模块显示「数据暂时不可用」，而绝不伪造 Funding/OI 数字。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ...core.errors import ProviderError
from ..base import DataProvider
from ..types import DataCategory


def _lat(started: datetime) -> float:
    return (datetime.now(timezone.utc) - started).total_seconds() * 1000


class GateFuturesProvider(DataProvider):
    """Gate.io 永续合约：持仓量 + 资金费率（实测国内可达）。"""

    name = "gate_futures"
    display_name = "Gate.io 合约"
    homepage = "https://www.gate.io"
    base_url = "https://api.gateio.ws"
    categories = {DataCategory.DERIVATIVES_FUNDING, DataCategory.DERIVATIVES_OI}
    capabilities = {"funding", "open_interest"}
    default_priority = 10
    region_hint = "cn-friendly"
    rate_limit_qps = 3.0
    symbol = "BTC_USDT"
    notes = "实测国内可达；提供永续合约持仓量与当前资金费率。"

    async def fetch_open_interest(self, symbol: str = "BTC", **_: Any):
        started = datetime.now(timezone.utc)
        data = await self.transport.get_json(
            "/api/v4/futures/usdt/contract_stats", params={"contract": self.symbol}, label="open_interest"
        )
        if not isinstance(data, list) or not data:
            raise ProviderError("Gate 合约统计返回为空", provider=self.name, failure_type="empty_data")
        row = data[0]
        oi = float(row.get("open_interest") or 0)
        return self.ok(
            {
                "open_interest": oi,
                "open_interest_usd": float(row.get("open_interest_usd") or 0),
                "open_interest_base": float(row.get("open_interest_base") or 0),
                "mark_price": float(row.get("mark_price") or 0),
                "index_price": float(row.get("index_price") or 0),
                "volume_24h": float(row.get("volume_24h") or 0),
                "volume_24h_usd": float(row.get("volume_24h_usd") or 0),
                "unit": "BTC",
            },
            DataCategory.DERIVATIVES_OI,
            latency_ms=_lat(started),
            endpoint="/api/v4/futures/usdt/contract_stats",
            raw=row,
        )

    async def fetch_funding(self, symbol: str = "BTC", **_: Any):
        started = datetime.now(timezone.utc)
        data = await self.transport.get_json(
            "/api/v4/futures/usdt/funding_rate", params={"contract": self.symbol, "limit": 1}, label="funding"
        )
        if not isinstance(data, list) or not data:
            raise ProviderError("Gate 资金费率返回为空", provider=self.name, failure_type="empty_data")
        row = data[0]
        rate = float(row.get("funding_rate") or 0)
        ts = int(row.get("t") or row.get("time") or 0)
        return self.ok(
            {
                "funding_rate": rate,
                "funding_rate_pct": rate * 100,
                "annualized_pct": rate * 3 * 365 * 100,  # 8h 一期 -> 每日 3 期
                "timestamp": ts,
                "exchange": "gate",
            },
            DataCategory.DERIVATIVES_FUNDING,
            latency_ms=_lat(started),
            endpoint="/api/v4/futures/usdt/funding_rate",
            raw=row,
        )


class BinanceFuturesProvider(DataProvider):
    """Binance USDⓈ-M 永续：资金费率 + 持仓量（部分地区不可达）。"""

    name = "binance_futures"
    display_name = "Binance 合约"
    homepage = "https://fapi.binance.com"
    base_url = "https://fapi.binance.com"
    categories = {DataCategory.DERIVATIVES_FUNDING, DataCategory.DERIVATIVES_OI}
    capabilities = {"funding", "open_interest"}
    default_priority = 20
    region_hint = "cn-hostile"
    rate_limit_qps = 3.0
    notes = "部分地区不可达；不可达时自动切换到 Gate 合约。"

    async def fetch_funding(self, symbol: str = "BTC", **_: Any):
        started = datetime.now(timezone.utc)
        data = await self.transport.get_json(
            "/fapi/v1/premiumIndex", params={"symbol": "BTCUSDT"}, label="funding"
        )
        if not isinstance(data, dict) or "lastFundingRate" not in data:
            raise ProviderError("Binance 合约返回格式异常", provider=self.name, failure_type="data_format_error")
        rate = float(data["lastFundingRate"])
        return self.ok(
            {
                "funding_rate": rate,
                "funding_rate_pct": rate * 100,
                "annualized_pct": rate * 3 * 365 * 100,
                "mark_price": float(data.get("markPrice") or 0),
                "index_price": float(data.get("indexPrice") or 0),
                "next_funding_time": int(data.get("nextFundingTime") or 0) // 1000,
                "basis_bps": (float(data.get("markPrice") or 0) - float(data.get("indexPrice") or 0))
                / float(data.get("indexPrice") or 1)
                * 10000,
                "exchange": "binance",
            },
            DataCategory.DERIVATIVES_FUNDING,
            latency_ms=_lat(started),
            endpoint="/fapi/v1/premiumIndex",
            raw=data,
        )

    async def fetch_open_interest(self, symbol: str = "BTC", **_: Any):
        started = datetime.now(timezone.utc)
        data = await self.transport.get_json(
            "/fapi/v1/openInterest", params={"symbol": "BTCUSDT"}, label="open_interest"
        )
        oi = float((data or {}).get("openInterest") or 0)
        if oi <= 0:
            raise ProviderError("Binance 持仓量为空", provider=self.name, failure_type="empty_data")
        return self.ok(
            {
                "open_interest": oi,
                "open_interest_usd": float(data.get("openInterestValue") or 0) if "openInterestValue" in data else 0.0,
                "unit": "BTC",
            },
            DataCategory.DERIVATIVES_OI,
            latency_ms=_lat(started),
            endpoint="/fapi/v1/openInterest",
            raw=data,
        )


class OkxDerivativesProvider(DataProvider):
    """OKX 永续：资金费率 + 持仓量。"""

    name = "okx_derivatives"
    display_name = "OKX 衍生品"
    homepage = "https://www.okx.com"
    base_url = "https://www.okx.com"
    categories = {DataCategory.DERIVATIVES_FUNDING, DataCategory.DERIVATIVES_OI}
    capabilities = {"funding", "open_interest"}
    default_priority = 30
    region_hint = "global"
    rate_limit_qps = 2.0
    inst_id = "BTC-USDT-SWAP"

    async def fetch_funding(self, symbol: str = "BTC", **_: Any):
        started = datetime.now(timezone.utc)
        data = await self.transport.get_json(
            "/api/v5/public/funding-rate", params={"instId": self.inst_id}, label="funding"
        )
        rows = (data or {}).get("data") or []
        if not rows:
            raise ProviderError("OKX 资金费率返回为空", provider=self.name, failure_type="empty_data")
        row = rows[0]
        rate = float(row.get("fundingRate") or 0)
        return self.ok(
            {
                "funding_rate": rate,
                "funding_rate_pct": rate * 100,
                "annualized_pct": rate * 3 * 365 * 100,
                "next_funding_time": int(row.get("nextFundingTime") or 0) // 1000,
                "exchange": "okx",
            },
            DataCategory.DERIVATIVES_FUNDING,
            latency_ms=_lat(started),
            endpoint="/api/v5/public/funding-rate",
            raw=row,
        )

    async def fetch_open_interest(self, symbol: str = "BTC", **_: Any):
        started = datetime.now(timezone.utc)
        data = await self.transport.get_json(
            "/api/v5/public/open-interest", params={"instId": self.inst_id}, label="open_interest"
        )
        rows = (data or {}).get("data") or []
        if not rows:
            raise ProviderError("OKX 持仓量返回为空", provider=self.name, failure_type="empty_data")
        row = rows[0]
        oi = float(row.get("oi") or 0)
        return self.ok(
            {
                "open_interest": oi,
                "open_interest_usd": float(row.get("oiUsd") or 0),
                "unit": row.get("oiCcy", "BTC"),
            },
            DataCategory.DERIVATIVES_OI,
            latency_ms=_lat(started),
            endpoint="/api/v5/public/open-interest",
            raw=row,
        )
