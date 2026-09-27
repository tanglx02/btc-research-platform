# -*- coding: utf-8 -*-
"""全平台数据字典（ORM）。

设计约束（来自产品原则）：
* 所有核心数据表必须包含：source_id / provider / observation_time / fetch_time /
  created_at / updated_at / quality_status —— 每个结论都可追溯、都知道来源与时间。
* Raw 数据与 Normalized 数据双存储：raw_* 表保留原始响应，
  Provider 停用或更换后可重新解析历史。
* PostgreSQL 生产环境建议使用 TimescaleDB 把 candles / market_prices 转为 hypertable。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# 主键类型：PostgreSQL 用 BIGSERIAL；SQLite 必须用 INTEGER，
# 因为 SQLite 只对 `INTEGER PRIMARY KEY` 自动生成 rowid，写成 BIGINT 会导致自增失效（NOT NULL 约束失败）。
PK_BIGINT = BigInteger().with_variant(Integer, "sqlite")


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON}


class SourceMixin:
    """所有核心数据的公共可追溯字段。"""

    id: Mapped[int] = mapped_column(PK_BIGINT, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True, comment="数据源 Provider 名称")
    quality_status: Mapped[str] = mapped_column(String(32), default="SINGLE_SOURCE", nullable=False)
    observation_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True, comment="数据本身时间")
    fetch_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True, comment="抓取时间")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


# ============================================================ 资产与 Provider 治理


class Asset(Base):
    __tablename__ = "assets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    category: Mapped[str] = mapped_column(String(32), default="crypto")
    quote_currency: Mapped[str] = mapped_column(String(16), default="USD")
    halving_dates: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProviderRecord(Base):
    """Provider 配置表：支持运行时启用/禁用/改优先级/改 Key/改代理。"""

    __tablename__ = "providers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(128), default="")
    module: Mapped[str] = mapped_column(String(255), default="")
    categories: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    manual_priority: Mapped[int | None] = mapped_column(Integer, default=None)
    api_key_env: Mapped[str | None] = mapped_column(String(64))
    proxy: Mapped[str | None] = mapped_column(String(255))
    timeout_connect: Mapped[float | None] = mapped_column(Float)
    timeout_read: Mapped[float | None] = mapped_column(Float)
    retries: Mapped[int | None] = mapped_column(Integer)
    qps: Mapped[float | None] = mapped_column(Float)
    extra_headers: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    disabled_reason: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ProviderHealth(Base):
    __tablename__ = "provider_health"
    id: Mapped[int] = mapped_column(PK_BIGINT, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    avg_latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    success_rate_1h: Mapped[float] = mapped_column(Float, default=0.0)
    success_rate_24h: Mapped[float] = mapped_column(Float, default=0.0)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_successes: Mapped[int] = mapped_column(Integer, default=0)
    failures_today: Mapped[int] = mapped_column(Integer, default=0)
    requests_24h: Mapped[int] = mapped_column(Integer, default=0)
    http_status: Mapped[int | None] = mapped_column(Integer)
    rate_limited: Mapped[bool] = mapped_column(Boolean, default=False)
    data_delay_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    completeness: Mapped[float] = mapped_column(Float, default=1.0)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    score_detail: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    last_success: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_type: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(Text)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class ProviderFailoverEvent(Base):
    __tablename__ = "provider_failover_events"
    id: Mapped[int] = mapped_column(PK_BIGINT, primary_key=True, autoincrement=True)
    category: Mapped[str] = mapped_column(String(64), index=True)
    from_provider: Mapped[str] = mapped_column(String(64))
    to_provider: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str | None] = mapped_column(Text)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class ProviderRequest(Base):
    """每一次外部请求的审计记录（限量保留，用于排障与限流分析）。"""

    __tablename__ = "provider_requests"
    id: Mapped[int] = mapped_column(PK_BIGINT, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    endpoint: Mapped[str] = mapped_column(String(255))
    http_status: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    failure_type: Mapped[str | None] = mapped_column(String(64))
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    request_params: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


# ============================================================ 市场数据（Raw + Normalized）


class RawMarketData(Base):
    """原始响应留档：Provider 更换后可重新解析历史数据。"""

    __tablename__ = "raw_market_data"
    id: Mapped[int] = mapped_column(PK_BIGINT, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    endpoint: Mapped[str] = mapped_column(String(255))
    category: Mapped[str] = mapped_column(String(64), index=True)
    request_params: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    payload: Mapped[str] = mapped_column(Text)
    http_status: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    unit: Mapped[str | None] = mapped_column(String(32))
    version: Mapped[str | None] = mapped_column(String(32))
    fetch_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class MarketPrice(SourceMixin, Base):
    __tablename__ = "market_prices"
    __table_args__ = (
        UniqueConstraint("symbol", "source_id", "observation_time", name="uq_market_price"),
        Index("ix_market_price_symbol_time", "symbol", "observation_time"),
    )
    symbol: Mapped[str] = mapped_column(String(16), default="BTC", index=True)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    quote: Mapped[str] = mapped_column(String(16), default="USD")
    cross_validation: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)


class Candle(SourceMixin, Base):
    __tablename__ = "candles"
    __table_args__ = (
        UniqueConstraint("symbol", "interval", "ts", "source_id", name="uq_candle"),
        Index("ix_candle_symbol_interval_ts", "symbol", "interval", "ts"),
    )
    symbol: Mapped[str] = mapped_column(String(16), default="BTC", index=True)
    interval: Mapped[str] = mapped_column(String(8), default="1d", index=True)
    ts: Mapped[int] = mapped_column(BigInteger, index=True, comment="开盘时间，UTC 秒")
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float, default=0.0)
    quote_volume: Mapped[float] = mapped_column(Float, default=0.0)
    trades: Mapped[int] = mapped_column(Integer, default=0)


class Orderbook(Base):
    __tablename__ = "orderbooks"
    id: Mapped[int] = mapped_column(PK_BIGINT, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(16), default="BTC")
    bids: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    asks: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    spread: Mapped[float | None] = mapped_column(Float)
    quality_status: Mapped[str] = mapped_column(String(32), default="SINGLE_SOURCE")
    observation_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetch_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


# ============================================================ 链上 / 资金流 / ETF / 衍生品 / 期权 / 宏观 / 情绪


class OnchainMetric(SourceMixin, Base):
    __tablename__ = "onchain_metrics"
    __table_args__ = (
        UniqueConstraint("metric", "symbol", "observation_ts", "source_id", name="uq_onchain"),
        Index("ix_onchain_metric_time", "metric", "observation_ts"),
    )
    symbol: Mapped[str] = mapped_column(String(16), default="BTC")
    metric: Mapped[str] = mapped_column(String(64), index=True, comment="mvrv / sopr / nupl / active_addresses ...")
    value: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str | None] = mapped_column(String(32))
    observation_ts: Mapped[int] = mapped_column(BigInteger, index=True)
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class ExchangeFlow(SourceMixin, Base):
    __tablename__ = "exchange_flows"
    __table_args__ = (UniqueConstraint("metric", "observation_ts", "source_id", name="uq_exflow"),)
    metric: Mapped[str] = mapped_column(String(64), index=True, comment="inflow / outflow / netflow / reserve")
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(16), default="BTC")
    observation_ts: Mapped[int] = mapped_column(BigInteger, index=True)


class EtfFlow(SourceMixin, Base):
    __tablename__ = "etf_flows"
    __table_args__ = (UniqueConstraint("issuer", "observation_ts", "source_id", name="uq_etf"),)
    issuer: Mapped[str] = mapped_column(String(32), default="ALL", index=True)
    inflow_usd: Mapped[float] = mapped_column(Float, default=0.0)
    outflow_usd: Mapped[float] = mapped_column(Float, default=0.0)
    net_flow_usd: Mapped[float] = mapped_column(Float, default=0.0)
    total_assets_usd: Mapped[float] = mapped_column(Float, default=0.0)
    observation_ts: Mapped[int] = mapped_column(BigInteger, index=True)


class Derivative(Base):
    __tablename__ = "derivatives"
    id: Mapped[int] = mapped_column(PK_BIGINT, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(16), default="BTC")
    exchange: Mapped[str] = mapped_column(String(32), default="", index=True)
    funding_rate: Mapped[float | None] = mapped_column(Float)
    funding_annualized_pct: Mapped[float | None] = mapped_column(Float)
    open_interest: Mapped[float | None] = mapped_column(Float)
    open_interest_usd: Mapped[float | None] = mapped_column(Float)
    liquidation_long_usd: Mapped[float | None] = mapped_column(Float)
    liquidation_short_usd: Mapped[float | None] = mapped_column(Float)
    long_short_ratio: Mapped[float | None] = mapped_column(Float)
    basis_bps: Mapped[float | None] = mapped_column(Float)
    quality_status: Mapped[str] = mapped_column(String(32), default="SINGLE_SOURCE")
    observation_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    fetch_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OptionMetric(Base):
    __tablename__ = "options"
    id: Mapped[int] = mapped_column(PK_BIGINT, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(16), default="BTC")
    open_interest: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)
    iv: Mapped[float | None] = mapped_column(Float)
    put_call_ratio: Mapped[float | None] = mapped_column(Float)
    skew: Mapped[float | None] = mapped_column(Float)
    quality_status: Mapped[str] = mapped_column(String(32), default="MISSING")
    observation_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    fetch_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class MacroSeries(Base):
    """宏观序列：严格区分 observation_ts（观测期）与 release_ts（发布日），防止未来数据泄漏。"""

    __tablename__ = "macro_series"
    __table_args__ = (
        UniqueConstraint("series_id", "observation_ts", "source_id", name="uq_macro"),
        Index("ix_macro_series_time", "series_id", "observation_ts"),
    )
    id: Mapped[int] = mapped_column(PK_BIGINT, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(String(64), index=True)
    series_id: Mapped[str] = mapped_column(String(32), index=True)
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(16), default="index")
    observation_ts: Mapped[int] = mapped_column(BigInteger, index=True, comment="观测所属期间")
    release_ts: Mapped[int | None] = mapped_column(BigInteger, index=True, comment="官方发布日，回测只能在此之后使用")
    revision: Mapped[int] = mapped_column(Integer, default=0, comment="修订次数：宏观数据会被事后修正")
    quality_status: Mapped[str] = mapped_column(String(32), default="SINGLE_SOURCE")
    fetch_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Sentiment(Base):
    __tablename__ = "sentiment"
    __table_args__ = (UniqueConstraint("metric", "observation_ts", "source_id", name="uq_sentiment"),)
    id: Mapped[int] = mapped_column(PK_BIGINT, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(String(64), index=True)
    metric: Mapped[str] = mapped_column(String(32), default="fear_greed", index=True)
    value: Mapped[float] = mapped_column(Float)
    classification: Mapped[str | None] = mapped_column(String(32))
    observation_ts: Mapped[int] = mapped_column(BigInteger, index=True)
    quality_status: Mapped[str] = mapped_column(String(32), default="SINGLE_SOURCE")
    fetch_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


# ============================================================ 指标 / 周期 / 风险 / Regime / 信号


class IndicatorDefinition(Base):
    """指标字典：每个指标都有公式、分类、解释、单位、来源，供前端「为什么」展开。"""

    __tablename__ = "indicator_definitions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name_cn: Mapped[str] = mapped_column(String(128))
    name_en: Mapped[str] = mapped_column(String(128), default="")
    category: Mapped[str] = mapped_column(String(32), index=True)
    formula: Mapped[str | None] = mapped_column(Text)
    formula_readable: Mapped[str | None] = mapped_column(Text)
    meaning_cn: Mapped[str | None] = mapped_column(Text, comment="普通人能看懂的含义")
    how_to_use: Mapped[str | None] = mapped_column(Text)
    bullish_hint: Mapped[str | None] = mapped_column(Text)
    bearish_hint: Mapped[str | None] = mapped_column(Text)
    unit: Mapped[str | None] = mapped_column(String(32))
    value_range: Mapped[str | None] = mapped_column(String(64))
    data_categories: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    recommended_providers: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    requires_paid_provider: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IndicatorValue(Base):
    __tablename__ = "indicator_values"
    __table_args__ = (
        UniqueConstraint("code", "symbol", "interval", "ts", name="uq_indicator_value"),
        Index("ix_indicator_code_ts", "code", "ts"),
    )
    id: Mapped[int] = mapped_column(PK_BIGINT, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(16), default="BTC")
    interval: Mapped[str] = mapped_column(String(8), default="1d")
    ts: Mapped[int] = mapped_column(BigInteger, index=True)
    value: Mapped[float] = mapped_column(Float)
    percentile: Mapped[float | None] = mapped_column(Float)
    zscore: Mapped[float | None] = mapped_column(Float)
    source_provider: Mapped[str] = mapped_column(String(64), default="derived")
    model_version: Mapped[str] = mapped_column(String(32), default="v1")
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CycleState(Base):
    __tablename__ = "cycle_states"
    id: Mapped[int] = mapped_column(PK_BIGINT, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), default="BTC")
    date: Mapped[int] = mapped_column(BigInteger, index=True)
    phase: Mapped[str] = mapped_column(String(32), index=True, comment="deep_bear / bear / accumulation / recovery ...")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    score_detail: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSON, comment="支持证据")
    counter_evidence: Mapped[dict[str, Any] | None] = mapped_column(JSON, comment="反向证据")
    similar_periods: Mapped[dict[str, Any] | None] = mapped_column(JSON, comment="历史相似阶段")
    model_version: Mapped[str] = mapped_column(String(32), default="cycle_v1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RiskScore(Base):
    __tablename__ = "risk_scores"
    id: Mapped[int] = mapped_column(PK_BIGINT, primary_key=True, autoincrement=True)
    date: Mapped[int] = mapped_column(BigInteger, index=True)
    trend_risk: Mapped[float] = mapped_column(Float, default=0.0)
    valuation_risk: Mapped[float] = mapped_column(Float, default=0.0)
    leverage_risk: Mapped[float] = mapped_column(Float, default=0.0)
    liquidity_risk: Mapped[float] = mapped_column(Float, default=0.0)
    macro_risk: Mapped[float] = mapped_column(Float, default=0.0)
    onchain_risk: Mapped[float] = mapped_column(Float, default=0.0)
    overall_risk: Mapped[float] = mapped_column(Float, default=0.0)
    level: Mapped[str] = mapped_column(String(16), default="unknown")
    reasons: Mapped[dict[str, Any] | None] = mapped_column(JSON, comment="可展开的全部原因")
    data_availability: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    model_version: Mapped[str] = mapped_column(String(32), default="risk_v1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MarketRegime(Base):
    __tablename__ = "market_regimes"
    id: Mapped[int] = mapped_column(PK_BIGINT, primary_key=True, autoincrement=True)
    date: Mapped[int] = mapped_column(BigInteger, index=True)
    trend_state: Mapped[str] = mapped_column(String(32))
    valuation_state: Mapped[str] = mapped_column(String(32))
    capital_flow_state: Mapped[str] = mapped_column(String(32))
    onchain_state: Mapped[str] = mapped_column(String(32))
    derivative_state: Mapped[str] = mapped_column(String(32))
    macro_state: Mapped[str] = mapped_column(String(32))
    sentiment_state: Mapped[str] = mapped_column(String(32))
    risk_state: Mapped[str] = mapped_column(String(32))
    cycle_state: Mapped[str] = mapped_column(String(32))
    regime: Mapped[str] = mapped_column(String(64), index=True)
    summary_cn: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    model_version: Mapped[str] = mapped_column(String(32), default="regime_v1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Signal(Base):
    __tablename__ = "signals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name_cn: Mapped[str] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(32))
    direction: Mapped[str] = mapped_column(String(16), default="neutral")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SignalEvent(Base):
    __tablename__ = "signal_events"
    id: Mapped[int] = mapped_column(PK_BIGINT, primary_key=True, autoincrement=True)
    signal_code: Mapped[str] = mapped_column(String(64), index=True)
    date: Mapped[int] = mapped_column(BigInteger, index=True)
    triggered: Mapped[bool] = mapped_column(Boolean, default=False)
    value: Mapped[float | None] = mapped_column(Float)
    threshold: Mapped[float | None] = mapped_column(Float)
    message_cn: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ModelVersion(Base):
    __tablename__ = "model_versions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(32), comment="cycle / valuation / risk / forecast / strategy")
    params: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    dev_period: Mapped[str | None] = mapped_column(String(64), comment="开发区间")
    test_period: Mapped[str | None] = mapped_column(String(64), comment="测试区间")
    metrics: Mapped[dict[str, Any] | None] = mapped_column(JSON, comment="样本外表现")
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ModelWeight(Base):
    __tablename__ = "model_weights"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    model_code: Mapped[str] = mapped_column(String(64), index=True)
    factor: Mapped[str] = mapped_column(String(64))
    weight: Mapped[float] = mapped_column(Float)
    rationale: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


# ============================================================ 回测 / 策略


class BacktestRun(Base):
    __tablename__ = "backtest_runs"
    id: Mapped[int] = mapped_column(PK_BIGINT, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    strategy_code: Mapped[str] = mapped_column(String(64), index=True)
    strategy_params: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    plan_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    start_date: Mapped[str] = mapped_column(String(16))
    end_date: Mapped[str] = mapped_column(String(16))
    initial_capital: Mapped[float] = mapped_column(Float, default=0.0)
    fee_rate: Mapped[float] = mapped_column(Float, default=0.001)
    slippage: Mapped[float] = mapped_column(Float, default=0.0)
    data_version: Mapped[str] = mapped_column(String(64), default="")
    model_version: Mapped[str] = mapped_column(String(32), default="bt_v1")
    validation_mode: Mapped[str] = mapped_column(String(32), default="insample", comment="insample/oos/walk_forward")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class BacktestResult(Base):
    __tablename__ = "backtest_results"
    id: Mapped[int] = mapped_column(PK_BIGINT, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(BigInteger, index=True)
    final_value: Mapped[float] = mapped_column(Float, default=0.0)
    total_invested: Mapped[float] = mapped_column(Float, default=0.0)
    net_profit: Mapped[float] = mapped_column(Float, default=0.0)
    roi: Mapped[float] = mapped_column(Float, default=0.0)
    cagr: Mapped[float] = mapped_column(Float, default=0.0)
    max_drawdown: Mapped[float] = mapped_column(Float, default=0.0)
    max_drawdown_duration_days: Mapped[int] = mapped_column(Integer, default=0)
    recovery_days: Mapped[int] = mapped_column(Integer, default=0)
    sharpe: Mapped[float] = mapped_column(Float, default=0.0)
    sortino: Mapped[float] = mapped_column(Float, default=0.0)
    volatility: Mapped[float] = mapped_column(Float, default=0.0)
    best_year: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    worst_year: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    btc_amount: Mapped[float] = mapped_column(Float, default=0.0)
    avg_cost: Mapped[float] = mapped_column(Float, default=0.0)
    total_fees: Mapped[float] = mapped_column(Float, default=0.0)
    trades: Mapped[int] = mapped_column(Integer, default=0)
    equity_curve: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    monthly_returns: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    warnings: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ============================================================ 用户计划 / 资产 / 快照


class UserPlan(Base):
    __tablename__ = "user_plans"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    initial_capital: Mapped[float] = mapped_column(Float, default=0.0)
    monthly_income: Mapped[float] = mapped_column(Float, default=0.0)
    monthly_contribution: Mapped[float] = mapped_column(Float, default=0.0)
    weekly_contribution: Mapped[float] = mapped_column(Float, default=0.0)
    contribution_frequency: Mapped[str] = mapped_column(String(16), default="monthly")
    start_date: Mapped[str] = mapped_column(String(16))
    end_date: Mapped[str | None] = mapped_column(String(16))
    cash_reserve: Mapped[float] = mapped_column(Float, default=0.0)
    max_single_contribution: Mapped[float] = mapped_column(Float, default=0.0)
    max_drawdown_tolerance: Mapped[float] = mapped_column(Float, default=50.0)
    strategy_code: Mapped[str] = mapped_column(String(64), default="dca_fixed")
    strategy_params: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class UserTransaction(Base):
    __tablename__ = "user_transactions"
    id: Mapped[int] = mapped_column(PK_BIGINT, primary_key=True, autoincrement=True)
    plan_id: Mapped[int | None] = mapped_column(Integer, index=True)
    side: Mapped[str] = mapped_column(String(8), default="buy")
    ts: Mapped[int] = mapped_column(BigInteger, index=True)
    date: Mapped[str] = mapped_column(String(16))
    price: Mapped[float] = mapped_column(Float)
    amount_btc: Mapped[float] = mapped_column(Float)
    amount_fiat: Mapped[float] = mapped_column(Float)
    fee: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    note: Mapped[str | None] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(16), default="manual", comment="manual / plan_simulation")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserHolding(Base):
    __tablename__ = "user_holdings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int | None] = mapped_column(Integer, index=True)
    symbol: Mapped[str] = mapped_column(String(16), default="BTC")
    total_btc: Mapped[float] = mapped_column(Float, default=0.0)
    total_invested: Mapped[float] = mapped_column(Float, default=0.0)
    avg_cost: Mapped[float] = mapped_column(Float, default=0.0)
    cash_balance: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"
    id: Mapped[int] = mapped_column(PK_BIGINT, primary_key=True, autoincrement=True)
    plan_id: Mapped[int | None] = mapped_column(Integer, index=True)
    date: Mapped[int] = mapped_column(BigInteger, index=True)
    market_value: Mapped[float] = mapped_column(Float, default=0.0)
    invested: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    roi: Mapped[float] = mapped_column(Float, default=0.0)
    max_drawdown: Mapped[float] = mapped_column(Float, default=0.0)
    price_used: Mapped[float] = mapped_column(Float, default=0.0)
    price_provider: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ============================================================ 数据质量 / 任务 / 审计 / 事件


class DataQuality(Base):
    __tablename__ = "data_quality"
    id: Mapped[int] = mapped_column(PK_BIGINT, primary_key=True, autoincrement=True)
    category: Mapped[str] = mapped_column(String(64), index=True)
    date: Mapped[int] = mapped_column(BigInteger, index=True)
    expected: Mapped[int] = mapped_column(Integer, default=0)
    actual: Mapped[int] = mapped_column(Integer, default=0)
    missing_count: Mapped[int] = mapped_column(Integer, default=0)
    conflicts: Mapped[int] = mapped_column(Integer, default=0)
    completeness: Mapped[float] = mapped_column(Float, default=1.0)
    status: Mapped[str] = mapped_column(String(16), default="ok")
    notes: Mapped[str | None] = mapped_column(Text)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class SyncCheckpoint(Base):
    """断点续传：任务崩溃后从上次位置继续，而不是从头重来。"""

    __tablename__ = "sync_checkpoints"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    category: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(64), default="")
    cursor_ts: Mapped[int] = mapped_column(BigInteger, default=0, comment="已同步到的时间（UTC 秒）")
    cursor_date: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="idle", comment="idle/running/paused/failed/done")
    total_rows: Mapped[int] = mapped_column(BigInteger, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    extra: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class SystemJob(Base):
    __tablename__ = "system_jobs"
    id: Mapped[int] = mapped_column(PK_BIGINT, primary_key=True, autoincrement=True)
    job_name: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), default="success", comment="success/failed/running/skipped")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    rows_affected: Mapped[int] = mapped_column(Integer, default=0)
    provider: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str | None] = mapped_column(Text)


class TimelineEvent(Base):
    """市场事件时间线：减半、ATH、暴跌、ETF、美联储议息、CPI..."""

    __tablename__ = "timeline_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[str] = mapped_column(String(16), index=True)
    ts: Mapped[int] = mapped_column(BigInteger, index=True)
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    title_cn: Mapped[str] = mapped_column(String(255))
    description_cn: Mapped[str | None] = mapped_column(Text)
    impact: Mapped[str] = mapped_column(String(16), default="neutral")
    source: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(PK_BIGINT, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(64), default="system")
    action: Mapped[str] = mapped_column(String(64), index=True)
    target: Mapped[str | None] = mapped_column(String(128))
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


# ============================================================ 智能监测与预警
# 增量模块：以下表全部为新增，不改动任何已有表结构。
# 条件模型刻意设计成「树」：Rule -> (Group -> Group ...) / Condition，
# 这样新增一种可监测数据只需要在指标目录里登记一条，不需要改规则引擎与通知系统。


class AlertRule(Base):
    """一条监测规则。状态机 NORMAL -> TRIGGERED -> COOLDOWN -> RECOVERED。"""

    __tablename__ = "alert_rules"
    id: Mapped[int] = mapped_column(PK_BIGINT, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    # 规则真假的根节点：指向一个 condition_group
    root_group_id: Mapped[int | None] = mapped_column(Integer, index=True)
    logic: Mapped[str] = mapped_column(String(8), default="AND", comment="根层组合逻辑 AND / OR")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    paused: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), default="WARNING", comment="INFO/WARNING/HIGH/CRITICAL")
    cooldown_seconds: Mapped[int] = mapped_column(Integer, default=86400, comment="触发后冷却秒数，冷却内不重复通知")
    notify_on_recover: Mapped[bool] = mapped_column(Boolean, default=False)
    # 触发门槛：数据质量与 Provider 要求
    min_quality: Mapped[str] = mapped_column(String(32), default="VERIFIED",
                                             comment="允许触发所需的最低数据质量：ANY/SINGLE_SOURCE/VERIFIED/CROSS_VERIFIED")
    require_fresh: Mapped[bool] = mapped_column(Boolean, default=True, comment="是否拒绝用过期(stale)数据触发")
    require_multi_source: Mapped[bool] = mapped_column(Boolean, default=False,
                                                       comment="是否要求至少两个独立数据源确认后才触发")
    check_interval_seconds: Mapped[int] = mapped_column(Integer, default=60, comment="本条规则的检测周期")
    plan_id: Mapped[int | None] = mapped_column(Integer, index=True, comment="关联的个人资金计划（可选）")
    template_code: Mapped[str | None] = mapped_column(String(64), comment="来源模板（仅用于展示）")
    # 运行态
    state: Mapped[str] = mapped_column(String(16), default="NORMAL", index=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_trigger_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_recovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trigger_count: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_hits: Mapped[int] = mapped_column(Integer, default=0, comment="连续满足次数（供连续次数条件使用）")
    condition_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                            comment="本次条件开始满足的时间（供持续时长条件使用）")
    last_eval: Mapped[dict[str, Any] | None] = mapped_column(JSON, comment="最近一次求值详情")
    last_error: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AlertConditionGroup(Base):
    """条件分组，支持嵌套：一个组包含若干子组与若干条件，用 AND/OR/NOT 组合。"""

    __tablename__ = "alert_condition_groups"
    id: Mapped[int] = mapped_column(PK_BIGINT, primary_key=True, autoincrement=True)
    rule_id: Mapped[int] = mapped_column(Integer, index=True)
    parent_group_id: Mapped[int | None] = mapped_column(Integer, index=True, comment="父分组，NULL 表示根")
    operator: Mapped[str] = mapped_column(String(8), default="AND", comment="AND / OR / NOT")
    label: Mapped[str | None] = mapped_column(String(128), comment="人类可读的组说明")
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AlertCondition(Base):
    """单个条件。“通用”体现在：监测对象由 metric_code 决定，条件类型由 operator 决定。"""

    __tablename__ = "alert_conditions"
    id: Mapped[int] = mapped_column(PK_BIGINT, primary_key=True, autoincrement=True)
    rule_id: Mapped[int] = mapped_column(Integer, index=True)
    group_id: Mapped[int | None] = mapped_column(Integer, index=True, comment="所属分组，NULL 表示直接挂在规则根上")
    metric_code: Mapped[str] = mapped_column(String(64), index=True, comment="指标目录中的编码，如 price / rsi14 / mvrv")
    operator: Mapped[str] = mapped_column(String(32), comment="gt/gte/lt/lte/eq/between/enter_range/exit_range/cross_above/cross_below/change_gt/change_lt/percentile_gt/percentile_lt/state_change")
    threshold: Mapped[float | None] = mapped_column(Float)
    threshold_high: Mapped[float | None] = mapped_column(Float, comment="区间上界")
    compare_metric: Mapped[str | None] = mapped_column(String(64), comment="交叉条件的右值指标，如 ma200")
    change_window: Mapped[str | None] = mapped_column(String(8), comment="变化率窗口：1h/4h/12h/24h/7d/30d")
    percentile_window_days: Mapped[int | None] = mapped_column(Integer, comment="历史分位回看天数，NULL 表示全历史")
    expected_state: Mapped[str | None] = mapped_column(String(64), comment="状态类条件的目标状态值")
    # 持续时间 / 连续次数 / 交叉抑制
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0, comment="需持续满足的秒数，0 表示瞬时")
    consecutive_count: Mapped[int] = mapped_column(Integer, default=1, comment="需连续满足的检测次数")
    min_quality: Mapped[str | None] = mapped_column(String(32), comment="该条条件单独的数据质量要求")
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AlertEvent(Base):
    """一次触发记录。即使邮件发不出去，这条记录也必须存在（保证提醒不丢）。"""

    __tablename__ = "alert_events"
    id: Mapped[int] = mapped_column(PK_BIGINT, primary_key=True, autoincrement=True)
    rule_id: Mapped[int] = mapped_column(Integer, index=True)
    rule_name: Mapped[str] = mapped_column(String(128), default="")
    severity: Mapped[str] = mapped_column(String(16), default="WARNING", index=True)
    event_type: Mapped[str] = mapped_column(String(16), default="triggered", index=True,
                                           comment="triggered / recovered / quality_warning")
    trigger_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    metric_values: Mapped[dict[str, Any] | None] = mapped_column(JSON, comment="求值时各指标的实际值")
    threshold: Mapped[dict[str, Any] | None] = mapped_column(JSON, comment="各条件对应的阈值")
    condition_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, comment="逐条件的结果明细（含为什么触发）")
    market_context: Mapped[dict[str, Any] | None] = mapped_column(JSON, comment="触发时的市场状态快照")
    provider: Mapped[str | None] = mapped_column(String(64), comment="本次判断所依赖的主要数据源")
    provider_chain: Mapped[str | None] = mapped_column(String(255), comment="实际使用的数据源链（含备用）")
    used_fallback: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否处于故障切换状态")
    data_quality: Mapped[str | None] = mapped_column(String(32))
    data_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notification_status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True,
                                                     comment="PENDING/SENDING/SENT/FAILED/SKIPPED")
    email_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    email_message_id: Mapped[str | None] = mapped_column(String(255))
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    recovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AlertNotificationChannel(Base):
    """通知渠道。第一阶段实现 email，其余类型预留（新增渠道不需要改 Alert Engine）。"""

    __tablename__ = "alert_notification_channels"
    id: Mapped[int] = mapped_column(PK_BIGINT, primary_key=True, autoincrement=True)
    rule_id: Mapped[int | None] = mapped_column(Integer, index=True, comment="NULL 表示全局渠道配置")
    channel_type: Mapped[str] = mapped_column(String(32), default="email", comment="email/telegram/webhook/...")
    target: Mapped[str | None] = mapped_column(String(255), comment="收件人 / 群组 / URL")
    config: Mapped[dict[str, Any] | None] = mapped_column(JSON, comment="渠道私有配置（敏感项只存引用，不存明文密码）")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 发信失败熔断：连续失败到阈值就停止重试并告警，避免邮件风暴
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disabled_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), comment="熔断恢复时间")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AlertNotificationLog(Base):
    """每一次发送尝试都留痕，便于回答‘为什么没收到’与‘重复发了几封’。"""

    __tablename__ = "alert_notification_logs"
    id: Mapped[int] = mapped_column(PK_BIGINT, primary_key=True, autoincrement=True)
    event_id: Mapped[int | None] = mapped_column(Integer, index=True)
    rule_id: Mapped[int | None] = mapped_column(Integer, index=True)
    channel_type: Mapped[str] = mapped_column(String(32), default="email")
    target: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True, comment="PENDING/SENDING/SENT/FAILED")
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    subject: Mapped[str | None] = mapped_column(String(255))
    smtp_response: Mapped[str | None] = mapped_column(Text, comment="SMTP 原始响应，便于排查")
    error_message: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class AlertCooldown(Base):
    """冷却记录。用独立表而不是只存在规则上，是为了能追溯‘当时为什么没再发一封’。"""

    __tablename__ = "alert_cooldowns"
    id: Mapped[int] = mapped_column(PK_BIGINT, primary_key=True, autoincrement=True)
    rule_id: Mapped[int] = mapped_column(Integer, index=True)
    event_id: Mapped[int | None] = mapped_column(Integer, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    reason: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AlertRuleVersion(Base):
    """规则版本快照。改了阈值之后，历史触发记录仍然能对应到当时的规则定义。"""

    __tablename__ = "alert_rule_versions"
    id: Mapped[int] = mapped_column(PK_BIGINT, primary_key=True, autoincrement=True)
    rule_id: Mapped[int] = mapped_column(Integer, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, comment="整条规则（含条件树）的完整快照")
    change_note: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AppSetting(Base):
    """运行时配置表：让所有配置都能在后台改，不必手工编辑 .env。

    设计取舍：这里**只存 key -> value**，不存中文名/说明/类型/是否敏感。
    那些属于「元数据」，放在 `core/settings_store.py` 的 SETTINGS_SPEC 里，
    好处是元数据随代码走、可在测试中直接断言，也不会出现库里有值但代码不认识它的情况。

    已配置的数据库值优先于 .env；未配置时回退 .env，再回退默认值。
    """

    __tablename__ = "app_settings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    updated_by: Mapped[str | None] = mapped_column(String(64), comment="谁改的：admin / install 等")


ALL_TABLES = [
    Asset, ProviderRecord, ProviderHealth, ProviderFailoverEvent, ProviderRequest,
    RawMarketData, MarketPrice, Candle, Orderbook,
    OnchainMetric, ExchangeFlow, EtfFlow, Derivative, OptionMetric,
    MacroSeries, Sentiment,
    IndicatorDefinition, IndicatorValue, CycleState, RiskScore, MarketRegime,
    Signal, SignalEvent, ModelVersion, ModelWeight,
    BacktestRun, BacktestResult,
    UserPlan, UserTransaction, UserHolding, PortfolioSnapshot,
    DataQuality, SyncCheckpoint, SystemJob, TimelineEvent, AuditLog,
    # 智能监测与预警（增量模块）
    AlertRule, AlertConditionGroup, AlertCondition, AlertEvent,
    AlertNotificationChannel, AlertNotificationLog, AlertCooldown, AlertRuleVersion,
    # 运行时配置（后台可视化配置中心）
    AppSetting,
]
