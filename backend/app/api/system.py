# -*- coding: utf-8 -*-
"""系统、数据源健康与后台管理 API。"""

from __future__ import annotations

import platform
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Query

from ..core.config import get_settings
from ..core.errors import NotFoundError, ValidationError
from ..db.migrate import table_stats
from ..providers.registry import get_registry
from ..scheduler.manager import get_scheduler
from .deps import AdminDep, HealthServiceDep, RequestIdDep
from .schemas import BackfillRequest, ProviderUpdateRequest

router = APIRouter()
settings = get_settings()


@router.get("/health", summary="健康检查")
async def health() -> dict[str, Any]:
    from ..db.base import check_connection

    db_ok = await check_connection()
    return {
        "status": "ok" if db_ok else "degraded",
        "app": settings.APP_NAME,
        "env": settings.APP_ENV,
        "version": "1.0.0",
        "python": platform.python_version(),
        "platform": platform.platform(),
        "database": "connected" if db_ok else "disconnected",
        "time": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/ready", summary="就绪检查（含 provider 就绪情况）")
async def ready() -> dict[str, Any]:
    from ..db.base import check_connection

    db_ok = await check_connection()
    registry = get_registry()
    available = sum(1 for p in registry.all() if p.enabled and p.is_configured())
    return {
        "ready": db_ok and available > 0,
        "database": db_ok,
        "available_providers": available,
        "total_providers": len(registry.all()),
    }


@router.get("/info", summary="系统信息（不含任何密钥）")
async def info() -> dict[str, Any]:
    return {
        "app": settings.APP_NAME,
        "env": settings.APP_ENV,
        "timezone": settings.TIMEZONE,
        "database_type": "sqlite" if settings.DATABASE_URL.startswith("sqlite") else "postgresql",
        "cache": "redis" if settings.REDIS_URL else "memory",
        "ai_assistant_enabled": settings.AI_ASSISTANT_ENABLED
        and bool(settings.OPENAI_API_KEY),
        "scheduler_enabled": settings.SCHEDULER_ENABLED,
        "providers": len(get_registry().all()),
        "note": "敏感配置（密钥/令牌）不会通过该接口返回。",
    }


# ---------------------------------------------------------------- 数据源中心
@router.get("/providers", summary="数据源清单与配置")
async def list_providers() -> dict[str, Any]:
    return {"count": len(get_registry().all()), "providers": get_registry().describe_all()}


@router.get("/providers/dashboard", summary="数据源健康总览")
async def providers_dashboard(service: HealthServiceDep) -> dict[str, Any]:
    return service.dashboard()


@router.get("/providers/categories", summary="每个数据类别的 Provider 链")
async def providers_categories(service: HealthServiceDep) -> dict[str, Any]:
    return service.categories()


@router.get("/providers/failovers", summary="自动故障切换事件")
async def failover_events(service: HealthServiceDep, limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    return {"events": await service.failover_events(limit)}


@router.post("/providers/test-all", summary="一键测试全部数据源")
async def test_all_providers(service: HealthServiceDep, _admin: AdminDep) -> dict[str, Any]:
    return await service.probe_all()


# ------------------------------------------------------------------ 配置中心
# 目标：所有配置都能在后台改，不必手工编辑 .env。
# GET  返回「元数据 + 当前值」，前端据此自动渲染表单（新增配置项不用改前端）；
# PUT  批量保存，保存后立刻写回 settings 单例，因此无需重启。
# 敏感项（密钥、密码）只写不读明文，列表只回传掩码与 has_value。


@router.get("/settings", summary="全部可配置项（含当前值与来源）")
async def list_settings() -> dict[str, Any]:
    from ..core.settings_store import get_settings_store

    store = get_settings_store()
    await store.load()
    return store.describe()


@router.put("/settings", summary="批量保存配置（保存后立即生效，无需重启）")
async def save_settings(payload: dict[str, Any], _admin: AdminDep) -> dict[str, Any]:
    from ..core.settings_store import get_settings_store

    if not payload:
        raise ValidationError("请求体为空，未做任何修改")
    unknown = [k for k in payload if k not in _settings_keys()]
    if unknown:
        raise ValidationError(
            f"未识别的配置项：{', '.join(sorted(unknown))}。"
            "请从 /system/settings 返回的 key 中选择。"
        )
    store = get_settings_store()
    result = await store.set_many(payload, updated_by="admin")
    result["note"] = "已保存并立即生效"
    return result


@router.post("/settings/reset", summary="把配置项恢复为跟随 .env / 默认值")
async def reset_settings(payload: dict[str, Any] | None = None, _admin: AdminDep = None) -> dict[str, Any]:
    from ..core.settings_store import get_settings_store

    keys = list((payload or {}).get("keys") or [])
    if not keys:
        raise ValidationError("请提供要恢复的 keys")
    unknown = [k for k in keys if k not in _settings_keys()]
    if unknown:
        raise ValidationError(f"未识别的配置项：{', '.join(sorted(unknown))}")
    store = get_settings_store()
    result = await store.reset(keys)
    result["note"] = "已恢复为跟随 .env / 默认值"
    return result


def _settings_keys() -> set[str]:
    from ..core.settings_store import SPEC_BY_KEY

    return set(SPEC_BY_KEY)


@router.get("/settings/keys", summary="可配置项 key 清单（供脚本/调试使用）")
async def list_setting_keys() -> dict[str, Any]:
    from ..core.settings_store import SETTINGS_SPEC

    return {
        "count": len(SETTINGS_SPEC),
        "keys": [
            {"key": s.key, "label": s.label, "category": s.category,
             "type": s.value_type, "sensitive": s.sensitive,
             "requires_restart": s.requires_restart}
            for s in SETTINGS_SPEC
        ],
    }


# ---------------------------------------------------------------- 代理


def _proxy_targets() -> list[dict[str, str]]:
    """默认探测目标：既有「国内直连大概率失败」的数据源，也有回显出口 IP 的中立站点。

    出口 IP 这一项很重要：它能证明「请求确实走了代理」，而不是碰巧直连成功。
    """
    return [
        {"key": "binance", "label": "Binance 现货（国内通常被墙）",
         "url": "https://api.binance.com/api/v3/ping"},
        {"key": "coinbase", "label": "Coinbase 现货",
         "url": "https://api.coinbase.com/v2/prices/BTC-USD/spot"},
        {"key": "kraken", "label": "Kraken 现货",
         "url": "https://api.kraken.com/0/public/Ticker?pair=XBTUSD"},
        {"key": "gemini", "label": "Gemini 现货",
         "url": "https://api.gemini.com/v1/pubticker/btcusd"},
        {"key": "bitfinex", "label": "Bitfinex 现货",
         "url": "https://api-pub.bitfinex.com/v2/ticker/tBTCUSD"},
        {"key": "coingecko", "label": "CoinGecko 市值（链宽只有 1 的类别）",
         "url": "https://api.coingecko.com/api/v3/ping"},
        {"key": "egress_ip", "label": "出口 IP（验证是否真的走了代理）",
         "url": "https://api.ipify.org?format=json"},
    ]


@router.get("/proxy", summary="当前生效的代理")
async def current_proxy() -> dict[str, Any]:
    from ..core.proxy import describe_proxy, resolve_preferred
    from ..core.settings_store import get_settings_store

    await get_settings_store().load()
    try:
        url = resolve_preferred(settings.SOCKS_PROXY, settings.HTTPS_PROXY, settings.HTTP_PROXY)
    except ValueError as exc:
        return {"configured": True, "invalid": True, "error": str(exc)}
    if not url:
        return {"configured": False, "note": "未配置代理，全部数据源直连"}
    info = describe_proxy(url)
    info["configured"] = True
    return info


@router.post("/proxy/test", summary="测试代理连通性（管理接口）")
async def test_proxy(payload: dict[str, Any] | None = None, _admin: AdminDep = None) -> dict[str, Any]:
    """拿一个代理地址，去真实请求几个数据源，逐个回报结果。

    目的很直接：配置代理这种事，不实际连一下谁也不知道配没配对。
    支持 `proxy_url` 省略时不带代理直连（用来对照「配之前」的基线）。
    """
    import time

    from ..core.errors import ProviderError
    from ..core.proxy import describe_proxy, normalize_proxy_url
    from ..core.settings_store import get_settings_store
    from ..providers.transport import ProviderTransport

    body = payload or {}
    raw_url = str(body.get("proxy_url") or "").strip()
    targets = list(body.get("targets") or []) or _proxy_targets()
    save = bool(body.get("save"))
    timeout = float(body.get("timeout") or 12.0)

    url = ""
    if raw_url:
        try:
            url = normalize_proxy_url(raw_url)
        except ValueError as exc:
            raise ValidationError(f"代理地址无法识别：{exc}")

    results: list[dict[str, Any]] = []
    # 直连对照时必须 allow_global_proxy=False：运行环境本身可能被注入 HTTP_PROXY，
    # 不强制绕过的话「对照组」其实也走了代理，看不出真实差距。
    transport = ProviderTransport(
        provider_name="proxy_test",
        proxy=url,
        timeout_connect=8.0,
        timeout_read=timeout,
        retries=0,
        qps=10.0,
        allow_global_proxy=bool(url),
    )
    try:
        for item in targets:
            started = time.perf_counter()
            entry: dict[str, Any] = {"key": item.get("key"), "label": item.get("label"),
                                     "url": item.get("url"), "ok": False}
            try:
                resp = await transport.request("GET", str(item.get("url")), label="proxy_test")
                elapsed = (time.perf_counter() - started) * 1000
                text = (resp.text or "")[:300]
                entry.update(ok=True, status=resp.status_code, latency_ms=round(elapsed, 1),
                             message=f"HTTP {resp.status_code}", preview=text)
            except ProviderError as exc:
                elapsed = (time.perf_counter() - started) * 1000
                entry.update(latency_ms=round(elapsed, 1), failure_type=exc.failure_type,
                             message=exc.message)
            except Exception as exc:  # 兜底：测试接口不能因为任何异常崩掉
                elapsed = (time.perf_counter() - started) * 1000
                failure_type, message = transport.classify(exc)
                entry.update(latency_ms=round(elapsed, 1), failure_type=failure_type, message=message)
            results.append(entry)
    finally:
        await transport.close()

    saved_as = None
    if save and url:
        key = "SOCKS_PROXY" if url.startswith(("socks",)) else "HTTPS_PROXY"
        await get_settings_store().load()
        await get_settings_store().set_many({key: url}, updated_by="admin")
        saved_as = key

    ok_count = sum(1 for r in results if r["ok"])
    return {
        "proxy": describe_proxy(url) if url else {"set": False},
        "mode": "使用指定代理" if url else "直连（对照基线）",
        "saved_as": saved_as,
        "targets_total": len(results),
        "targets_ok": ok_count,
        "results": results,
        "hint": None if ok_count else "全部失败：先看「出口 IP」那一项，若它也失败，说明代理本身就没连通。",
    }


@router.get("/dns/diagnose", summary="检测 DNS 是否被污染")
async def diagnose_dns(host: str = Query(..., min_length=3)) -> dict[str, Any]:
    """对比「系统 DNS」与「DoH 真实答案」，判断这个域名是否被污染。

    为什么要这个接口：国内环境下数据源最常见的症状是「时通时断」，
    用户通常会误以为是墙或者数据源挂了，真凶往往是本地 DNS 抢答污染。
    与其让人猜，直接把两边答案摆出来对比。
    """
    from ..core.dns_resolver import get_resolver

    host = host.strip().strip("/")
    if "://" in host:  # 允许直接粘贴 URL，容错比报错友好
        host = host.split("://", 1)[1].split("/")[0]

    resolver = get_resolver(endpoints=settings.DNS_DOH_ENDPOINTS.split(","),
                            ttl_seconds=float(settings.DNS_CACHE_TTL_SECONDS or 300))
    result = await resolver.diagnose(host)
    result["current_dns_mode"] = settings.DNS_MODE
    # 给一句能直接照做的结论，而不是只丢一堆 IP 让人自己判断
    if result["verdict"] in ("poisoned", "suspicious") and settings.DNS_MODE != "doh":
        result["action"] = "到「系统设置 → 网络与代理」把「DNS 解析方式」改为 DoH，保存后立即生效。"
    else:
        result["action"] = None
    # 不在这里 aclose：resolver 是进程级单例，可能与正在飞的请求共用同一个连接池。
    return result


async def _set_provider_proxy(service: Any, name: str, raw: str) -> dict[str, Any]:
    """给单个数据源配独立代理：立即生效 + 落库（重启后仍在）。

    留空（空字符串）表示回到「跟随全局代理」——多数源本来就该直连或跟全局。
    """
    from ..core.proxy import describe_proxy, normalize_proxy_url
    # 局部导入：get_session_factory 以前在这里漏过一次，导致「给单源配代理」整体 500。
    # 当时没有测试覆盖这条路径所以没暴露出来，已在 test_api_contract 补回归用例。
    from ..db.base import get_session_factory
    from ..db.models import ProviderRecord

    provider = service.registry.require(name)
    try:
        url = normalize_proxy_url(raw) if raw and raw.strip() else ""
    except ValueError as exc:
        raise ValidationError(f"代理地址无法识别：{exc}") from exc

    provider.transport.proxy = url
    await provider.transport.close()  # 丢掉旧连接池，否则旧连接还在按老方式出去

    from sqlalchemy import select  # 局部导入：避免给整个模块加 DB 依赖

    factory = get_session_factory()
    async with factory() as session:
        row = (await session.execute(select(ProviderRecord).where(ProviderRecord.name == name))).scalars().first()
        if row is None:
            session.add(ProviderRecord(name=name, display_name=provider.display_name, proxy=url or None))
        else:
            row.proxy = url or None
            row.updated_at = datetime.now(timezone.utc)
        await session.commit()

    return {
        "proxy": describe_proxy(url),
        "proxy_note": "该数据源已改用独立代理" if url else "已恢复为跟随全局代理",
    }


# 前端「后台管理」页用 action 简写驱动；这里归一化为显式布尔量，避免两种写法互相打架。
_ACTION_TO_ENABLED = {"enable": True, "disable": False}


@router.post("/providers/reenable-stale", summary="重新启用被系统自动停用的数据源")
async def reenable_stale_providers(
    payload: dict[str, Any] | None = None, service: HealthServiceDep = None, _admin: AdminDep = None
) -> dict[str, Any]:
    """换好代理之后点一下：让那些「因为连不上被自动下线」的源重新回到候选链。

    默认只动**自动停用**的，管理员手动禁用的不会被翻出来（避免覆盖人的决定）。
    """
    include_manual = bool((payload or {}).get("include_manual"))
    result = service.reenable_disabled(include_manual=include_manual)
    persisted = await service.drain()
    result["persisted"] = persisted > 0
    result["note"] = ("已恢复，建议再点一次「一键探测全部数据源」确认它们真的能用"
                      if result["restored"] else "没有被停用的数据源需要处理")
    return result


@router.patch("/providers/{name}", summary="启用/禁用/改优先级")
async def update_provider(
    name: str, payload: ProviderUpdateRequest, service: HealthServiceDep, _admin: AdminDep
) -> dict[str, Any]:
    if not service.registry.get(name):
        raise NotFoundError(f"未注册的数据源: {name}")

    enabled = payload.enabled
    locked = payload.locked
    if payload.action in _ACTION_TO_ENABLED:
        enabled = _ACTION_TO_ENABLED[str(payload.action)]
    elif payload.action == "unlock":
        locked = False

    result: dict[str, Any] = {"provider": name}
    if enabled is not None:
        result.update(service.set_enabled(name, enabled, payload.reason))
    if payload.priority is not None:
        # 改优先级默认顺带锁定；调用方显式传 locked=false 时保持不锁定
        result.update(service.set_priority(name, payload.priority, lock=locked if locked is not None else True))
    elif locked is False:
        result.update(service.unlock(name))
    if payload.proxy is not None:
        result.update(await _set_provider_proxy(service, name, payload.proxy))

    changed = result.keys() - {"provider"}
    if not changed:
        # 不允许返回「已应用」却没有改动任何东西 —— 这会让管理员误判线上状态
        raise ValidationError("请求中没有包含任何有效变更（enabled / priority / action）"
                              "，未做任何修改")

    # 等变更真正写进 providers 表再返回：接口说「已应用」，库里就必须已经是这个值。
    # 注意 proxy 是同步直写的（`_set_provider_proxy` 里已经 commit），不走下面的延迟
    # 队列，所以 drain() 很可能返回 0 —— 不能因为它为 0 就回一个假的 persisted=false。
    drained = await service.drain()
    result["changed"] = sorted(changed)
    result["persisted"] = drained > 0 or "proxy" in result
    return result


@router.get("/data-quality", summary="数据质量：覆盖率、缺口、冲突")
async def data_quality(service: HealthServiceDep) -> dict[str, Any]:
    coverage = await service.coverage()
    from ..db.models import DataQuality

    from ..db.base import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        from sqlalchemy import desc, select

        rows = (
            await session.execute(select(DataQuality).order_by(desc(DataQuality.checked_at)).limit(30))
        ).scalars().all()
    return {
        "coverage": coverage,
        "recent_checks": [
            {
                "category": r.category,
                "date": r.date,
                "completeness": r.completeness,
                "missing": r.missing_count,
                "status": r.status,
                "checked_at": r.checked_at.isoformat(),
            }
            for r in rows
        ],
    }


@router.get("/jobs", summary="任务列表与状态")
async def jobs() -> dict[str, Any]:
    return get_scheduler().status()


@router.post("/jobs/{job_id}/run", summary="手动执行任务")
async def run_job(job_id: str, _admin: AdminDep) -> dict[str, Any]:
    return await get_scheduler().run_job_now(job_id)


@router.post("/backfill", summary="手动触发历史回填（断点续传）")
async def backfill(payload: BackfillRequest, _admin: AdminDep) -> dict[str, Any]:
    return await get_scheduler().run_backfill(
        start_date=payload.start_date,
        end_date=payload.end_date,
        interval=payload.interval or "1d",
        reset=payload.reset,
    )


@router.get("/stats", summary="数据表行数统计")
async def stats() -> dict[str, Any]:
    return {"tables": await table_stats()}


@router.get("/failover-events", summary="故障切换事件（别名）")
async def failover_events_alias(service: HealthServiceDep, limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    return {"events": await service.failover_events(limit)}
