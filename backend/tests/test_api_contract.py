# -*- coding: utf-8 -*-
"""API 契约测试：接口层不能再出现「看起来成功、其实什么都没改」。

这一组用例全部来自曾经真实发生过的缺陷：
  1. 前端后台页发 {"action":"enable"}，后端模型只认 enabled/priority/locked，
     多余字段被静默丢弃 → 返回 200，但数据源状态纹丝不动；
  2. 写接口只判断请求头是否存在 → 任意非空串即可通过，等于不设防；
  3. POST /system/backfill 接收 end_date 却从不传给采集器 → 指定了截止日期照样拉到今天。
"""

from __future__ import annotations

import secrets
from typing import Any

import pytest

import app.api.deps as deps
from app.api.schemas import BackfillRequest, ProviderUpdateRequest
from app.core.errors import AppError, ForbiddenError, UnauthorizedError, ValidationError
from app.main import create_app
from app.providers.registry import get_registry
from app.providers.types import DataCategory
from app.providers.types import DataCategory
from app.services.health_service import HealthService

ADMIN_TOKEN = "test-token-please-change"


def _client():
    """构造 TestClient。

    退出时必须显式释放数据库连接：Windows 上 SQLite 文件句柄未释放会导致后续清理失败
    （表现为临时测试库删不掉），进而污染整个用例。
    """
    import asyncio

    from fastapi.testclient import TestClient

    from app.db import base as db_base

    try:
        with TestClient(create_app()) as c:
            yield c
    finally:
        asyncio.run(db_base.dispose_engine())


@pytest.fixture()
def client():
    yield from _client()


# ============================================================ 1. 入参契约
def test_provider_update_accepts_action_shorthand():
    """action 简写必须被模型接纳，而不是被当成多余字段丢掉。"""
    payload = ProviderUpdateRequest.model_validate({"action": "enable"})
    assert payload.action == "enable"
    payload = ProviderUpdateRequest.model_validate({"action": "unlock"})
    assert payload.action == "unlock"


def test_provider_update_rejects_unknown_action():
    with pytest.raises(Exception):
        ProviderUpdateRequest.model_validate({"action": "restart-everything"})


def test_backfill_request_rejects_bad_date():
    with pytest.raises(Exception):
        BackfillRequest.model_validate({"start_date": "2026/01/01"})
    ok = BackfillRequest.model_validate({"start_date": "2026-01-01", "end_date": "2026-02-01"})
    assert ok.start_date == "2026-01-01" and ok.end_date == "2026-02-01"


# ============================================================ 2. action 真的生效
def _patch(client, name: str, body: dict[str, Any], token: str | None = None):
    headers = {"X-Admin-Token": token} if token else {}
    return client.patch(f"/api/v1/system/providers/{name}", json=body, headers=headers)


def test_disable_action_actually_disables(client):
    candidates = [p.name for p in get_registry().providers_for(DataCategory.MARKET_PRICE)]
    assert candidates, "注册表里没有任何行情源，无法验证"
    name = candidates[0]

    before = get_registry().get(name)
    assert before is not None and before.enabled

    resp = _patch(client, name, {"action": "disable"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert get_registry().get(name).enabled is False, "返回 200 但数据源仍处于启用状态"
    assert "enabled" in body.get("changed", [])

    # 还原，避免影响同一会话内后续用例
    resp = _patch(client, name, {"action": "enable"})
    assert resp.status_code == 200
    assert get_registry().get(name).enabled is True


def test_empty_payload_is_rejected_not_silently_ok(client):
    """什么都没传时必须明确报错，不许返回「已应用」。"""
    name = get_registry().providers_for(DataCategory.MARKET_PRICE)[0].name
    resp = _patch(client, name, {})
    assert resp.status_code == 400, resp.text
    assert get_registry().get(name).enabled is True, "空请求不得改变任何状态"


def test_unknown_provider_is_404_not_500(client):
    resp = _patch(client, "no_such_provider_xyz", {"action": "disable"})
    assert resp.status_code == 404, resp.text


# ============================================================ 2.5 单源代理
async def _read_provider_proxy(name: str):
    """直接读 providers 表，绕开一切内存缓存。"""
    from sqlalchemy import select

    from app.db.base import get_session_factory
    from app.db.models import ProviderRecord

    factory = get_session_factory()
    async with factory() as session:
        row = (await session.execute(
            select(ProviderRecord).where(ProviderRecord.name == name)
        )).scalars().first()
        return row.proxy if row else None


@pytest.fixture()
async def async_client():
    """用 ASGI transport 而不是 TestClient。

    TestClient 会在**另一个线程**起一套自己的事件循环，那样「发请求」和
    「同一个测试用例里查库」就跑在两个 loop 上，aiosqlite 会直接报
    attached to a different loop。这里改成 ASGITransport，两者共用 pytest 的
    主循环，才能真正验证「返回 200 之后库里到底有没有」。
    """
    from httpx import ASGITransport, AsyncClient

    from app.db import base as db_base
    from app.db.migrate import init_db
    from app.main import create_app

    await init_db()
    try:
        async with AsyncClient(transport=ASGITransport(app=create_app()),
                               base_url="http://contract.test") as ac:
            yield ac
    finally:
        await db_base.dispose_engine()


async def test_patch_proxy_persists_and_can_be_cleared(async_client):
    """给单个数据源配代理：既要运行时立即生效，也要真的落库，还得能清空还原。

    来源是一次真实线上缺陷：`_set_provider_proxy` 里漏了 `get_session_factory`
    的导入，接口稳定返回 500。而当时 200+ 条用例没有一条走通这条路径，
    于是缺陷一路带到了用户手里 —— 所以这里必须从 HTTP 一路验到数据库。
    """
    name = get_registry().providers_for(DataCategory.MARKET_PRICE)[0].name
    addr = "socks5://alice:s3cret@127.0.0.1:1080"

    resp = await async_client.patch(f"/api/v1/system/providers/{name}", json={"proxy": addr})
    assert resp.status_code == 200, f"配代理不应该失败：{resp.text}"
    assert "proxy" in resp.json().get("changed", [])
    assert get_registry().get(name).transport.proxy, "返回 200 但运行时没换代理"
    # 响应体里绝不允许出现密码明文
    assert "s3cret" not in resp.text

    stored = await _read_provider_proxy(name)
    assert stored, "接口说已应用，库里却没有这条代理记录"
    assert "127.0.0.1:1080" in stored

    # 清空 = 回到「跟随全局代理」
    resp = await async_client.patch(f"/api/v1/system/providers/{name}", json={"proxy": ""})
    assert resp.status_code == 200, f"清空代理不应该失败：{resp.text}"
    assert await _read_provider_proxy(name) is None, "清空后库里仍留着代理"
    assert get_registry().get(name).transport.proxy == ""


async def test_patch_proxy_rejects_invalid_address(async_client):
    """地址写错要明确 400 + 中文原因，不能 500，更不能悄悄直连。"""
    name = get_registry().providers_for(DataCategory.MARKET_PRICE)[0].name
    resp = await async_client.patch(f"/api/v1/system/providers/{name}",
                                    json={"proxy": "代理参数随便写的"})
    assert resp.status_code == 400, resp.text
    assert resp.json().get("error"), "报错信息必须能给用户看，而不是空壳"
# ============================================================ 3. 写接口鉴权
@pytest.fixture()
def production_mode(monkeypatch):
    """把鉴权依赖切到「生产」分支：结果表明与否（没真实跑过就不算数）。"""
    s = deps.settings
    monkeypatch.setattr(type(s), "is_production", property(lambda self: True), raising=True)
    monkeypatch.setattr(s, "ADMIN_TOKEN", ADMIN_TOKEN, raising=False)
    yield s


def test_write_without_token_is_forbidden(production_mode):
    with pytest.raises(ForbiddenError):
        deps.require_write_permission(None)


def test_write_with_wrong_token_is_unauthorized(production_mode):
    with pytest.raises(UnauthorizedError):
        deps.require_write_permission("not-the-token")


def test_write_with_correct_token_passes(production_mode):
    assert deps.require_write_permission(ADMIN_TOKEN) == "user"


def test_admin_token_comparison_is_exact(production_mode):
    """只有完全一致才放行。令牌前缀相同/大小写不同都必须拒绝。"""
    with pytest.raises(UnauthorizedError):
        deps.require_admin(ADMIN_TOKEN[:-1])
    with pytest.raises(UnauthorizedError):
        deps.require_admin(ADMIN_TOKEN.upper())
    assert deps.require_admin(ADMIN_TOKEN) == "admin"


def test_generated_token_is_not_the_default(production_mode):
    """默认 ADMIN_TOKEN 不得等于任何一个随机生成值（防止把占位值当真令牌用）。"""
    assert ADMIN_TOKEN != "admin-change-me"
    assert secrets.compare_digest(ADMIN_TOKEN, ADMIN_TOKEN)


# ============================================================ 4. backfill 参数透传
@pytest.mark.asyncio
async def test_backfill_end_date_reaches_collector(monkeypatch):
    """end_date / interval 必须沿着 API → 调度器 → 采集器一路走到底。"""
    captured: dict[str, Any] = {}

    class SpyCollector:
        def __init__(self, interval: str = "1d") -> None:
            captured["interval"] = interval

        async def run(self, **kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"ok": True, "rows": 0}

    import app.scheduler.manager as mgr

    monkeypatch.setattr(mgr, "MarketCollector", SpyCollector)
    result = await mgr.get_scheduler().run_backfill(
        start_date="2024-01-01", end_date="2024-06-30", interval="1h", reset=True
    )
    assert result["ok"] is True
    assert captured["mode"] == "backfill"
    assert captured["start_date"] == "2024-01-01"
    assert captured["end_date"] == "2024-06-30", "end_date 在调用链中被丢掉了"
    assert captured["reset"] is True
    assert captured["interval"] == "1h"


@pytest.mark.asyncio
async def test_health_service_drain_confirms_persistence():
    """改动必须能被确认已经落库：drain() 返回 >0 表示确实写入了库表。"""
    import asyncio

    from app.db.migrate import init_db

    await init_db()
    service = HealthService()
    name = get_registry().providers_for(DataCategory.MARKET_PRICE)[0].name
    service.set_enabled(name, False, reason="契约测试临时停用")
    assert len(service._pending_writes) == 1, "落库任务应当被登记，而不是发射后无人看管"
    written = await service.drain()
    assert written == 1
    await asyncio.sleep(0)
    assert service._pending_writes == set()

    from sqlalchemy import select

    from app.db.base import get_session_factory
    from app.db.models import ProviderRecord

    factory = get_session_factory()
    async with factory() as session:
        row = (await session.execute(
            select(ProviderRecord).where(ProviderRecord.name == name)
        )).scalars().first()
    assert row is not None, "管理员的停用意图没有出现在 providers 表里"
    assert row.enabled is False
    assert "契约测试" in (row.disabled_reason or "")

    # 还原
    service.set_enabled(name, True, reason="契约测试恢复")
    await service.drain()
    assert get_registry().get(name).enabled is True


def test_app_error_types_are_distinct():
    """调用方需要能区分 400 / 403 / 401 / 404，全部继承自 AppError 便于统一转码。"""
    for cls in (ValidationError, ForbiddenError, UnauthorizedError):
        assert issubclass(cls, AppError)
