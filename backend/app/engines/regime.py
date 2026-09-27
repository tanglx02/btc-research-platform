# -*- coding: utf-8 -*-
"""综合市场状态引擎（Multi-Factor）。

不输出 BUY/SELL，只输出「状态」：
Trend / Valuation / CapitalFlow / Onchain / Derivative / Macro / Sentiment / Risk / Cycle
-> Market Regime
每次变化都建议写入 market_regimes 表，便于事后回看「当时系统到底判断了什么」。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

MODEL_VERSION = "regime_v1"


class MarketRegimeEngine:
    VERSION = MODEL_VERSION

    def compose(
        self,
        indicators: dict[str, Any],
        valuation: dict[str, Any],
        cycle: dict[str, Any],
        risk: dict[str, Any],
        sentiment_index: float | None = None,
        funding_annualized: float | None = None,
        etf_netflow_7d: float | None = None,
        macro: dict[str, Any] | None = None,
        onchain_available: bool = False,
    ) -> dict[str, Any]:
        macro = macro or {}

        # ---------- 趋势状态
        price, ma50, ma200 = indicators.get("price"), indicators.get("ma50"), indicators.get("ma200")
        trend_state = self._trend_state(price, ma50, ma200, indicators.get("change_30d"))

        # ---------- 估值状态
        valuation_state = valuation.get("state", "unknown")

        # ---------- 资金流状态（ETF 可用时用 ETF；否则标注不可用）
        if etf_netflow_7d is not None:
            if etf_netflow_7d > 5e8:
                capital_state = "strong_inflow"
            elif etf_netflow_7d > 0:
                capital_state = "mild_inflow"
            elif etf_netflow_7d < -5e8:
                capital_state = "strong_outflow"
            else:
                capital_state = "mild_outflow"
        else:
            capital_state = "unavailable"

        # ---------- 链上状态
        onchain_state = "unavailable" if not onchain_available else "neutral"

        # ---------- 衍生品状态
        derivative_state = self._derivative_state(funding_annualized)

        # ---------- 宏观状态
        macro_state = self._macro_state(macro)

        # ---------- 情绪状态
        sentiment_state = self._sentiment_state(sentiment_index)

        risk_state = risk.get("level", "unknown")
        cycle_state = cycle.get("phase", "unknown")

        regime, summary = self._regime_name(
            trend_state, valuation_state, cycle_state, risk_state, sentiment_state, capital_state
        )

        unavailable = [
            name
            for name, value in [
                ("ETF 资金流", capital_state == "unavailable"),
                ("链上数据", onchain_available is False),
                ("宏观数据", macro_state == "unavailable"),
                ("衍生品数据", derivative_state == "unavailable"),
                ("情绪指数", sentiment_state == "unavailable"),
            ]
            if value
        ]

        available_modules = 8 - len(unavailable)
        confidence = max(0.2, min(0.95, available_modules / 8 * 0.8 + 0.15))

        return {
            "date": int(datetime.now(timezone.utc).timestamp()),
            "trend_state": trend_state,
            "valuation_state": valuation_state,
            "capital_flow_state": capital_state,
            "onchain_state": onchain_state,
            "derivative_state": derivative_state,
            "macro_state": macro_state,
            "sentiment_state": sentiment_state,
            "risk_state": risk_state,
            "cycle_state": cycle_state,
            "regime": regime,
            "summary_cn": summary,
            "confidence": round(confidence, 3),
            "unavailable_modules": unavailable,
            "model_version": self.VERSION,
        }

    # ------------------------------------------------------------------ 分项
    @staticmethod
    def _trend_state(price, ma50, ma200, change_30d) -> str:
        if not price or not ma200:
            return "unavailable"
        if price > ma200 and (ma50 or 0) > ma200:
            return "strong_uptrend" if (change_30d or 0) > 10 else "uptrend"
        if price < ma200 and (ma50 or 0) < ma200:
            return "strong_downtrend" if (change_30d or 0) < -10 else "downtrend"
        return "sideways"

    @staticmethod
    def _derivative_state(funding: float | None) -> str:
        if funding is None:
            return "unavailable"
        if funding > 60:
            return "overheated_long"
        if funding > 20:
            return "crowded_long"
        if funding < -20:
            return "crowded_short"
        return "balanced"

    @staticmethod
    def _macro_state(macro: dict[str, Any]) -> str:
        if not macro:
            return "unavailable"
        us10y = macro.get("us10y")
        dxy = macro.get("dxy")
        if us10y is None and dxy is None:
            return "unavailable"
        # 仅做方向性判断（基于可得数据的变化率），不臆造数值
        dxy_change = macro.get("dxy_change_30d")
        us10y_change = macro.get("us10y_change_30d")
        if dxy_change is None and us10y_change is None:
            return "neutral"
        pressure = 0
        if dxy_change is not None and dxy_change > 2:
            pressure += 1
        if us10y_change is not None and us10y_change > 0.4:
            pressure += 1
        if dxy_change is not None and dxy_change < -2:
            pressure -= 1
        if us10y_change is not None and us10y_change < -0.4:
            pressure -= 1
        if pressure >= 1:
            return "tightening"
        if pressure <= -1:
            return "easing"
        return "neutral"

    @staticmethod
    def _sentiment_state(index: float | None) -> str:
        if index is None:
            return "unavailable"
        if index >= 80:
            return "extreme_greed"
        if index >= 60:
            return "greed"
        if index >= 45:
            return "neutral"
        if index >= 25:
            return "fear"
        return "extreme_fear"

    @staticmethod
    def _regime_name(trend, valuation, cycle, risk, sentiment, capital) -> tuple[str, str]:
        parts: list[str] = []
        if trend in ("strong_uptrend", "uptrend"):
            parts.append("上行趋势")
        elif trend in ("strong_downtrend", "downtrend"):
            parts.append("下行趋势")
        else:
            parts.append("趋势不明")

        if valuation in ("extreme_overvalue", "overvalue"):
            parts.append("估值偏高")
        elif valuation in ("deep_undervalue", "undervalue"):
            parts.append("估值偏低")
        else:
            parts.append("估值中性")

        if risk in ("high", "extreme"):
            parts.append("风险偏高")
        elif risk == "low":
            parts.append("风险较低")
        else:
            parts.append("风险中等")

        # parts 长度依赖上面各分支，这里按实际长度拼接，避免假定元素必然存在
        regime = " / ".join(parts)
        summary = (
            f"当前处于「{regime}」状态：{'、'.join(parts)}，"
            f"周期定位为 {cycle}，情绪读数 {sentiment}。"
            f"这只是对当前环境的客观描述，不构成买卖建议。"
        )
        return regime, summary
