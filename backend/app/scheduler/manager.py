# -*- coding: utf-8 -*-
"""任务调度：所有数据采集都是独立任务，支持暂停/恢复/手动执行/优先级。

调度器本身不会因为单个任务异常而停止。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from ..core.config import get_settings
from ..core.logging import get_logger
from ..db.base import get_session_factory
from ..collectors import (
    DataQualityScanner,
    DerivativesCollector,
    EtfCollector,
    GapRepairCollector,
    MacroCollector,
    MarketCollector,
    OnchainCollector,
    SentimentCollector,
)
from ..providers.router import get_router
from ..services.health_service import HealthService

logger = get_logger(__name__)


class SchedulerManager:
    """任务编排：核心数据优先（价格 > K 线 > 衍生品 > 其他）。"""

    def __init__(self) -> None:
        self.s = get_settings()
        self.scheduler = AsyncIOScheduler(timezone="UTC")
        self._jobs: dict[str, dict[str, Any]] = {}
        self._running = False
        self.router = get_router()
        self.health_service = HealthService()
        # 每日摘要 / 周报的「今天是否已发」标记（进程内即可，重启后同一天最多多发一次，
        # 这是可接受的：宁可多发一次摘要，也不要因为持久化失败而永远不发）
        self._digest_sent: dict[str, bool] = {}

    # ---------------------------------------------------------------- 任务定义
    def _build_jobs(self) -> list[dict[str, Any]]:
        return [
            {"id": "market_tick", "priority": 1, "seconds": max(10, self.s.JOB_MARKET_TICK_SECONDS),
             "desc": "BTC 实时价格（多源 + 交叉验证）",
             "fn": lambda: MarketCollector(interval="1d").run(mode="tick")},
            {"id": "market_1h", "priority": 2, "seconds": max(60, self.s.JOB_MARKET_1H_SECONDS),
             "desc": "1 小时 K 线增量同步",
             "fn": lambda: MarketCollector(interval="1h").run(mode="incremental")},
            {"id": "market_1d", "priority": 3, "seconds": max(300, self.s.JOB_MARKET_1D_SECONDS),
             "desc": "日线增量同步",
             "fn": lambda: MarketCollector(interval="1d").run(mode="incremental")},
            {"id": "derivatives", "priority": 4, "seconds": max(60, self.s.JOB_DERIVATIVES_SECONDS),
             "desc": "衍生品（资金费率 / 持仓量）",
             "fn": lambda: DerivativesCollector().run()},
            {"id": "onchain", "priority": 5, "seconds": max(300, self.s.JOB_ONCHAIN_SECONDS),
             "desc": "链上基础数据",
             "fn": lambda: OnchainCollector().run()},
            {"id": "sentiment", "priority": 6, "seconds": max(600, self.s.JOB_SENTIMENT_SECONDS),
             "desc": "情绪指标（恐慌贪婪）",
             "fn": lambda: SentimentCollector().run()},
            {"id": "macro", "priority": 7, "seconds": max(1800, self.s.JOB_MACRO_SECONDS),
             "desc": "宏观序列",
             "fn": lambda: MacroCollector().run()},
            # ETF 资金流原先复用 JOB_MACRO_SECONDS（无法单独调频），改为独立配置项
            {"id": "etf", "priority": 8, "seconds": max(1800, self.s.JOB_ETF_SECONDS),
             "desc": "ETF 资金流（未配置时自动跳过）",
             "fn": lambda: EtfCollector().run()},
            {"id": "quality", "priority": 9, "seconds": max(600, self.s.JOB_QUALITY_SECONDS),
             "desc": "数据质量扫描",
             "fn": lambda: DataQualityScanner().run()},
            {"id": "gap_repair", "priority": 10, "seconds": max(1800, self.s.JOB_QUALITY_SECONDS),
             "desc": "历史缺口自动补洞",
             "fn": lambda: GapRepairCollector().run()},
            {"id": "health_probe", "priority": 11, "seconds": max(60, self.s.JOB_HEALTH_SECONDS),
             "desc": "数据源健康检查",
             "fn": self._health_probe},
            # 原始响应留档表会持续增长，必须定期清理；清理的只是 raw 原始报文，
            # 标准化后的历史数据（candles 等）永不删除。
            {"id": "raw_retention", "priority": 12, "seconds": max(3600, self.s.JOB_RETENTION_SECONDS),
             "desc": f"原始数据留档清理（保留 {self.s.RAW_RETENTION_DAYS} 天）",
             "fn": self._purge_raw},
            # ---------------------------------------------------------------
            # 智能预警（复用同一个调度器，不另建定时系统）
            # alert_engine 负责「评估规则 + 首次发送」；alert_retry 负责「把没发出去的补发」。
            # 两个任务分开，是为了保证：即使发信很慢/卡住，规则评估也不会被拖住。
            # ---------------------------------------------------------------
            {"id": "alert_engine", "priority": 20,
             "seconds": max(30, self.s.JOB_ALERT_ENGINE_SECONDS),
             "desc": "智能预警：规则求值与触发",
             "fn": self._alert_cycle},
            {"id": "alert_retry", "priority": 21,
             "seconds": max(60, self.s.JOB_ALERT_RETRY_SECONDS),
             "desc": "智能预警：失败通知补发与渠道熔断恢复",
             "fn": self._alert_retry},
            {"id": "alert_digest", "priority": 22,
             "seconds": max(300, self.s.JOB_ALERT_DIGEST_SECONDS),
             "desc": "智能预警：每日摘要 / 周报（到点才发）",
             "fn": self._alert_digest},
        ]

    async def _alert_cycle(self) -> dict[str, Any]:
        """一轮规则求值。异常必须被吃掉，否则会击穿整个调度器。"""
        from ..alerts.pipeline import AlertPipeline

        try:
            result = await AlertPipeline().run_cycle()
            return {"ok": True, **result}
        except Exception as exc:  # noqa: BLE001
            logger.event("scheduler.alert_engine_failed", error=str(exc)[:300])
            return {"ok": False, "error": str(exc)[:300]}

    async def _alert_retry(self) -> dict[str, Any]:
        from ..alerts.pipeline import AlertPipeline

        try:
            result = await AlertPipeline().dispatch_pending(limit=200)
            return {"ok": True, **result}
        except Exception as exc:  # noqa: BLE001
            logger.event("scheduler.alert_retry_failed", error=str(exc)[:300])
            return {"ok": False, "error": str(exc)[:300]}

    async def _alert_digest(self) -> dict[str, Any]:
        """按配置的时间点发送摘要；同一时间窗内只发一次（用内存标记 + 时间判断）。"""
        from ..alerts.pipeline import send_digest

        now = datetime.now(timezone.utc)
        sent: list[str] = []
        try:
            if self._digest_due("daily", now, self.s.ALERT_DIGEST_DAILY_HOUR):
                await send_digest("daily")
                sent.append("daily")
            if now.weekday() == self.s.ALERT_DIGEST_WEEKLY_WEEKDAY and \
                    self._digest_due("weekly", now, self.s.ALERT_DIGEST_WEEKLY_HOUR):
                await send_digest("weekly")
                sent.append("weekly")
            return {"ok": True, "sent": sent, "checked_at": now.isoformat()}
        except Exception as exc:  # noqa: BLE001
            logger.event("scheduler.alert_digest_failed", error=str(exc)[:300])
            return {"ok": False, "error": str(exc)[:300]}

    def _digest_due(self, kind: str, now: datetime, hour: int) -> bool:
        """判断今天这个时刻是否该发摘要；发过就记一天，避免每 5 分钟重复发。"""
        if now.hour < hour:
            return False
        marker = f"{kind}:{now.date().isoformat()}"
        if self._digest_sent.get(marker):
            return False
        self._digest_sent[marker] = True
        # 只保留最近 14 天标记，避免无限增长
        if len(self._digest_sent) > 28:
            for key in sorted(self._digest_sent)[:len(self._digest_sent) - 28]:
                self._digest_sent.pop(key, None)
        return True

    async def _purge_raw(self) -> dict[str, Any]:
        """清理过期的 raw_market_data（只删原始报文，不影响任何标准化数据）。"""
        from ..db.repo import purge_old_raw

        factory = get_session_factory()
        try:
            async with factory() as session:
                removed = await purge_old_raw(session, self.s.RAW_RETENTION_DAYS)
            return {"ok": True, "deleted": removed, "retention_days": self.s.RAW_RETENTION_DAYS}
        except Exception as exc:  # noqa: BLE001 - 清理任务失败不能拖垮调度器
            logger.event("scheduler.retention_failed", error=str(exc)[:200])
            return {"ok": False, "error": str(exc)[:200]}

    async def _health_probe(self) -> dict[str, Any]:
        """被动健康探测：不主动打外部 API，只汇总运行时健康状态。"""
        return {"ok": True, "dashboard": self.health_service.dashboard()["summary"]}

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> None:
        if self._running:
            return
        for job in self._build_jobs():
            self.scheduler.add_job(
                self._safe_run(job["fn"], job["id"]),
                trigger=IntervalTrigger(seconds=job["seconds"]),
                id=job["id"],
                name=job["desc"],
                max_instances=1,
                coalesce=True,
                misfire_grace_time=120,
                replace_existing=True,
            )
            self._jobs[job["id"]] = {
                "id": job["id"],
                "description": job["desc"],
                "interval_seconds": job["seconds"],
                "priority": job["priority"],
                "status": "scheduled",
                "last_run": None,
                "last_status": None,
            }
        self.scheduler.start()
        self._running = True
        logger.event("scheduler.started", jobs=len(self._jobs))

    def _safe_run(self, coro_factory: Callable[[], Any], job_id: str) -> Callable[[], Any]:
        """包裹任务：记录最近运行状态，单个任务异常不击穿调度器。"""

        async def wrapper() -> None:
            started = datetime.now(timezone.utc)
            status = "success"
            try:
                result = await coro_factory()
                ok = bool(result.get("ok")) if isinstance(result, dict) else True
            except Exception as exc:  # noqa: BLE001
                status, ok = "failed", False
                logger.event("scheduler.task_error", job=job_id, error=str(exc)[:300])
            meta = self._jobs.setdefault(job_id, {})
            meta["last_run"] = started.isoformat()
            meta["last_status"] = status
            logger.event("scheduler.task_done", job=job_id, status=status, ok=ok)

        wrapper.__name__ = f"job_{job_id}"
        return wrapper

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        self._running = False
        logger.event("scheduler.stopped")

    def status(self) -> dict[str, Any]:
        jobs = []
        for ap_job in self.scheduler.get_jobs():
            meta = self._jobs.get(ap_job.id, {})
            jobs.append(
                {
                    "id": ap_job.id,
                    "name": ap_job.name,
                    "interval_seconds": meta.get("interval_seconds"),
                    "priority": meta.get("priority"),
                    "next_run": ap_job.next_run_time.isoformat() if ap_job.next_run_time else None,
                    "status": "running" if self._running else "stopped",
                }
            )
        return {"running": self._running, "job_count": len(jobs), "jobs": jobs}

    async def run_job_now(self, job_id: str) -> dict[str, Any]:
        """手动触发任务（立即执行一次，不影响调度计划）。"""
        for job in self._build_jobs():
            if job["id"] == job_id:
                try:
                    result = await job["fn"]()
                    return {"ok": True, "job": job_id, "result": result}
                except Exception as exc:  # noqa: BLE001
                    logger.event("scheduler.manual_failed", job=job_id, error=str(exc)[:200])
                    return {"ok": False, "job": job_id, "error": str(exc)[:200]}
        return {"ok": False, "job": job_id, "error": "任务不存在"}

    async def run_backfill(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        interval: str = "1d",
        reset: bool = False,
    ) -> dict[str, Any]:
        """手动历史回填（立即执行）。

        end_date / interval 曾经被 API 层接收但未向下透传，导致「指定了截止日期却仍然拉到今天」。
        MarketCollector.backfill 本身早就支持这两个参数，这里补上整条链路。
        """
        collector = MarketCollector(interval=interval or "1d")
        return await collector.run(
            mode="backfill", start_date=start_date, end_date=end_date, reset=reset
        )


_scheduler_singleton: SchedulerManager | None = None


def get_scheduler() -> SchedulerManager:
    global _scheduler_singleton
    if _scheduler_singleton is None:
        _scheduler_singleton = SchedulerManager()
    return _scheduler_singleton
