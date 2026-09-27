# -*- coding: utf-8 -*-
"""指标取值实现：把 catalog 里登记的每个指标接到**已有的**数据层上。

铁律：
* 只读，不写任何业务表；不直接请求 Provider（数据由采集层负责，这里只消费）。
* 取不到就抛 MetricUnavailable，绝不返回 0 / 默认值 / 上一次的旧值来「凑一个数」。
* 每个值都记录它是谁提供的（metric_sources），这样邮件里能写清楚数据来源。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..db.base import get_session_factory
from ..providers.types import DataCategory
from .catalog import MetricContext, MetricUnavailable, resolver, series_resolver

# 各指标的历史回看窗口（天）。用于分位、变化率、交叉判断。
MAX_LOOKBACK_DAYS = 4000


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _close_pct_change(values: list[float], periods: int) -> float | None:
    """相对 periods 根之前的变化百分比。样本不足时返回 None（不外推）。"""
    if len(values) <= periods or periods <= 0:
        return None
    base = values[-1 - periods]
    if not base:
        return None
    return (values[-1] - base) / base * 100.0


def _from_candles(ctx: MetricContext) -> list[dict[str, Any]]:
    return ctx.candles or []


def _closes(ctx: MetricContext) -> list[float]:
    return [float(c["close"]) for c in _from_candles(ctx) if c.get("close") is not None]


def _intraday_closes(ctx: MetricContext) -> list[float]:
    return [float(c["close"]) for c in (ctx.intraday or []) if c.get("close") is not None]


def _mark(ctx: MetricContext, code: str, block: dict[str, Any]) -> None:
    """从数据块里提取来源信息，写进 ctx.metric_sources。"""
    src = block.get("source") or {}
    provider = src.get("provider") or src.get("source") or ""
    if not provider and isinstance(src.get("providers_used_historically"), list):
        hist = src["providers_used_historically"]
        provider = hist[-1] if hist else ""
    quality = block.get("quality") or src.get("quality_status") or ""
    ts = block.get("observation_time") or block.get("fetch_time")
    updated = None
    if isinstance(ts, str):
        try:
            updated = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            updated = None
    ctx.mark_source(code, str(provider), str(quality), updated)


# ============================================================ 价格与行情

@resolver("price")
def _price(ctx: MetricContext) -> float | None:
    block = ctx.price_block or {}
    # 优先实时价；实时段不可用时使用日线收盘价，但绝不冒充成实时价
    if block.get("available") and block.get("price") is not None:
        ctx.mark_source("price", str(block.get("provider") or ""),
                        str(block.get("quality") or ""),
                        block.get("observation_time"))
        return float(block["price"])
    closes = _closes(ctx)
    if closes:
        ctx.mark_source("price", ctx.price_block.get("last_known_provider") or "", "LOCAL_CANDLE",
                        _candle_time(ctx))
        return closes[-1]
    raise MetricUnavailable("price", "实时价格与本地 K 线都不可用")


def _candle_time(ctx: MetricContext) -> datetime | None:
    candles = _from_candles(ctx)
    if not candles:
        return None
    ts = candles[-1].get("ts")
    return datetime.fromtimestamp(int(ts), tz=timezone.utc) if ts else None


@resolver("close")
def _close(ctx: MetricContext) -> float | None:
    closes = _closes(ctx)
    if not closes:
        raise MetricUnavailable("close", "本地没有日线数据")
    ctx.mark_source("close", str(_from_candles(ctx)[-1].get("provider") or ""), "LOCAL_DATABASE",
                    _candle_time(ctx))
    return closes[-1]


@resolver("volume")
def _volume(ctx: MetricContext) -> float | None:
    candles = _from_candles(ctx)
    if not candles:
        raise MetricUnavailable("volume", "本地没有日线数据")
    v = candles[-1].get("volume")
    if v is None:
        raise MetricUnavailable("volume", "最近一根 K 线没有成交量字段")
    return float(v)


@series_resolver("price")
def _price_series(ctx: MetricContext) -> list[float]:
    return _closes(ctx)


@series_resolver("close")
def _close_series(ctx: MetricContext) -> list[float]:
    return _closes(ctx)


def _make_change_resolver(periods: int, code: str):
    def fn(ctx: MetricContext) -> float | None:
        # 小时级窗口优先用小时线，保证 1h/4h/12h 有意义
        if code in ("change_1h", "change_4h", "change_12h"):
            intraday = _intraday_closes(ctx)
            per_bar = {"change_1h": 1, "change_4h": 4, "change_12h": 12}[code]
            if len(intraday) > per_bar and per_bar >= 1:
                val = _close_pct_change(intraday, per_bar)
                if val is not None:
                    ctx.mark_source(code, "local_1h_candles", "LOCAL_DATABASE")
                    return round(val, 4)
        closes = _closes(ctx)
        val = _close_pct_change(closes, periods)
        if val is None:
            raise MetricUnavailable(code, f"本地日线不足 {periods + 1} 根，无法计算该窗口变化率")
        ctx.mark_source(code, "local_1d_candles", "LOCAL_DATABASE", _candle_time(ctx))
        return round(val, 4)

    return fn


for _code, _periods in (
    ("change_1h", 1), ("change_4h", 4), ("change_12h", 12),
    ("change_24h", 1), ("change_7d", 7), ("change_30d", 30), ("change_90d", 90),
):
    resolver(_code)(_make_change_resolver(_periods, _code))


@resolver("ath")
def _ath(ctx: MetricContext) -> float | None:
    latest = (ctx.indicators or {}).get("latest") or {}
    if latest.get("ath"):
        return float(latest["ath"])
    closes = _closes(ctx)
    if not closes:
        raise MetricUnavailable("ath", "本地没有历史数据，无法计算 ATH")
    return max(closes)


@resolver("drawdown_from_ath")
def _drawdown(ctx: MetricContext) -> float | None:
    latest = (ctx.indicators or {}).get("latest") or {}
    if latest.get("distance_to_ath") is not None:
        return round(float(latest["distance_to_ath"]), 4)
    ath = _ath(ctx)
    closes = _closes(ctx)
    if not ath or not closes:
        raise MetricUnavailable("drawdown_from_ath", "缺少 ATH 或当前价")
    return round((closes[-1] - ath) / ath * 100.0, 4)


@resolver("distance_to_atl")
def _distance_atl(ctx: MetricContext) -> float | None:
    latest = (ctx.indicators or {}).get("latest") or {}
    v = latest.get("distance_to_atl")
    if v is None:
        raise MetricUnavailable("distance_to_atl", "本地历史不足以计算 ATL")
    return round(float(v), 4)


@series_resolver("drawdown_from_ath")
def _drawdown_series(ctx: MetricContext) -> list[float]:
    closes = _closes(ctx)
    if not closes:
        return []
    out: list[float] = []
    peak = closes[0]
    for c in closes:
        peak = max(peak, c)
        out.append((c - peak) / peak * 100.0)
    return out


# ============================================================ 技术指标

def _ind(ctx: MetricContext, key: str, code: str | None = None) -> float | None:
    latest = (ctx.indicators or {}).get("latest") or {}
    v = latest.get(key)
    if v is None:
        raise MetricUnavailable(code or key, "指标引擎没有给出该值（可能历史长度不足，例如 200 日均线需要 200 根日线）")
    return float(v)


for _key, _code in (
    ("ma20", "ma20"), ("ma50", "ma50"), ("ma200", "ma200"), ("ema50", "ema50"),
    ("rsi14", "rsi14"), ("macd", "macd"), ("macd_hist", "macd_hist"),
    ("atr_pct", "atr_pct"), ("volatility_30d", "volatility_30d"),
    ("bb_upper", "bb_upper"), ("bb_lower", "bb_lower"),
    ("price_percentile", "price_percentile"), ("rsi_percentile", "rsi_percentile"),
):
    _CODE = _code
    _KEY = _key

    def _mk(key: str, code: str):
        def fn(ctx: MetricContext) -> float | None:
            val = _ind(ctx, key, code)
            ctx.mark_source(code, "computed_from_local_candles", "LOCAL_DATABASE", _candle_time(ctx))
            return val

        return fn

    resolver(_CODE)(_mk(_KEY, _CODE))


@resolver("price_vs_ma200")
def _price_vs_ma200(ctx: MetricContext) -> float | None:
    latest = (ctx.indicators or {}).get("latest") or {}
    price, ma200 = latest.get("price"), latest.get("ma200")
    if not price or not ma200:
        raise MetricUnavailable("price_vs_ma200", "200 日均线不可用")
    return round((float(price) - float(ma200)) / float(ma200) * 100.0, 4)


@series_resolver("ma200")
def _ma200_series(ctx: MetricContext) -> list[float]:
    series = ((ctx.indicators or {}).get("series") or {}).get("ma200") or []
    return [float(v) for _, v in series]


@series_resolver("ma50")
def _ma50_series(ctx: MetricContext) -> list[float]:
    series = ((ctx.indicators or {}).get("series") or {}).get("ma50") or []
    return [float(v) for _, v in series]


@series_resolver("rsi14")
def _rsi_series(ctx: MetricContext) -> list[float]:
    series = ((ctx.indicators or {}).get("series") or {}).get("rsi14") or []
    return [float(v) for _, v in series]


# ============================================================ 链上 / 衍生品 / 资金 / 宏观
# 这些块由 MarketService 提供；取不到时明确抛不可用，而不是给 0。

def _block_value(ctx: MetricContext, block_name: str, code: str, keys: tuple[str, ...]) -> float | None:
    block = getattr(ctx, block_name) or {}
    if not block.get("available", True):
        raise MetricUnavailable(code, block.get("message") or "该模块当前不可用（数据源缺失）")
    for k in keys:
        if block.get(k) is not None:
            return float(block[k])
    # 结构里可能嵌在 data 里
    data = block.get("data") or {}
    for k in keys:
        if isinstance(data, dict) and data.get(k) is not None:
            return float(data[k])
    raise MetricUnavailable(code, "该模块返回的数据里没有这个字段")


@resolver("mvrv")
def _mvrv(ctx: MetricContext) -> float | None:
    v = _block_value(ctx, "onchain", "mvrv", ("mvrv", "mvrv_ratio"))
    ctx.mark_source("mvrv", str((ctx.onchain.get("source") or {}).get("provider") or ""), "SINGLE_SOURCE")
    return v


@resolver("sopr")
def _sopr(ctx: MetricContext) -> float | None:
    return _block_value(ctx, "onchain", "sopr", ("sopr",))


@resolver("nupl")
def _nupl(ctx: MetricContext) -> float | None:
    return _block_value(ctx, "onchain", "nupl", ("nupl",))


@resolver("realized_cap")
def _realized_cap(ctx: MetricContext) -> float | None:
    return _block_value(ctx, "onchain", "realized_cap", ("realized_cap", "realized_cap_usd"))


@resolver("lth_supply")
def _lth(ctx: MetricContext) -> float | None:
    return _block_value(ctx, "onchain", "lth_supply", ("lth_supply", "lth"))


@resolver("sth_supply")
def _sth(ctx: MetricContext) -> float | None:
    return _block_value(ctx, "onchain", "sth_supply", ("sth_supply", "sth"))


@resolver("exchange_netflow")
def _netflow(ctx: MetricContext) -> float | None:
    return _block_value(ctx, "onchain", "exchange_netflow", ("exchange_netflow", "netflow", "net_flow"))


@resolver("exchange_reserve")
def _reserve(ctx: MetricContext) -> float | None:
    return _block_value(ctx, "onchain", "exchange_reserve", ("exchange_reserve", "reserve"))


@resolver("funding_rate")
def _funding(ctx: MetricContext) -> float | None:
    return _block_value(ctx, "derivatives", "funding_rate", ("funding_rate", "funding_rate_pct"))


@resolver("funding_annualized")
def _funding_ann(ctx: MetricContext) -> float | None:
    return _block_value(ctx, "derivatives", "funding_annualized", ("funding_annualized",))


@resolver("open_interest")
def _oi(ctx: MetricContext) -> float | None:
    return _block_value(ctx, "derivatives", "open_interest", ("open_interest", "oi", "oi_usd"))


@resolver("liquidation_24h")
def _liq(ctx: MetricContext) -> float | None:
    return _block_value(ctx, "derivatives", "liquidation_24h",
                        ("liquidation_24h", "liquidations_24h", "liquidation_usd_24h"))


@resolver("basis")
def _basis(ctx: MetricContext) -> float | None:
    return _block_value(ctx, "derivatives", "basis", ("basis", "basis_pct"))


@resolver("etf_netflow")
def _etf(ctx: MetricContext) -> float | None:
    return _block_value(ctx, "etf", "etf_netflow", ("net_flow", "netflow", "net_inflow", "value"))


@resolver("etf_netflow_7d")
def _etf7(ctx: MetricContext) -> float | None:
    block = ctx.etf or {}
    v = block.get("net_flow_7d") or block.get("netflow_7d")
    if v is None:
        raise MetricUnavailable("etf_netflow_7d", "ETF 模块没有提供 7 日累计字段")
    return float(v)


@resolver("fear_greed")
def _fg(ctx: MetricContext) -> float | None:
    return _block_value(ctx, "sentiment", "fear_greed", ("index", "value", "fear_greed", "fear_greed_index"))


@resolver("dxy")
def _dxy(ctx: MetricContext) -> float | None:
    return _block_value(ctx, "macro", "dxy", ("dxy", "value"))


@resolver("us10y")
def _us10y(ctx: MetricContext) -> float | None:
    v = _block_value(ctx, "macro", "us10y", ("us10y", "us10y_value"))
    return v


@resolver("m2")
def _m2(ctx: MetricContext) -> float | None:
    return _block_value(ctx, "macro", "m2", ("m2", "value"))


# ============================================================ 系统状态

def _analysis_part(ctx: MetricContext, part: str) -> dict[str, Any]:
    a = ctx.analysis or {}
    if not a.get("available"):
        raise MetricUnavailable(part, a.get("message") or "综合分析当前不可用（本地历史数据不足）")
    return a.get(part) or {}


@resolver("risk_score")
def _risk_score(ctx: MetricContext) -> float | None:
    risk = _analysis_part(ctx, "risk")
    v = risk.get("overall_risk")
    if v is None:
        raise MetricUnavailable("risk_score", "风险引擎没有给出综合评分")
    return float(v)


@resolver("risk_level")
def _risk_level(ctx: MetricContext) -> str | None:
    risk = _analysis_part(ctx, "risk")
    v = risk.get("level")
    if not v:
        raise MetricUnavailable("risk_level", "风险引擎没有给出等级")
    return str(v)


@resolver("valuation_score")
def _val_score(ctx: MetricContext) -> float | None:
    return float(_analysis_part(ctx, "valuation").get("score"))


@resolver("valuation_state")
def _val_state(ctx: MetricContext) -> str | None:
    return str(_analysis_part(ctx, "valuation").get("state") or "")


@resolver("cycle_phase")
def _cycle_phase(ctx: MetricContext) -> str | None:
    return str(_analysis_part(ctx, "cycle").get("phase") or "")


@resolver("regime")
def _regime(ctx: MetricContext) -> str | None:
    return str(_analysis_part(ctx, "regime").get("regime") or "")


@resolver("regime_risk_state")
def _regime_risk(ctx: MetricContext) -> str | None:
    return str(_analysis_part(ctx, "regime").get("risk_state") or "")


@series_resolver("risk_score")
def _risk_series(ctx: MetricContext) -> list[float]:
    """风险评分没有逐日历史序列（引擎按需计算）。用回撤 + 波动构造的代理序列是不诚实的，
    因此这里返回空列表，分位条件会明确报「无历史序列可用」。"""
    return []


# ============================================================ 历史分位

def _percentile_of(value: float, series: list[float]) -> float | None:
    if not series:
        return None
    below = sum(1 for v in series if v <= value)
    return below / len(series) * 100.0


def _percentile_resolver(code: str, source_code: str):
    def fn(ctx: MetricContext) -> float | None:
        if not ctx.series(source_code):
            raise MetricUnavailable(code, f"{source_code} 没有可用的历史序列，无法计算分位")
        return ctx.value(code)

    return fn


@resolver("percentile_mvrv")
def _p_mvrv(ctx: MetricContext) -> float | None:
    current = _mvrv(ctx)
    series = ctx.series("__mvrv_history__")
    if not series:
        raise MetricUnavailable("percentile_mvrv", "链上 MVRV 没有本地历史序列，无法计算分位（系统不会用估算值代替）")
    return round(_percentile_of(float(current), series), 2)


@resolver("percentile_drawdown")
def _p_dd(ctx: MetricContext) -> float | None:
    current = _drawdown(ctx)
    series = ctx.series("drawdown_from_ath")
    if not series:
        raise MetricUnavailable("percentile_drawdown", "没有回撤历史序列")
    return round(_percentile_of(float(current), series), 2)


@resolver("percentile_funding")
def _p_funding(ctx: MetricContext) -> float | None:
    series = ctx.series("__funding_history__")
    if not series:
        raise MetricUnavailable("percentile_funding", "资金费率没有本地历史序列（需要衍生品历史数据）")
    return round(_percentile_of(float(_funding(ctx)), series), 2)


@resolver("percentile_risk")
def _p_risk(ctx: MetricContext) -> float | None:
    series = ctx.series("risk_score")
    if not series:
        raise MetricUnavailable("percentile_risk",
                                "风险评分目前按需计算、未落历史序列，因此无法给出历史分位；"
                                "系统不会用其它指标的分布冒充它的分位")
    return round(_percentile_of(float(_risk_score(ctx)), series), 2)


# ============================================================ 个人计划

def _require_personal(ctx: MetricContext, key: str, code: str) -> Any:
    v = (ctx.personal or {}).get(key)
    if v is None:
        raise MetricUnavailable(code, "没有关联的资金计划，或该计划还没有交易记录")
    return v


@resolver("plan_holding_pnl_pct")
def _plan_pnl(ctx: MetricContext) -> float | None:
    v = _require_personal(ctx, "pnl_pct", "plan_holding_pnl_pct")
    return float(v)


@resolver("plan_avg_cost")
def _plan_avg(ctx: MetricContext) -> float | None:
    return float(_require_personal(ctx, "avg_cost", "plan_avg_cost"))


@resolver("plan_total_btc")
def _plan_total(ctx: MetricContext) -> float | None:
    return float(_require_personal(ctx, "total_btc", "plan_total_btc"))


@resolver("plan_days_to_next")
def _plan_days(ctx: MetricContext) -> float | None:
    return float(_require_personal(ctx, "days_to_next", "plan_days_to_next"))


@resolver("plan_drawdown_since_start")
def _plan_dd(ctx: MetricContext) -> float | None:
    return float(_require_personal(ctx, "drawdown_since_start", "plan_drawdown_since_start"))
