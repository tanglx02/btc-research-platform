# -*- coding: utf-8 -*-
"""Provider 健康中心（Health Center）。

每个 Provider 维护：
- 状态机（ONLINE / DEGRADED / SLOW / RATE_LIMITED / AUTH_ERROR / NETWORK_ERROR / DATA_ERROR / OFFLINE / DISABLED / NOT_CONFIGURED）
- 成功率（1h / 24h 滑动窗口）、延迟（最近 / 均值 / P95）
- 连续失败/成功次数、今日失败数、最近错误原因、限流标记、数据延迟、完整性
- 综合评分（用于自动调整优先级，管理员可锁定）

设计要点：
* 抖动抑制：恢复为主源需要满足「连续成功 N 次 + 延迟正常 + 冷却期已过」。
* 健康评分只影响优先级排序，锁定（locked）的 Provider 不受影响。
"""

from __future__ import annotations

import asyncio
import statistics
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from ..core.config import get_settings
from ..core.logging import get_logger
from .types import ProviderStatus

logger = get_logger(__name__)

# failure_type -> status 映射
FAILURE_STATUS_MAP: dict[str, ProviderStatus] = {
    "timeout": ProviderStatus.NETWORK_ERROR,
    "connect_error": ProviderStatus.NETWORK_ERROR,
    "dns_error": ProviderStatus.NETWORK_ERROR,
    "proxy_error": ProviderStatus.NETWORK_ERROR,
    "tls_error": ProviderStatus.NETWORK_ERROR,
    "network_error": ProviderStatus.NETWORK_ERROR,
    "not_configured": ProviderStatus.NOT_CONFIGURED,
    "auth_error": ProviderStatus.AUTH_ERROR,
    "forbidden": ProviderStatus.AUTH_ERROR,
    "rate_limited": ProviderStatus.RATE_LIMITED,
    "server_error": ProviderStatus.DATA_ERROR,
    "data_format_error": ProviderStatus.DATA_ERROR,
    "data_quality_error": ProviderStatus.DATA_ERROR,
    "empty_data": ProviderStatus.DATA_ERROR,
    "stale_data": ProviderStatus.DEGRADED,
    "unknown": ProviderStatus.OFFLINE,
}


@dataclass
class _ProviderRecord:
    name: str
    display_name: str = ""
    events: deque = field(default_factory=lambda: deque(maxlen=20000))  # (ts, ok, latency_ms)
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    last_success: datetime | None = None
    last_failure: datetime | None = None
    last_failure_type: str = ""
    last_error: str = ""
    last_http_status: int | None = None
    last_latency_ms: float = 0.0
    rate_limited: bool = False
    rate_limited_until: datetime | None = None
    data_delay_seconds: float = 0.0
    completeness: float = 1.0
    consistency: float = 1.0       # 与其他源的一致性（0~1），由交叉验证回写
    failover_count_today: int = 0
    manual_status: ProviderStatus | None = None
    score_detail: dict[str, float] = field(default_factory=dict)


class ProviderHealthCenter:
    """线程安全（内部用 asyncio.Lock 保护临界区）的健康数据聚合。"""

    def __init__(self) -> None:
        self.s = get_settings()
        self._records: dict[str, _ProviderRecord] = {}
        self._lock = threading.RLock()
        self._async_lock = asyncio.Lock()
        self._failover_events: deque = deque(maxlen=500)
        self._day_key: str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._rolling_day_counter: dict[str, dict[str, int]] = {}

    # ---------------------------------------------------------------- 内部工具
    def _rec(self, name: str) -> _ProviderRecord:
        with self._lock:
            rec = self._records.get(name)
            if rec is None:
                rec = _ProviderRecord(name=name)
                self._records[name] = rec
            self._rotate_day()
            counter = self._rolling_day_counter.setdefault(
                name, {"fail": 0, "ok": 0, "date": self._day_key}
            )
            if counter["date"] != self._day_key:
                counter.update({"fail": 0, "ok": 0, "date": self._day_key})
            return rec

    def _rotate_day(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._day_key:
            self._day_key = today

    async def ensure(self, name: str, display_name: str = "") -> None:
        rec = self._rec(name)
        if display_name and not rec.display_name:
            rec.display_name = display_name

    # ---------------------------------------------------------------- 事件记录
    async def record_success(
        self,
        name: str,
        latency_ms: float = 0.0,
        *,
        http_status: int | None = None,
        delay_seconds: float = 0.0,
        completeness: float = 1.0,
    ) -> None:
        async with self._async_lock:
            rec = self._rec(name)
            now = datetime.now(timezone.utc)
            rec.events.append((now, True, latency_ms))
            rec.consecutive_failures = 0
            rec.consecutive_successes += 1
            rec.last_success = now
            rec.last_latency_ms = latency_ms
            rec.last_http_status = http_status
            rec.data_delay_seconds = delay_seconds
            rec.completeness = completeness
            rec.last_error = ""
            rec.last_failure_type = ""
            if rec.rate_limited_until and rec.rate_limited_until < now:
                rec.rate_limited = False
                rec.rate_limited_until = None
            self._rolling_day_counter[name]["ok"] += 1

    async def record_failure(
        self,
        name: str,
        failure_type: str = "unknown",
        message: str = "",
        *,
        latency_ms: float = 0.0,
        http_status: int | None = None,
    ) -> None:
        async with self._async_lock:
            rec = self._rec(name)
            now = datetime.now(timezone.utc)
            rec.events.append((now, False, latency_ms))
            rec.consecutive_successes = 0
            rec.consecutive_failures += 1
            rec.last_failure = now
            rec.last_failure_type = failure_type
            rec.last_error = (message or "")[:500]
            rec.last_latency_ms = latency_ms
            rec.last_http_status = http_status
            self._rolling_day_counter[name]["fail"] += 1
            if failure_type == "rate_limited":
                rec.rate_limited = True
                rec.rate_limited_until = now + timedelta(seconds=max(60, self.s.DEFAULT_BACKOFF_MAX * 10))
            logger.event(
                "provider.failure",
                provider=name,
                failure_type=failure_type,
                consecutive=rec.consecutive_failures,
                message=rec.last_error[:160],
            )

    async def record_cross_validation(self, name: str, consistent: bool, deviation_pct: float) -> None:
        """交叉验证结果回写，作为评分中的「一致性」因子。"""
        async with self._async_lock:
            rec = self._rec(name)
            target = 1.0 if consistent else max(0.0, 1.0 - min(deviation_pct, 5.0) / 5.0)
            # 指数平滑，避免偶发抖动过度影响评分
            rec.consistency = round(0.7 * rec.consistency + 0.3 * target, 4)

    async def record_failover(self, *, category: str, from_provider: str, to_provider: str, reason: str) -> None:
        async with self._async_lock:
            rec = self._rec(to_provider)
            rec.failover_count_today += 1
            self._failover_events.append(
                {
                    "ts": datetime.now(timezone.utc),
                    "category": category,
                    "from_provider": from_provider,
                    "to_provider": to_provider,
                    "reason": reason,
                }
            )
            logger.event(
                "provider.failover",
                category=category,
                from_provider=from_provider,
                to_provider=to_provider,
                reason=reason,
            )

    # ---------------------------------------------------------------- 指标计算
    def _window_stats(self, rec: _ProviderRecord, seconds: int) -> tuple[float, int, float]:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        oks = 0
        total = 0
        latencies: list[float] = []
        for ts, ok, lat in rec.events:
            ts_dt = ts if isinstance(ts, datetime) else datetime.fromtimestamp(ts, tz=timezone.utc)
            if ts_dt >= cutoff:
                total += 1
                if ok:
                    oks += 1
                    latencies.append(lat)
        rate = (oks / total) if total else 0.0
        avg_lat = statistics.mean(latencies) if latencies else 0.0
        return rate, total, avg_lat

    def _latency_score(self, latency_ms: float) -> float:
        if latency_ms <= 0:
            return 80.0
        if latency_ms <= 200:
            return 100.0
        if latency_ms >= 3000:
            return 0.0
        return max(0.0, 100.0 - (latency_ms - 200) / 2800 * 100)

    def compute_score(self, rec: _ProviderRecord) -> tuple[float, dict[str, float]]:
        rate1h, _, _ = self._window_stats(rec, 3600)
        rate24h, _, _ = self._window_stats(rec, self.s.HEALTH_WINDOW_24H)
        if not rec.events:
            stability = 100.0  # 无数据不惩罚
        else:
            stability = (0.6 * rate24h + 0.4 * rate1h) * 100
        latency = self._latency_score(rec.last_latency_ms or 0)
        freshness = 100.0
        if rec.last_success:
            age = (datetime.now(timezone.utc) - rec.last_success).total_seconds()
            if age > 3600:
                freshness = max(0.0, 100.0 - (age - 3600) / 3600 * 20)
        completeness = rec.completeness * 100
        consistency = rec.consistency * 100
        penalty = min(40.0, rec.consecutive_failures * 10)

        detail = {
            "stability": stability,
            "latency": latency,
            "consistency": consistency,
            "completeness": completeness,
            "freshness": freshness,
            "penalty": -penalty,
        }
        weights = {
            "stability": 0.35,
            "latency": 0.15,
            "consistency": 0.20,
            "completeness": 0.15,
            "freshness": 0.15,
        }
        score = sum(detail[k] * w for k, w in weights.items()) - penalty
        return max(0.0, min(100.0, score)), detail

    def status_of(self, name: str, *, enabled: bool, configured: bool) -> ProviderStatus:
        rec = self._rec(name)
        if rec.manual_status is not None:
            return rec.manual_status
        if not enabled:
            return ProviderStatus.DISABLED
        if not configured:
            return ProviderStatus.NOT_CONFIGURED
        if rec.rate_limited and rec.rate_limited_until and rec.rate_limited_until > datetime.now(timezone.utc):
            return ProviderStatus.RATE_LIMITED
        if rec.consecutive_failures >= self.s.FAILURE_THRESHOLD:
            return FAILURE_STATUS_MAP.get(rec.last_failure_type, ProviderStatus.OFFLINE)
        if rec.consecutive_failures > 0:
            return ProviderStatus.DEGRADED
        if rec.last_latency_ms and rec.last_latency_ms >= self.s.DEGRADED_LATENCY_MS:
            return ProviderStatus.DEGRADED
        if rec.last_latency_ms and rec.last_latency_ms >= self.s.SLOW_LATENCY_MS:
            return ProviderStatus.SLOW
        if rec.last_success is None and rec.last_failure is None:
            return ProviderStatus.UNKNOWN
        return ProviderStatus.ONLINE

    # ---------------------------------------------------------------- 输出
    def snapshot(self, name: str, *, display_name: str = "", enabled: bool = True,
                 configured: bool = True, priority: int = 0, is_backup: bool = False,
                 locked: bool = False, categories: list[str] | None = None):
        from .types import ProviderHealthSnapshot

        rec = self._rec(name)
        if display_name:
            rec.display_name = display_name
        rate1h, _, _ = self._window_stats(rec, 3600)
        rate24h, total24h, avg_lat = self._window_stats(rec, self.s.HEALTH_WINDOW_24H)
        score, detail = self.compute_score(rec)
        rec.score_detail = detail
        status = self.status_of(name, enabled=enabled, configured=configured)
        return ProviderHealthSnapshot(
            provider=name,
            display_name=rec.display_name or display_name or name,
            categories=categories or [],
            status=status,
            priority=priority,
            enabled=enabled,
            is_backup=is_backup,
            locked=locked,
            latency_ms=rec.last_latency_ms,
            avg_latency_ms=avg_lat,
            last_success=rec.last_success,
            last_failure=rec.last_failure,
            consecutive_failures=rec.consecutive_failures,
            consecutive_successes=rec.consecutive_successes,
            failures_today=self._rolling_day_counter[name]["fail"],
            requests_24h=total24h,
            success_rate_1h=rate1h,
            success_rate_24h=rate24h,
            http_status=rec.last_http_status,
            rate_limited=rec.rate_limited,
            data_delay_seconds=rec.data_delay_seconds,
            completeness=rec.completeness,
            last_error=rec.last_error,
            last_failure_type=rec.last_failure_type,
            score=score,
            score_detail=detail,
        )

    def failover_events(self, limit: int = 100) -> list[dict[str, Any]]:
        items = list(self._failover_events)[-limit:]
        return [
            {
                "ts": e["ts"].isoformat(),
                "category": e["category"],
                "from_provider": e["from_provider"],
                "to_provider": e["to_provider"],
                "reason": e["reason"],
            }
            for e in reversed(items)
        ]

    def cooldown_remaining(self, name: str) -> float:
        """熔断剩余时间（秒）。连续失败越多，退避越久，避免每次请求都被黑洞源拖住。"""
        rec = self._rec(name)
        if rec.consecutive_failures < 2 or rec.last_failure is None:
            return 0.0
        base = max(15.0, self.s.DEFAULT_BACKOFF_MAX * 2)
        cooldown = min(300.0, base * (2 ** min(rec.consecutive_failures - 2, 4)))
        elapsed = (datetime.now(timezone.utc) - rec.last_failure).total_seconds()
        return max(0.0, cooldown - elapsed)

    def in_cooldown(self, name: str) -> bool:
        return self.cooldown_remaining(name) > 0

    def can_promote(self, name: str) -> bool:
        """是否满足恢复为主源的条件（抖动抑制）。"""
        rec = self._rec(name)
        if rec.consecutive_successes < self.s.RECOVERY_SUCCESS_STREAK:
            return False
        if rec.last_latency_ms and rec.last_latency_ms > self.s.DEGRADED_LATENCY_MS:
            return False
        if rec.last_failure:
            elapsed = (datetime.now(timezone.utc) - rec.last_failure).total_seconds()
            if elapsed < self.s.RECOVERY_COOLDOWN_SECONDS:
                return False
        if rec.consistency < 0.9:
            return False
        return True

    def set_manual_status(self, name: str, status: ProviderStatus | None) -> None:
        self._rec(name).manual_status = status

    def reset_stats(self, name: str) -> None:
        rec = self._rec(name)
        rec.events.clear()
        rec.consecutive_failures = 0
        rec.consecutive_successes = 0
        rec.last_error = ""
        rec.last_failure = None
        rec.last_failure_type = ""
        rec.manual_status = None
        self._rolling_day_counter[name] = {"fail": 0, "ok": 0, "date": self._day_key}


_health_singleton: ProviderHealthCenter | None = None


def get_health_center() -> ProviderHealthCenter:
    global _health_singleton
    if _health_singleton is None:
        _health_singleton = ProviderHealthCenter()
    return _health_singleton
