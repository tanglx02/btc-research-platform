# -*- coding: utf-8 -*-
"""预测引擎（统计学视角，禁止假装精准）。

规则：
1. 只输出概率分布与区间，**不输出「必涨/必跌/抄底/逃顶」**。
2. 样本不足或一致性差时，直接返回「当前无法可靠预测」。
3. 必须同时给出：样本数量、所用数据区间、模型版本、不确定性来源。
"""

from __future__ import annotations

from typing import Any

import numpy as np

MODEL_VERSION = "forecast_v1"
MIN_SAMPLES = 30          # 低于该样本数直接拒绝输出
MIN_CONSISTENCY = 0.35    # 历史结果过于分散时也不输出


class ForecastEngine:
    VERSION = MODEL_VERSION

    def forecast(
        self,
        candles: list[dict[str, Any]],
        horizon_days: int = 30,
        *,
        max_samples: int = 5000,
    ) -> dict[str, Any]:
        """基于历史「相似状态」的向前收益分布，给出上涨/横盘/下跌概率。"""
        closes = np.array([c["close"] for c in candles], dtype=float)
        n = len(closes)
        if n < MIN_SAMPLES + horizon_days + 250:
            return self._refuse(
                f"历史数据不足（仅 {n} 根日线，至少需要 {MIN_SAMPLES + horizon_days + 250} 根）"
            )

        # ---- 构造特征：200 日偏离 + 回撤 + 近 30 日收益率 + 波动率
        window = 200
        features: list[tuple[float, float, float, float]] = []
        targets: list[float] = []
        for i in range(window, n - horizon_days):
            ma200 = float(np.mean(closes[i - window + 1 : i + 1]))
            peak = float(np.max(closes[: i + 1]))
            dev = (closes[i] - ma200) / ma200
            dd = (closes[i] - peak) / peak
            ret30 = (closes[i] - closes[i - 30]) / closes[i - 30]
            vol = float(np.std(np.diff(np.log(closes[i - 30 : i + 1])))) if i > 30 else 0.0
            future = (closes[i + horizon_days] - closes[i]) / closes[i]
            features.append((dev, dd, ret30, vol))
            targets.append(float(future))

        if len(targets) < MIN_SAMPLES:
            return self._refuse(f"可用历史样本不足（{len(targets)} 个）")

        X = np.array(features)
        y = np.array(targets)

        # ---- 当前状态
        cur_ma200 = float(np.mean(closes[-window:]))
        cur_peak = float(np.max(closes))
        cur_dev = (closes[-1] - cur_ma200) / cur_ma200
        cur_dd = (closes[-1] - cur_peak) / cur_peak
        cur_ret30 = (closes[-1] - closes[-31]) / closes[-31]
        cur_vol = float(np.std(np.diff(np.log(closes[-31:]))))

        # ---- 距离加权（马氏距离的简化版：按特征标准差归一化）
        scale = np.std(X, axis=0)
        scale = np.where(scale < 1e-9, 1.0, scale)
        diff = (X - np.array([cur_dev, cur_dd, cur_ret30, cur_vol])) / scale
        dist = np.sqrt(np.sum(diff**2, axis=1))
        k = min(int(len(targets) * 0.15), 200)
        k = max(k, MIN_SAMPLES)
        nearest_idx = np.argsort(dist)[:k]
        samples = y[nearest_idx]

        # ---- 一致性检查：样本自身离散度过大则拒绝
        consistency = float(np.mean(np.abs(samples - np.mean(samples)) < 0.25))
        if consistency < MIN_CONSISTENCY:
            return self._refuse(
                f"历史相似样本结果过于分散（一致性 {consistency:.2f}），当前无法可靠预测"
            )

        up = float(np.mean(samples > 0.05) * 100)
        down = float(np.mean(samples < -0.05) * 100)
        flat = max(0.0, 100.0 - up - down)

        p10, p25, p50, p75, p90 = [
            float(v) for v in np.percentile(samples, [10, 25, 50, 75, 90])
        ]
        price_now = float(closes[-1])

        return {
            "available": True,
            "horizon_days": horizon_days,
            "current_price": round(price_now, 2),
            "probabilities": {
                "up": round(up, 1),
                "flat": round(flat, 1),
                "down": round(down, 1),
            },
            "expected_return_pct": round(float(np.mean(samples)) * 100, 2),
            "range": {
                "p10": round(price_now * (1 + p10), 0),
                "p25": round(price_now * (1 + p25), 0),
                "median": round(price_now * (1 + p50), 0),
                "p75": round(price_now * (1 + p75), 0),
                "p90": round(price_now * (1 + p90), 0),
            },
            "sample_count": int(len(samples)),
            "consistency": round(consistency, 3),
            "data_range": {
                "start_ts": int(candles[0]["ts"]),
                "end_ts": int(candles[-1]["ts"]),
                "bars": n,
            },
            "model_version": self.VERSION,
            "uncertainty": [
                "历史相似不代表因果：样本来自统计类比，不是因果模型",
                "加密市场受政策、流动性与突发事件影响，历史上小概率事件频发",
                "样本内相似度高不等于样本外同样有效",
            ],
            "disclaimer": "预测结果仅为历史统计分布的概率描述，不构成投资建议。",
        }

    @staticmethod
    def _refuse(reason: str) -> dict[str, Any]:
        return {
            "available": False,
            "reason": reason,
            "message": "当前无法可靠预测",
            "model_version": MODEL_VERSION,
            "disclaimer": "系统宁可不给结论，也不用没有统计依据的数字误导用户。",
        }
