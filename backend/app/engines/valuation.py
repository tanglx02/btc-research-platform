# -*- coding: utf-8 -*-
"""估值引擎：输出**描述性状态**，不预测未来。

重要约束：任何估值状态都不能解释成「未来一定上涨」。
估值低只说明历史上类似位置的下行空间相对有限，估值高也不代表立刻下跌。
"""

from __future__ import annotations

from typing import Any

MODEL_VERSION = "valuation_v1"

STATE_CN = {
    "deep_undervalue": "深度低估区间",
    "undervalue": "偏低估",
    "fair": "合理区间",
    "overvalue": "偏高估",
    "extreme_overvalue": "极端高估区间",
}

STATE_DESC = {
    "deep_undervalue": "历史上出现这种状态的时间不多，通常与恐慌阶段重合。它描述的是概率环境，不是买入指令。",
    "undervalue": "低于长期中枢，位置相对偏低。",
    "fair": "处于历史中枢附近，谈不上便宜也不算贵。",
    "overvalue": "高于长期中枢，通常需要更多增量资金才能继续上行。",
    "extreme_overvalue": "历史上类似读数通常与强烈的乐观情绪重合，风险偏好需要更谨慎。",
}


class ValuationEngine:
    """基于价格分位、回撤、估值代理指标（MVRV 若可用）与波动环境综合估值。"""

    VERSION = MODEL_VERSION

    def evaluate(
        self,
        indicators: dict[str, Any],
        onchain: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        contributions: list[dict[str, Any]] = []
        weights_used = 0.0
        score_accum = 0.0
        missing: list[str] = []

        # 1) 价格历史分位（自有数据，恒可用）
        pct = indicators.get("price_percentile")
        if pct is not None:
            score = float(pct)
            score_accum += score * 0.30
            weights_used += 0.30
            contributions.append({"factor": "价格历史分位", "value": round(score, 1),
                                  "weight": 0.30, "explain": f"当前价格位于历史 {score:.0f}% 分位"})

        # 2) 距历史高点回撤
        dd = indicators.get("drawdown")
        if dd is not None:
            score = self._drawdown_to_score(float(dd))
            score_accum += score * 0.25
            weights_used += 0.25
            contributions.append({"factor": "距历史高点回撤", "value": round(float(dd), 1),
                                  "weight": 0.25, "explain": f"当前回撤 {dd:.1f}%，回撤越深分数越低"})

        # 3) 相对 200 日均线偏离（长期趋势位置）
        price = indicators.get("price")
        ma200 = indicators.get("ma200")
        if price and ma200:
            dev = (float(price) - float(ma200)) / float(ma200) * 100
            score = self._deviation_to_score(dev)
            score_accum += score * 0.20
            weights_used += 0.20
            contributions.append({"factor": "相对 200 日均线偏离", "value": round(dev, 1),
                                  "weight": 0.20, "explain": f"偏离 {dev:+.1f}%"})

        # 4) MVRV（需付费 Provider；缺失时降级并明确标注）
        mvrv = (onchain or {}).get("mvrv")
        if mvrv is not None:
            score = self._mvrv_to_score(float(mvrv))
            score_accum += score * 0.25
            weights_used += 0.25
            contributions.append({"factor": "MVRV", "value": round(float(mvrv), 2),
                                  "weight": 0.25, "explain": "链上浮盈倍数，越高代表获利了结压力越大"})
        else:
            missing.append("MVRV（需要付费链上数据源，当前未配置）")

        # 权重归一化：缺失的因子不会拉高或拉低结果
        score = (score_accum / weights_used) if weights_used > 0 else 50.0

        state = self._score_to_state(score)
        confidence = self._confidence(weights_used, len(contributions))

        return {
            "state": state,
            "state_cn": STATE_CN[state],
            "description": STATE_DESC[state],
            "score": round(score, 1),
            "confidence": round(confidence, 3),
            "contributions": contributions,
            "missing_factors": missing,
            "model_version": self.VERSION,
            "disclaimer": "估值只描述当前相对历史的位置，不构成买卖建议，也不代表未来收益。",
        }

    # ------------------------------------------------------------------ 映射
    @staticmethod
    def _drawdown_to_score(dd: float) -> float:
        """回撤 -80% => 0 分；0% => 100 分（线性）。"""
        return max(0.0, min(100.0, 100.0 + dd))

    @staticmethod
    def _deviation_to_score(dev: float) -> float:
        """偏离 -50% => 10 分；+200% => 100 分（分段线性）。"""
        if dev <= -50:
            return 10.0
        if dev <= 0:
            return 10 + (dev + 50) / 50 * 25
        if dev <= 100:
            return 35 + dev / 100 * 40
        return min(100.0, 75 + (dev - 100) / 100 * 25)

    @staticmethod
    def _mvrv_to_score(mvrv: float) -> float:
        """MVRV <1 => 极低；1~2 中低；2~3 中；3~4 高；>4 极高。"""
        if mvrv <= 1:
            return 10.0
        if mvrv <= 2:
            return 10 + (mvrv - 1) * 25
        if mvrv <= 3:
            return 35 + (mvrv - 2) * 30
        if mvrv <= 4:
            return 65 + (mvrv - 3) * 25
        return min(100.0, 90 + (mvrv - 4) * 5)

    @staticmethod
    def _score_to_state(score: float) -> str:
        if score < 20:
            return "deep_undervalue"
        if score < 40:
            return "undervalue"
        if score < 60:
            return "fair"
        if score < 80:
            return "overvalue"
        return "extreme_overvalue"

    @staticmethod
    def _confidence(weights_used: float, factors: int) -> float:
        """缺失因子越多，置信度越低（数据可用性直接反映到结论上）。"""
        base = 0.5 + 0.5 * weights_used
        if factors < 2:
            base *= 0.7
        return max(0.15, min(0.95, base))
