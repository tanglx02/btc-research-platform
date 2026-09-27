# -*- coding: utf-8 -*-
"""行情与市场分析 API。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Query

from ..core.errors import AppError
from ..db.base import get_session_factory
from ..db.models import TimelineEvent, UserPlan
from ..services import BacktestConfig, BacktestEngine
from .deps import AdminDep, MarketServiceDep, RequestIdDep, WriteDep
from .schemas import BacktestRequest

router = APIRouter()


@router.get("/overview", summary="首页总览：普通人视角的一句话结论")
async def overview(service: MarketServiceDep, rid: RequestIdDep) -> dict[str, Any]:
    return await service.overview()


@router.get("/price", summary="当前价格（含数据源与交叉验证）")
async def price(service: MarketServiceDep, symbol: str = "BTC") -> dict[str, Any]:
    return await service.current_price(symbol)


@router.get("/candles", summary="历史 K 线（优先读本地数据库）")
async def candles(
    service: MarketServiceDep,
    symbol: str = "BTC",
    interval: str = "1d",
    limit: int = Query(365, ge=1, le=5000),
) -> dict[str, Any]:
    return await service.candles(symbol, interval, limit)


@router.get("/indicators", summary="技术指标")
async def indicators(service: MarketServiceDep, symbol: str = "BTC", interval: str = "1d",
                     limit: int = Query(500, ge=50, le=5000)) -> dict[str, Any]:
    return await service.indicators(symbol, interval, limit)


@router.get("/analysis", summary="估值 / 周期 / 风险 / 综合状态")
async def analysis(service: MarketServiceDep) -> dict[str, Any]:
    return await service.analysis()


@router.get("/sentiment", summary="情绪指标")
async def sentiment(service: MarketServiceDep) -> dict[str, Any]:
    return await service.sentiment()


@router.get("/derivatives", summary="衍生品（资金费率 / 持仓量）")
async def derivatives(service: MarketServiceDep) -> dict[str, Any]:
    return await service.derivatives()


@router.get("/onchain", summary="链上数据")
async def onchain(service: MarketServiceDep) -> dict[str, Any]:
    return await service.onchain()


@router.get("/macro", summary="宏观数据")
async def macro(service: MarketServiceDep) -> dict[str, Any]:
    return await service.macro()


@router.get("/etf", summary="ETF 资金流（无数据源时明确告知）")
async def etf(service: MarketServiceDep) -> dict[str, Any]:
    return await service.etf()


@router.get("/forecast", summary="概率预测（样本不足时拒绝输出）")
async def forecast(service: MarketServiceDep, horizon: int = Query(30, ge=7, le=365)) -> dict[str, Any]:
    return await service.forecast(horizon)


@router.get("/timeline", summary="市场事件时间线")
async def timeline(limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    from sqlalchemy import select

    factory = get_session_factory()
    async with factory() as session:
        rows = (await session.execute(select(TimelineEvent).order_by(TimelineEvent.ts).limit(limit))).scalars().all()
    return {
        "count": len(rows),
        "events": [
            {"date": r.date, "type": r.event_type, "title": r.title_cn, "impact": r.impact, "source": r.source}
            for r in rows
        ],
    }


@router.post("/plan/{plan_id}/simulate-plan", summary="按当前数据模拟我的资金计划")
async def simulate_plan(
    plan_id: int,
    service: MarketServiceDep,
    _write: WriteDep,
    start_date: str | None = None,
    end_date: str | None = None,
    validation_mode: str = "insample",
) -> dict[str, Any]:
    """用真实历史数据回测用户的资金计划（防未来数据泄漏）。"""
    from sqlalchemy import select

    factory = get_session_factory()
    async with factory() as session:
        plan = await session.get(UserPlan, plan_id)
        if not plan:
            raise AppError("计划不存在", code="not_found")

    config = BacktestConfig(
        initial_capital=plan.initial_capital,
        monthly_contribution=plan.monthly_contribution,
        weekly_contribution=plan.weekly_contribution,
        contribution_frequency=plan.contribution_frequency,
        start_date=start_date or plan.start_date or "",
        end_date=end_date or plan.end_date or "",
        strategy_code=plan.strategy_code,
        strategy_params=plan.strategy_params or {},
        max_single_contribution=plan.max_single_contribution,
        cash_reserve=plan.cash_reserve,
        validation_mode=validation_mode,
        name=plan.name,
    )
    engine = BacktestEngine()
    result = await engine.run(config)
    result["plan"] = {"id": plan.id, "name": plan.name, "currency": plan.currency}
    result["generated_at"] = datetime.now(timezone.utc).isoformat()
    return result
