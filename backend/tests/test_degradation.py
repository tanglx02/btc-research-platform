# -*- coding: utf-8 -*-
"""断网 / 数据源全挂时的降级行为测试。

这条线决定了系统是「诚实」还是「看起来很专业」：
  * 所有 Provider 全挂 + 本地有历史 => 显示最后一次成功数据，并**明确标注 stale + 来源时间**；
  * 所有 Provider 全挂 + 本地无历史 => 明确告知没有数据，**绝不返回任何估算/伪造价格**；
  * 任何情况下都要把失败链路（attempts）带回前端，「为什么没有数据」必须可解释。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.db.base import get_session_factory
from app.db.migrate import init_db
from app.db.repo import upsert_candles, upsert_market_price
from app.providers.registry import ProviderRegistry
from app.providers.router import ResilientRouter
from app.services.market_service import MarketService

from helpers import build_candles


def failing_router(factory, names: tuple[str, ...] = ("net_a", "net_b")) -> ResilientRouter:
    """构造一个所有 Provider 都失败的路由器（模拟断网/全线 5xx）。"""
    registry = ProviderRegistry()
    for i, name in enumerate(names):
        registry.register(factory(name=name, price=61000.0, failures=99,
                                  failure_type="network_error", priority=10 + i * 10))
    return ResilientRouter(registry)


def _service(router: ResilientRouter) -> MarketService:
    svc = MarketService()
    svc.router = router
    return svc


@pytest.mark.asyncio
async def test_no_fake_price_when_all_sources_down_and_no_local_data(tmp_db_path,
                                                                    make_price_provider_factory):
    """最硬的一条：什么都没有的时候，必须如实说没有，而不是编一个价格出来。"""
    await init_db()
    result = await _service(failing_router(make_price_provider_factory)).current_price()

    assert result["available"] is False
    assert result["price"] is None
    assert "不做任何估算" in result["message"]
    assert result["attempts"], "失败链路必须保留，前端才能解释为什么没数据"
    assert len(result["attempts"]) == 2


@pytest.mark.asyncio
async def test_falls_back_to_last_local_price_and_marks_stale(tmp_db_path,
                                                              make_price_provider_factory):
    """本地有数据时降级使用，但必须标注 stale、来源时间与下降后的置信度。"""
    await init_db()
    factory = get_session_factory()
    observed = datetime.now(timezone.utc) - timedelta(hours=6)
    async with factory() as session:
        await upsert_market_price(session, {
            "symbol": "BTC", "source_id": "binance_vision", "price": 61234.5, "quote": "USDT",
            "quality_status": "CROSS_VERIFIED", "confidence": 1.0,
            "observation_time": observed, "fetch_time": observed,
        })

    result = await _service(failing_router(make_price_provider_factory)).current_price()

    assert result["available"] is True
    assert result["price"] == 61234.5
    assert result["stale"] is True
    assert result["source"]["used_fallback"] is True
    assert result["source"]["provider"] == "binance_vision"
    # 降级后置信度必须下降，不能让用户以为这是实时数据
    assert result["source"]["confidence"] < 1.0
    assert result["source"]["observation_time"] is not None
    assert result["source"]["attempts"], "即使降级也要能追查失败原因"


@pytest.mark.asyncio
async def test_partial_outage_still_returns_live_price(tmp_db_path, make_price_provider_factory):
    """主源失败但备用可用时，不算降级 —— 仍然是实时数据，只是走了 fallback 链。"""
    registry = ProviderRegistry()
    registry.register(make_price_provider_factory(name="dead_main", price=1.0, failures=99,
                                                  failure_type="server_error", priority=10))
    registry.register(make_price_provider_factory(name="alive_backup", price=64000.0, failures=0,
                                                  failure_type="server_error", priority=20))
    svc = _service(ResilientRouter(registry))
    result = await svc.current_price()
    assert result["available"] is True
    assert result["price"] == 64000.0
    assert result["stale"] is False


@pytest.mark.asyncio
async def test_history_reads_never_touch_network(tmp_db_path, make_price_provider_factory):
    """历史数据必须读本地库：即使全网断开，已入库的历史照样可查。"""
    await init_db()
    factory = get_session_factory()
    async with factory() as session:
        await upsert_candles(session, build_candles(120))

    result = await _service(failing_router(make_price_provider_factory)).candles(limit=60)
    assert result["available"] is True
    assert result["count"] == 60
    assert result["source"]["mode"] == "local_database"
    assert "unit_synthetic" in result["source"]["providers_used_historically"]


@pytest.mark.asyncio
async def test_indicators_report_unavailable_instead_of_estimating(tmp_db_path,
                                                                   make_price_provider_factory):
    await init_db()
    result = await _service(failing_router(make_price_provider_factory)).indicators()
    assert result["available"] is False
    assert "暂无" in result["message"] or "回填" in result["message"]
