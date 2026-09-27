# -*- coding: utf-8 -*-
"""辅助行情源：市值/总量数据 + 极简价格备用源。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ...core.errors import ProviderError
from ..base import DataProvider
from ..types import DataCategory


class CoinGeckoProvider(DataProvider):
    """CoinGecko：市值、供应量、ATH。免费无需 Key，但限流较严。"""

    name = "coingecko"
    display_name = "CoinGecko"
    homepage = "https://www.coingecko.com"
    base_url = "https://api.coingecko.com"
    categories = {DataCategory.MARKET_CAP, DataCategory.MARKET_PRICE}
    capabilities = {"market_cap", "price"}
    default_priority = 40
    region_hint = "cn-hostile"
    rate_limit_qps = 1.0
    notes = "免费无需 Key，提供市值/供应量/ATH；免费额度限流较严格。"

    async def fetch_price(self, symbol: str = "BTC", **_: Any):
        started = datetime.now(timezone.utc)
        data = await self.transport.get_json(
            "/api/v3/simple/price",
            params={"ids": "bitcoin", "vs_currencies": "usd", "include_last_updated_at": "true"},
            label="price",
        )
        row = (data or {}).get("bitcoin") or {}
        price = row.get("usd")
        if not price:
            raise ProviderError(
                f"CoinGecko 返回缺少价格字段: {str(row)[:120]}",
                provider=self.name,
                failure_type="data_format_error",
            )
        return self.ok(
            float(price),
            DataCategory.MARKET_PRICE,
            latency_ms=(datetime.now(timezone.utc) - started).total_seconds() * 1000,
            endpoint="/api/v3/simple/price",
            raw=row,
            quote="USD",
        )

    async def fetch_market_cap(self, symbol: str = "BTC", **_: Any):
        started = datetime.now(timezone.utc)
        data = await self.transport.get_json(
            "/api/v3/coins/markets",
            params={"vs_currency": "usd", "ids": "bitcoin", "price_change_percentage": "24h"},
            label="market_cap",
        )
        if not isinstance(data, list) or not data:
            raise ProviderError(
                "CoinGecko 返回格式异常",
                provider=self.name,
                failure_type="data_format_error",
            )
        row = data[0]
        return self.ok(
            {
                "market_cap": float(row.get("market_cap") or 0),
                "circulating_supply": float(row.get("circulating_supply") or 0),
                "total_supply": float(row.get("total_supply") or 0),
                "max_supply": float(row.get("max_supply") or 0),
                "fdv": float(row.get("fully_diluted_valuation") or 0),
                "volume_24h": float(row.get("total_volume") or 0),
                "price": float(row.get("current_price") or 0),
                "ath": float(row.get("ath") or 0),
                "ath_change_pct": float(row.get("ath_change_percentage") or 0),
                "price_change_24h_pct": float(row.get("price_change_percentage_24h") or 0),
                "observation_time": row.get("last_updated"),
            },
            DataCategory.MARKET_CAP,
            latency_ms=(datetime.now(timezone.utc) - started).total_seconds() * 1000,
            endpoint="/api/v3/coins/markets",
            raw=row,
        )


class BlockchainInfoTickerProvider(DataProvider):
    """blockchain.info 极简行情：无需 Key，实测国内可达，兜底备用源。"""

    name = "blockchain_info_ticker"
    display_name = "Blockchain.info Ticker"
    homepage = "https://blockchain.info"
    base_url = "https://blockchain.info"
    categories = {DataCategory.MARKET_PRICE}
    capabilities = {"price"}
    default_priority = 55
    region_hint = "cn-friendly"
    rate_limit_qps = 1.0
    notes = "无需 Key、实测国内可达，作为兜底备用价格源。"

    async def fetch_price(self, symbol: str = "BTC", **_: Any):
        started = datetime.now(timezone.utc)
        data = await self.transport.get_json("/ticker", label="price")
        row = (data or {}).get("USD") or {}
        price = row.get("last") or row.get("15m")
        if not price:
            raise ProviderError(
                "Blockchain.info 返回缺少 USD 价格",
                provider=self.name,
                failure_type="data_format_error",
            )
        return self.ok(
            float(price),
            DataCategory.MARKET_PRICE,
            latency_ms=(datetime.now(timezone.utc) - started).total_seconds() * 1000,
            endpoint="/ticker",
            raw=row,
            quote="USD",
        )
