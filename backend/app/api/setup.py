# -*- coding: utf-8 -*-
"""首次启动的安装引导 API。

三个接口构成一个闭环：

    GET  /system/setup/status     —— 当前装没装、支持哪几种数据库、缺什么驱动
    POST /system/setup/test       —— 拿用户填的参数真连一次，逐项报告（不写任何东西）
    POST /system/setup/complete   —— 自检通过才落盘 `.env`，随后就地完成初始化

分工上的关键取舍：**测试与保存是两个接口**。
合并成一个（「保存并测试」）看起来省一次点击，但用户会得到一半的诗区：
配置写进去了、服务却起不来。拆开之后，保存的前提一定是刚测过且通过了。
"""

from __future__ import annotations

import platform
from typing import Any

from fastapi import APIRouter, Request

from ..core.config import get_settings
from ..core.errors import ValidationError
from ..core.setup import complete_setup, needs_setup, probe_database
from ..core import setup as setup_core
from ..db.base import current_dialect, normalize_url
from ..db.dialects import (
    build_url,
    describe,
    is_driver_installed,
    mask_url,
    supported_dialects,
)

router = APIRouter(prefix="/setup", tags=["安装引导"])

_TIMEOUT_CAP = 30.0


# ---------------------------------------------------------------- 参数 -> 连接串


def url_from_spec(body: dict[str, Any]) -> str:
    """把前端填的结构化字段拼成连接串，或直接采信用户粘贴的完整地址。

    两种输入都支持是有必要的：向导里引导用戶按格子填（不容易写错），
    而老手往往手上已经有一条现成的连接串，让他再拆一遍纯属折腾。
    """
    explicit = str(body.get("database_url") or "").strip()
    if explicit:
        return explicit

    dialect = str(body.get("dialect") or "sqlite").strip().lower()
    if dialect == "sqlite":
        return build_url("sqlite", file_path=str(body.get("file_path") or "data/btc.db"))
    return build_url(
        dialect,
        host=str(body.get("host") or ""),
        port=str(body.get("port") or ""),
        database=str(body.get("database") or ""),
        username=str(body.get("username") or ""),
        password=str(body.get("password") or ""),
        sslmode=str(body.get("sslmode") or ""),
    )


def _failure_report(message: str, *, dialect: str | None = None) -> dict[str, Any]:
    """参数本身就有问题时，也按「自检报告」的形状返回。

    前端只有一套渲染逻辑，这里如果抛 422，用户看到的是一句 JSON 错误而不是
    「哪一项没填对」，排障成本立刻翻倍。
    """
    return {
        "ok": False,
        "checks": [{"name": "连接参数", "ok": False, "detail": message}],
        "hint": message,
        "summary": message,
        "dialect": dialect or "",
        "masked_url": "",
        "latency_ms": 0.0,
        "server_version": None,
        "existing_tables": 0,
        "expected_tables": 0,
        "writable": False,
    }


# ---------------------------------------------------------------- 接口


@router.get("/status", summary="安装引导：当前状态")
async def setup_status() -> dict[str, Any]:
    """告诉前端「要不要进引导页」，以及引导页需要展示些什么。"""
    s = get_settings()
    completed = not needs_setup(s)
    masked = ""
    try:
        masked = mask_url(normalize_url(s.DATABASE_URL, s))
    except Exception:  # noqa: BLE001 - 连接串写坏时不能连状态接口一起挂掉
        masked = "(当前连接串无法解析)"

    return {
        "completed": completed,
        "needs_setup": not completed,
        "current_dialect": current_dialect(s),
        "current_url_masked": masked,
        "env_file": str(setup_core.ENV_PATH),
        "env_file_exists": setup_core.ENV_PATH.exists(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "dialects": supported_dialects(),
        "note": (
            "SQLite 是本机文件，多台机器各存一份；"
            "要让多台设备看到完全一致的数据，请选 PostgreSQL 或 MySQL。"
        ),
    }


@router.post("/test", summary="安装引导：连接自检（不写入任何配置）")
async def test_connection(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """拿用户刚填的参数真连一次数据库。

    这一步刻意不落盘：装机的人需要在**确认**之前反复试，写早了反而成了负担。
    """
    body = payload or {}
    try:
        url = url_from_spec(body)
    except ValueError as exc:
        return _failure_report(str(exc), dialect=str(body.get("dialect") or ""))

    try:
        timeout = float(body.get("timeout") or 8.0)
    except (TypeError, ValueError):
        timeout = 8.0
    timeout = min(max(timeout, 1.0), _TIMEOUT_CAP)

    result = await probe_database(url, timeout=timeout)
    result["database_url"] = ""  # 绝不回传原始连接串：里面可能有明文口令
    return result


@router.post("/complete", summary="安装引导：自检通过后写入配置并完成初始化")
async def finish_setup(payload: dict[str, Any] | None = None,
                       request: Request = None) -> dict[str, Any]:
    """落盘 `.env` 并在本进程里立即完成初始化，无需重启。

    只在「尚未安装」时允许执行 —— 已经装好的系统不允许通过 API 改数据库，
    那等于给任何人留了一个后门（把数据导到他自己的库里也不难）。
    """
    s = get_settings()
    if not needs_setup(s):
        raise ValidationError(
            "已经完成安装了。如需更换数据库，请编辑配置文件 "
            f"{setup_core.ENV_PATH} 里的 DATABASE_URL 后重启服务。"
        )
    # 生产环境下多一层管理令牌校验：本机向导无所谓，暴露出去的实例必须有门槛。
    if s.is_production:
        from .deps import require_admin

        require_admin((request.headers.get("x-admin-token") if request else None))

    body = payload or {}
    try:
        url = url_from_spec(body)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc

    return await complete_setup(url)


@router.get("/dialects", summary="安装引导：各数据库的驱动就绪情况")
async def list_dialects() -> dict[str, Any]:
    return {
        "count": 3,
        "asyncpg_installed": is_driver_installed("postgresql"),
        "aiomysql_installed": is_driver_installed("mysql"),
        "dialects": supported_dialects(),
        "recommended": describe("postgresql") if is_driver_installed("postgresql") else describe("sqlite"),
    }
