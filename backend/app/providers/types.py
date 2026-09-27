# -*- coding: utf-8 -*-
"""Provider 领域公共类型。

所有 Provider 无论底层是 REST、CSV、WebSocket 还是本地文件，
对外都必须返回统一的 :class:`ProviderResult`，从而与业务逻辑彻底解耦。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Generic, TypeVar

T = TypeVar("T")

# ---------------------------------------------------------------- 数据类别


class DataCategory(str, Enum):
    """数据类别。每种类别拥有独立的主/备 Provider 链。"""

    MARKET_PRICE = "market_price"
    MARKET_TICKER_24H = "market_ticker_24h"
    OHLCV = "ohlcv"
    ORDERBOOK = "orderbook"
    MARKET_CAP = "market_cap"
    DERIVATIVES_FUNDING = "derivatives_funding"
    DERIVATIVES_OI = "derivatives_oi"
    DERIVATIVES_LIQUIDATION = "derivatives_liquidation"
    ONCHAIN_BASIC = "onchain_basic"
    ONCHAIN_ADVANCED = "onchain_advanced"
    EXCHANGE_FLOW = "exchange_flow"
    ETF_FLOW = "etf_flow"
    MACRO_SERIES = "macro_series"
    SENTIMENT_INDEX = "sentiment_index"


class ProviderStatus(str, Enum):
    ONLINE = "ONLINE"
    DEGRADED = "DEGRADED"
    SLOW = "SLOW"
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_ERROR = "AUTH_ERROR"
    NETWORK_ERROR = "NETWORK_ERROR"
    DATA_ERROR = "DATA_ERROR"
    OFFLINE = "OFFLINE"
    DISABLED = "DISABLED"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    UNKNOWN = "UNKNOWN"

    @property
    def healthy(self) -> bool:
        return self in (ProviderStatus.ONLINE, ProviderStatus.DEGRADED, ProviderStatus.SLOW)


class QualityStatus(str, Enum):
    """数据置信度状态，必须在 API 与前端显式呈现，不允许隐藏。"""

    VERIFIED = "VERIFIED"              # 多源交叉验证通过
    CROSS_VERIFIED = "CROSS_VERIFIED"  # 多源交叉验证通过（>2 源）
    SINGLE_SOURCE = "SINGLE_SOURCE"    # 仅单一数据源可得
    ESTIMATED = "ESTIMATED"            # 发生降級/推导，需降置信度
    STALE = "STALE"                    # 已是最近一次可信数据
    CONFLICT = "CONFLICT"              # 多源不一致
    MISSING = "MISSING"                # 明确无数据（绝不伪造）
    NOT_CONFIGURED = "NOT_CONFIGURED"  # 数据源未配置


@dataclass
class Candle:
    """标准 K 线（归一化后统一使用 UTC 秒级时间戳）。"""

    ts: int          # 开盘时间（秒，UTC）
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float = 0.0
    trades: int = 0
    interval: str = "1d"

    def to_list(self) -> list[Any]:
        return [self.ts, self.open, self.high, self.low, self.close, self.volume]


@dataclass
class SeriesPoint:
    ts: int
    value: float


@dataclass
class MetricPoint:
    """通用指标点：用于链上/宏观/ETF/情绪等时间序列。"""

    ts: int
    name: str
    value: float
    unit: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class FetchTrace:
    """一次真实抓取的可追溯元信息（用于原始数据存储与审计）。"""

    provider: str
    endpoint: str
    request_params: dict[str, Any] = field(default_factory=dict)
    http_status: int | None = None
    latency_ms: float = 0.0
    attempt: int = 0
    fetch_time: datetime | None = None
    raw_payload: Any = None
    note: str = ""


@dataclass
class ProviderResult(Generic[T]):
    """Provider 统一返回值。"""

    data: T
    provider: str
    category: DataCategory
    observation_time: datetime | None = None  # 数据本身的时间
    fetch_time: datetime | None = None        # 抓取时间
    latency_ms: float = 0.0
    quality: QualityStatus = QualityStatus.SINGLE_SOURCE
    confidence: float = 1.0
    trace: FetchTrace | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def source(self) -> str:
        return self.provider


@dataclass
class ProviderAttempt:
    """Failover 链路中一次尝试的记录（用于故障事件与前端展示）。"""

    provider: str
    ok: bool
    latency_ms: float = 0.0
    failure_type: str | None = None
    http_status: int | None = None
    message: str = ""
    priority: int = 0


@dataclass
class RoutedResult(Generic[T]):
    """经过 Failover 路由后的最终结果，携带完整来源信息与 Attempts。"""

    data: T
    provider: str
    category: DataCategory
    primary_provider: str
    used_fallback: bool = False
    failover_reason: str = ""
    failover_time: datetime | None = None
    quality: QualityStatus = QualityStatus.SINGLE_SOURCE
    confidence: float = 1.0
    observation_time: datetime | None = None
    fetch_time: datetime | None = None
    latency_ms: float = 0.0
    attempts: list[ProviderAttempt] = field(default_factory=list)
    cross_validation: dict[str, Any] | None = None
    trace: FetchTrace | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def is_stale(self) -> bool:
        return self.quality == QualityStatus.STALE

    def to_source_block(self) -> dict[str, Any]:
        """统一的前端「数据从哪里来」信息块。"""
        return {
            "provider": self.provider,
            "display_name": self.meta.get("display_name", self.provider),
            "primary_provider": self.primary_provider,
            "used_fallback": self.used_fallback,
            "failover_reason": self.failover_reason,
            "failover_time": self.failover_time.isoformat() if self.failover_time else None,
            "quality": self.quality.value,
            "confidence": round(self.confidence, 4),
            "observation_time": self.observation_time.isoformat() if self.observation_time else None,
            "fetch_time": self.fetch_time.isoformat() if self.fetch_time else None,
            "latency_ms": round(self.latency_ms, 2),
            "attempts": [
                {
                    "provider": a.provider,
                    "ok": a.ok,
                    "latency_ms": round(a.latency_ms, 2),
                    "failure_type": a.failure_type,
                    "http_status": a.http_status,
                    "message": a.message,
                    "priority": a.priority,
                }
                for a in self.attempts
            ],
            "cross_validation": self.cross_validation,
        }


@dataclass
class ProviderHealthSnapshot:
    provider: str
    display_name: str
    categories: list[str]
    status: ProviderStatus
    priority: int
    enabled: bool
    is_backup: bool
    locked: bool
    latency_ms: float
    avg_latency_ms: float
    last_success: datetime | None
    last_failure: datetime | None
    consecutive_failures: int
    consecutive_successes: int
    failures_today: int
    requests_24h: int
    success_rate_1h: float
    success_rate_24h: float
    http_status: int | None
    rate_limited: bool
    data_delay_seconds: float
    completeness: float
    last_error: str
    last_failure_type: str
    score: float
    score_detail: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "display_name": self.display_name,
            "categories": self.categories,
            "status": self.status.value,
            "healthy": self.status.healthy,
            "priority": self.priority,
            "enabled": self.enabled,
            "is_backup": self.is_backup,
            "locked": self.locked,
            "latency_ms": round(self.latency_ms, 2),
            "avg_latency_ms": round(self.avg_latency_ms, 2),
            "last_success": self.last_success.isoformat() if self.last_success else None,
            "last_failure": self.last_failure.isoformat() if self.last_failure else None,
            "consecutive_failures": self.consecutive_failures,
            "consecutive_successes": self.consecutive_successes,
            "failures_today": self.failures_today,
            "requests_24h": self.requests_24h,
            "success_rate_1h": round(self.success_rate_1h, 4),
            "success_rate_24h": round(self.success_rate_24h, 4),
            "http_status": self.http_status,
            "rate_limited": self.rate_limited,
            "data_delay_seconds": round(self.data_delay_seconds, 2),
            "completeness": round(self.completeness, 4),
            "last_error": self.last_error,
            "last_failure_type": self.last_failure_type,
            "score": round(self.score, 2),
            "score_detail": {k: round(v, 2) for k, v in self.score_detail.items()},
        }
