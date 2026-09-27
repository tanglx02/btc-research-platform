# -*- coding: utf-8 -*-
"""可监测指标目录（Metric Catalog）。

这是「通用条件规则引擎」能够真正做到通用的关键所在：

    新增一种可监测数据 = 在 CATALOG 里登记一条 + 提供一个取值函数。
    规则引擎、条件求值、通知模板、前端下拉框全部自动获得该指标，
    不需要改动 alert_engine / notifier / 任何一张表。

设计要点：
1. **绝不重复造数据**。每个 metric 的取值函数都只从已有的数据层取数：
   本地数据库（candles / onchain_metrics / derivatives / etf_flows / sentiment / macro_series）、
   已有的指标引擎（engines.indicators）、已有的分析引擎（valuation / cycle / risk / regime）。
   这里不新建任何一张数据表，也不直接请求 Provider。
2. **取不到就是取不到**。函数返回 None 时，条件求值会判为「不可用」，
   规则不会触发，并且在事件里明确写明原因，绝不用 0 或默认值冒充真实数据。
3. **单位必须写清楚**。每个指标带 unit 与 direction（越大越危险 / 越小越危险），
   前端据此给出通俗解释，避免用户把 0.05% 的资金费率当成 5% 填进去。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Literal

# 指标分组：决定前端下拉框里的分类
Group = Literal["price", "technical", "onchain", "derivatives", "fund", "macro", "state", "personal"]


@dataclass(slots=True)
class MetricSpec:
    """一个可监测指标的定义。"""

    code: str                                   # 内部编码，条件里存的就是它
    name_cn: str                                # 用户在界面里看到的名字
    group: Group
    unit: str = ""                              # 单位，如 "USD" / "%" / "倍"
    description: str = ""                       # 「这个条件代表什么？」
    direction: str = "neutral"                  # higher_worse / lower_worse / neutral
    supports_change: bool = True                 # 是否支持变化率条件（1h/24h/7d...）
    extra: dict[str, Any] = field(default_factory=dict)


class MetricUnavailable(Exception):
    """指标当前不可用（数据缺失 / 引擎拒绝计算）。"""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code} 不可用：{reason}")
        self.code = code
        self.reason = reason


# ------------------------------------------------------------------ 注册表

CATALOG: dict[str, MetricSpec] = {}
# code -> 取值函数；签名 (ctx) -> float | str | None
_RESOLVERS: dict[str, Callable[["MetricContext"], Any]] = {}


def register(spec: MetricSpec) -> MetricSpec:
    """登记一个指标定义（MetricSpec）。"""
    CATALOG[spec.code] = spec
    return spec


def resolver(code: str) -> Callable[["MetricContext"], Any]:
    """登记某个指标的取值函数（装饰器）。"""

    def deco(fn: Callable[["MetricContext"], Any]) -> Callable[["MetricContext"], Any]:
        _RESOLVERS[code] = fn
        return fn

    return deco


def metric(code: str) -> Callable[["MetricContext"], Any]:
    """``resolver`` 的别名，语义上更贴近「这个函数的返回值属于 code 指标」。

    保留两种写法都能用，避免调用方记错名字。
    """
    return resolver(code)


def get_spec(code: str) -> MetricSpec | None:
    return CATALOG.get(code)


def has_metric(code: str) -> bool:
    return code in CATALOG


def all_specs() -> list[MetricSpec]:
    return sorted(CATALOG.values(), key=lambda s: (s.group, s.code))


def spec_to_dict(spec: MetricSpec) -> dict[str, Any]:
    return {
        "code": spec.code,
        "name_cn": spec.name_cn,
        "group": spec.group,
        "unit": spec.unit,
        "description": spec.description,
        "direction": spec.direction,
        "supports_change": spec.supports_change,
        **spec.extra,
    }


# ------------------------------------------------------------------ 求值上下文

class MetricContext:
    """一次求值过程中共享的上下文。

    **性能关键**：同一个上下文对象在整条规则（以及同一轮所有规则）内复用，
    所有原始数据只抓一次、指标只算一次。
    否则 500 条规则会变成 500 次 Provider 请求 —— 这正是需求里明确禁止的做法。
    """

    def __init__(
        self,
        *,
        candles: list[dict[str, Any]] | None = None,
        intraday: list[dict[str, Any]] | None = None,
        price_block: dict[str, Any] | None = None,
        indicators: dict[str, Any] | None = None,
        analysis: dict[str, Any] | None = None,
        onchain: dict[str, Any] | None = None,
        derivatives: dict[str, Any] | None = None,
        etf: dict[str, Any] | None = None,
        sentiment: dict[str, Any] | None = None,
        macro: dict[str, Any] | None = None,
        personal: dict[str, Any] | None = None,
    ) -> None:
        self.candles = candles or []
        self.intraday = intraday or []
        self.price_block = price_block or {}
        self.indicators = indicators or {}
        self.analysis = analysis or {}
        self.onchain = onchain or {}
        self.derivatives = derivatives or {}
        self.etf = etf or {}
        self.sentiment = sentiment or {}
        self.macro = macro or {}
        self.personal = personal or {}
        # 指标名 -> 数据来源 Provider（用于事件里如实记录「谁提供的数据」）
        self.metric_sources: dict[str, str] = {}
        # 指标名 -> 该数据的数据质量
        self.metric_quality: dict[str, str] = {}
        # 指标名 -> 数据时间
        self.metric_updated_at: dict[str, datetime] = {}
        # 缓存各指标解析结果，避免同一指标被多条规则反复解析
        self._cache: dict[str, Any] = {}

    # -------------------------------------------------- 取值
    def value(self, code: str) -> Any:
        """取一个指标的当前值。不可用时抛 MetricUnavailable（由上层转成「未触发 + 原因」）。"""
        if code in self._cache:
            cached = self._cache[code]
            if isinstance(cached, MetricUnavailable):
                raise cached
            return cached
        fn = _RESOLVERS.get(code)
        if fn is None:
            exc = MetricUnavailable(code, "该指标尚未注册取值实现")
            self._cache[code] = exc
            raise exc
        try:
            val = fn(self)
        except MetricUnavailable as exc:
            self._cache[code] = exc
            raise
        except Exception as exc:  # noqa: BLE001 - 单个指标异常不应击穿整轮求值
            wrapped = MetricUnavailable(code, f"取值异常：{exc}")
            self._cache[code] = wrapped
            raise wrapped
        if val is None:
            exc = MetricUnavailable(code, "当前没有可用数据（取不到就是取不到，不做估算）")
            self._cache[code] = exc
            raise exc
        self._cache[code] = val
        return val

    # -------------------------------------------------- 历史序列（供分位 / 变化率 / 交叉使用）
    def series(self, code: str) -> list[float]:
        """取该指标的历史序列（升序）。没有历史的指标返回空列表。"""
        key = f"__series__{code}"
        if key in self._cache:
            return self._cache[key]
        out = _SERIES_RESOLVERS.get(code, lambda _ctx: [])(self)
        self._cache[key] = out
        return out

    def mark_source(self, code: str, provider: str, quality: str = "",
                    updated_at: datetime | None = None) -> None:
        if provider:
            self.metric_sources.setdefault(code, provider)
        if quality:
            self.metric_quality.setdefault(code, quality)
        if updated_at:
            self.metric_updated_at.setdefault(code, updated_at)


# code -> 历史序列函数
_SERIES_RESOLVERS: dict[str, Callable[[MetricContext], list[float]]] = {}


def series_resolver(code: str) -> Callable[[Callable[[MetricContext], list[float]]], Callable[[MetricContext], list[float]]]:
    def deco(fn: Callable[[MetricContext], list[float]]) -> Callable[[MetricContext], list[float]]:
        _SERIES_RESOLVERS[code] = fn
        return fn

    return deco


# ------------------------------------------------------------------ 内部小工具

def _latest(block: dict[str, Any]) -> dict[str, Any]:
    return (block or {}).get("latest") or {}


def _indicators_latest(ctx: MetricContext) -> dict[str, Any]:
    return _latest(ctx.indicators)


def _drawdown_pct(ctx: MetricContext) -> float | None:
    """相对 ATH 的回撤（负数，如 -31.2 表示回撤 31.2%）。"""
    latest = _indicators_latest(ctx)
    ath = latest.get("ath")
    price = latest.get("price")
    if not ath or not price:
        return None
    return (price - ath) / ath * 100.0


def _as_float(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        f = float(v)
        return f if f == f else None  # 过滤 NaN
    except (TypeError, ValueError):
        return None


def _series_from_candles(ctx: MetricContext, fn: Callable[[list[float]], list[float | None]]) -> list[float]:
    """把某个指标的计算结果转成「有限值」的历史序列。"""
    closes = [float(c["close"]) for c in ctx.candles if c.get("close") is not None]
    if not closes:
        return []
    out = fn(closes)
    return [float(v) for v in out if v is not None and v == v]


# ============================================================ 价格与行情

register(MetricSpec("price", "BTC 当前价格", "price", "USD",
                    "比特币最新成交价（USD）。跌破/突破某个价位时提醒你。",
                    "neutral", extra={"aliases": ["BTC 价格", "现价"]}))

register(MetricSpec("close", "BTC 收盘价（日线）", "price", "USD",
                    "最近一根已完结日线的收盘价。与实时价的区别是不会被盘中波动干扰。",
                    "neutral"))

register(MetricSpec("change_1h", "1 小时涨跌幅", "price", "%",
                    "过去 1 小时的价格变化幅度。适合捕捉急涨急跌。",
                    "neutral", supports_change=False))

register(MetricSpec("change_4h", "4 小时涨跌幅", "price", "%",
                    "过去 4 小时的价格变化幅度。", "neutral", supports_change=False))

register(MetricSpec("change_12h", "12 小时涨跌幅", "price", "%",
                    "过去 12 小时的价格变化幅度。", "neutral", supports_change=False))

register(MetricSpec("change_24h", "24 小时涨跌幅", "price", "%",
                    "过去 24 小时的价格变化幅度，即常说的「日涨跌幅」。",
                    "neutral", supports_change=False,
                    extra={"aliases": ["24小时跌幅", "24H 变化"]}))

register(MetricSpec("change_7d", "7 天涨跌幅", "price", "%",
                    "过去 7 天的价格变化幅度。", "neutral", supports_change=False))

register(MetricSpec("change_30d", "30 天涨跌幅", "price", "%",
                    "过去 30 天的价格变化幅度。", "neutral", supports_change=False))

register(MetricSpec("change_90d", "90 天涨跌幅", "price", "%",
                    "过去 90 天的价格变化幅度。", "neutral", supports_change=False))

register(MetricSpec("ath", "历史最高价 ATH", "price", "USD",
                    "有史以来的最高收盘价。许多监测条件以它为参照。", "neutral", supports_change=False))

register(MetricSpec("drawdown_from_ath", "距 ATH 回撤", "price", "%",
                    "当前价格相对历史最高价下跌的百分比（负数）。例如 -30 表示已从最高点回撤 30%。",
                    "lower_worse", supports_change=False,
                    extra={"aliases": ["回撤", "距ATH回撤"]}))

register(MetricSpec("distance_to_atl", "距历史最低价涨幅", "price", "%",
                    "当前价格相对历史最低价上涨的百分比。", "neutral", supports_change=False))

register(MetricSpec("volume", "成交量", "price", "BTC",
                    "最近一根 K 线的成交量。", "neutral"))

# ============================================================ 技术指标

register(MetricSpec("rsi14", "RSI(14) 相对强弱", "technical", "",
                    "衡量近期涨跌力度的指标，0~100。低于 30 通常被视为超卖，高于 70 被视为超买。",
                    "lower_worse", extra={"aliases": ["RSI"]}))

register(MetricSpec("rsi_percentile", "RSI 历史分位", "technical", "%",
                    "当前 RSI 在历史所有取值中的位置。15% 表示比历史上 85% 的时间都低。",
                    "lower_worse", supports_change=False))

register(MetricSpec("ma20", "20 日均线", "technical", "USD",
                    "最近 20 个交易日的平均价格，代表短期成本。", "neutral"))

register(MetricSpec("ma50", "50 日均线", "technical", "USD",
                    "最近 50 个交易日的平均价格，代表中期成本。", "neutral"))

register(MetricSpec("ma200", "200 日均线", "technical", "USD",
                    "最近 200 个交易日的平均价格，是判断牛熊分界最常用的长期均线。", "neutral"))

register(MetricSpec("ema50", "50 日指数均线", "technical", "USD",
                    "对近期价格赋予更高权重的 50 日均线。", "neutral"))

register(MetricSpec("price_vs_ma200", "价格偏离 200 日均线", "technical", "%",
                    "当前价相对 200 日均线的偏离百分比。正值说明在长期均线之上。",
                    "neutral", supports_change=False))

register(MetricSpec("macd", "MACD", "technical", "",
                    "快慢均线之差，反映趋势动能。", "neutral"))

register(MetricSpec("macd_hist", "MACD 柱", "technical", "",
                    "MACD 与其信号线的差。由负转正常被视为动能转强。", "neutral"))

register(MetricSpec("atr_pct", "ATR 波动率", "technical", "%",
                    "平均真实波动幅度占价格的比例，衡量当前波动剧烈程度。", "higher_worse"))

register(MetricSpec("volatility_30d", "30 日年化波动率", "technical", "%",
                    "按最近 30 天收益算出的年化波动率。数值越高，市场越不稳定。", "higher_worse"))

register(MetricSpec("bb_upper", "布林带上轨", "technical", "USD",
                    "价格波动区间的上界，突破常代表短期过热。", "neutral"))

register(MetricSpec("bb_lower", "布林带下轨", "technical", "USD",
                    "价格波动区间的下界，跌破常代表短期超卖。", "neutral"))

register(MetricSpec("price_percentile", "价格历史分位", "technical", "%",
                    "当前价格在历史所有价格中的位置。越高说明相对历史越贵。",
                    "higher_worse", supports_change=False))

# ============================================================ 链上

register(MetricSpec("mvrv", "MVRV 市场价值/实现价值", "onchain", "倍",
                    "市值与实现市值之比。高于 3 通常是历史高位区，低于 1 通常是历史低位区。",
                    "higher_worse", extra={"aliases": ["MVRV"]}))

register(MetricSpec("sopr", "SOPR 支出产出利润率", "onchain", "",
                    "链上卖出时的盈亏比。略高于 1 表示整体在盈利卖出，跌破 1 常见于恐慌割肉。",
                    "neutral"))

register(MetricSpec("nupl", "NUPL 净未实现盈亏", "onchain", "",
                    "全市场账面盈亏占市值的比例。接近 0.75 常对应极度乐观。",
                    "higher_worse"))

register(MetricSpec("realized_cap", "实现市值", "onchain", "USD",
                    "按链上最后一次移动时的价格计算的市值，常被视为「链上成本」。", "neutral"))

register(MetricSpec("lth_supply", "长期持有者持仓", "onchain", "BTC",
                    "长期持有者（通常 >155 天）持有的 BTC 数量。", "neutral"))

register(MetricSpec("sth_supply", "短期持有者持仓", "onchain", "BTC",
                    "短期持有者持有的 BTC 数量。", "neutral"))

register(MetricSpec("exchange_netflow", "交易所净流入", "onchain", "BTC",
                    "流入交易所减去流出交易所的净量。正值通常被解读为潜在抛压上升。",
                    "higher_worse"))

register(MetricSpec("exchange_reserve", "交易所余额", "onchain", "BTC",
                    "交易所地址中留存的 BTC 总量。持续下降常被解读为筹码离场。", "neutral"))

# ============================================================ 衍生品

register(MetricSpec("funding_rate", "资金费率", "derivatives", "%",
                    "永续合约多空双方互相支付的费率。正值越高说明多头越拥挤，杠杆风险越大。",
                    "higher_worse", extra={"aliases": ["Funding", "Funding Rate"]}))

register(MetricSpec("funding_annualized", "年化资金费率", "derivatives", "%",
                    "把当期资金费率折算成年化后的数值，便于跨周期比较。", "higher_worse"))

register(MetricSpec("open_interest", "未平仓合约量 OI", "derivatives", "USD",
                    "市场上尚未平仓的合约总规模，反映杠杆水平。", "higher_worse"))

register(MetricSpec("liquidation_24h", "24 小时爆仓金额", "derivatives", "USD",
                    "过去 24 小时被强制平仓的合约金额。", "higher_worse"))

register(MetricSpec("basis", "基差 Basis", "derivatives", "%",
                    "期货价格与现货价格的差额比例。", "neutral"))

# ============================================================ 资金流 / ETF

register(MetricSpec("etf_netflow", "ETF 单日净流入", "fund", "USD",
                    "美国现货 ETF 当日资金净流入（负值表示净流出）。", "neutral",
                    extra={"aliases": ["ETF Flow"]}))

register(MetricSpec("etf_netflow_7d", "ETF 7 日累计净流入", "fund", "USD",
                    "最近 7 个交易日 ETF 资金净流入合计。", "neutral"))

# ============================================================ 情绪 / 宏观

register(MetricSpec("fear_greed", "恐惧贪婪指数", "macro", "",
                    "0~100 的市场情绪指标。越低越恐慌，越高越贪婪。", "higher_worse",
                    extra={"aliases": ["Fear & Greed", "恐慌贪婪"]}))

register(MetricSpec("dxy", "美元指数 DXY", "macro", "",
                    "美元相对一篮子货币的强弱。走强通常压制风险资产。", "higher_worse"))

register(MetricSpec("us10y", "美国 10 年期国债收益率", "macro", "%",
                    "全球资产定价的锚，快速上行通常压制风险资产估值。", "higher_worse"))

register(MetricSpec("m2", "M2 货币供应量", "macro", "USD",
                    "广义货币供应量，反映市场流动性环境。", "neutral"))

# ============================================================ 系统状态（已有引擎的计算结果）

register(MetricSpec("risk_score", "综合风险评分", "state", "",
                    "系统按多维度算出的风险分（0~100），越高说明当前环境越脆弱。",
                    "higher_worse", supports_change=False,
                    extra={"aliases": ["Risk Score", "风险评分"]}))

register(MetricSpec("risk_level", "风险等级", "state", "",
                    "风险等级的离散取值：low / moderate / elevated / high / extreme。",
                    "neutral", supports_change=False, extra={"stateful": True}))

register(MetricSpec("valuation_score", "估值评分", "state", "",
                    "系统按回撤、均线偏离、MVRV 等算出的估值分（0~100），越高越贵。",
                    "higher_worse", supports_change=False))

register(MetricSpec("valuation_state", "估值状态", "state", "",
                    "估值状态的离散取值（如 低估 / 合理 / 偏高 / 极端高估）。",
                    "neutral", supports_change=False, extra={"stateful": True}))

register(MetricSpec("cycle_phase", "市场周期阶段", "state", "",
                    "系统判定的周期阶段（如 熊市 / 底部构筑 / 上涨 / 高位风险 / 下跌）。",
                    "neutral", supports_change=False, extra={"stateful": True}))

register(MetricSpec("regime", "综合市场状态", "state", "",
                    "把趋势、估值、资金流、链上、衍生品、宏观等合成的整体状态。",
                    "neutral", supports_change=False, extra={"stateful": True}))

register(MetricSpec("regime_risk_state", "综合状态·风险分项", "state", "",
                    "综合状态里的风险分项，独立于 risk_score 的原始评分。",
                    "neutral", supports_change=False, extra={"stateful": True}))

register(MetricSpec("percentile_mvrv", "MVRV 历史分位", "onchain", "%",
                    "当前 MVRV 在历史序列中的位置。>90% 说明比历史上 90% 的时间都贵。",
                    "higher_worse", supports_change=False))

register(MetricSpec("percentile_funding", "资金费率历史分位", "derivatives", "%",
                    "当前资金费率在历史中的位置，用于识别极端拥挤。",
                    "higher_worse", supports_change=False))

register(MetricSpec("percentile_drawdown", "回撤历史分位", "state", "%",
                    "当前回撤在历史回撤序列中的位置（分位越高说明这次跌得越狠）。",
                    "higher_worse", supports_change=False))

register(MetricSpec("percentile_risk", "风险评分历史分位", "state", "%",
                    "当前风险评分在历史中的位置。", "higher_worse", supports_change=False))

# ============================================================ 个人资金计划

register(MetricSpec("plan_holding_pnl_pct", "我的持仓盈亏比例", "personal", "%",
                    "基于你自己录入的交易记录算出的浮动盈亏比例。", "lower_worse",
                    supports_change=False))

register(MetricSpec("plan_avg_cost", "我的持仓平均成本", "personal", "USD",
                    "你当前持仓的平均买入成本价。", "neutral", supports_change=False))

register(MetricSpec("plan_total_btc", "我的持仓数量", "personal", "BTC",
                    "账本里算出的当前 BTC 持仓量。", "neutral", supports_change=False))

register(MetricSpec("plan_days_to_next", "距下次定投天数", "personal", "天",
                    "按你的定投频率推算，距离下一次计划投入还有多少天。到点提醒你。",
                    "neutral", supports_change=False))

register(MetricSpec("plan_drawdown_since_start", "计划启动以来回撤", "personal", "%",
                    "从计划起始日至今的最大回撤幅度。", "lower_worse", supports_change=False))
