# -*- coding: utf-8 -*-
"""自动故障切换测试（核心验收项）。

必须验证的完整链路：
    主源失败 -> 备用 1 接管
    备用 1 也失败 -> 备用 2 接管
    备用 3 -> ...
    全部失败 -> 抛 AllProvidersFailedError，**绝不生成假数据**

以及：
  * 未配置的数据源必须被跳过而不是失败计数；
  * 被禁用的数据源必须在 attempts 里显式留下 disabled 痕迹；
  * 403/auth_error 会自动停用该 Provider（避免无效重试）；
  * 「数据源本就不支持某项」不得连坐停用该 Provider。
"""

from __future__ import annotations

import pytest

from app.core.errors import AllProvidersFailedError
from app.providers.registry import ProviderRegistry
from app.providers.router import ResilientRouter
from app.providers.types import DataCategory


def build_registry(factory, specs: list[tuple[str, float, int, str]]) -> ProviderRegistry:
    """specs = [(name, price, failures_before_success, failure_type)]"""
    reg = ProviderRegistry()
    for i, (name, price, failures, ftype) in enumerate(specs):
        cls = factory
        reg.register(cls(name=name, price=price, failures=failures, failure_type=ftype, priority=10 + i * 10))
    return reg


@pytest.mark.asyncio
async def test_primary_success_no_failover(make_price_provider_factory):
    reg = build_registry(
        make_price_provider_factory,
        [("p_main", 61000.0, 0, "server_error"), ("p_backup", 60000.0, 0, "server_error")],
    )
    router = ResilientRouter(reg)
    result = await router.fetch(DataCategory.MARKET_PRICE)
    assert result.provider == "p_main"
    assert result.used_fallback is False
    assert result.data == 61000.0
    assert len(result.attempts) == 1


@pytest.mark.asyncio
async def test_failover_chain_main_then_backup(make_price_provider_factory):
    """主源失败 -> 备用 1 接管，且 attempts 里保留失败原因。"""
    reg = build_registry(
        make_price_provider_factory,
        [
            ("p_main", 61000.0, 99, "server_error"),
            ("p_backup", 60500.0, 0, "server_error"),
        ],
    )
    router = ResilientRouter(reg)
    result = await router.fetch(DataCategory.MARKET_PRICE)
    assert result.provider == "p_backup"
    assert result.used_fallback is True
    assert result.primary_provider == "p_main"
    assert result.failover_reason == "server_error"
    names = [a.provider for a in result.attempts]
    assert names == ["p_main", "p_backup"]
    assert result.attempts[0].ok is False
    assert result.attempts[1].ok is True
    # 降级使用的数据必须降低置信度
    assert result.confidence < 1.0


@pytest.mark.asyncio
async def test_failover_three_levels(make_price_provider_factory):
    """主断 -> 备 1 断 -> 第三源接管。"""
    reg = build_registry(
        make_price_provider_factory,
        [
            ("p_main", 61000.0, 99, "timeout"),
            ("p_b1", 60900.0, 99, "dns_error"),
            ("p_b2", 60800.0, 0, "server_error"),
            ("p_b3", 100.0, 0, "server_error"),
        ],
    )
    router = ResilientRouter(reg)
    result = await router.fetch(DataCategory.MARKET_PRICE)
    assert result.provider == "p_b2"
    assert [a.provider for a in result.attempts] == ["p_main", "p_b1", "p_b2"]
    assert [a.failure_type for a in result.attempts[:2]] == ["timeout", "dns_error"]


@pytest.mark.asyncio
async def test_all_providers_failed_raises_no_fake_data(make_price_provider_factory):
    """全断时必须抛错 —— 绝不能用随机数/旧值/硬编码冒充实时数据。"""
    reg = build_registry(
        make_price_provider_factory,
        [
            ("p_main", 61000.0, 99, "server_error"),
            ("p_b1", 60900.0, 99, "timeout"),
            ("p_b2", 60800.0, 99, "network_error"),
        ],
    )
    router = ResilientRouter(reg)
    with pytest.raises(AllProvidersFailedError) as exc:
        await router.fetch(DataCategory.MARKET_PRICE)
    payload = exc.value.to_dict()
    # 失败链路完整保留，前端可展示「为什么没有数据」
    attempts = payload["error"]["attempts"]
    assert [a["provider"] for a in attempts] == ["p_main", "p_b1", "p_b2"]
    assert all(a["ok"] is False for a in attempts)
    # 最重要的是：没有返回任何 price
    assert "price" not in payload["error"] and "data" not in payload["error"]


@pytest.mark.asyncio
async def test_disabled_provider_is_skipped_with_trace(make_price_provider_factory):
    reg = build_registry(
        make_price_provider_factory,
        [("p_main", 61000.0, 0, "server_error"), ("p_off", 60000.0, 0, "server_error")],
    )
    reg.set_enabled("p_main", False, reason="测试：手动停用")
    router = ResilientRouter(reg)
    result = await router.fetch(DataCategory.MARKET_PRICE)
    assert result.provider == "p_off"
    disabled = [a for a in result.attempts if a.provider == "p_main"]
    assert disabled and disabled[0].failure_type == "disabled"


@pytest.mark.asyncio
async def test_auth_error_auto_disables_provider(make_price_provider_factory):
    reg = build_registry(
        make_price_provider_factory,
        [("p_main", 61000.0, 99, "auth_error"), ("p_backup", 60000.0, 0, "server_error")],
    )
    router = ResilientRouter(reg)
    result = await router.fetch(DataCategory.MARKET_PRICE)
    assert result.provider == "p_backup"
    assert reg.get("p_main").enabled is False
    assert "auth_error" in reg.get("p_main").disabled_reason


@pytest.mark.asyncio
async def test_unsupported_input_does_not_disable_provider(make_price_provider_factory):
    """「本数据源不提供这个序列」= 切下一个源，不能把健康的源停用下线。"""
    reg = build_registry(
        make_price_provider_factory,
        [("p_main", 61000.0, 99, "unsupported_input"), ("p_backup", 60000.0, 0, "server_error")],
    )
    router = ResilientRouter(reg)
    result = await router.fetch(DataCategory.MARKET_PRICE)
    assert result.provider == "p_backup"
    assert reg.get("p_main").enabled is True


@pytest.mark.asyncio
async def test_timeout_is_classified_as_timeout(make_price_provider_factory):
    """超时必须是硬超时，不能把整个服务拖住。"""
    import asyncio

    from app.core.errors import ProviderError

    reg = build_registry(make_price_provider_factory, [("p_main", 61000.0, 0, "server_error")])

    async def slow_fetch(symbol: str = "BTC"):
        await asyncio.sleep(30)

    reg.get("p_main").fetch_price = slow_fetch  # type: ignore[method-assign]
    router = ResilientRouter(reg)
    with pytest.raises(AllProvidersFailedError) as exc:
        await router.fetch(DataCategory.MARKET_PRICE)
    payload = exc.value.to_dict()
    assert payload["error"]["attempts"][0]["failure_type"] == "timeout"
