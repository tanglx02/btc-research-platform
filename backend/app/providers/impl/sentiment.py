# -*- coding: utf-8 -*-
"""情绪数据源：Crypto Fear & Greed Index（无需 Key，实测国内可达）。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ...core.errors import ProviderError
from ..base import DataProvider
from ..types import DataCategory


def _lat(started: datetime) -> float:
    return (datetime.now(timezone.utc) - started).total_seconds() * 1000


CLASSIFICATION_CN = {
    "Extreme Fear": "极度恐慌",
    "Fear": "恐慌",
    "Neutral": "中性",
    "Greed": "贪婪",
    "Extreme Greed": "极度贪婪",
}


class AlternativeMeFngProvider(DataProvider):
    """alternative.me 恐慌贪婪指数。免费、无需 Key、实测国内可达。"""

    name = "alternative_me_fng"
    display_name = "Crypto Fear & Greed Index"
    homepage = "https://alternative.me/crypto/fear-and-greed-index/"
    base_url = "https://api.alternative.me"
    categories = {DataCategory.SENTIMENT_INDEX}
    capabilities = {"sentiment"}
    default_priority = 10
    region_hint = "cn-friendly"
    rate_limit_qps = 0.5
    notes = "免费无需 Key、实测可达；提供恐慌贪婪指数及其历史序列。"

    async def fetch_sentiment(self, limit: int = 30, **_: Any):
        started = datetime.now(timezone.utc)
        data = await self.transport.get_json("/fng/", params={"limit": limit}, label="fng")
        rows = (data or {}).get("data") or []
        if not rows:
            raise ProviderError("恐慌贪婪指数返回为空", provider=self.name, failure_type="empty_data")

        series = []
        for row in rows:
            try:
                value = float(row["value"])
                ts = int(row["timestamp"])
            except (KeyError, ValueError, TypeError):
                continue
            series.append({"ts": ts, "value": value, "classification": row.get("value_classification", "")})

        latest = series[0]
        cls_en = latest["classification"]
        return self.ok(
            {
                "index": latest["value"],
                "classification": cls_en,
                "classification_cn": CLASSIFICATION_CN.get(cls_en, cls_en),
                "value_yesterday": series[1]["value"] if len(series) > 1 else None,
                "change_1d": (latest["value"] - series[1]["value"]) if len(series) > 1 else 0.0,
                "series": series,
            },
            DataCategory.SENTIMENT_INDEX,
            latency_ms=_lat(started),
            endpoint="/fng/",
            raw={"latest": latest},
            unit="index_0_100",
        )
