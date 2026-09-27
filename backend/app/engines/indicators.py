# -*- coding: utf-8 -*-
"""技术指标引擎（纯 numpy 实现，避免重依赖）。

所有指标：
1. 只使用 **已发生的** 数据（不使用未来数据）——回测与实盘共用同一套实现；
2. 返回 (ts, value) 序列，可写入 indicator_values 供回放使用；
3. 每个指标都能在 indicator_definitions 中找到通俗解释。
"""

from __future__ import annotations

from typing import Any

import numpy as np

MIN_BARS_NEEDED = 210  # 200 日均线所需的最小样本


# ------------------------------------------------------------------ 基础函数


def sma(values: np.ndarray, window: int) -> np.ndarray:
    if len(values) < window:
        return np.full(len(values), np.nan)
    out = np.full(len(values), np.nan)
    cumsum = np.cumsum(np.insert(values, 0, 0.0))
    out[window - 1 :] = (cumsum[window:] - cumsum[:-window]) / window
    return out


def ema(values: np.ndarray, span: int) -> np.ndarray:
    if len(values) == 0:
        return np.array([])
    alpha = 2.0 / (span + 1.0)
    out = np.empty(len(values), dtype=float)
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    return out


def rsi(values: np.ndarray, period: int = 14) -> np.ndarray:
    if len(values) <= period:
        return np.full(len(values), np.nan)
    deltas = np.diff(values)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.full(len(values), np.nan)
    avg_loss = np.full(len(values), np.nan)
    prev_gain = float(np.mean(gains[:period]))
    prev_loss = float(np.mean(losses[:period]))
    avg_gain[period] = prev_gain
    avg_loss[period] = prev_loss
    for i in range(period + 1, len(values)):
        prev_gain = (prev_gain * (period - 1) + gains[i - 1]) / period
        prev_loss = (prev_loss * (period - 1) + losses[i - 1]) / period
        avg_gain[i] = prev_gain
        avg_loss[i] = prev_loss
    rs = np.divide(avg_gain, avg_loss, out=np.full(len(values), np.nan), where=avg_loss != 0)
    rsi_vals = 100.0 - 100.0 / (1.0 + rs)
    rsi_vals[(avg_loss == 0) & (avg_gain > 0)] = 100.0
    return rsi_vals


def macd(values: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    ema_fast = ema(values, fast)
    ema_slow = ema(values, slow)
    line = ema_fast - ema_slow
    sig = ema(line, signal)
    hist = line - sig
    return line, sig, hist


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    return np.maximum.reduce([high - low, np.abs(high - prev_close), np.abs(low - prev_close)])


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    tr = true_range(high, low, close)
    return ema(tr, period)


def bollinger(values: np.ndarray, window: int = 20, k: float = 2.0):
    mid = sma(values, window)
    std = np.full(len(values), np.nan)
    for i in range(window - 1, len(values)):
        std[i] = float(np.std(values[i - window + 1 : i + 1]))
    upper = mid + k * std
    lower = mid - k * std
    return upper, mid, lower


def realized_volatility(close: np.ndarray, window: int = 30, annualize: int = 365) -> np.ndarray:
    """年化波动率：加密市场 365 交易日。"""
    if len(close) <= window:
        return np.full(len(close), np.nan)
    returns = np.diff(np.log(np.clip(close, 1e-9, None)))
    out = np.full(len(close), np.nan)
    for i in range(window, len(close)):
        out[i] = float(np.std(returns[i - window : i]) * np.sqrt(annualize) * 100)
    return out


def max_drawdown(close: np.ndarray) -> tuple[float, int, int]:
    """返回 (最大回撤%, 峰值索引, 谷底索引)。"""
    if len(close) == 0:
        return 0.0, 0, 0
    peak = np.maximum.accumulate(close)
    dd = (close - peak) / np.where(peak > 0, peak, np.nan)
    idx = int(np.nanargmin(dd)) if np.isfinite(dd).any() else 0
    peak_idx = int(np.argmax(close[: idx + 1])) if idx > 0 else 0
    return float(np.nanmin(dd) * 100), peak_idx, idx


def drawdown_series(close: np.ndarray) -> np.ndarray:
    peak = np.maximum.accumulate(close)
    return (close - peak) / np.where(peak > 0, peak, np.nan) * 100


def percentile_of(value: float, series: np.ndarray) -> float:
    """当前值在历史序列中的百分位（0~100）。"""
    clean = series[~np.isnan(series)] if series.dtype == float else series
    if len(clean) == 0:
        return float("nan")
    return float((clean <= value).sum() / len(clean) * 100)


def vwap(high: np.ndarray, low: np.ndarray, close: np.ndarray, volume: np.ndarray) -> np.ndarray:
    typical = (high + low + close) / 3
    pv = typical * np.maximum(volume, 0)
    cum_pv = np.cumsum(pv)
    cum_v = np.cumsum(np.maximum(volume, 1e-9))
    return cum_pv / cum_v


def pct_change(values: np.ndarray, periods: int) -> float:
    if len(values) <= periods or periods <= 0:
        return float("nan")
    base = values[-1 - periods]
    if base == 0:
        return float("nan")
    return float((values[-1] - base) / base * 100)


# ------------------------------------------------------------------ 指标引擎


class IndicatorEngine:
    """一次性计算全部核心指标。输入必须是按时间升序的标准化 K 线。"""

    VERSION = "ind_v1"

    def __init__(self) -> None:
        pass

    @staticmethod
    def _arrays(candles: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "ts": np.array([c["ts"] for c in candles], dtype=np.int64),
            "open": np.array([c["open"] for c in candles], dtype=float),
            "high": np.array([c["high"] for c in candles], dtype=float),
            "low": np.array([c["low"] for c in candles], dtype=float),
            "close": np.array([c["close"] for c in candles], dtype=float),
            "volume": np.array([c.get("volume", 0) or 0 for c in candles], dtype=float),
        }

    def compute(self, candles: list[dict[str, Any]], include_series: bool = True) -> dict[str, Any]:
        if not candles:
            return {"latest": {}, "series": {}, "meta": {"bars": 0}, "version": self.VERSION}

        arr = self._arrays(candles)
        close, high, low, vol = arr["close"], arr["high"], arr["low"], arr["volume"]

        ma20 = sma(close, 20)
        ma50 = sma(close, 50)
        ma200 = sma(close, 200)
        ema50 = ema(close, 50)
        rsi14 = rsi(close, 14)
        macd_line, macd_signal, macd_hist = macd(close)
        atr14 = atr(high, low, close, 14)
        bb_upper, bb_mid, bb_lower = bollinger(close, 20)
        vol30 = realized_volatility(close, 30)
        vwap_vals = vwap(high, low, close, vol)
        dd = drawdown_series(close)
        mdd, mdd_peak_idx, mdd_trough_idx = max_drawdown(close)

        def last(arr_: np.ndarray) -> float | None:
            val = arr_[-1] if len(arr_) else np.nan
            return None if val is None or not np.isfinite(val) else float(val)

        latest: dict[str, Any] = {
            "price": float(close[-1]),
            "ma20": last(ma20),
            "ma50": last(ma50),
            "ma200": last(ma200),
            "ema50": last(ema50),
            "rsi14": last(rsi14),
            "macd": last(macd_line),
            "macd_signal": last(macd_signal),
            "macd_hist": last(macd_hist),
            "atr14": last(atr14),
            "atr_pct": (last(atr14) or 0) / close[-1] * 100 if close[-1] else None,
            "bb_upper": last(bb_upper),
            "bb_mid": last(bb_mid),
            "bb_lower": last(bb_lower),
            "volatility_30d": last(vol30),
            "vwap": last(vwap_vals),
            "drawdown": last(dd),
            "max_drawdown": float(mdd),
            "change_24h": pct_change(close, 1),
            "change_7d": pct_change(close, 7),
            "change_30d": pct_change(close, 30),
            "change_90d": pct_change(close, 90),
            "change_365d": pct_change(close, 365),
        }

        # 历史分位（用完整历史计算，反映当前值在历史中的位置）
        latest["price_percentile"] = percentile_of(float(close[-1]), close)
        if latest["rsi14"] is not None:
            latest["rsi_percentile"] = percentile_of(latest["rsi14"], rsi14[~np.isnan(rsi14)])
        if latest["volatility_30d"] is not None:
            latest["volatility_percentile"] = percentile_of(latest["volatility_30d"], vol30[~np.isnan(vol30)])

        ath = float(np.nanmax(close))
        latest["ath"] = ath
        latest["distance_to_ath"] = float((close[-1] - ath) / ath * 100) if ath else 0.0

        atl = float(np.nanmin(close))
        latest["atl"] = atl
        latest["distance_to_atl"] = float((close[-1] - atl) / atl * 100) if atl else 0.0

        series: dict[str, list[Any]] = {}
        if include_series:
            ts_list = [int(t) for t in arr["ts"]]

            def to_series(arr_: np.ndarray, round_to: int = 4) -> list[Any]:
                out = []
                for t, v in zip(ts_list, arr_):
                    if v is None or not np.isfinite(v):
                        continue
                    out.append([int(t), round(float(v), round_to)])
                return out

            series["ma20"] = to_series(ma20)
            series["ma50"] = to_series(ma50)
            series["ma200"] = to_series(ma200)
            series["ema50"] = to_series(ema50)
            series["rsi14"] = to_series(rsi14)
            series["macd"] = to_series(macd_line)
            series["macd_signal"] = to_series(macd_signal)
            series["macd_hist"] = to_series(macd_hist)
            series["atr14"] = to_series(atr14)
            series["bb_upper"] = to_series(bb_upper)
            series["bb_mid"] = to_series(bb_mid)
            series["bb_lower"] = to_series(bb_lower)
            series["volatility_30d"] = to_series(vol30)
            series["vwap"] = to_series(vwap_vals)
            series["drawdown"] = to_series(dd)
            # 价格单独保留 6 位小数：它是阈值比较的直接对象，
            # 若只留 2 位，回测里"是否越过阈值"的判定会被舍入误差改写。
            series["price"] = [[int(t), round(float(c), 6)] for t, c in zip(ts_list, close)]

        return {
            "latest": latest,
            "series": series,
            "meta": {
                "bars": len(candles),
                "start_ts": int(arr["ts"][0]),
                "end_ts": int(arr["ts"][-1]),
                "max_drawdown_window": {
                    "peak_ts": int(arr["ts"][mdd_peak_idx]),
                    "trough_ts": int(arr["ts"][mdd_trough_idx]),
                },
            },
            "version": self.VERSION,
        }
