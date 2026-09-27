# -*- coding: utf-8 -*-
"""数据源健康服务：数据源中心 / 数据质量页的数据供给。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, select

from ..core.logging import get_logger
from ..core.proxy import normalize_proxy_url
from ..db.base import get_session_factory
from ..db.models import ProviderFailoverEvent, ProviderRecord
from ..db.repo import candle_coverage
from ..providers.registry import DataCategory, get_registry
from ..providers.router import get_router
from ..providers.types import ProviderStatus

logger = get_logger(__name__)

STATUS_ICON = {
    ProviderStatus.ONLINE: "✅",
    ProviderStatus.DEGRADED: "⚠️",
    ProviderStatus.SLOW: "🐢",
    ProviderStatus.RATE_LIMITED: "⏳",
    ProviderStatus.AUTH_ERROR: "🔑",
    ProviderStatus.NETWORK_ERROR: "📡",
    ProviderStatus.DATA_ERROR: "🧩",
    ProviderStatus.OFFLINE: "❌",
    ProviderStatus.DISABLED: "🚫",
    ProviderStatus.NOT_CONFIGURED: "➖",
    ProviderStatus.UNKNOWN: "❓",
}


class HealthService:
    def __init__(self) -> None:
        self.registry = get_registry()
        self.router = get_router()
        # 记录「已提交但尚未确认落库」的任务，供 drain() 等待
        self._pending_writes: set[Any] = set()

    def dashboard(self) -> dict[str, Any]:
        snapshots = []
        for provider in self.registry.all():
            snapshots.append(
                self.registry.health.snapshot(
                    provider.name,
                    display_name=provider.display_name,
                    enabled=provider.enabled,
                    configured=provider.is_configured(),
                    priority=provider.manual_priority if provider.manual_priority is not None else provider.default_priority,
                    is_backup=False,
                    locked=provider.locked,
                    categories=sorted(c.value for c in provider.categories),
                )
            )

        total = len(snapshots)
        healthy = sum(1 for s in snapshots if s.status.healthy)
        not_configured = sum(1 for s in snapshots if s.status == ProviderStatus.NOT_CONFIGURED)
        disabled = sum(1 for s in snapshots if s.status == ProviderStatus.DISABLED)
        failures_today = sum(s.failures_today for s in snapshots)
        avg_latency = (
            sum(s.latency_ms for s in snapshots if s.latency_ms) / max(1, len([s for s in snapshots if s.latency_ms]))
        )

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "summary": {
                "total_providers": total,
                "healthy": healthy,
                "not_configured": not_configured,
                "disabled": disabled,
                "failures_today": failures_today,
                "avg_latency_ms": round(avg_latency, 2),
                "health_ratio": round(healthy / total, 3) if total else 0.0,
            },
            "providers": [
                {**s.to_dict(), "icon": STATUS_ICON.get(s.status, "❓")} for s in sorted(snapshots, key=lambda x: x.priority)
            ],
        }

    def categories(self) -> dict[str, Any]:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "categories": self.registry.category_matrix(),
        }

    async def failover_events(self, limit: int = 50) -> list[dict[str, Any]]:
        factory = get_session_factory()
        async with factory() as session:
            rows = (
                await session.execute(select(ProviderFailoverEvent).order_by(desc(ProviderFailoverEvent.occurred_at)).limit(limit))
            ).scalars().all()
        return [
            {
                "ts": r.occurred_at.isoformat(),
                "category": r.category,
                "from_provider": r.from_provider,
                "to_provider": r.to_provider,
                "reason": r.reason,
                "resolved": r.resolved,
            }
            for r in rows
        ]

    async def coverage(self) -> dict[str, Any]:
        factory = get_session_factory()
        async with factory() as session:
            daily = await candle_coverage(session, "BTC", "1d")
            hourly = await candle_coverage(session, "BTC", "1h")
        completeness = (
            round(daily["count"] / daily["expected_days"], 4)
            if daily.get("expected_days")
            else 0.0
        )
        return {
            "daily": daily,
            "hourly": hourly,
            "daily_completeness": completeness,
        }

    async def probe_all(self) -> dict[str, Any]:
        started = datetime.now(timezone.utc)
        results = await self.router.probe_all()
        return {
            "started_at": started.isoformat(),
            "duration_ms": round((datetime.now(timezone.utc) - started).total_seconds() * 1000, 2),
            "results": results,
            "ok_count": sum(1 for r in results if r["ok"]),
            "fail_count": sum(1 for r in results if not r["ok"]),
        }

    # ---------------------------------------------------------------- 运维操作
    def set_enabled(self, name: str, enabled: bool, reason: str = "") -> dict[str, Any]:
        self.registry.set_enabled(name, enabled, reason)
        self._persist(name)
        return {"provider": name, "enabled": enabled, "reason": reason or ("已启用" if enabled else "已禁用")}

    def reenable_disabled(self, *, include_manual: bool = False) -> dict[str, Any]:
        """把被停用的数据源重新启用。

        典型场景：换了代理 / 网络恢复之后，之前因超时被自动停用的源其实已经能用。
        `include_manual=False`（默认）时只恢复**系统自动停用**的那些，
        不会把你手动下线的源重新打开——手动决定不该被批量操作覆盖。
        """
        restored: list[str] = []
        skipped: list[dict[str, str]] = []
        for p in self.registry.all():
            if p.enabled:
                continue
            reason = p.disabled_reason or ""
            is_auto = reason.startswith("自动停用")
            if is_auto or include_manual:
                self.registry.set_enabled(p.name, True)
                self._persist(p.name)
                restored.append(p.name)
            else:
                skipped.append({"provider": p.name, "reason": reason or "管理员手动禁用"})
        return {"restored": restored, "skipped": skipped,
                "count": len(restored)}

    def set_priority(self, name: str, priority: int, lock: bool = True) -> dict[str, Any]:
        self.registry.set_priority(name, priority, lock=lock)
        self._persist(name)
        return {"provider": name, "priority": priority, "locked": lock}

    def unlock(self, name: str) -> dict[str, Any]:
        self.registry.unlock(name)
        self._persist(name)
        return {"provider": name, "locked": False}

    async def restore_proxy_overrides(self) -> list[str]:
        """启动时把「数据源独立代理」从数据库读回来，让重启后仍然生效。"""
        factory = get_session_factory()
        async with factory() as session:
            rows = (await session.execute(
                select(ProviderRecord).where(ProviderRecord.proxy.is_not(None))
            )).scalars().all()
        applied: list[str] = []
        for row in rows:
            if not row.proxy:
                continue
            p = self.registry.get(row.name)
            if p is None:
                continue
            try:
                p.transport.proxy = normalize_proxy_url(row.proxy)
            except ValueError as exc:
                logger.error(f"数据源 {row.name} 的代理地址无法解析，已忽略：{exc}")
                continue
            applied.append(row.name)
        if applied:
            logger.event("provider.proxy_restored", providers=applied)
        return applied

    def _persist(self, name: str) -> None:
        """把运行时变更落库（幂等），重启后保留管理员意图。

        历史问题：这里原来是纯「发射后不管」的 create_task —— 接口已经返回「已应用」，
        写库任务却可能还在队列里，一旦进程在这几毫秒内退出，管理员的启停决定就丢了，
        而且这种丢失不会有任何报错。现在改为登记到 _pending_writes，
        由调用方通过 `await service.drain()` 在返回前确认写库结果。
        """
        import asyncio

        provider = self.registry.get(name)
        if not provider:
            return

        async def write() -> None:
            factory = get_session_factory()
            async with factory() as session:
                row = (await session.execute(select(ProviderRecord).where(ProviderRecord.name == name))).scalars().first()
                data = {
                    "display_name": provider.display_name,
                    "enabled": provider.enabled,
                    "locked": provider.locked,
                    "manual_priority": provider.manual_priority,
                    "api_key_env": provider.api_key_env or None,
                    # proxy 列不在这里写：它承载的是「该数据源的独立代理」真实地址，
                    # 由 set_provider_proxy 单独维护；这里若用掩码覆盖会把它冲掉。
                    "disabled_reason": provider.disabled_reason or None,
                    "categories": sorted(c.value for c in provider.categories),
                }
                if row:
                    for key, value in data.items():
                        setattr(row, key, value)
                    row.updated_at = datetime.now(timezone.utc)
                else:
                    session.add(ProviderRecord(name=name, **data))
                await session.commit()

        def _done(task: Any) -> None:
            self._pending_writes.discard(task)
            exc = task.exception() if not task.cancelled() else None
            if exc is not None:
                # 落库失败必须留痕：否则运营会以为改动永久生效了
                logger.error(f"provider 配置持久化失败 provider={name}: {exc}")

        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(write())
            self._pending_writes.add(task)
            task.add_done_callback(_done)
        except RuntimeError:
            # 无事件循环（脚本/命令行场景）：同步执行，保证这一步真的发生
            asyncio.run(write())

    async def drain(self) -> int:
        """等待所有挂起的落库任务完成，返回已完成的任务数。

        返回 0 表示没有待落库的改动 —— 调用方可以据此确认「改动确实已经写进数据库」。
        """
        if not self._pending_writes:
            return 0
        import asyncio

        tasks = list(self._pending_writes)
        done, _ = await asyncio.wait(tasks, timeout=10)
        still = len(self._pending_writes)
        logger.event("health.persist_drained", waited=len(tasks), finished=len(done), pending=still)
        if still:
            logger.error(f"仍有 {still} 个 provider 配置写入未在 10 秒内完成")
        return len(done)
