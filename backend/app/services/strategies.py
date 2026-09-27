# -*- coding: utf-8 -*-
"""定投策略引擎：规则完全可配置，且与回测共用同一份实现。

关键：策略函数签名统一为 (params, context) -> multiplier，
context 中的所有字段都必须来自「决策时点已知的数据」，
回测与实盘共用，杜绝未来数据泄漏。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class StrategyContext:
    """决策时可使用的全部信息（严格限制在决策时点之前）。"""

    ts: int
    price: float
    drawdown_pct: float          # 距历史高点回撤（负百分比）
    ath: float                   # 截至当前已知的最高价
    price_percentile: float | None = None  # 截至当前已知价格分位
    ma200: float | None = None
    rsi14: float | None = None
    risk_level: str | None = None
    cycle_phase: str | None = None
    cash_balance: float = 0.0


StrategyFn = Callable[[dict[str, Any], StrategyContext], float]


# ------------------------------------------------------------------ 策略实现


def _dca_fixed(params: dict[str, Any], ctx: StrategyContext) -> float:
    """固定金额定投。"""
    return 1.0


def _dca_drawdown(params: dict[str, Any], ctx: StrategyContext) -> float:
    """回撤加仓：距 ATH 回撤越深，投入倍数越大。

    params 示例：{"ladder": [[-10, 1.0], [-20, 1.2], [-30, 1.5], [-40, 2.0], [-50, 2.5]]}
    """
    ladder: list[list[float]] = params.get("ladder") or [
        [-10, 1.0], [-20, 1.2], [-30, 1.5], [-40, 2.0], [-50, 2.5], [-70, 3.0],
    ]
    multiplier = float(params.get("base_multiplier", 1.0))
    # 从最深的一档开始匹配，命中即停。
    # 早期实现漏了 break，导致任何回撤最终都被最浅的一档（-10）覆盖，
    # 倍数恒等于 1 —— 回撤加仓策略与固定定投结果完全一样，功能形同虚设。
    for threshold, mult in sorted(ladder, key=lambda x: x[0]):
        if ctx.drawdown_pct <= float(threshold):
            multiplier = float(mult)
            break
    return multiplier


def _dca_valuation(params: dict[str, Any], ctx: StrategyContext) -> float:
    """估值加仓：价格分位越低投入越多。

    params 示例：{"low_percentile": 30, "high_percentile": 80, "max_multiplier": 2.0, "min_multiplier": 0.5}
    """
    if ctx.price_percentile is None:
        return 1.0
    low = float(params.get("low_percentile", 30))
    high = float(params.get("high_percentile", 85))
    max_m = float(params.get("max_multiplier", 2.0))
    min_m = float(params.get("min_multiplier", 0.5))
    pct = float(ctx.price_percentile)
    if pct <= low:
        return max_m
    if pct >= high:
        return min_m
    return round(max_m - (pct - low) / max(1e-9, high - low) * (max_m - min_m), 3)


def _dca_risk(params: dict[str, Any], ctx: StrategyContext) -> float:
    """风险调整：风险越高投入越少（或反之，由 params 决定方向）。"""
    mapping: dict[str, float] = params.get("mapping") or {
        "low": 1.0, "moderate": 1.0, "elevated": 0.9, "high": 0.7, "extreme": 0.5,
    }
    if not ctx.risk_level:
        return 1.0
    return float(mapping.get(ctx.risk_level, 1.0))


def _dca_cycle(params: dict[str, Any], ctx: StrategyContext) -> float:
    """周期调整：熊市/底部加仓，顶部减少。"""
    mapping: dict[str, float] = params.get("mapping") or {
        "deep_bear": 2.0, "bear": 1.5, "accumulation": 1.5, "recovery": 1.2,
        "markup": 1.0, "acceleration": 0.8, "distribution": 0.6, "top_risk": 0.4,
        "correction": 1.2, "range": 1.0,
    }
    if not ctx.cycle_phase:
        return 1.0
    return float(mapping.get(ctx.cycle_phase, 1.0))


# ------------------------------------------------------------------ 回测用状态分类

# 回测时不能依赖 RiskEngine / CycleEngine —— 它们需要链上、情绪、资金费率等，
# 而回测的历史切片里拿不到这些（拿未来的就更不行）。
# 这里给出一套**只依赖价格历史**的简化分类，用于回测内的策略决策。
# 与 engines 中的完整版本相比区分度更低，但保证因果闭环。


def classify_risk_level(drawdown_pct: float, annual_vol_pct: float | None) -> str:
    """用「距高点回撤 + 实现波动」判断风险等级（均为截至当日可算出的量）。"""
    score = 0.0
    dd = abs(drawdown_pct)
    if dd >= 70:
        score += 55
    elif dd >= 50:
        score += 40
    elif dd >= 30:
        score += 25
    elif dd >= 15:
        score += 10
    if annual_vol_pct is not None:
        if annual_vol_pct >= 100:
            score += 30
        elif annual_vol_pct >= 70:
            score += 20
        elif annual_vol_pct >= 50:
            score += 10
    if score >= 80:
        return "extreme"
    if score >= 60:
        return "high"
    if score >= 40:
        return "elevated"
    if score >= 20:
        return "moderate"
    return "low"


def classify_cycle_phase(drawdown_pct: float, price_vs_ma200: float | None,
                         change_30d: float | None) -> str:
    """用「回撤深度 + 相对 200 日均线位置 + 30 日动量」判断大致阶段。"""
    dev = price_vs_ma200  # 已归一化为 (price-ma200)/ma200
    momentum = change_30d or 0.0
    heater = momentum > 25
    if dev is None:
        # 没有足够历史算均线时，只用回撤做粗分类
        return "deep_bear" if drawdown_pct <= -60 else "bear" if drawdown_pct <= -35 else "range"
    if dev > 0.5 and heater:
        return "acceleration"
    if dev > 0.3:
        return "markup"
    if dev > 0.05:
        return "recovery" if momentum > 0 else "distribution"
    if dev > -0.10:
        return "range"
    if drawdown_pct <= -60:
        return "deep_bear"
    if drawdown_pct <= -35:
        return "accumulation"
    return "correction" if momentum < 0 else "bear"


STRATEGIES: dict[str, StrategyFn] = {
    "dca_fixed": _dca_fixed,
    "dca_drawdown": _dca_drawdown,
    "dca_valuation": _dca_valuation,
    "dca_risk": _dca_risk,
    "dca_cycle": _dca_cycle,
}

STRATEGY_META: dict[str, dict[str, Any]] = {
    "dca_fixed": {
        "name_cn": "固定金额定投",
        "description": "每个周期投入固定金额，最简单、最容易坚持。",
        "default_params": {},
        "params_schema": {},
    },
    "dca_drawdown": {
        "name_cn": "回撤加仓",
        "description": "距离历史高点回撤越深，投入金额越大。适合希望在下跌中摊低成本的用户。",
        "default_params": {"ladder": [[-10, 1.0], [-20, 1.2], [-30, 1.5], [-40, 2.0], [-50, 2.5], [-70, 3.0]]},
        "params_schema": {
            "ladder": {"type": "list", "desc": "[[回撤阈值%, 倍数], ...]，按回撤从浅到深排列"},
        },
    },
    "dca_valuation": {
        "name_cn": "估值加仓",
        "description": "价格处于历史较低分位时投入更多，高分位时减少。",
        "default_params": {"low_percentile": 30, "high_percentile": 85, "max_multiplier": 2.0, "min_multiplier": 0.5},
        "params_schema": {
            "low_percentile": {"type": "number", "desc": "低于该分位按最大倍数投入"},
            "high_percentile": {"type": "number", "desc": "高于该分位按最小倍数投入"},
            "max_multiplier": {"type": "number", "desc": "最大倍数"},
            "min_multiplier": {"type": "number", "desc": "最小倍数"},
        },
    },
    "dca_risk": {
        "name_cn": "风险调整",
        "description": "根据系统风险评分动态调整投入金额。",
        "default_params": {"mapping": {"low": 1.0, "moderate": 1.0, "elevated": 0.9, "high": 0.7, "extreme": 0.5}},
        "params_schema": {"mapping": {"type": "object", "desc": "风险等级 -> 倍数"}},
    },
    "dca_cycle": {
        "name_cn": "周期调整",
        "description": "根据系统识别的市场周期阶段调整投入金额。",
        "default_params": {"mapping": {"deep_bear": 2.0, "bear": 1.5, "accumulation": 1.5, "recovery": 1.2,
                                        "markup": 1.0, "acceleration": 0.8, "distribution": 0.6,
                                        "top_risk": 0.4, "correction": 1.2, "range": 1.0}},
        "params_schema": {"mapping": {"type": "object", "desc": "周期阶段 -> 倍数"}},
    },
}


def multiplier_for(strategy_code: str, params: dict[str, Any], ctx: StrategyContext) -> float:
    fn = STRATEGIES.get(strategy_code, _dca_fixed)
    try:
        value = float(fn(params or {}, ctx))
    except Exception:  # noqa: BLE001 - 策略异常不应中断回测，退化为倍数 1
        return 1.0
    return max(0.0, min(10.0, value))


def list_strategies() -> list[dict[str, Any]]:
    return [{"code": code, **meta} for code, meta in STRATEGY_META.items()]
