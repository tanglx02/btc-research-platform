# -*- coding: utf-8 -*-
"""风险引擎：六维独立分析 + 可展开的全部原因。

每个维度明确列出：得分、等级、判断依据、使用了哪些数据、哪些数据缺失。
数据缺失 -> 该维度标注 unavailable，而不是假装无事发生。
"""

from __future__ import annotations

from typing import Any

MODEL_VERSION = "risk_v1"

LEVELS = [
    (0, 20, "low", "低风险"),
    (20, 40, "moderate", "中等"),
    (40, 60, "elevated", "偏高"),
    (60, 80, "high", "高风险"),
    (80, 100, "extreme", "极高风险"),
]


def level_of(score: float) -> tuple[str, str]:
    for low, high, code, cn in LEVELS:
        if score < high:
            return code, cn
    return "extreme", "极高风险"


class RiskEngine:
    VERSION = MODEL_VERSION

    def analyze(
        self,
        indicators: dict[str, Any],
        valuation_state: str | None = None,
        funding_annualized: float | None = None,
        open_interest_usd: float | None = None,
        macro: dict[str, Any] | None = None,
        onchain_available: bool = False,
    ) -> dict[str, Any]:
        macro = macro or {}
        availability: dict[str, bool] = {}

        # ---------------- 估值风险
        val_map = {"deep_undervalue": 5, "undervalue": 15, "fair": 40, "overvalue": 65, "extreme_overvalue": 85}
        if valuation_state:
            valuation_risk = float(val_map.get(valuation_state, 40))
            availability["valuation"] = True
            valuation_reasons = [f"估值状态：{valuation_state}"]
        else:
            valuation_risk = 40.0
            availability["valuation"] = False
            valuation_reasons = ["估值数据不可用，该维度使用中性默认值且不参与重点加权"]

        # ---------------- 波动风险
        vol = indicators.get("volatility_30d")
        vol_pctile = indicators.get("volatility_percentile")
        if vol is not None:
            volatility_risk = float(min(100.0, max(0.0, vol * 1.6)))
            availability["volatility"] = True
            volatility_reasons = [
                f"30 日年化波动率 {vol:.1f}%",
                f"处于历史 {vol_pctile:.0f}% 分位" if vol_pctile is not None else "历史分位数据不足",
            ]
        else:
            volatility_risk = 40.0
            availability["volatility"] = False
            volatility_reasons = ["波动率数据不足"]

        # ---------------- 杠杆风险
        availability["leverage"] = funding_annualized is not None or open_interest_usd is not None
        if funding_annualized is not None:
            leverage_risk = float(min(100.0, max(0.0, (funding_annualized - 5) * 2.2)))
            leverage_reasons = [f"资金费率年化 {funding_annualized:.1f}%"]
            if funding_annualized > 40:
                leverage_reasons.append("多头杠杆拥挤，回调时容易出现连锁强平")
        else:
            leverage_risk = 30.0
            leverage_reasons = ["衍生品数据源不可用，杠杆维度按不可知处理"]

        # ---------------- 流动性风险（成交量-proxy）
        volume = indicators.get("volume_latest")
        availability["liquidity"] = bool(volume)
        if volume:
            liquidity_risk = 30.0
            liquidity_reasons = [f"最新成交量数据可得（{volume:.0f} BTC）"]
        else:
            liquidity_risk = 40.0
            liquidity_reasons = ["未获取到成交量数据，流动性风险按中性处理"]

        # ---------------- 宏观风险
        availability["macro"] = bool(macro.get("dxy") or macro.get("us10y"))
        if availability["macro"]:
            macro_risk = 40.0
            macro_reasons: list[str] = []
            dxy = macro.get("dxy")
            if dxy is not None:
                macro_reasons.append(f"美元指数 {dxy}")
            us10y = macro.get("us10y")
            if us10y is not None:
                macro_reasons.append(f"10 年期美债收益率 {us10y}%")
                if us10y >= 4.5:
                    macro_risk += 15
                elif us10y <= 3.0:
                    macro_risk -= 10
            macro_risk = float(max(0.0, min(100.0, macro_risk)))
        else:
            macro_risk = 40.0
            macro_reasons = ["宏观数据源未配置或不可用"]

        # ---------------- 链上风险
        availability["onchain"] = onchain_available
        if onchain_available:
            onchain_risk = 45.0
            onchain_reasons = ["链上基础数据可用（费用/算力/拥堵度等）"]
        else:
            onchain_risk = 40.0
            onchain_reasons = ["链上数据不可用，该维度不参与判断"]

        weights = {
            "trend": 0.20, "valuation": 0.20, "leverage": 0.15,
            "liquidity": 0.10, "macro": 0.15, "onchain": 0.10, "volatility": 0.10,
        }

        # 趋势风险：跌破长期均线 + 深度回撤
        trend_risk = 40.0
        trend_reasons: list[str] = []
        price, ma200, ma50 = indicators.get("price"), indicators.get("ma200"), indicators.get("ma50")
        if price and ma200:
            if price < ma200:
                trend_risk += 25
                trend_reasons.append("价格位于 200 日均线下方，中期趋势偏弱")
            else:
                trend_risk -= 15
                trend_reasons.append("价格位于 200 日均线上方")
        dd = indicators.get("drawdown")
        if dd is not None:
            if dd < -60:
                trend_risk += 15
                trend_reasons.append(f"距历史高点回撤 {dd:.0f}%，处于深度回撤区间")
            elif dd < -30:
                trend_risk += 8
                trend_reasons.append(f"距历史高点回撤 {dd:.0f}%")
        trend_risk = float(max(0.0, min(100.0, trend_risk)))
        availability["trend"] = bool(price and ma200)

        parts = {
            "trend": (trend_risk, trend_reasons),
            "valuation": (valuation_risk, valuation_reasons),
            "volatility": (volatility_risk, volatility_reasons),
            "leverage": (leverage_risk, leverage_reasons),
            "liquidity": (liquidity_risk, liquidity_reasons),
            "macro": (macro_risk, macro_reasons),
            "onchain": (onchain_risk, onchain_reasons),
        }

        # 缺失维度不参与加权（避免用默认值稀释真实信号）
        total_w = sum(weights[k] for k in parts if availability.get(k, False)) or 1.0
        overall = sum(parts[k][0] * weights[k] for k in parts if availability.get(k, False)) / total_w

        level_code, level_cn = level_of(overall)
        return {
            "overall_risk": round(overall, 1),
            "level": level_code,
            "level_cn": level_cn,
            "dimensions": {
                k: {
                    "score": round(v[0], 1),
                    "level": level_of(v[0])[0],
                    "level_cn": level_of(v[0])[1],
                    "available": availability.get(k, False),
                    "reasons": v[1],
                }
                for k, v in parts.items()
            },
            "data_availability": availability,
            "weights": weights,
            "model_version": self.VERSION,
            "note": "风险评分描述当前环境的脆弱程度，不是对未来价格的预测。",
        }
