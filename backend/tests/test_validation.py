# -*- coding: utf-8 -*-
"""数据质量与交叉验证测试。

原则：
  * 多源一致 -> VERIFIED / CROSS_VERIFIED
  * 多源冲突 -> CONFLICT，且**必须保留全部来源值**（系统不偷偷挑一个）
  * 脏数据（负数、天文数字、NaN、格式错误）-> 在计算之前就被拒绝
  * 单源 -> SINGLE_SOURCE，明确降置信度，不能装成已验证
"""

from __future__ import annotations

import pytest

from app.providers.types import QualityStatus
from app.providers.validation import SourceValue, completeness_ratio, cross_validate, sanity_check_price


def sv(name: str, value: float) -> SourceValue:
    return SourceValue(provider=name, value=value, latency_ms=120.0)


# ---------------------------------------------------------------- 交叉验证
def test_single_source_is_not_verified():
    out = cross_validate([sv("a", 60000.0)])
    assert out.verdict == "SINGLE_SOURCE"
    assert out.quality == QualityStatus.SINGLE_SOURCE
    assert "仅 1 个数据源" in out.note


def test_two_consistent_sources_verified():
    out = cross_validate([sv("a", 60000.0), sv("b", 60010.0)])
    assert out.verdict == "VERIFIED"
    assert out.source_count == 2
    assert out.max_deviation_pct < 0.35


def test_three_sources_cross_verified():
    out = cross_validate([sv("a", 60000.0), sv("b", 60005.0), sv("c", 60012.0)])
    assert out.verdict == "CROSS_VERIFIED"
    assert out.quality == QualityStatus.CROSS_VERIFIED


def test_conflict_keeps_every_source():
    """冲突时必须把所有来源原样保留，前端要能展开看到差异。"""
    out = cross_validate([sv("a", 60000.0), sv("b", 72000.0)])
    assert out.verdict == "CONFLICT"
    assert out.quality == QualityStatus.CONFLICT
    assert out.max_deviation_pct > 0.35
    payload = out.to_dict()
    assert len(payload["sources"]) == 2
    assert {s["provider"] for s in payload["sources"]} == {"a", "b"}
    # 中位数作为折中值，但差异必须在 note 里说清楚
    assert out.median == pytest.approx(66000.0)
    assert "异常差异" in payload["note"]


def test_no_data_returns_missing_never_estimate():
    out = cross_validate([])
    assert out.verdict == "NO_DATA"
    assert out.quality == QualityStatus.MISSING
    assert out.median == 0.0


def test_non_finite_values_are_dropped():
    out = cross_validate([sv("a", 60000.0), sv("bad", float("nan")), sv("c", 60002.0)])
    assert out.source_count == 2


# ---------------------------------------------------------------- 脏数据拦截
@pytest.mark.parametrize("price,expected_ok", [
    (0.0, False),
    (-100.0, False),
    (10_000_001.0, False),
    (60000.0, True),
])
def test_sanity_check_price_bounds(price, expected_ok):
    ok, _ = sanity_check_price(price)
    assert ok is expected_ok


def test_sanity_check_reference_jump():
    """与参考值相比出现不可能的跳变（例如 -95%）应被拒绝。"""
    ok, reason = sanity_check_price(3000.0, reference=60000.0)
    assert ok is False
    assert "偏差过大" in reason


# ---------------------------------------------------------------- 完整性
def test_completeness_ratio():
    assert completeness_ratio(100, 100) == 1.0
    assert completeness_ratio(100, 80) == pytest.approx(0.8)
    assert completeness_ratio(0, 0) == 1.0  # 没有预期数据不算缺失
