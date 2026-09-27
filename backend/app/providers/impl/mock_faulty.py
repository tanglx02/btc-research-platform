# -*- coding: utf-8 -*-
"""测试专用 Mock Provider（严格隔离）。

安全约束：
1. 模块名以 `mock_` 开头 —— 注册表默认**不会**自动发现，只有测试环境显式载入。
2. Provider 名称统一带 `mock_` 前缀，写入数据库时来源一目了然。
3. 返回值固定且可断言，**绝不用于生产数据展示**。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ...core.errors import ProviderError
from ..base import DataProvider
from ..types import Candle, DataCategory


def _lat(started: datetime) -> float:
    return (datetime.now(timezone.utc) - started).total_seconds() * 1000


class MockStaticProvider(DataProvider):
    """固定值 Provider：仅用于 provider/route/engine 单元测试。"""

    name = "mock_static"
    display_name = "Mock 固定值（测试隔离）"
    base_url = "http://mock.invalid"
    categories = {DataCategory.MARKET_PRICE, DataCategory.OHLCV}
    capabilities = {"price", "ohlcv"}
    default_priority = 1
    region_hint = "test"
    notes = "测试隔离专用，永远不参与生产链路。"

    fixed_price: float = 50000.0

    async def fetch_price(self, symbol: str = "BTC", **_: Any):
        started = datetime.now(timezone.utc)
        return self.ok(
            self.fixed_price,
            DataCategory.MARKET_PRICE,
            latency_ms=_lat(started),
            endpoint="mock://price",
            is_mock=True,
        )

    async def fetch_ohlcv(self, symbol: str = "BTC", interval: str = "1d", limit: int = 10, **_: Any):
        started = datetime.now(timezone.utc)
        base = 1_700_000_000
        candles = [
            Candle(
                ts=base + i * 86400,
                open=100 + i,
                high=110 + i,
                low=90 + i,
                close=105 + i,
                volume=1000 + i,
                interval=interval,
            )
            for i in range(limit)
        ]
        return self.ok(candles, DataCategory.OHLCV, latency_ms=_lat(started), endpoint="mock://ohlcv", is_mock=True)


class MockAlwaysFailProvider(DataProvider):
    """永远失败：用于验证「自动切换到下一个 Provider」。"""

    name = "mock_always_fail"
    display_name = "Mock 恒定失败（故障注入）"
    base_url = "http://mock.invalid"
    categories = {DataCategory.MARKET_PRICE}
    capabilities = {"price"}
    default_priority = 0
    region_hint = "test"
    notes = "故障注入专用：每次调用必然超时。"

    async def fetch_price(self, symbol: str = "BTC", **_: Any):
        raise ProviderError(
            "Mock 模拟超时失败", provider=self.name, failure_type="timeout", retryable=True
        )


class MockBadDataProvider(DataProvider):
    """返回脏数据：用于验证「坏数据不能悄悄覆盖正确数据」。"""

    name = "mock_bad_data"
    display_name = "Mock 脏数据（质量校验）"
    base_url = "http://mock.invalid"
    categories = {DataCategory.MARKET_PRICE}
    capabilities = {"price"}
    default_priority = 2
    region_hint = "test"
    notes = "返回负数价格，验证出口处的数据质量拦截。"

    async def fetch_price(self, symbol: str = "BTC", **_: Any):
        raise ProviderError(
            "Mock 模拟脏数据：价格为 -1", provider=self.name, failure_type="data_quality_error"
        )

    # 注意：即使 Provider 直接返回脏值，Router 的 _sanity 也会拦截。
    async def fetch_price_unchecked(self, symbol: str = "BTC", **_: Any):
        started = datetime.now(timezone.utc)
        return self.ok(-1.0, DataCategory.MARKET_PRICE, latency_ms=_lat(started), endpoint="mock://bad")
