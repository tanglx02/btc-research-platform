# -*- coding: utf-8 -*-
"""把进程从「刚创建 App」推进到「可以服务」的全部启动动作。

为什么抽出来
------------
以前这些动作直接写在 `main.create_app` 的 lifespan 里，看起来没什么问题 ——
直到「首次启动的安装向导」出现：向导填完数据库连接之后，不走 lifespan，
而是要在请求里当场完成同样的初始化。

两段逻辑如果各写一遍，结局几乎可以预料：某次改动只改了 lifespan 那一份，
表现为「向导装完能进页面，重启服务后反而进不去」。这类 bug 极其难查，
因为它只在重启之后出现。所以这里只留一份实现，两处都调用它。
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..core.logging import get_logger

logger = get_logger(__name__)

_started = False


def has_started() -> bool:
    """启动动作是否已经跑过（避免重复注册 Provider 或重复启动调度器）。"""
    return _started


async def _bootstrap_first_price() -> None:
    """后台采集一次最新价格。

    关键点：这是**装饰性预热**，不是系统可用性的前置条件。
    即使所有 Provider 都不可达，也必须让 API 立刻可用 —— 页面会走本地缓存/降级逻辑，
    而不是让整个服务卡在启动阶段。
    """
    await asyncio.sleep(1.0)
    try:
        from ..providers.router import get_router
        from ..providers.types import DataCategory

        result = await asyncio.wait_for(
            get_router().fetch_validated(DataCategory.MARKET_PRICE), timeout=30
        )
        logger.event("app.first_price", price=result.data, provider=result.provider)
    except asyncio.TimeoutError:
        logger.event("app.first_price_timeout")
    except Exception as exc:  # noqa: BLE001 - 预热失败不影响启动
        logger.event("app.first_price_failed", error=str(exc)[:200])


async def run_startup_tasks(settings: Any) -> dict[str, Any]:
    """建表 → 种子数据 → 加载后台配置 → 注册 Provider → 恢复独立代理 → 起调度器。

    每一步都单独包了 try：`种子数据失败` 不该让整个服务起不来，
    否则用户看到的是「打不开」，而真正的原因藏在日志里。

    Returns:
        一份可观测的启动摘要（装了哪些东西、哪些失败了），供 /system/info 与日志使用。
    """
    global _started
    summary: dict[str, Any] = {
        "seeded": {}, "settings_loaded": False, "providers": 0,
        "proxy_restored": 0, "scheduler": False, "failed": [],
    }

    # 1) 数据库结构与种子数据
    from ..db.migrate import enable_timescaledb, init_db
    from ..db.seed import seed_all

    await init_db()
    await enable_timescaledb()
    try:
        summary["seeded"] = await seed_all()
        logger.event("app.seed", **summary["seeded"])
    except Exception as exc:  # noqa: BLE001
        summary["failed"].append(f"seed: {exc}"[:200])
        logger.warning("app.seed_failed error=%s", exc)

    # 2) 运行时配置（后台改过的配置优先于 .env）—— 必须在 Provider 注册之前，
    #    否则数据源会先按 .env 判定「未配置」，后台填的密钥要等下次才生效。
    try:
        from ..core.settings_store import get_settings_store

        await get_settings_store().load(force=True)
        summary["settings_loaded"] = True
        logger.event("app.settings_loaded")
    except Exception as exc:  # noqa: BLE001
        summary["failed"].append(f"settings: {exc}"[:200])
        logger.warning("app.settings_load_failed error=%s，回退到 .env 与默认值", exc)

    # 3) Provider 注册
    registry = None
    try:
        from ..providers.registry import get_registry

        registry = get_registry()
        provider_count = registry.autodiscover() if not registry.all() else len(registry.all())
        summary["providers"] = provider_count
        logger.event("app.providers_registered", count=provider_count)
    except Exception as exc:  # noqa: BLE001
        summary["failed"].append(f"providers: {exc}"[:200])
        logger.warning("app.providers_register_failed error=%s", exc)

    # 4) 恢复「数据源独立代理」（数据库里的真实地址）
    try:
        from ..services.health_service import HealthService

        restored = await HealthService().restore_proxy_overrides()
        summary["proxy_restored"] = len(restored)
        if restored:
            logger.event("app.provider_proxy_restored", count=len(restored), providers=restored)
    except Exception as exc:  # noqa: BLE001
        summary["failed"].append(f"proxy_restore: {exc}"[:200])
        logger.warning("app.provider_proxy_restore_failed error=%s", exc)

    # 5) 装饰性预热 + 调度器
    if settings.APP_ENV != "test":
        asyncio.create_task(_bootstrap_first_price(), name="bootstrap-first-price")

    if settings.SCHEDULER_ENABLED and settings.APP_ENV != "test":
        try:
            from ..scheduler.manager import get_scheduler

            get_scheduler().start()
            summary["scheduler"] = True
        except Exception as exc:  # noqa: BLE001
            summary["failed"].append(f"scheduler: {exc}"[:200])
            logger.warning("app.scheduler_start_failed error=%s", exc)

    _started = True
    logger.event("app.startup_tasks_done", **summary)
    return summary


async def shutdown_tasks(settings: Any) -> None:
    """与 `run_startup_tasks` 配对的收尾动作。"""
    global _started
    if settings.SCHEDULER_ENABLED:
        try:
            from ..scheduler.manager import get_scheduler

            get_scheduler().stop()
        except Exception:  # noqa: BLE001
            pass
    try:
        from ..providers.registry import get_registry

        await get_registry().close_all()
    except Exception:  # noqa: BLE001
        pass
    _started = False
