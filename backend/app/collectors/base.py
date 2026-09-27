# -*- coding: utf-8 -*-
"""采集器基类：统一的 checkpoint（断点续传）+ 任务状态记录。

历史数据抓取的铁律：
1. 分批拉取，每批成功后立即写 checkpoint；崩溃后从断点继续，绝不从头重来。
2. 支持暂停 / 继续 / 重试 / 跳过 / 重新同步。
3. 写库前做数据合理性校验，脏数据不能覆盖正确数据。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from ..core.logging import get_logger
from ..db.base import get_session_factory
from ..db.models import SyncCheckpoint, SystemJob

logger = get_logger(__name__)


class BaseCollector(ABC):
    """所有采集任务的公共骨架。"""

    task_name: str = "abstract"
    category: str = ""
    batch_days: int = 500
    supported_intervals: tuple[str, ...] = ("1m", "5m", "15m", "1h", "4h", "1d", "1w")

    # -------------------------------------------------------------- checkpoint
    async def get_checkpoint(self) -> dict[str, Any]:
        factory = get_session_factory()
        async with factory() as session:
            row = (
                await session.execute(select(SyncCheckpoint).where(SyncCheckpoint.task_name == self.task_name))
            ).scalars().first()
            if not row:
                return {
                    "task_name": self.task_name,
                    "category": self.category,
                    "cursor_ts": 0,
                    "cursor_date": None,
                    "status": "idle",
                    "total_rows": 0,
                    "last_error": None,
                    "exists": False,
                }
            return {
                "task_name": row.task_name,
                "category": row.category,
                "provider": row.provider,
                "cursor_ts": row.cursor_ts,
                "cursor_date": row.cursor_date,
                "status": row.status,
                "total_rows": row.total_rows,
                "last_error": row.last_error,
                "exists": True,
            }

    async def save_checkpoint(
        self,
        *,
        cursor_ts: int,
        cursor_date: str | None = None,
        status: str = "running",
        rows_delta: int = 0,
        provider: str = "",
        error: str | None = None,
        extra: dict[str, Any] | None = None,
        rewind: bool = False,
    ) -> None:
        """写入断点。

        默认 cursor 单调递增（`max`），防止乱序回写把进度倒退；
        但 `rewind=True` 时允许回退 —— 重新同步（reset）必须真的能清零，
        否则 `backfill --reset` 看起来成功、实际还是从旧断点继续。
        """
        factory = get_session_factory()
        async with factory() as session:
            row = (
                await session.execute(select(SyncCheckpoint).where(SyncCheckpoint.task_name == self.task_name))
            ).scalars().first()
            if not row:
                row = SyncCheckpoint(
                    task_name=self.task_name,
                    category=self.category,
                    cursor_ts=cursor_ts,
                    cursor_date=cursor_date,
                    status=status,
                    total_rows=max(0, rows_delta),
                    provider=provider,
                    last_error=error,
                    extra=extra,
                )
                session.add(row)
            else:
                row.cursor_ts = int(cursor_ts) if rewind else max(row.cursor_ts or 0, int(cursor_ts))
                if cursor_date:
                    row.cursor_date = cursor_date
                row.status = status
                row.total_rows = (row.total_rows or 0) + max(0, rows_delta)
                if provider:
                    row.provider = provider
                row.last_error = error
                if extra:
                    row.extra = {**(row.extra or {}), **extra}
                row.updated_at = datetime.now(timezone.utc)
            await session.commit()

    async def reset_checkpoint(self) -> None:
        await self.save_checkpoint(cursor_ts=0, cursor_date=None, status="idle",
                                   rewind=True, rows_delta=0, error=None,
                                   extra={"reset": True})

    # -------------------------------------------------------------- job 记录
    async def _job_start(self, job_name: str, **fields: Any) -> int:
        factory = get_session_factory()
        async with factory() as session:
            job = SystemJob(job_name=job_name, status="running", started_at=datetime.now(timezone.utc))
            session.add(job)
            await session.commit()
            await session.refresh(job)
            return int(job.id)

    async def _job_finish(
        self, job_id: int, status: str, started: float, rows: int = 0, message: str = "", provider: str = ""
    ) -> None:
        factory = get_session_factory()
        async with factory() as session:
            job = await session.get(SystemJob, job_id)
            if job:
                job.status = status
                job.finished_at = datetime.now(timezone.utc)
                job.duration_ms = round((time.perf_counter() - started) * 1000, 2)
                job.rows_affected = rows
                job.message = message[:500]
                job.provider = provider
                await session.commit()

    # -------------------------------------------------------------- 生命周期
    async def run(self, **kwargs: Any) -> dict[str, Any]:
        """带 job 记录的统一执行入口。"""
        started = time.perf_counter()
        job_id = await self._job_start(self.task_name)
        try:
            result = await self.execute(**kwargs)
            await self._job_finish(job_id, "success", started, rows=int(result.get("rows", 0)))
            return result
        except Exception as exc:  # noqa: BLE001 - 单点失败不应击穿调度器
            logger.event("collector.failed", task=self.task_name, error=str(exc)[:300])
            await self._job_finish(job_id, "failed", started, message=str(exc)[:300])
            return {"ok": False, "task": self.task_name, "error": str(exc)[:300]}

    @abstractmethod
    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """子类实现具体采集逻辑。"""
