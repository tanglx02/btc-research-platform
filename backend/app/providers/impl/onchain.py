# -*- coding: utf-8 -*-
"""链上数据源。

诚实原则：
- mempool.space / blockchain.info 提供的是**真实可得**的基础链上数据（费用、拥堵、算力、地址活跃度）。
- MVRV / SOPR / NUPL / Puell 等高级估值指标需要付费 API（Glassnode / CryptoQuant）。
  系统内置对应 Provider，但未配置 Key 时状态为 NOT_CONFIGURED，
  页面上显示「数据源未配置」，绝不用随机值或模拟值替代。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ...core.errors import ProviderError
from ..base import DataProvider
from ..types import DataCategory


def _lat(started: datetime) -> float:
    return (datetime.now(timezone.utc) - started).total_seconds() * 1000


class MempoolSpaceProvider(DataProvider):
    """mempool.space：无需 Key，实测国内可达。提供费用、拥堵、算力、难度。"""

    name = "mempool_space"
    display_name = "mempool.space"
    homepage = "https://mempool.space"
    base_url = "https://mempool.space"
    categories = {DataCategory.ONCHAIN_BASIC}
    capabilities = {"onchain_basic"}
    default_priority = 10
    region_hint = "cn-friendly"
    rate_limit_qps = 1.5
    notes = "无需 Key、实测国内可达；提供手续费率、内存池、算力、难度等真实基础链上数据。"

    async def fetch_onchain_basic(self, **_: Any):
        started = datetime.now(timezone.utc)
        fees_task = self.transport.get_json("/api/v1/fees/recommended", label="fees")
        mempool_task = self.transport.get_json("/api/mempool", label="mempool")
        height_task = self.transport.get_text("/api/blocks/tip/height", label="height")
        fees, mempool, height = await asyncio_gather(fees_task, mempool_task, height_task)

        if not isinstance(fees, dict) or "fastestFee" not in fees:
            raise ProviderError("mempool.space 费用数据格式异常", provider=self.name, failure_type="data_format_error")

        return self.ok(
            {
                "fee_fastest_satvb": float(fees.get("fastestFee") or 0),
                "fee_half_hour_satvb": float(fees.get("halfHourFee") or 0),
                "fee_hour_satvb": float(fees.get("hourFee") or 0),
                "fee_minimum_satvb": float(fees.get("minimumFee") or 0),
                "mempool_tx_count": int((mempool or {}).get("count") or 0),
                "mempool_vsize_mb": round(float((mempool or {}).get("vsize") or 0) / 1_000_000, 4),
                "tip_height": int(height or 0),
            },
            DataCategory.ONCHAIN_BASIC,
            latency_ms=_lat(started),
            endpoint="/api/v1/fees/recommended+/api/mempool+/api/blocks/tip/height",
            raw={"fees": fees, "mempool": mempool},
        )


class BlockchainInfoStatsProvider(DataProvider):
    """blockchain.info 公开统计：算力、难度、地址活跃度、链上交易量（无需 Key）。"""

    name = "blockchain_info_stats"
    display_name = "Blockchain.info 统计"
    homepage = "https://www.blockchain.com/explorer"
    base_url = "https://blockchain.info"
    categories = {DataCategory.ONCHAIN_BASIC}
    capabilities = {"onchain_basic"}
    default_priority = 20
    region_hint = "cn-friendly"
    rate_limit_qps = 1.0
    notes = "无需 Key、实测可达；提供算力、难度、区块奖励等基础数据。"
    # 2026-09 实测：`/q/btcstcirculating` 已返回 404（官方下线），
    # 改用仍在提供数据的 `/q/totalbc`（已挖出总量，单位 satoshi）作为供应量来源。
    supply_endpoint: str = "/q/totalbc"

    async def fetch_onchain_basic(self, **_: Any):
        started = datetime.now(timezone.utc)
        difficulty = await self.transport.get_text("/q/getdifficulty", label="difficulty")
        hashrate_task = self.transport.get_text("/q/hashrate", label="hashrate")
        block_count_task = self.transport.get_text("/q/getblockcount", label="blockcount")
        supply_task = self.transport.get_text(self.supply_endpoint, label="supply")
        hashrate, block_count, supply = await asyncio_gather(
            hashrate_task, block_count_task, supply_task
        )

        try:
            diff = float(difficulty)
            gh = float(hashrate or 0)  # GH/s
        except (TypeError, ValueError) as exc:
            raise ProviderError(
                f"blockchain.info 返回非数字: {difficulty!r}", provider=self.name, failure_type="data_format_error"
            ) from exc

        try:
            raw_supply = float(supply or 0)
            # /q/totalbc 返回单位为 satoshi
            circulating = raw_supply / 1e8 if self.supply_endpoint.endswith("totalbc") else raw_supply
        except (TypeError, ValueError):
            circulating = None

        return self.ok(
            {
                "difficulty": diff,
                "hashrate_ghs": gh,
                "hashrate_ehs": gh / 1e9,
                "block_height": int(float(block_count or 0)),
                "circulating_supply": circulating,
            },
            DataCategory.ONCHAIN_BASIC,
            latency_ms=_lat(started),
            endpoint=f"/q/getdifficulty+/q/hashrate+/q/getblockcount+{self.supply_endpoint}",
            raw={"difficulty": diff, "hashrate_ghs": gh, "circulating_supply": circulating},
        )


class GlassnodeProvider(DataProvider):
    """Glassnode：MVRV / SOPR / NUPL / Puell / 交易所流量 / ETF 等高级指标。

    需要付费 API Key。未配置时状态 NOT_CONFIGURED —— 系统明确显示未配置，不生成任何替代数据。
    """

    name = "glassnode"
    display_name = "Glassnode（需 API Key）"
    homepage = "https://studio.glassnode.com"
    base_url = "https://api.glassnode.com"
    categories = {DataCategory.ONCHAIN_ADVANCED, DataCategory.EXCHANGE_FLOW}
    capabilities = {"onchain_advanced", "exchange_flow"}
    requires_api_key = True
    api_key_env = "GLASSNODE_API_KEY"
    default_priority = 5
    region_hint = "global"
    rate_limit_qps = 0.5
    notes = "付费源。配置 GLASSNODE_API_KEY 后自动启用 MVRV/SOPR/NUPL 等高级链上指标。"

    # 指标 -> Glassnode endpoint
    METRIC_ENDPOINTS: dict[str, str] = {
        "mvrv": "/v1/metrics/market/mvrv",
        "mvrv_zscore": "/v1/metrics/market/mvrv_z_score",
        "realized_cap": "/v1/metrics/market/marketcap_realized_usd",
        "sopr": "/v1/metrics/indicators/sopr",
        "asopr": "/v1/metrics/indicators/sopr_adjusted",
        "lth_sopr": "/v1/metrics/indicators/sopr_long_term_holders",
        "nupl": "/v1/metrics/indicators/net_unrealized_profit_loss",
        "puell_multiple": "/v1/metrics/indicators/puell_multiple",
        "rhodl": "/v1/metrics/indicators/rhodl_ratio",
        "reserve_risk": "/v1/metrics/indicators/reserve_risk",
        "active_addresses": "/v1/metrics/addresses/active_count",
        "exchange_netflow": "/v1/metrics/transactions/transfers_volume_exchanges_net",
        "exchange_reserve": "/v1/metrics/distribution/balance_exchanges",
        "lth_supply": "/v1/metrics/supply/lth_sum",
        "sth_supply": "/v1/metrics/supply/sth_sum",
    }

    async def _series(self, metric: str, since: int | None = None) -> list[dict[str, Any]]:
        endpoint = self.METRIC_ENDPOINTS.get(metric)
        if not endpoint:
            raise ProviderError(
                f"Glassnode 未支持的指标: {metric}", provider=self.name, failure_type="data_format_error"
            )
        params: dict[str, Any] = {"a": "BTC", "i": "24h", "f": "JSON", "timestamp_format": "unix", "api_key": self.api_key()}
        if since:
            params["s"] = int(since)
        data = await self.transport.get_json(endpoint, params=params, label=metric)
        if not isinstance(data, list) or not data:
            raise ProviderError(f"Glassnode {metric} 返回为空", provider=self.name, failure_type="empty_data")
        out = []
        for row in data:
            out.append(
                {
                    "ts": int(row.get("t") or 0),
                    "value": float(row.get("v") or 0) if row.get("v") is not None else None,
                }
            )
        return [r for r in out if r["value"] is not None]

    async def fetch_onchain_advanced(self, metrics: list[str] | None = None, **_: Any):
        started = datetime.now(timezone.utc)
        wanted = metrics or ["mvrv", "nupl", "sopr", "puell_multiple", "realized_cap"]
        result: dict[str, Any] = {}
        for m in wanted:
            try:
                result[m] = await self._series(m)
            except ProviderError:
                result[m] = []  # 单个指标缺失不影响其他指标
        if not any(result.values()):
            raise ProviderError("Glassnode 未返回任何有效指标", provider=self.name, failure_type="empty_data")
        return self.ok(
            result,
            DataCategory.ONCHAIN_ADVANCED,
            latency_ms=_lat(started),
            endpoint="multiple",
            metrics=wanted,
        )

    async def fetch_exchange_flow(self, **_: Any):
        started = datetime.now(timezone.utc)
        data: dict[str, Any] = {}
        for m in ("exchange_reserve", "exchange_netflow"):
            try:
                s = await self._series(m)
                if s:
                    data[m] = s[-1]["value"]
            except ProviderError:
                continue
        if not data:
            raise ProviderError("Glassnode 交易所流量不可用", provider=self.name, failure_type="empty_data")
        return self.ok(
            data,
            DataCategory.EXCHANGE_FLOW,
            latency_ms=_lat(started),
            endpoint="/v1/metrics/distribution,transactions",
        )


async def asyncio_gather(*awaitables: Any) -> list[Any]:
    """包装 asyncio.gather：任一失败即抛出对应的 ProviderError（保留原文）。"""
    import asyncio

    results = await asyncio.gather(*awaitables, return_exceptions=True)
    for item in results:
        if isinstance(item, BaseException):
            raise item
    return list(results)
