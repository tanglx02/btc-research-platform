# -*- coding: utf-8 -*-
"""API 请求/响应模型。所有输入在边界处校验。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class BacktestRequest(BaseModel):
    name: str = ""
    strategy_code: str = "dca_fixed"
    strategy_params: dict[str, Any] = Field(default_factory=dict)
    initial_capital: float = 0.0
    monthly_contribution: float = 0.0
    weekly_contribution: float = 0.0
    contribution_frequency: str = "monthly"
    start_date: str = ""
    end_date: str = ""
    fee_rate: float = 0.001
    slippage: float = 0.0005
    max_single_contribution: float = 0.0
    cash_reserve: float = 0.0
    validation_mode: str = "insample"
    oos_split: float = 0.3

    @field_validator("fee_rate", "slippage")
    @classmethod
    def _non_negative_ratio(cls, v: float) -> float:
        if v < 0 or v > 0.1:
            raise ValueError("费率/滑点必须在 0 ~ 0.1 之间")
        return v

    @field_validator("initial_capital", "monthly_contribution", "weekly_contribution")
    @classmethod
    def _non_negative_money(cls, v: float) -> float:
        if v < 0:
            raise ValueError("金额不能为负数")
        return v


class PlanRequest(BaseModel):
    name: str = "我的 BTC 计划"
    currency: str = "CNY"
    initial_capital: float = 0.0
    monthly_income: float = 0.0
    monthly_contribution: float = 0.0
    weekly_contribution: float = 0.0
    contribution_frequency: str = "monthly"
    start_date: str = ""
    end_date: str | None = None
    cash_reserve: float = 0.0
    max_single_contribution: float = 0.0
    max_drawdown_tolerance: float = 50.0
    strategy_code: str = "dca_fixed"
    strategy_params: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = None


class TransactionRequest(BaseModel):
    date: str
    price: float
    amount_btc: float = 0.0
    amount_fiat: float = 0.0
    fee: float = 0.0
    side: str = "buy"
    currency: str = "CNY"
    note: str | None = None
    source: str = "manual"

    @field_validator("price")
    @classmethod
    def _positive_price(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("价格必须大于 0")
        return v


class ReplayRequest(BaseModel):
    date: str
    perspective: str = "then"


class AssistantAskRequest(BaseModel):
    question: str = Field(..., min_length=2, max_length=1000)
    context_module: str | None = None


class ProviderUpdateRequest(BaseModel):
    """数据源更新入参。同时支持两种写法：

    1. 显式字段：``{"enabled": true}`` / ``{"locked": false}`` / ``{"priority": 3}``
    2. 动作简写：``{"action": "enable" | "disable" | "unlock"}``

    历史缺陷记录：前端「后台管理」页一直发送 ``{"action": "enable"}``，而本模型原来只有
    ``enabled/priority/locked/reason``，多余字段被静默丢弃 —— 接口返回 200，实际一个字段都没改。
    后果是管理员以为已停用某数据源，流量却照旧打过去，且无人察觉。因此这里显式声明 action
    字段并由路由归一化，避免同类「看起来成功、实则空操作」的问题。
    """

    enabled: bool | None = None
    priority: int | None = None
    locked: bool | None = None
    reason: str = ""
    action: Literal["enable", "disable", "unlock"] | None = None
    # 单个数据源独立走代理（`providers.proxy` 列早在第一版就有，但一直没接出来）。
    # 用法：只给某个数据源配代理，其余仍直连；留空 = 跟随全局代理设置。
    proxy: str | None = None


class BackfillRequest(BaseModel):
    start_date: str | None = None
    end_date: str | None = None
    interval: str = "1d"
    reset: bool = False

    @field_validator("start_date", "end_date")
    @classmethod
    def _date_format(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        from datetime import datetime

        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("日期必须是 YYYY-MM-DD 格式") from exc
        return v
