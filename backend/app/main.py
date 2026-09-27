# -*- coding: utf-8 -*-
"""BTC 全市场智能研究平台 —— FastAPI 应用入口。

启动流程：
1. 集中配置校验（缺失即快速失败）
2. 初始化数据库（幂等）+ 种子数据
3. 自动发现并注册所有 Provider
4. 启动采集调度器（可选）
5. 挂载静态前端与 API 路由
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import alerts, assistant, market, portfolio, research, system
from .core.config import Settings, get_settings
from .core.errors import AppError
from .core.logging import get_logger, setup_logging
from .db.migrate import enable_timescaledb, init_db
from .db.seed import seed_all
from .providers.registry import get_registry
from .providers.router import get_router

async def _bootstrap_first_price() -> None:
    """后台采集一次最新价格。

    关键点：这是**装饰性预热**，不是系统可用性的前置条件。
    即使所有 Provider 都不可达，也必须让 API 立刻可用 —— 页面会走本地缓存/降级逻辑，
    而不是让整个服务卡在启动阶段。
    """
    await asyncio.sleep(1.0)
    try:
        router = get_router()
        from .providers.types import DataCategory

        result = await asyncio.wait_for(
            router.fetch_validated(DataCategory.MARKET_PRICE), timeout=30
        )
        logger.event("app.first_price", price=result.data, provider=result.provider)
    except asyncio.TimeoutError:
        logger.event("app.first_price_timeout")
    except Exception as exc:  # noqa: BLE001 - 预热失败不影响启动
        logger.event("app.first_price_failed", error=str(exc)[:200])


logger = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or get_settings()
    s.validate_for_startup()
    setup_logging()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.event("app.starting", env=s.APP_ENV, version="1.0.0")

        # 1) 数据库
        await init_db()
        await enable_timescaledb()
        counts = await seed_all()
        logger.event("app.seed", **counts)

        # 2) 运行时配置（后台改过的配置优先于 .env）—— 必须在 Provider 注册之前，
        #    否则数据源会先按 .env 判定「未配置」，后台填的密钥要等下次才生效。
        try:
            from .core.settings_store import get_settings_store

            await get_settings_store().load(force=True)
            logger.event("app.settings_loaded")
        except Exception as exc:  # noqa: BLE001
            logger.warning("app.settings_load_failed error=%s，回退到 .env 与默认值", exc)

        # 3) Provider 注册
        registry = get_registry()
        provider_count = registry.autodiscover() if not registry.all() else len(registry.all())
        logger.event("app.providers_registered", count=provider_count)

        # 3.5) 恢复「数据源独立代理」（数据库里的真实地址），必须在注册之后、采集之前
        try:
            from .services.health_service import HealthService

            restored = await HealthService().restore_proxy_overrides()
            if restored:
                logger.event("app.provider_proxy_restored", count=len(restored), providers=restored)
        except Exception as exc:  # noqa: BLE001
            logger.warning("app.provider_proxy_restore_failed error=%s", exc)

        # 4) 首次启动时做一次基础采集（后台进行，绝不阻塞服务可用）
        if s.APP_ENV != "test":
            asyncio.create_task(_bootstrap_first_price(), name="bootstrap-first-price")

        # 5) 调度器
        if s.SCHEDULER_ENABLED and s.APP_ENV != "test":
            from .scheduler.manager import get_scheduler

            get_scheduler().start()

        yield

        # 关闭
        if s.SCHEDULER_ENABLED:
            try:
                from .scheduler.manager import get_scheduler

                get_scheduler().stop()
            except Exception:  # noqa: BLE001
                pass
        try:
            await registry.close_all()
        except Exception:  # noqa: BLE001
            pass
        logger.event("app.stopped")

    app = FastAPI(
        title=s.APP_NAME,
        description=(
            "BTC 全市场智能研究、历史数据、周期分析、个人资金计划、策略回测与风险监测平台。"
            "多数据源自动故障切换、本地历史数据优先、结论可追溯。"
        ),
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs" if not s.is_production else None,
        redoc_url=None,
    )

    # ---------------- 安全头与 CORS（生产禁止通配符）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=s.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID", "X-Admin-Token"],
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    # ---------------- 统一错误处理（不暴露内部细节）
    @app.exception_handler(AppError)
    async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict())

    @app.exception_handler(Exception)
    async def unhandled_handler(_: Request, exc: Exception) -> JSONResponse:
        logger.event("app.unhandled_error", error=str(exc)[:300])
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal_error", "message": "服务器内部错误", "detail": {}}},
        )

    # ---------------- 路由
    prefix = s.API_PREFIX
    app.include_router(system.router, prefix=prefix + "/system", tags=["系统/数据源中心"])
    app.include_router(market.router, prefix=prefix + "/market", tags=["行情与分析"])
    app.include_router(research.router, prefix=prefix + "/research", tags=["回测/回放/模型"])
    app.include_router(portfolio.router, prefix=prefix + "/portfolio", tags=["资金计划/资产"])
    app.include_router(assistant.router, prefix=prefix + "/assistant", tags=["AI 解释助手"])
    app.include_router(alerts.router, prefix=prefix + "/alerts", tags=["智能监测/条件预警"])

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}

    # ---------------- 静态前端（零构建 SPA）
    frontend_dir = Path(__file__).resolve().parents[2] / "frontend"
    static_dir = frontend_dir / "static"
    if (frontend_dir / "index.html").exists():
        if static_dir.exists():
            app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(str(frontend_dir / "index.html"))

    return app


app = create_app()


def main() -> None:
    import uvicorn

    s = get_settings()
    uvicorn.run(
        "app.main:app",
        host=s.HOST,
        port=s.PORT,
        reload=not s.is_production,
        log_config=None,
        access_log=not s.is_production,
    )


if __name__ == "__main__":
    main()
