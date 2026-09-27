# -*- coding: utf-8 -*-
"""API 依赖：request_id 贯穿、管理员鉴权、服务实例获取。"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, Header, Request

from ..core.config import get_settings
from ..core.errors import ForbiddenError, UnauthorizedError
from ..core.logging import new_request_id, request_id_var
from ..services import HealthService, MarketService, PlanService, ReplayService

settings = get_settings()


async def bind_request_id(request: Request) -> str:
    rid = request.headers.get("X-Request-ID") or new_request_id()
    request_id_var.set(rid)
    request.state.request_id = rid
    return rid


def require_admin(x_admin_token: Annotated[str | None, Header()] = None) -> str:
    """管理员操作鉴权。生产环境必须设置 ADMIN_TOKEN。

    与 require_write_permission 保持一致：常量时间比对，避免时序差异泄露令牌前缀。
    """
    if not settings.is_production:
        return "dev"
    if not x_admin_token:
        raise ForbiddenError("生产环境下管理操作需要鉴权")
    if not secrets.compare_digest(str(x_admin_token), str(settings.ADMIN_TOKEN)):
        raise UnauthorizedError("管理员令牌不正确")
    return "admin"


def require_write_permission(x_admin_token: Annotated[str | None, Header()] = None) -> str:
    """写操作（计划/交易/回测）鉴权：生产环境需要令牌，开发环境放开便于本地使用。

    修复说明：早期版本只判断「请求头是否存在」，任意非空字符串都能通过，
    等于写接口完全不设防。这里改为与 ADMIN_TOKEN 常量时间比对。
    """
    if not settings.is_production:
        return "dev"
    if not x_admin_token:
        raise ForbiddenError("生产环境下写操作需要鉴权")
    # 恒定时间比对，避免时序侧信道；不相等即拒绝，不做任何"宽松通过"
    if not secrets.compare_digest(str(x_admin_token), str(settings.ADMIN_TOKEN)):
        raise UnauthorizedError("管理员令牌不正确")
    return "user"


RequestIdDep = Annotated[str, Depends(bind_request_id)]
AdminDep = Annotated[str, Depends(require_admin)]
WriteDep = Annotated[str, Depends(require_write_permission)]


def get_market_service() -> MarketService:
    return MarketService()


def get_health_service() -> HealthService:
    return HealthService()


def get_plan_service() -> PlanService:
    return PlanService()


def get_replay_service() -> ReplayService:
    return ReplayService()


MarketServiceDep = Annotated[MarketService, Depends(get_market_service)]
HealthServiceDep = Annotated[HealthService, Depends(get_health_service)]
PlanServiceDep = Annotated[PlanService, Depends(get_plan_service)]
ReplayServiceDep = Annotated[ReplayService, Depends(get_replay_service)]
