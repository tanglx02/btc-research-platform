# -*- coding: utf-8 -*-
"""模型版本登记中心。

需求原文约束：
- 「模型不能只追历史收益，必须记录开发区间 / 测试区间 / 参数 / 版本」；
- 「必须支持 Walk-Forward、样本外测试、参数敏感性」；
- 「必须说明『在什么情况下有效』」。

因此这里对**每一个正在运行的模型**登记一份档案，并且必须诚实：
规则型引擎（参数不是拟合出来的）会明确写「未做参数优化，因此也不存在过拟合」，
同时明确写「尚未做严格样本外检验」，不允许含糊带过。

每个模型档案包含：
  code / name / kind     —— 标识
  params                 —— 实际使用的参数（可从代码直接对照）
  dev_period             —— 开发时可见的历史区间
  test_period            —— 样本外检验区间（未做必须写「未进行」）
  applicable_when        —— 什么情况下结论可信
  fail_when              —— 什么情况下结论会失效
  validation             —— 检验状态与计划
  metrics                —— 已完成的检验结果（没有就是没有）
"""

from __future__ import annotations

from typing import Any

# 版本变更记录（长期维护：升级模型时必须在此追加说明）
CHANGELOG: list[dict[str, str]] = [
    {"version": "cycle_v1", "date": "2026-09", "change": "首个版本：十阶段多维打分，输出支持证据与反向证据"},
    {"version": "valuation_v1", "date": "2026-09", "change": "首个版本：分位/回撤/波动加权，缺项自动降权并声明缺失"},
    {"version": "risk_v1", "date": "2026-09", "change": "首个版本：七维风险，缺失维度标注 unavailable"},
    {"version": "regime_v1", "date": "2026-09", "change": "首个版本：融合八类因子输出市场状态"},
    {"version": "forecast_v1", "date": "2026-09", "change": "首个版本：历史相似状态分布，样本/一致性不足则拒绝输出"},
    {"version": "ind_v1", "date": "2026-09", "change": "首个版本：本地日线计算技术指标"},
    {"version": "bt_v1", "date": "2026-09", "change": "首个版本：逐日推进回测，严格只用 t 时刻可见数据"},
]

MODELS: list[dict[str, Any]] = [
    {
        "code": "cycle_v1",
        "name": "市场周期定位引擎",
        "kind": "cycle",
        "params": {
            "dimensions": ["趋势", "估值", "资金/杠杆", "情绪", "宏观"],
            "trend_inputs": ["price vs ma200", "ma50 vs ma200", "change_30d", "change_90d", "rsi14"],
            "output_phases": 10,
            "hardcoded_halving_cycle": False,
            "note": "打分权重为人工设定的经验权重，不是拟合得到，因此不存在参数过拟合；但也意味着它没有被优化到历史最优。",
        },
        "dev_period": "2013-01 ~ 2026-09（本地已落库的全部日线，设计时可见）",
        "test_period": "未进行严格样本外检验",
        "applicable_when": [
            "本地日线历史足够长（至少覆盖一轮完整牛熊），当前分位/均线才有参照意义",
            "趋势与情绪读数方向一致时，阶段判断相对稳定",
            "用于描述「现在大概在什么位置」，而非择时",
        ],
        "fail_when": [
            "外部冲击主导的行情（交易所暴雷、政策突变、流动性危机）：历史形态类比失效",
            "震荡市：多个维度互相矛盾，模型会给出震荡/方向不明，此时应以下方原始数据自行判断",
            "缺失链上/宏观/衍生品数据时，只依赖价格维度，置信度会显著下降",
        ],
        "validation": {
            "oos_validated": False,
            "walk_forward": False,
            "parameter_sensitivity": False,
            "plan": "后续版本计划用 2013-2018 作为开发区间、2019-2026 作为样本外区间重跑，并在模型实验室公布对照结果。",
        },
        "active": True,
    },
    {
        "code": "valuation_v1",
        "name": "估值状态引擎",
        "kind": "valuation",
        "params": {
            # 权重与 valuation.py 中的 4 个因子块一一对应，改代码时请同步改这里
            "factors": {
                "price_percentile": 0.30,
                "drawdown_from_ath": 0.25,
                "ma200_deviation": 0.20,
                "mvrv": 0.25,
            },
            "missing_policy": "缺失因子自动剔除并重新归一化权重，同时在 missing_factors 中声明",
            "output_states": ["deep_undervalue", "undervalue", "fair", "overvalue", "extreme_overvalue"],
        },
        "dev_period": "2013-01 ~ 2026-09（与本地日线覆盖一致）",
        "test_period": "未进行严格样本外检验",
        "applicable_when": [
            "用于理解「当前相对历史处在什么价位环境」",
            "MVRV 可用时结论更完整（需要付费链上数据源）",
            "长期视角（数月以上）比短期视角更有意义",
        ],
        "fail_when": [
            "把「低估」理解成「马上要涨」：估值可以持续低估数年",
            "结构性变化（如现货 ETF 上市改变定价机制）会让历史分位的可比性下降",
            "历史长度不足时，分位数本身不稳定",
        ],
        "validation": {
            "oos_validated": False,
            "walk_forward": False,
            "parameter_sensitivity": False,
            "plan": "权重为人工设定；计划用不同权重组合做敏感性分析，确认状态判定不会因小幅调整而翻转。",
        },
        "active": True,
    },
    {
        "code": "risk_v1",
        "name": "七维风险引擎",
        "kind": "risk",
        "params": {
            # 与 risk.py 的 weights 字典保持一致
            "dimensions": ["趋势", "估值", "波动", "杠杆", "流动性", "宏观", "链上"],
            "weights": {"trend": 0.20, "valuation": 0.20, "leverage": 0.15,
                        "liquidity": 0.10, "macro": 0.15, "onchain": 0.10, "volatility": 0.10},
            "levels": "0-20 低 / 20-40 中 / 40-60 偏高 / 60-80 高 / 80-100 极高",
            "volatility_map": "min(100, 30日年化波动率 × 1.6)",
            "missing_policy": "缺失维度标注 unavailable 并使用中性默认值，不参与重点加权",
            "known_limitation": "流动性与链上两维当前为常量分（30 / 45），区分度有限，不代表真实变化。",
        },
        "dev_period": "2013-01 ~ 2026-09",
        "test_period": "未进行严格样本外检验",
        "applicable_when": [
            "评估当前环境下「仓位应该更保守还是可以更积极」",
            "数据维度齐全时（尤其杠杆与宏观可用）结论更可靠",
        ],
        "fail_when": [
            "多数外部数据源不可用时，只剩下价格与波动两个维度，综合分会被中性值拉平，可能低估真实风险",
            "风险评分高不代表立刻下跌，风险评分低也不代表安全",
        ],
        "validation": {
            "oos_validated": False,
            "walk_forward": False,
            "parameter_sensitivity": False,
            "plan": "计划检验：高风险读数出现后未来 30/90 日的回撤分布，与低风险读数做对照。",
        },
        "active": True,
    },
    {
        "code": "regime_v1",
        "name": "综合市场状态引擎",
        "kind": "regime",
        "params": {
            "inputs": ["trend", "valuation", "capital_flow", "onchain", "derivative", "macro", "sentiment", "risk", "cycle"],
            "missing_policy": "缺失模块从合成中剔除并在 unavailable_modules 中列出",
            "confidence": "按可用模块占比计算，模块缺失会直接降低置信度",
        },
        "dev_period": "2013-01 ~ 2026-09",
        "test_period": "未进行严格样本外检验",
        "applicable_when": [
            "需要一个统一的「当前处于什么环境」描述时",
            "置信度 0.6 以上时可作为配置参考",
        ],
        "fail_when": [
            "置信度低于 0.5（多数外部数据源不可用）时应只作为描述，不作为决策依据",
            "各模块方向严重冲突时，综合状态本身就会不稳定",
        ],
        "validation": {
            "oos_validated": False,
            "walk_forward": False,
            "parameter_sensitivity": False,
            "plan": "计划按历史各 regime 状态下的未来收益分布做统计对照。",
        },
        "active": True,
    },
    {
        "code": "forecast_v1",
        "name": "概率区间预测引擎",
        "kind": "forecast",
        "params": {
            "method": "历史相似状态（200日偏离/回撤/30日收益/波动率）最近邻 K 条的向前收益分布",
            "features": ["ma200_deviation", "drawdown", "ret_30d", "volatility_30d"],
            "horizon_days": 30,
            "min_samples": 30,
            "min_consistency": 0.35,
            "refuse_policy": "样本数或一致性不足时直接返回 unavailable，不做任何输出",
            "outputs": ["上涨/横盘/下跌概率", "p10-p90 区间", "样本数", "一致性"],
        },
        "dev_period": "2013-01 ~ 2026-09（每次预测只用预测时点之前的数据）",
        "test_period": "未进行严格样本外检验（但样本选择天然避免未来信息）",
        "applicable_when": [
            "一致性 0.6 以上、样本数 100 以上时，区间才具备参考价值",
            "用于理解「可能的结果分布」，不是目标价",
        ],
        "fail_when": [
            "样本不足或一致性低 → 系统直接拒答，不做预测",
            "从未出现过的市场环境（缺乏历史相似样本）→ 类比不成立",
            "把概率当确定性：即便上涨概率 60%，仍有 40% 的情形不涨",
        ],
        "validation": {
            "oos_validated": False,
            "walk_forward": False,
            "parameter_sensitivity": False,
            "plan": "计划对历史每个交易日做滚动预测，统计真实结果落在 p10-p90 区间内的比例（覆盖率检验）。",
        },
        "active": True,
    },
    {
        "code": "ind_v1",
        "name": "技术指标计算引擎",
        "kind": "indicator",
        "params": {
            "source": "本地 candles 表（不依赖第三方实时接口）",
            "indicators": ["ma20", "ma50", "ma200", "ema12", "ema26", "macd", "rsi14",
                           "atr14", "bollinger", "volatility_30d", "price_percentile", "drawdown"],
            "future_leak_protection": "所有滚动窗口只用 ts <= 当前 bar 的数据",
        },
        "dev_period": "不适用（确定性公式计算，无拟合参数）",
        "test_period": "不适用",
        "applicable_when": ["任何本地历史完整的时点"],
        "fail_when": ["本地历史缺失或存在未修补的缺口 → 均线/分位会失真（数据质量中心会提示缺口）"],
        "validation": {
            "oos_validated": True,
            "walk_forward": False,
            "parameter_sensitivity": False,
            "note": "指标为标准公式，无拟合参数，因此不存在过拟合问题；缺口检测由数据质量中心负责。",
        },
        "active": True,
    },
    {
        "code": "bt_v1",
        "name": "策略回测引擎",
        "kind": "strategy",
        "params": {
            "execution": "逐日推进，第 t 天只用 ts <= t 的数据",
            "recomputed_each_step": ["历史最高价 ATH", "价格分位", "MA200"],
            "costs": "手续费 fee_rate + 滑点 slippage 双边计入",
            "validation_modes": ["insample", "oos"],
            "walk_forward_status": "未实现：walk_forward 目前退化为与 oos 相同的一次性切分",
            # 只做一次切分、没有滚动多折。登记档案不能比代码更会说。
            "walk_forward_status": "未实现：walk_forward 目前退化为与 oos 相同的一次性切分",
            "strategy_state_inputs": ["drawdown_pct", "price_percentile", "ma200",
                                      "risk_level(价格历史推导)", "cycle_phase(价格历史推导)"],
            "backtest_state_note": (
                "回测中不使用完整 RiskEngine / CycleEngine：它们需要链上、情绪、资金费率，"
                "而历史切片里没有这些；改用 strategies.classify_risk_level / classify_cycle_phase "
                "从价格历史推导，保证因果闭环（代价是区分度低于实时版本）。"
            ),
            "default_fee_rate": 0.001,
            "default_slippage": 0.0005,
            "known_bug_fixed": [
                "dca_drawdown 回撤阶梯缺 break，导致倍数恒为 1（已修）",
                "dca_risk / dca_cycle 因 ctx 缺 risk_level / cycle_phase 恒为 1（已修）",
            ],
            "versioned": "每次回测写入 backtest_runs，绑定 model_version 与 data_version（历史 K 线范围）",
        },
        "dev_period": "由使用者在每次回测中显式指定（记录在 runs 表）",
        "test_period": "oos / walk_forward 模式下由 oos_split 划出，记录在 result.validation",
        "applicable_when": [
            "评估不同投入规则的长期效果差异",
            "使用样本外模式（oos / walk_forward）检验策略是否被凑出来",
        ],
        "fail_when": [
            "只用样本内最优结果宣传：必须在样本外复跑后才算数",
            "本地历史区间过短或与所测策略场景不符",
            "把回测收益当成未来收益",
        ],
        "validation": {
            "oos_validated": "由使用者选择 oos 模式决定",
            "walk_forward": False,
            "parameter_sensitivity": False,
            "note": (
                "同一策略、不同起始日期的结果差异很大，回测页面会把这个局限明确写在结果旁边。"
                "walk_forward 尚未实现滚动多折检验。"
            ),
        },
        "active": True,
    },
    {
        "code": "replay_v1",
        "name": "历史回放引擎",
        "kind": "replay",
        "params": {
            "window": "取回放日之前最近的 N 根日线（见 ReplayService.window），全部 ts <= 回放日",
            "recomputed_each_replay": ["指标", "估值", "周期", "风险", "当时 ATH", "价格分位"],
            "context_sources": ["sentiment", "derivatives", "onchain", "macro", "timeline"],
            "aftermath_policy": "事后视角单独返回并标注；样本不足以覆盖 30/90/180/365 天时如实为空，不用最后一根冒充",
        },
        "dev_period": "不适用（回放不是预测模型，只是历史切片重算）",
        "test_period": "不适用",
        "applicable_when": [
            "想看「那一天系统本来会显示什么」，用来校准自己对指标的直觉",
            "本地已回填到该日期之后（否则会明确拒绝回放）",
        ],
        "fail_when": [
            "把回放当时的指标当成预测：回放展示的是当天已经知道的信息",
            "本地历史未覆盖该日期：系统会明确提示需要回填，不会用更早的数据冒充",
            "similar_periods 里的 30/90 日收益是**事后**结果，仅作对照学习，不能作为信号使用",
        ],
        "validation": {
            "oos_validated": True,
            "walk_forward": False,
            "parameter_sensitivity": False,
            "note": "回放本身不含拟合参数；其可信度取决于本地历史是否完整、来源是否可信。",
        },
        "active": True,
    },
]


def _archive_text(model: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("【什么情况下有效】")
    lines.extend(f"- {x}" for x in model.get("applicable_when", []))
    lines.append("")
    lines.append("【什么情况下失效 / 不能怎么用】")
    lines.extend(f"- {x}" for x in model.get("fail_when", []))
    lines.append("")
    lines.append("【检验状态】")
    for key, value in (model.get("validation") or {}).items():
        lines.append(f"- {key}: {value}")
    return "\n".join(lines)


async def register_models() -> int:
    """幂等登记全部模型档案（重复执行不会产生重复行）。

    诚实说明：当前实现按 code **就地覆盖同版本内容**（不清空历史行，但也不会新建版本行），
    因此「历史上某次回测到底用了哪套参数」主要依赖 CHANGELOG 文字记录与 backtest_runs
    里保存的结果快照，而不是可精确回放的旧参数快照。
    若需要严格的版本追溯，后续应改为追加不活跃的新版本行（active=False）。
    """
    from sqlalchemy import select

    from .base import get_session_factory
    from .models import ModelVersion

    factory = get_session_factory()
    written = 0
    async with factory() as session:
        for model in MODELS:
            notes = _archive_text(model)
            row = (
                await session.execute(select(ModelVersion).where(ModelVersion.code == model["code"]))
            ).scalars().first()
            payload = dict(
                name=model["name"],
                kind=model["kind"],
                params=model["params"],
                dev_period=model.get("dev_period"),
                test_period=model.get("test_period"),
                metrics=model.get("validation"),
                active=bool(model.get("active", False)),
                notes=notes,
            )
            if row:
                for key, value in payload.items():
                    setattr(row, key, value)
            else:
                session.add(ModelVersion(code=model["code"], **payload))
            written += 1
        await session.commit()
    return written
