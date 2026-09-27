# -*- coding: utf-8 -*-
"""分析引擎集合：指标 / 估值 / 周期 / 风险 / 综合状态 / 预测。

每个引擎相互独立、可单独升级（带 model_version），
输入统一为「标准化后的本地数据」，不直接依赖任何具体 Provider。
"""

from .cycle import CycleEngine
from .forecast import ForecastEngine
from .indicators import IndicatorEngine
from .regime import MarketRegimeEngine
from .risk import RiskEngine
from .valuation import ValuationEngine

__all__ = [
    "IndicatorEngine",
    "ValuationEngine",
    "CycleEngine",
    "RiskEngine",
    "MarketRegimeEngine",
    "ForecastEngine",
]
