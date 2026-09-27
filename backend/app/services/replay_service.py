# -*- coding: utf-8 -*-
"""历史回放：把任意历史日期「当时能看到的世界」重建出来。

铁律：
1. 当时视角 —— 只能使用 observation_time <= 回放日期 的数据；
2. 事后视角 —— 明确标记「这是事后才知道的结果」，仅作为对照；
3. 缺失的数据如实标注，不用后来补充的数据冒充当时就有。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import numpy as np
from sqlalchemy import desc, select

from ..core.logging import get_logger
from ..db.base import get_session_factory
from ..db.models import Candle, Derivative, MacroSeries, OnchainMetric, Sentiment, TimelineEvent
from ..db.repo import latest_candles
from ..engines import CycleEngine, IndicatorEngine, RiskEngine, ValuationEngine

logger = get_logger(__name__)

DAY = 86400
MODEL_VERSION = "replay_v1"


class ReplayService:
    VERSION = MODEL_VERSION

    def __init__(self) -> None:
        self.indicator_engine = IndicatorEngine()
        self.valuation_engine = ValuationEngine()
        self.cycle_engine = CycleEngine()
        self.risk_engine = RiskEngine()

    # ---------------------------------------------------------------- 主入口
    async def replay(self, date_str: str, perspective: str = "then") -> dict[str, Any]:
        target_ts = int(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
        target_day_end = target_ts + DAY

        factory = get_session_factory()
        async with factory() as session:
            # 只取回放日之前的数据。
            # 关键坑：必须先按 ts **倒序**取最近的 N 根再翻转。
            # 若写成 order_by(ts).limit(N)，取到的是「历史上最早的 N 根」——
            # 回放 2021-11 时会实际算到 2021-06，日期静悄悄错位，结论全错。
            candles_rows = (
                await session.execute(
                    select(Candle)
                    .where(Candle.symbol == "BTC", Candle.interval == "1d", Candle.ts <= target_day_end)
                    .order_by(desc(Candle.ts))
                    .limit(3000)
                )
            ).scalars().all()
            candles_rows = list(reversed(list(candles_rows)))

        if not candles_rows:
            return {"available": False, "date": date_str, "message": "该日期之前的本地历史数据不足，无法回放"}

        candles = [
            {"ts": c.ts, "open": c.open, "high": c.high, "low": c.low,
             "close": c.close, "volume": c.volume, "provider": c.source_id}
            for c in candles_rows
        ]

        # 严格截断到回放日
        candles = [c for c in candles if c["ts"] <= target_day_end]
        if not candles:
            return {"available": False, "date": date_str, "message": "该日期之前没有可用 K 线"}

        # 诚实校验：本地历史必须真正覆盖到回放日附近。
        # 如果本地最后一根 K 线比回放日早很多天，说明历史数据还没补齐，
        # 这时必须明确告知用户，而不是拿更早的日期冒充回放日。
        lag_days = int((target_day_end - int(candles[-1]["ts"])) // DAY)
        if lag_days > 3:
            have_date = datetime.fromtimestamp(int(candles[-1]["ts"]), tz=timezone.utc).strftime("%Y-%m-%d")
            return {
                "available": False,
                "date": date_str,
                "message": (
                    f"本地历史数据只到 {have_date}，无法回放 {date_str}。"
                    f"请先补齐该日期之前的历史（btcctl.py backfill），系统不会用更早的数据冒充回放日。"
                ),
                "local_latest_date": have_date,
                "lag_days": lag_days,
            }

        closes = np.array([c["close"] for c in candles], dtype=float)
        snapshot_price = float(closes[-1])
        ath = float(np.max(closes))
        percentile = float((closes <= snapshot_price).sum() / len(closes) * 100)

        indicators = self.indicator_engine.compute(candles)
        latest = indicators["latest"]
        latest["price_percentile"] = percentile
        latest["volume_latest"] = candles[-1]["volume"]

        # 先取「当时」的周边上下文（情绪/资金费率/宏观/链上），
        # 再算估值与周期 —— 周期/风险必须拿到当时的 K 线与当时的杠杆数据，
        # 否则 similar_periods 恒为空、缺少杠杆维度，回放分数不等于当时的实盘分数。
        context = await self._context_at(target_day_end)

        # 把「当时的」上下文整理成各引擎期望的入参形状；拿不到就传 None，
        # 引擎会如实标注该维度不可用，而不是假设一个值。
        sentiment_block = context.get("sentiment") or {}
        sentiment_index = sentiment_block.get("index") if sentiment_block.get("available") else None
        derivatives_block = context.get("derivatives") or {}
        funding_annualized = (
            derivatives_block.get("funding_annualized_pct") if derivatives_block.get("available") else None
        )
        onchain_block = context.get("onchain") or {}
        metrics = onchain_block.get("metrics") or {}
        mvrv = metrics.get("mvrv")
        macro_series = (context.get("macro") or {}).get("series") or {}
        macro_flat = {
            "dxy": macro_series.get("DXY", {}).get("value"),
            "us10y": macro_series.get("US10Y", {}).get("value"),
        }

        valuation = self.valuation_engine.evaluate(latest, {"mvrv": mvrv} if mvrv is not None else {})
        cycle = self.cycle_engine.analyze(
            latest,
            valuation_state=valuation["state"],
            sentiment_index=sentiment_index,
            funding_annualized=funding_annualized,
            candles=candles,   # 必须传：similar_periods 依赖历史 K 线，不传则恒为空
        )
        risk = self.risk_engine.analyze(
            latest,
            valuation_state=valuation["state"],
            funding_annualized=funding_annualized,
            macro=macro_flat,
            onchain_available=bool(onchain_block.get("available")),
        )

        result: dict[str, Any] = {
            "available": True,
            "date": date_str,
            "perspective": perspective,
            "model_version": self.VERSION,
            "snapshot": {
                "price": round(snapshot_price, 2),
                "ath_then": round(ath, 2),
                "drawdown_pct": round((snapshot_price - ath) / ath * 100, 2),
                "price_percentile_then": round(percentile, 1),
                "data_provider": candles[-1]["provider"],
            },
            "indicators": {"latest": latest, "meta": indicators["meta"]},
            "valuation": valuation,
            "cycle": cycle,
            "risk": risk,
            "sentiment": context["sentiment"],
            "derivatives": context["derivatives"],
            "onchain": context["onchain"],
            "macro": context["macro"],
            "timeline_nearby": context["timeline"],
            "data_availability": context["availability"],
            "note": "以上所有数值均来自「当时已经存在」的数据，未使用任何未来信息。",
        }

        if perspective == "aftermath":
            result["aftermath"] = await self._aftermath(candles, target_day_end)
            result["note"] = (
                "事后视角：下面包含回放日之后实际发生的结果，仅用于对照学习，"
                "不能用来评价当时决策的对错。"
            )
        return result

    # ---------------------------------------------------------------- 上下文
    async def _context_at(self, target_ts: int) -> dict[str, Any]:
        from ..db.models import Sentiment as SentimentModel

        factory = get_session_factory()
        async with factory() as session:
            sentiment_row = (
                await session.execute(
                    select(SentimentModel)
                    .where(SentimentModel.observation_ts <= target_ts)
                    .order_by(desc(SentimentModel.observation_ts))
                    .limit(1)
                )
            ).scalars().first()
            derivative_row = (
                await session.execute(
                    select(Derivative)
                    .where(Derivative.observation_time <= datetime.fromtimestamp(target_ts, tz=timezone.utc))
                    .order_by(desc(Derivative.observation_time))
                    .limit(1)
                )
            ).scalars().first()
            onchain_rows = (
                await session.execute(
                    select(OnchainMetric)
                    .where(OnchainMetric.observation_ts <= target_ts)
                    .order_by(desc(OnchainMetric.observation_ts))
                    .limit(20)
                )
            ).scalars().all()
            macro_rows = (
                await session.execute(
                    select(MacroSeries)
                    .where(MacroSeries.observation_ts <= target_ts)
                    .order_by(desc(MacroSeries.observation_ts))
                    .limit(60)
                )
            ).scalars().all()
            timeline_rows = (
                await session.execute(
                    select(TimelineEvent)
                    .where(TimelineEvent.ts.between(target_ts - 30 * DAY, target_ts + 30 * DAY))
                    .order_by(TimelineEvent.ts)
                )
            ).scalars().all()

        macro_map: dict[str, Any] = {}
        for row in macro_rows:
            macro_map.setdefault(row.series_id, {"value": row.value, "ts": row.observation_ts, "provider": row.source_id})

        return {
            "sentiment": (
                {
                    "available": True,
                    "index": sentiment_row.value,
                    "classification": sentiment_row.classification,
                    "observation_ts": sentiment_row.observation_ts,
                    "provider": sentiment_row.source_id,
                }
                if sentiment_row
                else {"available": False, "message": "当时没有采集到情绪数据"}
            ),
            "derivatives": (
                {
                    "available": True,
                    "funding_rate": derivative_row.funding_rate,
                    "funding_annualized_pct": derivative_row.funding_annualized_pct,
                    "open_interest": derivative_row.open_interest,
                    "provider": derivative_row.source_id,
                    "observation_time": derivative_row.observation_time.isoformat() if derivative_row.observation_time else None,
                }
                if derivative_row
                else {"available": False, "message": "当时没有采集到衍生品数据"}
            ),
            "onchain": {
                "available": bool(onchain_rows),
                "metrics": {r.metric: r.value for r in onchain_rows},
                "provider": onchain_rows[0].source_id if onchain_rows else None,
            },
            "macro": {
                "available": bool(macro_map),
                "series": macro_map,
            },
            "timeline": [
                {
                    "date": r.date,
                    "type": r.event_type,
                    "title": r.title_cn,
                    "impact": r.impact,
                }
                for r in timeline_rows
            ],
            "availability": {
                "sentiment": bool(sentiment_row),
                "derivatives": bool(derivative_row),
                "onchain": bool(onchain_rows),
                "macro": bool(macro_map),
            },
        }

    async def _aftermath(self, candles: list[dict[str, Any]], from_ts: int) -> dict[str, Any]:
        """事后发生了什么（严格标注为事后视角）。"""
        factory = get_session_factory()
        async with factory() as session:
            future_rows = await latest_candles(session, "BTC", "1d", 10000)
        futures = [c for c in future_rows if c["ts"] > from_ts]
        base_price = float(candles[-1]["close"])

        horizons = {}
        for days in (30, 90, 180, 365):
            # 必须真正有足够的未来数据才给出该期限的结果。
            # 早期写法是 idx = min(days, len(futures)-1)，在样本不足时会拿最后一根
            # 冒充 365 天后的价格 —— 那是「看起来有答案」的错误数据，必须禁止。
            if len(futures) > days:
                idx = days
                price = futures[idx]["close"]
                horizons[f"{days}d"] = {
                    "price": round(price, 2),
                    "return_pct": round((price - base_price) / base_price * 100, 2),
                    "date": datetime.fromtimestamp(futures[idx]["ts"], tz=timezone.utc).strftime("%Y-%m-%d"),
                    "available_days": days,
                }
            else:
                horizons[f"{days}d"] = {
                    "price": None,
                    "return_pct": None,
                    "available_days": len(futures),
                    "note": f"本地仅有 {len(futures)} 天的后续数据，不足以计算 {days} 天后的结果",
                }

        peak_after = max([f["close"] for f in futures], default=base_price)
        trough_after = min([f["close"] for f in futures], default=base_price)
        return {
            "horizons": horizons,
            "max_gain_pct": round((peak_after - base_price) / base_price * 100, 2),
            "max_loss_pct": round((trough_after - base_price) / base_price * 100, 2),
            "days_available": len(futures),
            "perspective": "aftermath",
            "warning": "这是事后才知道的结果，实盘当时无法预知。",
        }
