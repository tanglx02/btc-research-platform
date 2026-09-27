# -*- coding: utf-8 -*-
"""资金计划与账本的账必须对得上。

这一组用例锁定的都是「删了/改了却发现资产没变」这类事故：
  1. 删除计划时流水没级联 → 计划没了，钱还在账上；
  2. 同一天反复记快照 → 净值曲线上同一天出现好几个互相矛盾的点；
  3. 卖出不结转成本 → 均价被系统性抬高、浮盈被系统性低估；
  4. 多币种金额直接相加 → 数字看着合理，口径其实混了。
"""

from __future__ import annotations

import pytest

from app.db.migrate import init_db
from app.db.repo import upsert_candles
from app.services.plan_service import PlanService

from helpers import build_candles


async def _seed_price(rows: int = 5) -> float:
    from app.db.base import get_session_factory

    candles = build_candles(rows, base=60000.0)
    factory = get_session_factory()
    async with factory() as session:
        await upsert_candles(session, candles)
    return float(candles[-1]["close"])


@pytest.fixture()
async def _db():
    await init_db()
    return None


PLAN_PAYLOAD = {
    "name": "单元测试计划",
    "currency": "CNY",
    "initial_capital": 100000,
    "monthly_income": 30000,
    "monthly_contribution": 3000,
    "weekly_contribution": 700,
    "contribution_frequency": "monthly",
    "start_date": "2024-01-01",
    "end_date": "2025-01-01",
    "cash_reserve": 5000,
    "max_single_contribution": 5000,
    "max_drawdown_tolerance": 20,
    "strategy_code": "dca_fixed",
    "strategy_params": {},
    "notes": "",
}


@pytest.mark.asyncio
async def test_deleting_plan_also_removes_its_transactions(_db):
    service = PlanService()
    await _seed_price()
    plan = await service.create(dict(PLAN_PAYLOAD))
    await service.add_transaction(plan["id"], {"price": 60000, "amount_fiat": 60000, "side": "buy", "date": "2024-02-01"})
    await service.add_transaction(plan["id"], {"price": 62000, "amount_fiat": 31000, "side": "buy", "date": "2024-03-01"})
    assert (await service.holdings(plan["id"]))["transaction_count"] == 2

    result = await service.delete(plan["id"])
    assert result["deleted"] is True
    assert result["transactions_removed"] == 2, "计划删了但流水还在 —— 资产账会凭空多出一笔"

    after = await service.holdings(plan["id"])
    assert after["transaction_count"] == 0
    assert after["total_btc"] == 0.0
    assert await service.transactions(plan["id"]) == []


@pytest.mark.asyncio
async def test_snapshot_is_stored_once_per_day(_db):
    service = PlanService()
    await _seed_price()
    plan = await service.create(dict(PLAN_PAYLOAD))

    first = await service.snapshot(plan["id"])
    assert first["mode"] == "created"
    second = await service.snapshot(plan["id"])
    assert second["mode"] == "updated", "同一天重复记快照会产生互相矛盾的净值点"

    from sqlalchemy import func, select

    from app.db.base import get_session_factory
    from app.db.models import PortfolioSnapshot

    factory = get_session_factory()
    async with factory() as session:
        count = (await session.execute(
            select(func.count()).select_from(PortfolioSnapshot).where(PortfolioSnapshot.plan_id == plan["id"])
        )).scalar_one()
    assert count == 1


@pytest.mark.asyncio
async def test_selling_reduces_cost_basis(_db):
    """卖出必须按持仓比例结转成本，否则均价会被抬高。"""
    service = PlanService()
    await _seed_price()
    plan = await service.create(dict(PLAN_PAYLOAD))

    await service.add_transaction(plan["id"], {"price": 50000, "amount_fiat": 50000, "side": "buy", "date": "2024-02-01"})
    before = await service.holdings(plan["id"])
    assert before["total_btc"] == pytest.approx(1.0)
    assert before["avg_cost"] == pytest.approx(50000)

    # 以同一价格卖出一半：均价不应变化，已实现盈亏为 0
    await service.add_transaction(plan["id"], {"price": 50000, "amount_btc": 0.5, "side": "sell", "date": "2024-03-01"})
    after = await service.holdings(plan["id"])
    assert after["total_btc"] == pytest.approx(0.5)
    assert after["avg_cost"] == pytest.approx(50000)
    assert after["cost_basis"] == pytest.approx(25000), "卖出后剩余持仓的成本基数没有按比例结转"
    assert after["total_invested"] == pytest.approx(50000), "累计投入本金是历史事实，不该被抹掉"
    assert after["realized_pnl"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_mixed_currency_is_flagged_not_silently_summed(_db):
    """CNY 与 USD 混记时必须明确告警，并且不得擅自换算。"""
    service = PlanService()
    await _seed_price()
    plan = await service.create(dict(PLAN_PAYLOAD))

    await service.add_transaction(plan["id"], {"price": 60000, "amount_fiat": 60000, "side": "buy",
                                               "date": "2024-02-01", "currency": "CNY"})
    await service.add_transaction(plan["id"], {"price": 60000, "amount_fiat": 60000, "side": "buy",
                                               "date": "2024-03-01", "currency": "USD"})

    raw = await service.holdings(plan["id"])
    assert raw["mixed_currency"] is True
    assert raw["fx_conversion"]["applied"] is False
    assert "未做换算" in raw["fx_conversion"]["message"]
    # 未换算时 CNY 记为 60000、USD 也记为 60000，两者只是原样相加，且已明确标注不可直接使用
    assert raw["total_invested"] == pytest.approx(120000)

    converted = await service.holdings(plan["id"], usd_to_plan_rate=7.2)
    assert converted["fx_conversion"]["applied"] is True
    assert converted["fx_conversion"]["rate_used"] == pytest.approx(7.2)
    assert converted["total_invested"] == pytest.approx(60000 + 60000 * 7.2)
    assert converted["current_price_in_plan_currency"] is not None


@pytest.mark.asyncio
async def test_next_contribution_respects_end_date(_db):
    """计划到期后必须明确告知停止加码，而不是继续给建议金额装作没事。"""
    service = PlanService()
    await _seed_price(rows=400)
    plan = await service.create(dict(PLAN_PAYLOAD, end_date="2024-06-30"))
    await service.add_transaction(plan["id"], {"price": 60000, "amount_fiat": 60000, "side": "buy", "date": "2024-06-20"})

    advice = await service.next_contribution(plan["id"])
    assert advice["available"] is True
    assert advice["plan_status"] == "ended", "已过截止日期仍被当作进行中的计划"
    assert any("到期" in w for w in advice["warnings"])


@pytest.mark.asyncio
async def test_next_contribution_uses_drawdown_tolerance_and_reserve(_db):
    """max_drawdown_tolerance / cash_reserve / monthly_income 必须真正参与判断。"""
    service = PlanService()
    await _seed_price(rows=400)
    ok_plan = await service.create(dict(PLAN_PAYLOAD, end_date="2099-12-31",
                                        start_date="2024-01-01", max_drawdown_tolerance=99))
    ok = await service.next_contribution(ok_plan["id"])
    assert ok["beyond_drawdown_tolerance"] is False
    assert ok["guards"]["initial_capital"] == 100000
    assert ok["guards"]["disposable"] == pytest.approx(25000)

    tight = await service.create(dict(PLAN_PAYLOAD, name="紧", end_date="2099-12-31",
                                      max_drawdown_tolerance=1))
    advice = await service.next_contribution(tight["id"])
    assert isinstance(advice["beyond_drawdown_tolerance"], bool)
    # 容忍度设成 1% 时，只要当周月出现回撤就会被标记（序列本身波动约 ±20%）
    assert advice["beyond_drawdown_tolerance"] is True
    assert any("容忍度" in w for w in advice["warnings"])
