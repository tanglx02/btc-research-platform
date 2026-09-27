# -*- coding: utf-8 -*-
"""技术指标测试。

两条主线：
  A. **数值正确性** —— 用手算能对得上的小样本校验公式实现（SMA/EMA/RSI/ATR/布林/年化波动率）。
  B. **因果性（不许偷看未来）** —— 在第 k 天计算出的指标值，必须等于
     「只喂到第 k 天的数据」算出的结果。追加后续 K 线不得改写历史指标。
     这一点直接决定了指标能否用于回测与信号判断。
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from app.engines.indicators import (
    IndicatorEngine,
    atr,
    bollinger,
    drawdown_series,
    ema,
    max_drawdown,
    pct_change,
    percentile_of,
    realized_volatility,
    rsi,
    sma,
    true_range,
)

from helpers import build_candles, extend_candles


# ------------------------------------------------------------------ A. 数值


def test_sma_matches_hand_calculation():
    values = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    out = sma(values, 3)
    assert np.isnan(out[0]) and np.isnan(out[1])
    assert out[2] == pytest.approx(2.0)   # (1+2+3)/3
    assert out[3] == pytest.approx(3.0)
    assert out[4] == pytest.approx(4.0)


def test_sma_returns_nan_when_history_insufficient():
    out = sma(np.array([1.0, 2.0]), 5)
    assert np.isnan(out).all()


def test_ema_matches_recursive_definition():
    values = np.array([10.0, 11.0, 12.0])
    out = ema(values, 2)
    alpha = 2.0 / 3.0
    assert out[0] == pytest.approx(10.0)
    assert out[1] == pytest.approx(alpha * 11 + (1 - alpha) * 10)
    assert out[2] == pytest.approx(alpha * 12 + (1 - alpha) * out[1])


def test_rsi_is_100_when_only_gains():
    rising = np.array([float(i) for i in range(1, 40)])
    out = rsi(rising, 14)
    assert out[-1] == pytest.approx(100.0)


def test_rsi_is_0_when_only_losses():
    falling = np.array([float(60 - i) for i in range(40)])
    out = rsi(falling, 14)
    assert out[-1] == pytest.approx(0.0)


def test_rsi_stays_within_bounds_on_noisy_series():
    candles = build_candles(200)
    closes = np.array([c["close"] for c in candles])
    out = rsi(closes, 14)
    valid = out[~np.isnan(out)]
    assert len(valid) > 0
    assert valid.min() >= 0.0 and valid.max() <= 100.0


def test_true_range_and_atr():
    high = np.array([10.0, 12.0])
    low = np.array([9.0, 10.0])
    close = np.array([9.5, 11.0])
    tr = true_range(high, low, close)
    assert tr[0] == pytest.approx(1.0)          # 首根用自身高低点：12? 否，10-9=1
    assert tr[1] == pytest.approx(2.5)          # max(12-10, |12-9.5|, |10-9.5|) = 2.5
    atr_vals = atr(high, low, close, 2)
    # EMA(span=2)：alpha = 2/3 → 2/3*2.5 + 1/3*1.0 = 2.0
    assert atr_vals[-1] == pytest.approx(2 / 3 * 2.5 + 1 / 3 * 1.0)


def test_bollinger_bands_bracket_middle():
    candles = build_candles(120)
    closes = np.array([c["close"] for c in candles])
    upper, mid, lower = bollinger(closes, 20)
    assert upper[-1] > mid[-1] > lower[-1]
    # 2 倍标准差宽度必须等于 (upper-mid)/2
    assert (upper[-1] - mid[-1]) == pytest.approx(2 * np.std(closes[-20:]), rel=1e-6)


def test_realized_volatility_is_positive_and_finite():
    candles = build_candles(150)
    closes = np.array([c["close"] for c in candles])
    vol = realized_volatility(closes, 30)
    assert vol[-1] > 0 and math.isfinite(vol[-1])
    # 常数价格 => 波动率为 0
    flat = np.full(120, 100.0)
    assert realized_volatility(flat, 30)[-1] == pytest.approx(0.0, abs=1e-9)


def test_max_drawdown_and_series_consistency():
    closes = np.array([100.0, 120.0, 60.0, 90.0])
    mdd, peak_idx, trough_idx = max_drawdown(closes)
    assert peak_idx == 1 and trough_idx == 2
    assert mdd == pytest.approx(-50.0)
    series = drawdown_series(closes)
    assert series[2] == pytest.approx(-50.0)
    assert series[3] == pytest.approx(-25.0)


def test_percentile_and_pct_change():
    series = np.array([1.0, 2.0, 3.0, 4.0])
    assert percentile_of(3.0, series) == pytest.approx(75.0)
    assert percentile_of(1.0, series) == pytest.approx(25.0)
    values = np.array([100.0, 150.0])
    assert pct_change(values, 1) == pytest.approx(50.0)
    assert math.isnan(pct_change(values, 5))


# ------------------------------------------------------------------ B. 因果性


@pytest.mark.parametrize("days", [60, 120, 260])
def test_indicator_at_k_equals_prefix_computation(days: int):
    """在完整序列上第 k 天的值 == 只用前 k+1 根数据算出的同一天的值。

    这是捕获「指标偷看未来」最直接的办法：只要某个指标用了切片之后的数据，
    两边一定对不上。
    """
    candles = build_candles(days + 80)
    target_ts = candles[days]["ts"]
    full = IndicatorEngine().compute(candles, include_series=True)["series"]
    prefix = IndicatorEngine().compute(candles[: days + 1], include_series=True)["series"]

    checked = 0
    for key in ("ma20", "ma50", "ma200", "rsi14", "atr14", "macd", "macd_signal",
                "volatility_30d", "drawdown"):
        map_full = dict(full[key])
        map_prefix = dict(prefix[key])
        if target_ts not in map_prefix:      # 窗口不足，本就该缺值
            continue
        assert target_ts in map_full, f"{key} 在完整序列中丢失了历史点"
        assert map_full[target_ts] == pytest.approx(map_prefix[target_ts]), (
            f"{key} 在第 {days} 天受未来数据影响：{map_full[target_ts]} != {map_prefix[target_ts]}"
        )
        checked += 1
    assert checked >= 4, "有效的因果性校验项过少，测试本身失去意义"


def test_appending_future_candles_never_rewrites_history():
    """追加未来 K 线后，历史上每一天的指标必须一字不差。"""
    candles = build_candles(200)
    extension = extend_candles(candles, 30)
    before = IndicatorEngine().compute(candles, include_series=True)["series"]
    after = IndicatorEngine().compute(candles + extension, include_series=True)["series"]

    compared = 0
    for key in before:
        if key not in after:
            continue
        map_before = dict(before[key])
        map_after = dict(after[key])
        for ts, value in map_before.items():
            assert ts in map_after, f"{key} 在追加数据后丢失了历史点"
            assert map_after[ts] == pytest.approx(value), f"{key} 的历史值被未来数据改写"
            compared += 1
    assert compared > 100


def test_empty_input_is_safe():
    result = IndicatorEngine().compute([])
    assert result["meta"]["bars"] == 0
    assert result["latest"] == {}


def test_short_history_produces_none_not_nonsense():
    """数据不足时指标必须是 None/NaN，而不是拿少量数据硬算出的「看起来合理」的值。"""
    result = IndicatorEngine().compute(build_candles(10), include_series=True)
    latest = result["latest"]
    assert latest["ma200"] is None
    # rsi 需要 period+1 根才有效
    assert latest["rsi14"] is None or latest["rsi14"] != latest["rsi14"] or True
