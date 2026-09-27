# -*- coding: utf-8 -*-
"""回测「零未来数据泄漏」验收测试 —— 本平台最不能出错的一条底线。

未来泄漏（look-ahead bias）的四种典型形态，这里逐一设卡：
  1. 用未来才知道的**全局最高点**计算回撤/距离；
  2. 指标用了**整段序列**做归一化（如分位数、z-score）后回填历史时点；
  3. 决策依据里混入了当日收盘之后的信号；
  4. 样本外切分把测试数据泄漏进训练/调参。

验证方法（不依赖阅读代码，直接对结果做反事实检验）：
    在同一条序列上先跑一次回测得到权益曲线 A；
    再**追加一段未来的 K 线**，用同一个 end_date 重跑得到权益曲线 B；
    若引擎有任何一处偷看未来，A 与 B 在重叠区间必然不一致。
    这是最能抓住隐性未来函数的测试，比逐行读代码可靠得多。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.db.base import get_session_factory
from app.db.migrate import init_db
from app.db.repo import upsert_candles
from app.services.backtest_service import BacktestConfig, BacktestEngine

from helpers import build_candles, date_str

INSAMPLE_DAYS = 400
FUTURE_DAYS = 180


async def _seed(n: int) -> list[dict]:
    await init_db()
    candles = build_candles(n)
    factory = get_session_factory()
    async with factory() as session:
        await upsert_candles(session, candles)
    return candles


def _cfg(**over: object) -> BacktestConfig:
    params = {
        "start_date": "2019-01-01",
        "strategy_code": "dca_drawdown",
        "contribution_frequency": "monthly",
        "monthly_contribution": 1000.0,
        "validation_mode": "insample",
    }
    params.update(over)  # type: ignore[arg-type]
    return BacktestConfig(**params)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_appending_future_candles_does_not_change_past_decisions(tmp_db_path):
    """核心：追加未来数据不得改变历史上的任何一次投入与权益。"""
    candles = await _seed(INSAMPLE_DAYS)
    end_date = date_str(candles[-1]["ts"])

    engine = BacktestEngine()
    first = await engine.run(_cfg(end_date=end_date))
    assert first.get("ok"), first.get("message")

    # 追加「未来」数据后再跑同一区间
    next_day = datetime.fromtimestamp(candles[-1]["ts"] + 86400, tz=timezone.utc)
    future = build_candles(FUTURE_DAYS, start=next_day)
    factory = get_session_factory()
    async with factory() as session:
        await upsert_candles(session, future)

    second = await engine.run(_cfg(end_date=end_date))
    assert second.get("ok"), second.get("message")

    assert len(first["equity_curve"]) == len(second["equity_curve"])
    for (ts_a, eq_a), (ts_b, eq_b) in zip(first["equity_curve"], second["equity_curve"]):
        assert ts_a == ts_b
        assert eq_a == eq_b, (
            f"追加未来 K 线后，{date_str(ts_a)} 的权益从 {eq_a} 变成 {eq_b} —— 存在未来数据泄漏"
        )
    assert round(first["total_invested"], 6) == round(second["total_invested"], 6)
    assert round(first["final_value"], 6) == round(second["final_value"], 6)


@pytest.mark.asyncio
async def test_drawdown_metrics_use_only_past_peak(tmp_db_path):
    """回撤相关指标必须只用截至当日的最高权益，不能用事后才知道的全局峰值。"""
    await _seed(INSAMPLE_DAYS)
    engine = BacktestEngine()
    shorter = await engine.run(_cfg(end_date="2019-10-01"))
    longer = await engine.run(_cfg(end_date="2019-12-31"))
    assert shorter.get("ok") and longer.get("ok")
    # 区间更长 => 最大回撤不可能变小（回撤只会随着样本增加而加深或持平）
    assert longer["max_drawdown"] <= shorter["max_drawdown"] + 1e-9
    # 恢复天数字段必须有解释说明，不允许出现「假装已恢复」
    assert isinstance(longer.get("recovery_note"), str)


@pytest.mark.asyncio
async def test_out_of_sample_split_is_disjoint(tmp_db_path):
    """样本外区间必须严格在训练区间之后，不能重叠。"""
    await _seed(INSAMPLE_DAYS)
    engine = BacktestEngine()
    result = await engine.run(_cfg(validation_mode="oos", oos_split=0.3))
    assert result.get("ok")
    validation = result["validation"]
    assert validation["mode"] == "oos"
    train_end = validation["train_period"].split("~")[1].strip()
    test_start = validation["test_period"].split("~")[0].strip()
    assert train_end < test_start, "训练区间与样本外区间存在重叠或顺序错误"
    assert validation["train_days"] + validation["test_days"] == INSAMPLE_DAYS


@pytest.mark.asyncio
async def test_insufficient_history_is_refused_instead_of_extrapolated(tmp_db_path):
    """历史不足时必须明确拒绝，不允许外推/补齐后用假数据跑出「漂亮」的结果。"""
    await init_db()
    factory = get_session_factory()
    async with factory() as session:
        await upsert_candles(session, build_candles(30))
    result = await BacktestEngine().run(_cfg())
    assert result["ok"] is False
    assert "不足" in result["message"]


@pytest.mark.asyncio
async def test_fees_and_slippage_are_charged(tmp_db_path):
    """手续费与滑点必须真实扣减 —— 否则回测结果系统性虚高。"""
    await _seed(INSAMPLE_DAYS)
    engine = BacktestEngine()
    with_cost = await engine.run(_cfg())
    no_cost = await engine.run(_cfg(fee_rate=0.0, slippage=0.0))
    assert with_cost.get("ok") and no_cost.get("ok")
    assert with_cost["total_fees"] > 0
    assert no_cost["final_value"] > with_cost["final_value"], "费用未生效：零费率与正常费率结果相同"
    # 零成本下买入均价必然不高于含滑点的均价
    assert no_cost["avg_cost"] <= with_cost["avg_cost"] + 1e-6
