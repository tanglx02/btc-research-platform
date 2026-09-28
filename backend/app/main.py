# -*- coding: utf-8 -*-
"""BTC 全市场智能研究平台 —— FastAPI 应用入口。

启动流程：
1. 集中配置校验（缺失即快速失败）
2. **首次启动**：未完成安装引导时不建库、不起调度，
   `/api/v1/**` 除 `/system/setup/*` 外全部返回 503，前端自动进入引导页
3. 初始化数据库（幂等）+ 种子数据
4. 自动发现并注册所有 Provider
5. 启动采集调度器（可选）
6. 挂载静态前端与 API 路由
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import alerts, assistant, market, portfolio, research, setup as setup_api, system
from .core.bootstrap import has_started, run_startup_tasks, shutdown_tasks
from .core.config import Settings, get_settings
from .core.errors import AppError
from .core.logging import get_logger, setup_logging
from .core.setup import ensure_adopted_existing_install, needs_setup
from .providers.registry import get_registry


logger = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or get_settings()
    s.validate_for_startup()
    setup_logging()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.event("app.starting", env=s.APP_ENV, version="1.0.0")

        # 0) 安装引导未完成：除了引导接口，其它一切都不启动。
        #    理由很简单 —— 这时候数据库还没定，任何试图建表/读取的动作都注定失败，
        #    不如干脆不启动，让前端把人带去引导页。
        if needs_setup(s):
            try:
                adopted = await ensure_adopted_existing_install(s)
            except Exception as exc:  # noqa: BLE001 - 兼容判定失败时必须按「未安装」走
                logger.warning("app.adopt_failed error=%s", exc)
                adopted = False
            if adopted:
                logger.event("app.setup_adopted", hint="检测到已有数据库，自动标记为已安装")
            else:
                logger.event("app.awaiting_setup", hint="等待安装向导完成数据库配置")
                yield
                logger.event("app.stopped", reason="安装引导未完成，未初始化任何服务")
                return

        await run_startup_tasks(s)

        yield

        # 关闭
        if has_started():
            await shutdown_tasks(s)
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

    # ---------------- 安装闸门
    # 未完成安装引导时，只放行引导接口本身。少了这道闸门的话，
    # 前端会在没有数据库的情况下反复请求各业务接口，得到一堆与真因无关的报错。
    prefix = s.API_PREFIX
    setup_prefix = prefix + "/system/setup"

    @app.middleware("http")
    async def setup_gate(request: Request, call_next):
        if request.url.path.startswith(prefix + "/") and not request.url.path.startswith(setup_prefix):
            if needs_setup(get_settings()):
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": {
                            "code": "setup_required",
                            "message": "尚未完成安装引导：请先选择数据库并通过连接自检。",
                            "detail": {"setup_endpoint": setup_prefix + "/status"},
                        }
                    },
                )
        return await call_next(request)

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

    app.include_router(setup_api.router, prefix=prefix + "/system", tags=["安装引导"])
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
