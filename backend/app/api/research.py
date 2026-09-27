# -*- coding: utf-8 -*-
"""回测 / 回放 / 策略实验室 / 模型实验室 API。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from ..db.base import get_session_factory
from ..db.models import IndicatorDefinition, ModelVersion, Signal
from ..services import BacktestConfig, BacktestEngine, ReplayService
from .deps import RequestIdDep, WriteDep
from .schemas import BacktestRequest, ReplayRequest

router = APIRouter()


@router.post("/backtest", summary="运行策略回测")
async def run_backtest(payload: BacktestRequest, _write: WriteDep, rid: RequestIdDep) -> dict[str, Any]:
    engine = BacktestEngine()
    config = BacktestConfig(**payload.model_dump())
    return await engine.run(config)


@router.get("/backtest/runs", summary="历史回测记录（含模型版本与数据版本）")
async def list_runs(limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    engine = BacktestEngine()
    return {"runs": await engine.list_runs(limit)}


@router.get("/strategies", summary="策略清单与参数说明")
async def list_strategies() -> dict[str, Any]:
    from ..services.strategies import list_strategies as _list

    return {"strategies": _list()}


@router.post("/replay", summary="历史回放（严格防止未来数据泄漏）")
async def replay(payload: ReplayRequest, rid: RequestIdDep) -> dict[str, Any]:
    service = ReplayService()
    return await service.replay(payload.date, payload.perspective)


@router.get("/replay", summary="历史回放（GET 便捷方式）")
async def replay_get(date: str = Query(..., min_length=8, max_length=10),
                     perspective: str = Query("then", pattern="^(then|aftermath)$"),
                     rid: RequestIdDep = "") -> dict[str, Any]:
    service = ReplayService()
    return await service.replay(date, perspective)


@router.get("/indicators/dictionary", summary="指标字典（通俗解释）")
async def indicator_dictionary(category: str | None = None) -> dict[str, Any]:
    from sqlalchemy import select

    factory = get_session_factory()
    async with factory() as session:
        stmt = select(IndicatorDefinition)
        if category:
            stmt = stmt.where(IndicatorDefinition.category == category)
        rows = (await session.execute(stmt)).scalars().all()
    return {
        "count": len(rows),
        "indicators": [
            {
                "code": r.code,
                "name_cn": r.name_cn,
                "name_en": r.name_en,
                "category": r.category,
                "meaning_cn": r.meaning_cn,
                "how_to_use": r.how_to_use,
                "formula": r.formula,
                "formula_readable": r.formula_readable,
                "unit": r.unit,
                "value_range": r.value_range,
                "requires_paid_provider": r.requires_paid_provider,
            }
            for r in rows
        ],
    }


@router.get("/indicators/{code}/explain", summary="单个指标的可追溯解释")
async def explain_indicator(code: str) -> dict[str, Any]:
    """返回指标的含义、数据来源、计算方式，供前端「为什么」展开使用。"""
    from sqlalchemy import select

    factory = get_session_factory()
    async with factory() as session:
        row = (
            await session.execute(select(IndicatorDefinition).where(IndicatorDefinition.code == code))
        ).scalars().first()
    if not row:
        return {"available": False, "message": f"未找到指标 {code} 的定义"}
    return {
        "available": True,
        "code": row.code,
        "name_cn": row.name_cn,
        "meaning_cn": row.meaning_cn,
        "how_to_use": row.how_to_use,
        "formula": row.formula,
        "formula_readable": row.formula_readable,
        "bullish_hint": row.bullish_hint,
        "bearish_hint": row.bearish_hint,
        "unit": row.unit,
        "requires_paid_provider": row.requires_paid_provider,
    }


@router.get("/models", summary="模型版本列表（开发区间/测试区间/有效条件/失效条件）")
async def list_models() -> dict[str, Any]:
    """每个模型都必须能回答：用了什么参数、在什么数据上开发、有没有样本外检验、
    什么情况下结论可信、什么情况下会失效。做不到就该写「未进行」，不允许含糊带过。"""
    from sqlalchemy import select

    from ..db.models import ModelVersion, ModelWeight

    factory = get_session_factory()
    async with factory() as session:
        rows = (await session.execute(select(ModelVersion).order_by(ModelVersion.id))).scalars().all()
        weights = (await session.execute(select(ModelWeight))).scalars().all()

    by_model: dict[str, list[dict[str, Any]]] = {}
    for w in weights:
        by_model.setdefault(w.model_code, []).append(
            {"factor": w.factor, "weight": w.weight, "rationale": w.rationale}
        )
    return {
        "count": len(rows),
        "models": [
            {
                "code": r.code,
                "name": r.name,
                "kind": r.kind,
                "params": r.params,
                "dev_period": r.dev_period,
                "test_period": r.test_period,
                "metrics": r.metrics,
                "active": r.active,
                "notes": r.notes,
                "weights": by_model.get(r.code, []),
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
        "disclaimer": "模型不能只看历史收益。这里公开每个模型的开发区间、参数与失效条件；"
                      "标注「未进行样本外检验」的模型不得作为决策依据，只能作为描述工具。",
    }


@router.get("/signals", summary="信号定义")
async def list_signals() -> dict[str, Any]:
    from sqlalchemy import select

    factory = get_session_factory()
    async with factory() as session:
        rows = (await session.execute(select(Signal))).scalars().all()
    return {
        "signals": [
            {"code": r.code, "name_cn": r.name_cn, "category": r.category,
             "direction": r.direction, "description": r.description}
            for r in rows
        ]
    }
