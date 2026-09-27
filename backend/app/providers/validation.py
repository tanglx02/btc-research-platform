# -*- coding: utf-8 -*-
"""多数据源交叉验证（Cross Validation）。

原则：不能偷偷选一个源。必须记录每个源的值、偏差，并明确给出
VERIFIED / CONFLICT 判定，让前端展示「数据源一致」或「存在异常差异」。
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

from ..core.config import get_settings
from .types import QualityStatus


@dataclass
class SourceValue:
    provider: str
    value: float
    latency_ms: float = 0.0
    observation_time: str | None = None


@dataclass
class ValidationOutcome:
    verdict: str                      # VERIFIED | CONFLICT | SINGLE_SOURCE
    quality: QualityStatus
    median: float
    mean: float
    min_value: float
    max_value: float
    max_deviation_pct: float
    spread_pct: float
    source_count: int
    values: list[SourceValue] = field(default_factory=list)
    tolerance_pct: float = 0.35
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "quality": self.quality.value,
            "median": self.median,
            "mean": round(self.mean, 6),
            "min": self.min_value,
            "max": self.max_value,
            "max_deviation_pct": round(self.max_deviation_pct, 4),
            "spread_pct": round(self.spread_pct, 4),
            "source_count": self.source_count,
            "tolerance_pct": self.tolerance_pct,
            "note": self.note,
            "sources": [
                {
                    "provider": v.provider,
                    "value": v.value,
                    "deviation_pct": round(
                        abs(v.value - self.median) / self.median * 100 if self.median else 0.0, 4
                    ),
                    "latency_ms": round(v.latency_ms, 2),
                    "observation_time": v.observation_time,
                }
                for v in self.values
            ],
        }


def cross_validate(
    values: list[SourceValue],
    *,
    tolerance_pct: float | None = None,
    min_sources: int | None = None,
) -> ValidationOutcome:
    """对同一指标的多个来源值做统计交叉验证。

    - 少于 min_sources 个来源 -> SINGLE_SOURCE（置信度下降，但不报错）
    - 最大偏差 > 容差        -> CONFLICT（记录冲突，前端必须提示）
    - 否则                    -> VERIFIED / CROSS_VERIFIED
    """
    s = get_settings()
    tol = tolerance_pct if tolerance_pct is not None else s.CROSS_VALIDATION_TOLERANCE_PCT
    need = min_sources if min_sources is not None else s.CROSS_VALIDATION_MIN_SOURCES

    clean = [v for v in values if v.value is not None and _is_finite(v.value)]
    if not clean:
        return ValidationOutcome(
            verdict="NO_DATA",
            quality=QualityStatus.MISSING,
            median=0.0, mean=0.0, min_value=0.0, max_value=0.0,
            max_deviation_pct=0.0, spread_pct=0.0, source_count=0,
            tolerance_pct=tol, note="无可用数据：不生成任何估计值",
        )

    nums = [v.value for v in clean]
    median = statistics.median(nums)
    mean = statistics.fmean(nums)
    min_v, max_v = min(nums), max(nums)
    dev = max((abs(v - median) / median * 100 for v in nums), default=0.0) if median else 0.0
    spread = ((max_v - min_v) / median * 100) if median else 0.0

    if len(clean) < need:
        return ValidationOutcome(
            verdict="SINGLE_SOURCE",
            quality=QualityStatus.SINGLE_SOURCE,
            median=median, mean=mean, min_value=min_v, max_value=max_v,
            max_deviation_pct=dev, spread_pct=spread, source_count=len(clean),
            tolerance_pct=tol, values=clean,
            note=f"仅 {len(clean)} 个数据源可用（需 {need} 个才能交叉验证），结果置信度已下调",
        )

    if dev > tol:
        return ValidationOutcome(
            verdict="CONFLICT",
            quality=QualityStatus.CONFLICT,
            median=median, mean=mean, min_value=min_v, max_value=max_v,
            max_deviation_pct=dev, spread_pct=spread, source_count=len(clean),
            tolerance_pct=tol, values=clean,
            note=f"数据源之间存在异常差异（最大偏差 {dev:.3f}% > 容差 {tol}%），当前结果仅供参考",
        )

    return ValidationOutcome(
        verdict="CROSS_VERIFIED" if len(clean) >= 3 else "VERIFIED",
        quality=QualityStatus.CROSS_VERIFIED if len(clean) >= 3 else QualityStatus.VERIFIED,
        median=median, mean=mean, min_value=min_v, max_value=max_v,
        max_deviation_pct=dev, spread_pct=spread, source_count=len(clean),
        tolerance_pct=tol, values=clean,
        note=f"{len(clean)} 个数据源一致（最大偏差 {dev:.3f}%）",
    )


def _is_finite(x: Any) -> bool:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return False
    return f == f and abs(f) != float("inf")


def sanity_check_price(price: float, *, reference: float | None = None) -> tuple[bool, str]:
    """价格合理性校验：防止脏数据写入数据库覆盖正确数据。"""
    if not _is_finite(price) or price <= 0:
        return False, "价格非正数或非法"
    if price < 1 or price > 10_000_000:
        return False, "价格超出合理区间（1 ~ 10,000,000 USD）"
    if reference and reference > 0:
        deviation = abs(price - reference) / reference * 100
        if deviation > 25:
            return False, f"与参考值偏差过大（{deviation:.2f}%），拒绝写入以免污染历史数据"
    return True, ""


def completeness_ratio(expected: int, actual: int) -> float:
    if expected <= 0:
        return 1.0
    return max(0.0, min(1.0, actual / expected))
