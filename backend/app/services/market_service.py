# -*- coding: utf-8 -*-
"""行情服务层：业务编排。只依赖 Provider 抽象层与本地数据库。

降级运行原则：
- 实时取数失败 -> 使用本地最近一次可信数据，标记 STALE + 显示最后更新时间
- 某类数据全部不可用 -> 该模块返回 unavailable，其他模块照常
- 绝不用假数据填充
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, select

from ..core.cache import CacheManager, get_cache
from ..core.errors import AllProvidersFailedError
from ..core.logging import get_logger
from ..db.base import get_session_factory
from ..db.models import Candle, Derivative, MacroSeries, MarketPrice, OnchainMetric, Sentiment
from ..db.repo import candle_coverage, latest_candles, latest_price
from ..engines import (
    CycleEngine,
    ForecastEngine,
    IndicatorEngine,
    MarketRegimeEngine,
    RiskEngine,
    ValuationEngine,
)
from ..providers.registry import get_registry
from ..providers.router import get_router
from ..providers.types import DataCategory, QualityStatus

logger = get_logger(__name__)


class MarketService:
    """对外提供「带来源、带解释、带降级」的市场数据。"""

    def __init__(self) -> None:
        self.router = get_router()
        self.registry = get_registry()
        self.cache: CacheManager = get_cache()
        self.indicators_engine = IndicatorEngine()
        self.valuation_engine = ValuationEngine()
        self.cycle_engine = CycleEngine()
        self.risk_engine = RiskEngine()
        self.regime_engine = MarketRegimeEngine()
        self.forecast_engine = ForecastEngine()

    # ---------------------------------------------------------------- 实时价格
    async def current_price(self, symbol: str = "BTC") -> dict[str, Any]:
        """当前价格：优先真实抓取 + 交叉验证；全挂时降级到本地最近可信值。"""
        try:
            result = await self.router.fetch_validated(DataCategory.MARKET_PRICE)
            return {
                "available": True,
                "symbol": symbol,
                "price": float(result.data),
                "quote": result.meta.get("quote", "USD"),
                "stale": False,
                "source": result.to_source_block(),
            }
        except AllProvidersFailedError as exc:
            factory = get_session_factory()
            async with factory() as session:
                fallback = await latest_price(session, symbol)
            if not fallback:
                return {
                    "available": False,
                    "symbol": symbol,
                    "price": None,
                    "message": "所有数据源均不可用，且本地无历史可信数据：不做任何估算",
                    "attempts": exc.attempts,
                }
            return {
                "available": True,
                "symbol": symbol,
                "price": fallback["price"],
                "quote": "USD",
                "stale": True,
                "message": "数据源暂时不可用，当前显示最后一次成功获取的价格",
                "source": {
                    "provider": fallback["provider"],
                    "quality": QualityStatus.STALE.value,
                    "used_fallback": True,
                    "observation_time": fallback["observation_time"],
                    "fetch_time": fallback["fetch_time"],
                    "confidence": round(max(0.2, fallback["confidence"] * 0.6), 3),
                    "attempts": exc.attempts,
                    "note": "所有 Provider 均失败，已降级到本地最近可信数据",
                },
            }

    # ---------------------------------------------------------------- K 线
    async def candles(
        self, symbol: str = "BTC", interval: str = "1d", limit: int = 365, **_: Any
    ) -> dict[str, Any]:
        """本地优先读取历史 K 线（原则上不再请求第三方）。"""
        factory = get_session_factory()
        async with factory() as session:
            rows = await latest_candles(session, symbol, interval, limit)
            coverage = await candle_coverage(session, symbol, interval)
        providers = [p.name for p in self.registry.providers_for(DataCategory.OHLCV)]
        return {
            "available": bool(rows),
            "symbol": symbol,
            "interval": interval,
            "count": len(rows),
            "candles": rows,
            "source": {
                "mode": "local_database",
                "note": "历史数据读取自本地数据库，不依赖第三方实时接口",
                "local_coverage": coverage,
                "providers_available_for_refresh": providers[:5],
                "providers_used_historically": sorted({r["provider"] for r in rows}),
            },
        }

    # ---------------------------------------------------------------- 指标
    async def indicators(self, symbol: str = "BTC", interval: str = "1d", limit: int = 500) -> dict[str, Any]:
        """指标计算。纯 CPU 运算、同一份输入必然同一份输出，因此可以做结果缓存。

        缓存键里带上【最后一根 K 线时间戳 + 根数】：只要采集写入了新数据，键必然变化，
        于是「永远读不到基于旧数据的计算结果」，不需要靠 TTL 赌。
        """
        factory = get_session_factory()
        async with factory() as session:
            rows = await latest_candles(session, symbol, interval, limit)
        if not rows:
            return {"available": False, "message": "本地暂无足够历史数据，请先执行历史回填"}

        cache_key = CacheManager.make_key(
            "indicators", symbol, interval, limit, int(rows[-1]["ts"]), len(rows)
        )
        hit = self.cache.get(cache_key)
        if hit is not None:
            return {**hit, "cache": {"hit": True, "ttl_seconds": self.cache.default_ttl}}

        result = self.indicators_engine.compute(rows)
        result["available"] = True
        result["source"] = {
            "mode": "computed_from_local_candles",
            "provider_of_data": sorted({r["provider"] for r in rows}),
            "bars_used": len(rows),
            "computed_at": datetime.now(timezone.utc).isoformat(),
        }
        result["cache"] = {"hit": False, "ttl_seconds": self.cache.default_ttl}
        self.cache.set(cache_key, {k: v for k, v in result.items() if k != "cache"})
        return result

    # ---------------------------------------------------------------- 各类模块数据
    async def sentiment(self) -> dict[str, Any]:
        try:
            res = await self.router.fetch(DataCategory.SENTIMENT_INDEX)
            return {"available": True, **res.data, "source": res.to_source_block()}
        except AllProvidersFailedError as exc:
            factory = get_session_factory()
            async with factory() as session:
                row = (
                    await session.execute(select(Sentiment).order_by(desc(Sentiment.observation_ts)).limit(1))
                ).scalars().first()
            if row:
                return {
                    "available": True,
                    "index": row.value,
                    "classification_cn": row.classification,
                    "stale": True,
                    "source": {"provider": row.source_id, "quality": QualityStatus.STALE.value,
                               "observation_time": datetime.fromtimestamp(row.observation_ts, tz=timezone.utc).isoformat()},
                }
            return {"available": False, "module": "sentiment", "message": "情绪数据源不可用", "attempts": exc.attempts}

    async def derivatives(self) -> dict[str, Any]:
        out: dict[str, Any] = {"available": False, "funding": None, "open_interest": None}
        sources: list[dict[str, Any]] = []
        try:
            funding = await self.router.fetch(DataCategory.DERIVATIVES_FUNDING)
            out["funding"] = funding.data
            sources.append(funding.to_source_block())
            out["available"] = True
        except AllProvidersFailedError:
            out["funding_error"] = "资金费率数据源不可用"
        try:
            oi = await self.router.fetch(DataCategory.DERIVATIVES_OI)
            out["open_interest"] = oi.data
            sources.append(oi.to_source_block())
            out["available"] = True
        except AllProvidersFailedError:
            out["oi_error"] = "持仓量数据源不可用"
        if out["available"]:
            factory = get_session_factory()
            async with factory() as session:
                row = (await session.execute(select(Derivative).order_by(desc(Derivative.observation_time)).limit(1))).scalars().first()
            if row:
                out["latest_recorded_at"] = row.observation_time.isoformat() if row.observation_time else None
        out["sources"] = sources
        return out

    async def onchain(self) -> dict[str, Any]:
        try:
            res = await self.router.fetch(DataCategory.ONCHAIN_BASIC)
            return {"available": True, "metrics": res.data, "source": res.to_source_block()}
        except AllProvidersFailedError as exc:
            return {"available": False, "module": "onchain", "message": "链上数据源不可用", "attempts": exc.attempts}

    async def macro(self) -> dict[str, Any]:
        result: dict[str, Any] = {"available": False, "series": {}}
        for sid in ("DXY", "US10Y"):
            try:
                res = await self.router.fetch(DataCategory.MACRO_SERIES, series_id=sid, limit=40)
                points = res.data
                if points:
                    result["series"][sid] = {
                        "latest": points[-1]["value"],
                        "previous_30d": points[max(0, len(points) - 31)]["value"] if len(points) > 30 else None,
                        "points": points[-40:],
                        "source": res.to_source_block(),
                    }
                    result["available"] = True
            except AllProvidersFailedError:
                result.setdefault("errors", []).append(f"{sid} 数据源不可用")
        return result

    async def etf(self) -> dict[str, Any]:
        """ETF 模块：没有可信数据源时明确告知，不做任何填充。"""
        try:
            res = await self.router.fetch(DataCategory.ETF_FLOW)
            return {"available": True, **res.data, "source": res.to_source_block()}
        except AllProvidersFailedError as exc:
            configured = any(
                p.is_configured() for p in self.registry.providers_for(DataCategory.ETF_FLOW)
            )
            return {
                "available": False,
                "module": "etf",
                "message": "ETF 数据源未配置" if not configured else "ETF 数据源当前不可用",
                "hint": "ETF 逐日资金流缺少稳定的免费公开接口；在「数据源中心」配置 Coinglass / CryptoQuant Key 后即可启用",
                "attempts": exc.attempts,
            }

    # ---------------------------------------------------------------- 综合分析
    async def _latest_onchain_value(self, metric: str) -> float | None:
        factory = get_session_factory()
        async with factory() as session:
            row = (
                await session.execute(
                    select(OnchainMetric)
                    .where(OnchainMetric.metric == metric)
                    .order_by(desc(OnchainMetric.observation_ts))
                    .limit(1)
                )
            ).scalars().first()
            return float(row.value) if row else None

    async def _latest_sentiment_index(self) -> float | None:
        factory = get_session_factory()
        async with factory() as session:
            row = (
                await session.execute(select(Sentiment).order_by(desc(Sentiment.observation_ts)).limit(1))
            ).scalars().first()
            return float(row.value) if row else None

    async def hypothesis_inputs(self) -> dict[str, Any]:
        """收集所有引擎需要的输入（任一缺失都如实记录）。"""
        indicators = await self.indicators(limit=500)
        has_indicators = bool(indicators.get("available"))

        mvrv = await self._latest_onchain_value("mvrv")
        sentiment_index = await self._latest_sentiment_index()

        funding_annualized = None
        try:
            f = await self.router.fetch(DataCategory.DERIVATIVES_FUNDING)
            funding_annualized = float(f.data.get("annualized_pct") or 0) or None
        except AllProvidersFailedError:
            pass

        macro_snapshot: dict[str, Any] = {}
        factory = get_session_factory()
        async with factory() as session:
            for sid in ("DXY", "US10Y"):
                rows = (
                    await session.execute(
                        select(MacroSeries)
                        .where(MacroSeries.series_id == sid)
                        .order_by(desc(MacroSeries.observation_ts))
                        .limit(31)
                    )
                ).scalars().all()
                if rows:
                    latest_row = rows[0]
                    macro_snapshot[sid.lower()] = latest_row.value
                    if len(rows) >= 2:
                        macro_snapshot[f"{sid.lower()}_change_30d"] = (
                            latest_row.value - rows[-1].value if len(rows) > 1 else 0.0
                        )
                        if sid == "DXY":
                            macro_snapshot["dxy_change_30d"] = (
                                (latest_row.value - rows[-1].value) / rows[-1].value * 100 if rows[-1].value else 0.0
                            )

        return {
            "indicators": indicators,
            "has_indicators": has_indicators,
            "mvrv": mvrv,
            "sentiment_index": sentiment_index,
            "funding_annualized": funding_annualized,
            "macro": macro_snapshot,
        }

    async def analysis(self) -> dict[str, Any]:
        """估值 + 周期 + 风险 + 综合状态的一次性计算。"""
        inputs = await self.hypothesis_inputs()
        if not inputs["has_indicators"]:
            return {"available": False, "message": "本地历史数据不足，无法计算分析指标"}

        latest = inputs["indicators"]["latest"]
        latest["volume_latest"] = None
        candles = inputs["indicators"]["meta"]

        valuation = self.valuation_engine.evaluate(latest, {"mvrv": inputs["mvrv"]} if inputs["mvrv"] else {})
        cycle = self.cycle_engine.analyze(
            latest,
            valuation_state=valuation["state"],
            sentiment_index=inputs["sentiment_index"],
            funding_annualized=inputs["funding_annualized"],
        )
        risk = self.risk_engine.analyze(
            latest,
            valuation_state=valuation["state"],
            funding_annualized=inputs["funding_annualized"],
            macro=inputs["macro"],
            onchain_available=inputs["mvrv"] is not None,
        )
        regime = self.regime_engine.compose(
            latest,
            valuation,
            cycle,
            risk,
            sentiment_index=inputs["sentiment_index"],
            funding_annualized=inputs["funding_annualized"],
            macro=inputs["macro"],
            onchain_available=inputs["mvrv"] is not None,
        )
        return {
            "available": True,
            "valuation": valuation,
            "cycle": cycle,
            "risk": risk,
            "regime": regime,
            "meta": candles,
        }

    async def forecast(self, horizon_days: int = 30) -> dict[str, Any]:
        factory = get_session_factory()
        async with factory() as session:
            candles = await latest_candles(session, "BTC", "1d", 5000)
        return self.forecast_engine.forecast(candles, horizon_days)

    # ---------------------------------------------------------------- 首页总览
    async def overview(self) -> dict[str, Any]:
        price_block = await self.current_price()
        indicators_block = await self.indicators(limit=500)
        analysis = await self.analysis() if indicators_block.get("available") else None
        sentiment_block = await self.sentiment()
        derivatives_block = await self.derivatives()

        latest = indicators_block.get("latest", {})
        changes = {k: latest.get(k) for k in ("change_24h", "change_7d", "change_30d", "change_365d")}

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "price": price_block,
            "changes": changes,
            "indicators": indicators_block,
            "analysis": analysis,
            "sentiment": sentiment_block,
            "derivatives": derivatives_block,
            "modules": {
                "market": bool(price_block.get("available")),
                "indicators": bool(indicators_block.get("available")),
                "sentiment": bool(sentiment_block.get("available")),
                "derivatives": bool(derivatives_block.get("available")),
                "analysis": bool(analysis),
            },
        }
