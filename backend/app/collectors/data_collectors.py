# -*- coding: utf-8 -*-
"""非行情类采集器：衍生品、链上、情绪、宏观、ETF。

共同原则：某一类数据全部不可用 => 该模块显示「数据暂时不可用」，
**其他模块照常运行**（局部故障不影响整体）。绝不写假数据。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..core.config import get_settings
from ..core.errors import AllProvidersFailedError
from ..core.logging import get_logger
from ..db.base import get_session_factory
from ..db.models import Derivative, EtfFlow, MacroSeries, OnchainMetric, Sentiment
from ..db.repo import upsert_rows
from ..providers.router import ResilientRouter, get_router
from ..providers.types import DataCategory
from .base import BaseCollector

logger = get_logger(__name__)


class _BaseTypedCollector(BaseCollector):
    router: ResilientRouter

    def __init__(self, router: ResilientRouter | None = None) -> None:
        self.s = get_settings()
        self.router = router or get_router()

    async def _record_failure(self, exc: Exception) -> None:
        logger.event("collect.category_failed", task=self.task_name, error=str(exc)[:200])
        await self.save_checkpoint(cursor_ts=int(datetime.now(timezone.utc).timestamp()), status="failed",
                                   error=str(exc)[:200])


class DerivativesCollector(_BaseTypedCollector):
    task_name = "derivatives_5m"
    category = "derivatives"

    async def execute(self, **_: Any) -> dict[str, Any]:
        try:
            funding = await self.router.fetch(DataCategory.DERIVATIVES_FUNDING)
        except AllProvidersFailedError as exc:
            await self._record_failure(exc)
            return {"ok": False, "module": "derivatives", "reason": "all_providers_failed"}

        oi_data: dict[str, Any] | None = None
        try:
            oi = await self.router.fetch(DataCategory.DERIVATIVES_OI)
            oi_data = oi.data
        except AllProvidersFailedError:
            oi_data = None  # OI 不可用不影响 funding 入库

        now = datetime.now(timezone.utc)
        factory = get_session_factory()
        async with factory() as session:
            session.add(
                Derivative(
                    source_id=funding.provider,
                    exchange=str(funding.data.get("exchange", funding.provider)),
                    funding_rate=float(funding.data.get("funding_rate") or 0),
                    funding_annualized_pct=float(funding.data.get("annualized_pct") or 0),
                    open_interest=float((oi_data or {}).get("open_interest") or 0) or None,
                    open_interest_usd=float((oi_data or {}).get("open_interest_usd") or 0) or None,
                    basis_bps=float(funding.data.get("basis_bps") or 0) or None,
                    quality_status=funding.quality.value,
                    observation_time=now,
                    fetch_time=now,
                )
            )
            await session.commit()
        await self.save_checkpoint(cursor_ts=int(now.timestamp()), status="running", rows_delta=1,
                                   provider=funding.provider)
        return {
            "ok": True,
            "funding": float(funding.data.get("funding_rate") or 0),
            "provider": funding.provider,
            "oi_available": oi_data is not None,
        }


class OnchainCollector(_BaseTypedCollector):
    task_name = "onchain_1h"
    category = "onchain"

    async def execute(self, **_: Any) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        try:
            basic = await self.router.fetch(DataCategory.ONCHAIN_BASIC)
        except AllProvidersFailedError as exc:
            await self._record_failure(exc)
            return {"ok": False, "module": "onchain", "reason": "all_providers_failed"}

        rows = []
        obs_ts = int(now.timestamp()) // 3600 * 3600
        for key, value in basic.data.items():
            if isinstance(value, (int, float)):
                rows.append(
                    {
                        "symbol": "BTC",
                        "source_id": basic.provider,
                        "metric": key,
                        "value": float(value),
                        "unit": "auto",
                        "observation_ts": obs_ts,
                        "quality_status": basic.quality.value,
                        "observation_time": now,
                        "fetch_time": now,
                    }
                )
        factory = get_session_factory()
        async with factory() as session:
            # 同一小时重复采集必须幂等（调度任务会周期性重跑）
            written = await upsert_rows(
                session,
                OnchainMetric,
                rows,
                ["metric", "symbol", "observation_ts", "source_id"],
                ["value", "unit", "quality_status", "observation_time", "fetch_time", "updated_at"],
            )
        await self.save_checkpoint(cursor_ts=obs_ts, status="running", rows_delta=written, provider=basic.provider)
        return {"ok": True, "metrics": written, "provider": basic.provider}


class SentimentCollector(_BaseTypedCollector):
    task_name = "sentiment_daily"
    category = "sentiment"

    async def execute(self, **_: Any) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        try:
            res = await self.router.fetch(DataCategory.SENTIMENT_INDEX)
        except AllProvidersFailedError as exc:
            await self._record_failure(exc)
            return {"ok": False, "module": "sentiment", "reason": "all_providers_failed"}

        data = res.data
        factory = get_session_factory()
        async with factory() as session:
            await upsert_rows(
                session,
                Sentiment,
                [{
                    "source_id": res.provider,
                    "metric": "fear_greed",
                    "value": float(data["index"]),
                    "classification": data.get("classification_cn") or data.get("classification"),
                    "observation_ts": int(data["series"][0]["ts"]),
                    "quality_status": res.quality.value,
                    "fetch_time": now,
                }],
                ["metric", "observation_ts", "source_id"],
                ["value", "classification", "quality_status", "fetch_time", "updated_at"],
            )
        await self.save_checkpoint(cursor_ts=int(data["series"][0]["ts"]), status="running", rows_delta=1,
                                   provider=res.provider)
        return {"ok": True, "index": float(data["index"]), "provider": res.provider}


class MacroCollector(_BaseTypedCollector):
    task_name = "macro_daily"
    category = "macro"

    # 每个序列会沿「主源 -> 备用源」链路自动尝试，单个序列失败不影响其它序列
    SERIES_TO_FETCH = ["DXY", "US10Y", "US2Y", "SPX", "GOLD", "VIX", "USDCNY"]

    async def execute(self, **_: Any) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        saved = 0
        failed_series: list[str] = []
        provider = ""
        factory = get_session_factory()
        async with factory() as session:
            for series_id in self.SERIES_TO_FETCH:
                try:
                    res = await self.router.fetch(DataCategory.MACRO_SERIES, series_id=series_id, limit=30)
                except AllProvidersFailedError:
                    # 该序列所有数据源都不可用：如实记录，不让局部失败拖垮整个宏观模块
                    failed_series.append(series_id)
                    continue
                provider = res.provider
                rows = [
                    {
                        "source_id": res.provider,
                        "series_id": series_id,
                        "value": float(point["value"]),
                        "unit": res.meta.get("unit", "index"),
                        "observation_ts": int(point["ts"]),
                        # 保守假设：发布日 = 观测日 +1 天，防止回测用到尚未公开的宏观数据
                        "release_ts": int(point["ts"]) + 86400,
                        "quality_status": res.quality.value,
                        "fetch_time": now,
                    }
                    for point in res.data
                ]
                saved += await upsert_rows(
                    session,
                    MacroSeries,
                    rows,
                    ["series_id", "observation_ts", "source_id"],
                    ["value", "unit", "release_ts", "quality_status", "fetch_time", "updated_at"],
                )
        if saved:
            await self.save_checkpoint(cursor_ts=int(now.timestamp()), status="running", rows_delta=saved,
                                       provider=provider, extra={"failed_series": failed_series})
        return {
            "ok": saved > 0,
            "rows": saved,
            "provider": provider,
            "failed_series": failed_series,
            "reason": ("全部宏观序列均无可用数据源" if not saved else None),
        }


class EtfCollector(_BaseTypedCollector):
    task_name = "etf_daily"
    category = "etf"

    async def execute(self, **_: Any) -> dict[str, Any]:
        """ETF 资金流：无可用/未配置数据源时，明确返回不可用，不做任何填充。"""
        now = datetime.now(timezone.utc)
        try:
            res = await self.router.fetch(DataCategory.ETF_FLOW)
        except AllProvidersFailedError as exc:
            await self._record_failure(exc)
            return {
                "ok": False,
                "module": "etf",
                "reason": "no_provider_available",
                "message": "ETF 数据源未配置或不可用，模块显示「数据暂时不可用」",
                "detail": str(exc)[:200],
            }

        factory = get_session_factory()
        saved = 0
        async with factory() as session:
            for row in res.data.get("history", [])[-30:]:
                try:
                    ts = int(datetime.strptime(row["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
                except (KeyError, ValueError, TypeError):
                    continue
                session.add(
                    EtfFlow(
                        source_id=res.provider,
                        issuer="ALL",
                        inflow_usd=float(row.get("inflow_usd") or 0),
                        outflow_usd=float(row.get("outflow_usd") or 0),
                        net_flow_usd=float(row.get("net_flow_usd") or 0),
                        total_assets_usd=float(row.get("total_assets_usd") or 0),
                        observation_ts=ts,
                        quality_status=res.quality.value,
                        observation_time=datetime.fromtimestamp(ts, tz=timezone.utc),
                        fetch_time=now,
                    )
                )
                saved += 1
            await session.commit()
        return {"ok": saved > 0, "rows": saved, "provider": res.provider}
