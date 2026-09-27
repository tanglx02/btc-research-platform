# -*- coding: utf-8 -*-
"""回测与指标测试的公共构造工具。

重要说明（数据真实性约束）：
    本模块生成的都是**测试夹具**（Fixture），只写入 pytest 的临时数据库，
    永远不会进入 `data/btc.db`，也不会被用于任何对外展示。
    生产路径上系统仍然坚持「取不到就是取不到，绝不用假数据填补」。
    这里的合成序列是用来**检验算法因果性**的尺子，不是行情数据。
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

SOURCE = "unit_synthetic"

_T0 = datetime(2019, 1, 1, tzinfo=timezone.utc)


def synthetic_series(n: int, *, start: datetime | None = None,
                     base: float = 8000.0, seed: int = 7) -> list[float]:
    """确定性的合成价格序列（趋势 + 周期 + 小幅噪声），不用随机数保证可复现。"""
    start_dt = start or _T0
    out = []
    for i in range(n):
        trend = base * (1 + 0.0016 * i)
        wave = 1 + 0.18 * math.sin(i / 47.0) + 0.09 * math.sin(i / 113.0)
        jitter = 1 + 0.012 * math.sin(i * 1.7 + seed)
        out.append(round(trend * wave * jitter, 2))
    return out


def candle(ts: int, price: float, source: str = SOURCE) -> dict:
    """构造一根合法日线。"""
    return {
        "symbol": "BTC",
        "interval": "1d",
        "ts": ts,
        "open": round(price * 0.998, 2),
        "high": round(price * 1.015, 2),
        "low": round(price * 0.985, 2),
        "close": price,
        "volume": 1234.5,
        "source_id": source,
        "quality_status": "SINGLE_SOURCE",
        "observation_time": datetime.fromtimestamp(ts, tz=timezone.utc),
        "fetch_time": datetime.now(timezone.utc),
    }


def build_candles(n: int, *, start: datetime | None = None,
                  base: float = 8000.0, source: str = SOURCE) -> list[dict]:
    """生成 n 根连续日线（按时间升序）。"""
    start_dt = start or _T0
    prices = synthetic_series(n, base=base)
    return [
        candle(int((start_dt + timedelta(days=i)).timestamp()), p, source)
        for i, p in enumerate(prices)
    ]


def extend_candles(candles: list[dict], n: int, *, source: str = SOURCE) -> list[dict]:
    """在原序列末尾**接着生成** n 根日线（不覆盖已有时间点）。"""
    last_ts = max(c["ts"] for c in candles)
    last_price = candles[-1]["close"]
    start_dt = datetime.fromtimestamp(last_ts + 86400, tz=timezone.utc)
    return build_candles(n, start=start_dt, base=float(last_price), source=source)


def date_str(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
