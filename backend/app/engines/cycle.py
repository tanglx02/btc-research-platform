# -*- coding: utf-8 -*-
"""周期引擎：识别 BTC 当前所处阶段。

不强依赖「四年减半」硬编码，而是综合：
趋势 / 估值 / 资金 / 情绪 / 杠杆 / 宏观 多个维度。
输出还包含：**支持证据** 与 **反向证据** ——用户能同时看到两面。
"""

from __future__ import annotations

from typing import Any

MODEL_VERSION = "cycle_v1"

PHASES = {
    "deep_bear": "深度熊市",
    "bear": "熊市下行",
    "accumulation": "底部构筑",
    "recovery": "恢复期",
    "markup": "趋势上涨",
    "acceleration": "加速上涨",
    "distribution": "高位分配",
    "top_risk": "顶部风险区",
    "correction": "下跌调整",
    "range": "震荡整理",
}

PHASE_ADVICE = {
    "deep_bear": "历史上这个阶段波动巨大，仓位管理比方向判断更重要。",
    "bear": "下行趋势中，逆势加仓需要更长的资金规划周期。",
    "accumulation": "价格在低位反复震荡，方向尚不明确，需要更长的时间确认。",
    "recovery": "情绪逐步修复，通常需要时间与成交配合确认。",
    "markup": "趋势相对明确，但也要防范快速回撤。",
    "acceleration": "上涨速度加快，短期情绪高涨，风险偏好快速上升。",
    "distribution": "高位反复震荡，资金分歧加大。",
    "top_risk": "多个情绪/估值指标同时过热，历史上这种状态持续性有限。",
    "correction": "从高点回落，趋势尚未确认恢复。",
    "range": "方向不明，等待更多证据。",
}


class CycleEngine:
    VERSION = MODEL_VERSION

    def analyze(
        self,
        indicators: dict[str, Any],
        valuation_state: str | None = None,
        sentiment_index: float | None = None,
        funding_annualized: float | None = None,
        drawdown_from_ath: float | None = None,
        candles: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        evidence: list[dict[str, Any]] = []
        counter: list[dict[str, Any]] = []

        price = indicators.get("price")
        ma50 = indicators.get("ma50")
        ma200 = indicators.get("ma200")
        rsi = indicators.get("rsi14")
        change_30d = indicators.get("change_30d")
        change_90d = indicators.get("change_90d")
        dd = indicators.get("drawdown") if drawdown_from_ath is None else drawdown_from_ath

        # ---- 趋势维度
        trend_score = 0.0
        if price and ma200:
            dev = (price - ma200) / ma200
            if price > ma200:
                trend_score += 1.0
                evidence.append({"dim": "趋势", "point": f"价格高于 200 日均线（偏离 {dev*100:+.1f}%）"})
            else:
                trend_score -= 1.0
                counter.append({"dim": "趋势", "point": f"价格低于 200 日均线（偏离 {dev*100:+.1f}%）"})
        if ma50 and ma200:
            if ma50 > ma200:
                trend_score += 0.5
                evidence.append({"dim": "趋势", "point": "中期均线在长期均线上方（多头结构）"})
            else:
                trend_score -= 0.5
                counter.append({"dim": "趋势", "point": "中期均线仍在长期均线下方"})

        # ---- 动量维度
        momentum = 0.0
        if change_30d is not None:
            if change_30d > 15:
                momentum += 1.0
                evidence.append({"dim": "动量", "point": f"近 30 日上涨 {change_30d:.1f}%"})
            elif change_30d < -15:
                momentum -= 1.0
                counter.append({"dim": "动量", "point": f"近 30 日下跌 {change_30d:.1f}%"})
        if rsi is not None:
            if rsi > 70:
                momentum += 0.5
                evidence.append({"dim": "动量", "point": f"RSI {rsi:.0f}，短期偏热"})
            elif rsi < 30:
                momentum -= 0.5
                counter.append({"dim": "动量", "point": f"RSI {rsi:.0f}，短期偏冷"})

        # ---- 估值维度
        valuation_bias = 0.0
        if valuation_state == "extreme_overvalue":
            valuation_bias = 1.5
            evidence.append({"dim": "估值", "point": "估值处于极端高位"})
        elif valuation_state == "overvalue":
            valuation_bias = 0.7
            evidence.append({"dim": "估值", "point": "估值偏高"})
        elif valuation_state == "undervalue":
            valuation_bias = -0.7
            counter.append({"dim": "估值", "point": "估值偏低，下行空间可能有限"})
        elif valuation_state == "deep_undervalue":
            valuation_bias = -1.5
            counter.append({"dim": "估值", "point": "估值处于历史极低区间"})

        # ---- 情绪/杠杆（过热检测）
        heat = 0.0
        if sentiment_index is not None:
            if sentiment_index >= 75:
                heat += 1.0
                evidence.append({"dim": "情绪", "point": f"恐慌贪婪指数 {sentiment_index:.0f}（贪婪）"})
            elif sentiment_index <= 25:
                heat -= 1.0
                counter.append({"dim": "情绪", "point": f"恐慌贪婪指数 {sentiment_index:.0f}（恐慌）"})
        if funding_annualized is not None:
            if funding_annualized > 40:
                heat += 1.0
                evidence.append({"dim": "杠杆", "point": f"资金费率年化 {funding_annualized:.0f}%，多头拥挤"})
            elif funding_annualized < -10:
                heat -= 0.5
                counter.append({"dim": "杠杆", "point": f"资金费率年化 {funding_annualized:.0f}%，空头占优"})

        total = trend_score + momentum + valuation_bias + heat
        phase = self._classify(total, dd, rsi, momentum)
        confidence = self._confidence(len(evidence) + len(counter))

        return {
            "phase": phase,
            "phase_cn": PHASES[phase],
            "advice": PHASE_ADVICE[phase],
            "confidence": round(confidence, 3),
            "composite_score": round(total, 2),
            "dimensions": {
                "trend": round(trend_score, 2),
                "momentum": round(momentum, 2),
                "valuation": round(valuation_bias, 2),
                "heat": round(heat, 2),
            },
            "evidence": evidence,
            "counter_evidence": counter,
            "similar_periods": self._find_similar_periods(candles or [], price, ma200, rsi, dd),
            "model_version": self.VERSION,
            "note": "周期阶段是对当前状态的统计归类和描述，不是对未来走势的承诺。",
        }

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _classify(total: float, dd: float | None, rsi: float | None, momentum: float) -> str:
        if total >= 4.0:
            return "top_risk" if (rsi or 0) > 70 else "acceleration"
        if total >= 2.0:
            return "markup" if momentum > 0 else "distribution"
        if total >= 0.5:
            return "recovery"
        if total > -1.0:
            return "range"
        if total > -2.5:
            return "correction"
        if dd is not None and dd <= -60:
            return "deep_bear"
        if dd is not None and dd <= -35:
            return "accumulation"
        return "bear"

    @staticmethod
    def _confidence(n_points: int) -> float:
        return max(0.3, min(0.9, 0.35 + n_points * 0.07))

    @staticmethod
    def _find_similar_periods(
        candles: list[dict[str, Any]],
        price: float | None,
        ma200: float | None,
        rsi: float | None,
        dd: float | None,
        top_n: int = 3,
    ) -> list[dict[str, Any]]:
        """基于历史 K 线找「状态相似」的历史窗口（只用该时点之前的信息）。"""
        if not candles or price is None or ma200 is None or len(candles) < 260:
            return []
        results: list[dict[str, Any]] = []
        closes = [c["close"] for c in candles]
        target_dev = (price - ma200) / ma200
        target_dd = dd if dd is not None else 0.0

        for i in range(200, len(candles) - 30):
            window = closes[: i + 1]
            hist_ma200 = sum(window[-200:]) / 200
            peak = max(window)
            hist_dev = (closes[i] - hist_ma200) / hist_ma200
            hist_dd = (closes[i] - peak) / peak * 100
            distance = abs(hist_dev - target_dev) * 2 + abs(hist_dd - target_dd) / 100
            results.append({"idx": i, "ts": candles[i]["ts"], "price": closes[i], "distance": distance})

        results.sort(key=lambda x: x["distance"])
        picks = results[:top_n]
        out: list[dict[str, Any]] = []
        for p in picks:
            i = p["idx"]
            future_30 = closes[i + 30] if i + 30 < len(closes) else None
            future_90 = closes[i + 90] if i + 90 < len(closes) else None
            out.append(
                {
                    "date": candles[i].get("_date") or p["ts"],
                    "price": round(p["price"], 2),
                    "similarity": round(max(0.0, 1 - p["distance"]), 3),
                    "return_30d_pct": round((future_30 - p["price"]) / p["price"] * 100, 2) if future_30 else None,
                    "return_90d_pct": round((future_90 - p["price"]) / p["price"] * 100, 2) if future_90 else None,
                }
            )
        return out
