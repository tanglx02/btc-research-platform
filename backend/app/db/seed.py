# -*- coding: utf-8 -*-
"""初始化种子数据：资产、指标字典、信号定义、市场事件时间线。

指标字典是「普通人也能看懂」的基础：每个指标都必须有通俗含义、使用方法、
看多/看空含义，以及是否需要付费数据源。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from ..core.logging import get_logger
from ..db.models import Asset, IndicatorDefinition, Signal, TimelineEvent
from .base import get_session_factory

logger = get_logger(__name__)

# ---------------------------------------------------------------- 资产
ASSETS = [
    {"symbol": "BTC", "name": "Bitcoin", "category": "crypto", "quote_currency": "USD",
     "halving_dates": ["2012-11-28", "2016-07-09", "2020-05-11", "2024-04-20"],
     "notes": "首个加密货币，固定 2100 万枚上限，约每四年减半一次。"},
]

# ---------------------------------------------------------------- 指标字典
INDICATORS: list[dict[str, object]] = [
    # -------- 价格与趋势 --------
    {"code": "price", "name_cn": "BTC 价格", "name_en": "Price", "category": "market",
     "formula_readable": "最后一次成交价格", "meaning_cn": "当前一枚比特币值多少钱。所有分析的起点。",
     "how_to_use": "单独看价格没有意义，需要结合趋势、估值和周期一起判断。",
     "unit": "USD", "requires_paid_provider": False},
    {"code": "change_24h", "name_cn": "24 小时涨跌", "name_en": "24h Change", "category": "market",
     "formula_readable": "(当前价 - 24小时前价格) / 24小时前价格", "meaning_cn": "过去一天涨了多少或跌了多少。",
     "how_to_use": "短期波动参考；单日波动在加密市场属于正常现象。", "unit": "%", "requires_paid_provider": False},
    {"code": "ma200", "name_cn": "200 日均价", "name_en": "MA200", "category": "trend",
     "formula": "MA(n) = mean(close[-n:])", "formula_readable": "过去 200 天收盘价的平均值",
     "meaning_cn": "衡量最近半年平均成本。价格在它上方通常代表中期趋势偏强。",
     "how_to_use": "价格上穿常被认为趋势转强，下穿常被认为趋势转弱；但不是买卖指令。",
     "unit": "USD", "requires_paid_provider": False},
    {"code": "ema50", "name_cn": "50 日指数均线", "name_en": "EMA50", "category": "trend",
     "formula": "EMA_t = a*close_t + (1-a)*EMA_{t-1}", "formula_readable": "更重视近期价格的平均值",
     "meaning_cn": "比普通均线更灵敏地反映中期趋势。", "how_to_use": "与 200 日均线配合看多头排列/空头排列。",
     "unit": "USD", "requires_paid_provider": False},
    {"code": "rsi14", "name_cn": "RSI 相对强弱", "name_en": "RSI(14)", "category": "momentum",
     "formula": "RSI = 100 - 100/(1+RS)", "formula_readable": "一段时间内涨幅占总波动的比例",
     "meaning_cn": "0~100 的量表。高于 70 通常表示短期偏热，低于 30 表示偏冷。",
     "how_to_use": "极端值只说明短期情绪过热或过冷，不代表立刻反转。", "unit": "0-100",
     "value_range": "0~100", "requires_paid_provider": False},
    {"code": "macd", "name_cn": "MACD", "name_en": "MACD", "category": "momentum",
     "formula": "MACD = EMA12 - EMA26", "formula_readable": "快慢均线的差值",
     "meaning_cn": "判断趋势强度与转折。柱状由负转正常被视为动能改善。",
     "how_to_use": "震荡行情中容易频繁假信号，需结合趋势指标。", "requires_paid_provider": False},
    {"code": "atr14", "name_cn": "ATR 真实波幅", "name_en": "ATR(14)", "category": "volatility",
     "formula": "ATR = EMA(TR, 14)", "formula_readable": "近期日波动幅度的平均值",
     "meaning_cn": "一天通常能波动多少钱。用来判断风险和设置止损参考。",
     "how_to_use": "ATR 变大说明波动加剧，风险上升。", "unit": "USD", "requires_paid_provider": False},
    {"code": "volatility_30d", "name_cn": "30 日年化波动率", "name_en": "30d Volatility", "category": "volatility",
     "formula_readable": "过去 30 天日收益率标准差 × √365", "meaning_cn": "衡量价格波动剧烈程度。",
     "how_to_use": "高波动意味着同样的仓位承担更大风险。", "unit": "%", "requires_paid_provider": False},
    {"code": "drawdown", "name_cn": "回撤", "name_en": "Drawdown", "category": "risk",
     "formula": "DD = (current - peak) / peak", "formula_readable": "距离历史最高价跌了多少",
     "meaning_cn": "从最高点回落的幅度，用来理解当前处于什么样的跌幅环境。",
     "how_to_use": "回撤越深，通常意味着恐慌程度越高，但也可能是基本面恶化，需要区分。",
     "unit": "%", "requires_paid_provider": False},
    {"code": "distance_to_ath", "name_cn": "距历史新高", "name_en": "Distance to ATH", "category": "market",
     "formula_readable": "(当前价 - 历史最高价) / 历史最高价", "meaning_cn": "距离历史最高点还有多远。",
     "how_to_use": "定投加仓策略常用此指标设定加仓档位。", "unit": "%", "requires_paid_provider": False},
    # -------- 估值/链上 --------
    {"code": "mvrv", "name_cn": "MVRV 估值比", "name_en": "MVRV", "category": "onchain",
     "formula": "MVRV = MarketCap / RealizedCap",
     "formula_readable": "市值 ÷ 按链上成本价计算的价值",
     "meaning_cn": "衡量全体持币者平均有多少浮盈。数值越高说明盈利盘越厚，获利了结压力越大。",
     "how_to_use": ">3 通常代表估值偏高；<1 通常代表市场整体处于亏损，历史上多为底部区域附近。",
     "unit": "ratio", "requires_paid_provider": True},
    {"code": "realized_cap", "name_cn": "已实现市值", "name_en": "Realized Cap", "category": "onchain",
     "formula_readable": "按每枚币最后一次移动时的价格加权计算的总价值",
     "meaning_cn": "近似表示市场里真实投入的资金规模。", "unit": "USD", "requires_paid_provider": True},
    {"code": "sopr", "name_cn": "SOPR 花费产出利润率", "name_en": "SOPR", "category": "onchain",
     "formula_readable": "卖出所得价格 ÷ 买入成本价格（链上加权）",
     "meaning_cn": ">1 表示整体在盈利卖出，<1 表示在亏损卖出。",
     "how_to_use": "长期跌破 1 通常代表投降式抛售；在牛市 <1 的位置常形成支撑。",
     "requires_paid_provider": True},
    {"code": "nupl", "name_cn": "NUPL 未实现盈亏", "name_en": "NUPL", "category": "onchain",
     "formula_readable": "(市值 - 已实现市值) / 市值", "meaning_cn": "市场整体浮盈比例，接近情绪温度计。",
     "unit": "-1~1", "requires_paid_provider": True},
    {"code": "puell_multiple", "name_cn": "Puell 倍数", "name_en": "Puell Multiple", "category": "onchain",
     "formula_readable": "矿工日收入 ÷ 一年平均日收入", "meaning_cn": "衡量矿工收入是否异常高，间接反映周期热度。",
     "requires_paid_provider": True},
    {"code": "active_addresses", "name_cn": "活跃地址数", "name_en": "Active Addresses", "category": "onchain",
     "meaning_cn": "一天内有多少地址发生交易，近似反映链上使用热度。",
     "how_to_use": "持续上升说明网络使用增加；突然飙升常伴随投机。", "requires_paid_provider": True},
    {"code": "exchange_netflow", "name_cn": "交易所净流量", "name_en": "Exchange Netflow", "category": "flow",
     "formula_readable": "流入交易所 - 流出交易所",
     "meaning_cn": "净流入增加通常意味着更多币被送去交易所准备卖出；净流出通常意味着转入自托管长期持有。",
     "requires_paid_provider": True},
    {"code": "fee_rate", "name_cn": "链上手续费", "name_en": "Mempool Fee", "category": "onchain",
     "meaning_cn": "当前链上转账所需手续费率，反映区块空间拥堵程度。",
     "how_to_use": "费用高说明链上需求旺盛（或短期拥堵），是真实可测的网络活动指标。",
     "unit": "sat/vB", "requires_paid_provider": False},
    {"code": "hashrate", "name_cn": "全网算力", "name_en": "Hashrate", "category": "onchain",
     "meaning_cn": "保护网络安全所需的计算能力，代表矿工投入。",
     "how_to_use": "算力长期增长通常代表矿工对长期有信心。", "requires_paid_provider": False},
    # -------- 衍生品 --------
    {"code": "funding_rate", "name_cn": "资金费率", "name_en": "Funding Rate", "category": "derivatives",
     "formula_readable": "永续合约多空双方定期支付的费用率",
     "meaning_cn": "正值表示多头支付空头（多头过热）；负值表示空头支付多头。",
     "how_to_use": "极端正值代表杠杆做多拥挤，回调风险上升。", "unit": "%/8h", "requires_paid_provider": False},
    {"code": "open_interest", "name_cn": "未平仓合约量", "name_en": "Open Interest", "category": "derivatives",
     "meaning_cn": "市场上未平仓的合约总量，代表杠杆资金规模。",
     "how_to_use": "OI 快速上升 + 价格快速上涨，通常代表杠杆推动，脆弱性更高。",
     "unit": "BTC", "requires_paid_provider": False},
    {"code": "basis", "name_cn": "期现基差", "name_en": "Basis", "category": "derivatives",
     "formula_readable": "(合约价 - 现货价) / 现货价", "meaning_cn": "合约相对现货的溢价程度。",
     "unit": "bps", "requires_paid_provider": False},
    # -------- ETF / 资金流 --------
    {"code": "etf_net_flow", "name_cn": "ETF 净流入", "name_en": "ETF Net Flow", "category": "etf",
     "meaning_cn": "美国现货 ETF 一天内资金净流入/流出规模，代表传统渠道资金的进出。",
     "how_to_use": "持续净流入通常代表机构资金介入；转为净流出代表需求减弱。",
     "unit": "USD", "requires_paid_provider": True},
    # -------- 宏观 --------
    {"code": "dxy", "name_cn": "美元指数", "name_en": "DXY", "category": "macro",
     "meaning_cn": "美元相对一篮子货币的强弱。美元走强通常压制风险资产。",
     "how_to_use": "观察趋势而非单日数值。", "requires_paid_provider": False},
    {"code": "us10y", "name_cn": "美国 10 年期国债收益率", "name_en": "US 10Y Yield", "category": "macro",
     "meaning_cn": "全球资金的无风险参考利率。收益率走高通常压缩高估值资产。",
     "unit": "%", "requires_paid_provider": False},
    {"code": "fed_funds", "name_cn": "联邦基金利率", "name_en": "Fed Funds Rate", "category": "macro",
     "meaning_cn": "美联储政策利率，决定全球美元资金成本。", "unit": "%", "requires_paid_provider": True},
    {"code": "cpi", "name_cn": "CPI 通胀", "name_en": "CPI", "category": "macro",
     "meaning_cn": "居民消费价格涨幅。通胀高企时货币政策通常偏紧。", "requires_paid_provider": True},
    {"code": "m2", "name_cn": "M2 货币供应量", "name_en": "M2", "category": "macro",
     "meaning_cn": "经济中的广义货币总量，近似代表流动性水位。", "requires_paid_provider": True},
    # -------- 情绪 --------
    {"code": "fear_greed", "name_cn": "恐慌贪婪指数", "name_en": "Fear & Greed Index", "category": "sentiment",
     "meaning_cn": "0 表示极度恐慌，100 表示极度贪婪。综合波动率、动量、社交媒体等多维度。",
     "how_to_use": "极端贪婪时风险偏好高，极端恐慌时常出现在下跌后期。",
     "unit": "0-100", "requires_paid_provider": False},
    # -------- 综合 --------
    {"code": "valuation_score", "name_cn": "估值评分", "name_en": "Valuation Score", "category": "valuation",
     "meaning_cn": "综合 MVRV、价格百分位、回撤等得出的估值状态描述。",
     "how_to_use": "估值高不代表马上跌，估值低也不代表马上涨，它描述的是概率环境。",
     "requires_paid_provider": False},
    {"code": "risk_score", "name_cn": "综合风险评分", "name_en": "Overall Risk", "category": "risk",
     "meaning_cn": "综合估值、波动、杠杆、流动性、宏观、链上六个维度的风险打分。",
     "unit": "0-100", "requires_paid_provider": False},
]

# ---------------------------------------------------------------- 信号
SIGNALS: list[dict[str, str]] = [
    {"code": "rsi_overbought", "name_cn": "RSI 进入超买区", "category": "momentum", "direction": "bearish",
     "description": "RSI(14) > 70，短期情绪偏热。"},
    {"code": "rsi_oversold", "name_cn": "RSI 进入超卖区", "category": "momentum", "direction": "bullish",
     "description": "RSI(14) < 30，短期情绪偏冷。"},
    {"code": "above_ma200", "name_cn": "价格站上 200 日均线", "category": "trend", "direction": "bullish",
     "description": "中期趋势偏强。"},
    {"code": "below_ma200", "name_cn": "价格跌破 200 日均线", "category": "trend", "direction": "bearish",
     "description": "中期趋势偏弱。"},
    {"code": "funding_extreme", "name_cn": "资金费率极端", "category": "derivatives", "direction": "bearish",
     "description": "资金费率年化超过 50%，杠杆多头拥挤。"},
    {"code": "deep_drawdown", "name_cn": "深度回撤", "category": "risk", "direction": "neutral",
     "description": "距历史高点回撤超过 40%。"},
    {"code": "fng_extreme_fear", "name_cn": "极度恐慌", "category": "sentiment", "direction": "bullish",
     "description": "恐慌贪婪指数低于 20。"},
    {"code": "fng_extreme_greed", "name_cn": "极度贪婪", "category": "sentiment", "direction": "bearish",
     "description": "恐慌贪婪指数高于 80。"},
    {"code": "volatility_spike", "name_cn": "波动率飙升", "category": "volatility", "direction": "neutral",
     "description": "30 日年化波动率超过历史 80 分位。"},
]

# ---------------------------------------------------------------- 时间线（已确认的历史事实）
TIMELINE: list[dict[str, str]] = [
    {"date": "2012-11-28", "event_type": "halving", "title_cn": "第一次减半：区块奖励 50 → 25 BTC", "impact": "bullish"},
    {"date": "2016-07-09", "event_type": "halving", "title_cn": "第二次减半：25 → 12.5 BTC", "impact": "bullish"},
    {"date": "2020-05-11", "event_type": "halving", "title_cn": "第三次减半：12.5 → 6.25 BTC", "impact": "bullish"},
    {"date": "2024-04-20", "event_type": "halving", "title_cn": "第四次减半：6.25 → 3.125 BTC", "impact": "bullish"},
    {"date": "2017-12-17", "event_type": "ath", "title_cn": "2017 年周期高点", "impact": "bearish"},
    {"date": "2018-12-15", "event_type": "bottom", "title_cn": "2018 年熊市底部区域", "impact": "bullish"},
    {"date": "2020-03-13", "event_type": "crash", "title_cn": "312 流动性危机单日暴跌", "impact": "bullish"},
    {"date": "2021-11-10", "event_type": "ath", "title_cn": "2021 年周期高点", "impact": "bearish"},
    {"date": "2022-11-21", "event_type": "bottom", "title_cn": "FTX 事件后周期低点区域", "impact": "bullish"},
    {"date": "2024-01-10", "event_type": "etf", "title_cn": "美国现货 BTC ETF 正式获批并开始交易", "impact": "bullish"},
    {"date": "2024-03-14", "event_type": "ath", "title_cn": "减半前创出历史新高", "impact": "neutral"},
]


def _ts(date_str: str) -> int:
    return int(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


async def seed_all() -> dict[str, int]:
    """幂等写入种子数据（重复运行不会产生重复记录）。"""
    # 模型版本档案：先登记，保证「模型实验室」页面永远有内容
    from .model_registry import register_models

    factory = get_session_factory()
    counts: dict[str, int] = {}
    try:
        counts["models"] = await register_models()
    except Exception as exc:  # noqa: BLE001 - 模型登记失败不能阻断业务启动
        logger.event("db.model_register_failed", reason=str(exc)[:200])
    async with factory() as session:
        for asset in ASSETS:
            exists = await session.execute(select(Asset).where(Asset.symbol == asset["symbol"]))
            if not exists.scalars().first():
                session.add(Asset(**asset))  # type: ignore[arg-type]
                counts["assets"] = counts.get("assets", 0) + 1

        for row in INDICATORS:
            exists = await session.execute(
                select(IndicatorDefinition).where(IndicatorDefinition.code == row["code"])
            )
            existing = exists.scalars().first()
            if existing:
                for key, value in row.items():
                    if hasattr(existing, key):
                        setattr(existing, key, value)
            else:
                session.add(IndicatorDefinition(**row))  # type: ignore[arg-type]
                counts["indicators"] = counts.get("indicators", 0) + 1

        for row in SIGNALS:
            exists = await session.execute(select(Signal).where(Signal.code == row["code"]))
            if not exists.scalars().first():
                session.add(Signal(**row))  # type: ignore[arg-type]
                counts["signals"] = counts.get("signals", 0) + 1

        for row in TIMELINE:
            exists = await session.execute(
                select(TimelineEvent).where(
                    TimelineEvent.date == row["date"], TimelineEvent.event_type == row["event_type"]
                )
            )
            if not exists.scalars().first():
                session.add(
                    TimelineEvent(
                        date=row["date"],
                        ts=_ts(row["date"]),
                        event_type=row["event_type"],
                        title_cn=row["title_cn"],
                        impact=row.get("impact", "neutral"),
                        source="system_seed",
                    )
                )
                counts["timeline"] = counts.get("timeline", 0) + 1

        await session.commit()
    logger.event("db.seed_done", **counts)
    return counts
