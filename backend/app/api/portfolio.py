# -*- coding: utf-8 -*-
"""个人资金计划与资产账本 API。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from .deps import PlanServiceDep, RequestIdDep, WriteDep
from .schemas import PlanRequest, TransactionRequest

router = APIRouter()


@router.get("/plans", summary="计划列表")
async def list_plans(service: PlanServiceDep) -> dict[str, Any]:
    return {"plans": await service.list()}


@router.post("/plans", summary="创建资金计划")
async def create_plan(payload: PlanRequest, service: PlanServiceDep, _write: WriteDep, rid: RequestIdDep) -> dict[str, Any]:
    return await service.create(payload.model_dump())


@router.get("/plans/{plan_id}", summary="计划详情")
async def get_plan(plan_id: int, service: PlanServiceDep) -> dict[str, Any]:
    plan = await service.get(plan_id)
    return plan or {"available": False, "message": "计划不存在"}


@router.patch("/plans/{plan_id}", summary="更新计划")
async def update_plan(plan_id: int, payload: PlanRequest, service: PlanServiceDep, _write: WriteDep) -> dict[str, Any]:
    updated = await service.update(plan_id, payload.model_dump(exclude_unset=True))
    return updated or {"available": False, "message": "计划不存在"}


@router.delete("/plans/{plan_id}", summary="删除计划（连同该计划的交易流水一起删除）")
async def delete_plan(plan_id: int, service: PlanServiceDep, _write: WriteDep) -> dict[str, Any]:
    result = await service.delete(plan_id)
    if not result["deleted"]:
        return {"available": False, "message": "计划不存在"}
    return {"available": True, **result}


@router.get("/plans/{plan_id}/next", summary="下次投入时间与建议金额")
async def next_contribution(plan_id: int, service: PlanServiceDep) -> dict[str, Any]:
    return await service.next_contribution(plan_id)


@router.get("/plans/{plan_id}/snapshot", summary="生成资产快照")
async def snapshot(plan_id: int, service: PlanServiceDep, _write: WriteDep) -> dict[str, Any]:
    return await service.snapshot(plan_id)


@router.get("/strategies", summary="可用定投策略")
async def strategies(service: PlanServiceDep) -> dict[str, Any]:
    return {"strategies": service.strategies()}


# ---------------------------------------------------------------- 账本
@router.get("/transactions", summary="交易记录")
async def list_transactions(
    service: PlanServiceDep, plan_id: int | None = None, limit: int = Query(500, ge=1, le=2000)
) -> dict[str, Any]:
    return {"transactions": await service.transactions(plan_id, limit)}


@router.post("/transactions/{plan_id}", summary="新增一笔交易")
async def add_transaction(
    plan_id: int, payload: TransactionRequest, service: PlanServiceDep, _write: WriteDep
) -> dict[str, Any]:
    return await service.add_transaction(plan_id, payload.model_dump())


@router.delete("/transactions/{tx_id}", summary="删除一笔交易")
async def delete_transaction(tx_id: int, service: PlanServiceDep, _write: WriteDep) -> dict[str, Any]:
    return {"deleted": await service.delete_transaction(tx_id)}


@router.get("/holdings", summary="持仓、平均成本与浮盈亏")
async def holdings(
    service: PlanServiceDep,
    plan_id: int | None = None,
    usd_to_plan_rate: float | None = Query(
        None, gt=0,
        description="1 USD 折合计划币种的汇率。不传则不做换算，并在响应里标注 mixed_currency（系统没有汇率数据源，不会臆造汇率）",
    ),
) -> dict[str, Any]:
    return await service.holdings(plan_id, usd_to_plan_rate=usd_to_plan_rate)
