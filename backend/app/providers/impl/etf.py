# -*- coding: utf-8 -*-
"""ETF 资金流数据源。

现实约束：BTC 现货 ETF 的逐日资金流缺少稳定的免费公开 API。
本模块提供两个付费 Provider（Coinglass / CryptoQuant 风格接口），
未配置 Key 时状态为 NOT_CONFIGURED，ETF 页面显示「数据源未配置」，
**绝不用模拟/随机数据冒充真实 ETF 资金流**。

用户配置任意一家后，ETF 模块即刻自动启用，无需修改任何业务代码。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ...core.errors import ProviderError
from ..base import DataProvider
from ..types import DataCategory


def _lat(started: datetime) -> float:
    return (datetime.now(timezone.utc) - started).total_seconds() * 1000


class CoinglassEtfProvider(DataProvider):
    """Coinglass BTC 现货 ETF 资金流（需 API Key）。"""

    name = "coinglass_etf"
    display_name = "Coinglass ETF（需 API Key）"
    homepage = "https://www.coinglass.com"
    base_url = "https://open-api-v3.coinglass.com"
    categories = {DataCategory.ETF_FLOW}
    capabilities = {"etf_flow"}
    requires_api_key = True
    api_key_env = "COINGLASS_API_KEY"
    default_priority = 10
    region_hint = "global"
    rate_limit_qps = 0.5
    notes = "付费源；配置 COINGLASS_API_KEY 后 ETF 模块自动启用。"

    async def fetch_etf_flow(self, **_: Any):
        started = datetime.now(timezone.utc)
        data = await self.transport.get_json(
            "/api/etf/btc/flow-history",
            headers={"accept": "application/json", "CG-API-KEY": self.api_key()},
            label="etf_flow",
        )
        rows = (data or {}).get("data") or []
        if not rows:
            raise ProviderError("Coinglass ETF 数据为空", provider=self.name, failure_type="empty_data")

        flows = []
        for row in rows:
            flows.append(
                {
                    "date": row.get("date"),
                    "net_flow_usd": float(row.get("net_flow", 0) or 0),
                    "inflow_usd": float(row.get("inflow", 0) or 0),
                    "outflow_usd": float(row.get("outflow", 0) or 0),
                    "total_assets_usd": float(row.get("total_assets", 0) or 0),
                }
            )
        flows.sort(key=lambda x: x["date"] or "")

        latest = flows[-1] if flows else {}
        return self.ok(
            {
                "latest": latest,
                "history": flows[-180:],
                "net_flow_7d": sum(f["net_flow_usd"] for f in flows[-7:] if f["net_flow_usd"]),
                "net_flow_30d": sum(f["net_flow_usd"] for f in flows[-30:] if f["net_flow_usd"]),
                "cumulative_flow": sum(f["net_flow_usd"] for f in flows if f["net_flow_usd"]),
            },
            DataCategory.ETF_FLOW,
            latency_ms=_lat(started),
            endpoint="/api/etf/btc/flow-history",
            note="真实 ETF 资金流（来自 Coinglass）",
        )


class CryptoQuantEtfProvider(DataProvider):
    """CryptoQuant ETF / 储备数据（需 API Key），作为 ETF 备用源。"""

    name = "cryptoquant_etf"
    display_name = "CryptoQuant（需 API Key）"
    homepage = "https://cryptoquant.com"
    base_url = "https://api.cryptoquant.com"
    categories = {DataCategory.ETF_FLOW}
    capabilities = {"etf_flow"}
    requires_api_key = True
    api_key_env = "CRYPTOQUANT_API_KEY"
    default_priority = 20
    region_hint = "global"
    rate_limit_qps = 0.5
    notes = "付费源；配置 CRYPTOQUANT_API_KEY 后作为 ETF 备用数据源。"

    async def fetch_etf_flow(self, **_: Any):
        started = datetime.now(timezone.utc)
        data = await self.transport.get_json(
            "/v1/btc/etf/flow",
            headers={"Authorization": f"Bearer {self.api_key()}"},
            label="etf_flow",
        )
        rows = (data or {}).get("result", {}).get("data") or (data or {}).get("data") or []
        if not rows:
            raise ProviderError("CryptoQuant ETF 返回为空", provider=self.name, failure_type="empty_data")
        flows = [
            {
                "date": row.get("date"),
                "net_flow_usd": float(row.get("net_flow_usd", 0) or 0),
                "inflow_usd": float(row.get("inflow_usd", 0) or 0),
                "outflow_usd": float(row.get("outflow_usd", 0) or 0),
                "total_assets_usd": float(row.get("holdings_usd", 0) or 0),
            }
            for row in rows
        ]
        flows.sort(key=lambda x: x["date"] or "")
        return self.ok(
            {
                "latest": flows[-1] if flows else {},
                "history": flows[-180:],
                "net_flow_7d": sum(f["net_flow_usd"] for f in flows[-7:]),
                "net_flow_30d": sum(f["net_flow_usd"] for f in flows[-30:]),
                "cumulative_flow": sum(f["net_flow_usd"] for f in flows),
            },
            DataCategory.ETF_FLOW,
            latency_ms=_lat(started),
            endpoint="/v1/btc/etf/flow",
            note="真实 ETF 资金流（来自 CryptoQuant）",
        )
