# -*- coding: utf-8 -*-
"""回测引擎。

防「未来数据泄漏」的硬性保证：
1. 逐日推进，第 t 天只能用 ts <= t 的数据（含 ATH / 分位 / 均线）；
2. 分位数、均线等指标按「当时可见窗口」实时计算，而不是用全样本值；
3. 支持 In-Sample / Out-of-Sample / Walk-Forward 三种验证模式，并记录到 backtest_runs；
4. 结果永久绑定 model_version 与 data_version（历史 K 线的覆盖范围）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import numpy as np

from ..core.logging import get_logger
from ..db.base import get_session_factory
from ..db.models import BacktestResult, BacktestRun
from ..db.repo import candle_coverage, latest_candles
from .strategies import (
    StrategyContext,
    classify_cycle_phase,
    classify_risk_level,
    multiplier_for,
)

logger = get_logger(__name__)

MODEL_VERSION = "bt_v1"
DAY = 86400


@dataclass
class BacktestConfig:
    initial_capital: float = 0.0
    monthly_contribution: float = 0.0
    weekly_contribution: float = 0.0
    contribution_frequency: str = "monthly"   # monthly | weekly | none
    start_date: str = ""
    end_date: str = ""
    strategy_code: str = "dca_fixed"
    strategy_params: dict[str, Any] = field(default_factory=dict)
    fee_rate: float = 0.001
    slippage: float = 0.0005
    max_single_contribution: float = 0.0
    cash_reserve: float = 0.0
    validation_mode: str = "insample"         # insample | oos | walk_forward
    oos_split: float = 0.3
    name: str = ""


@dataclass
class BacktestState:
    cash: float = 0.0
    btc: float = 0.0
    invested: float = 0.0
    fees: float = 0.0
    trades: int = 0


class BacktestEngine:
    VERSION = MODEL_VERSION

    # ---------------------------------------------------------------- 主入口
    async def run(self, config: BacktestConfig) -> dict[str, Any]:
        factory = get_session_factory()
        async with factory() as session:
            all_candles = await latest_candles(session, "BTC", "1d", 10000)
        if not all_candles:
            return {"ok": False, "message": "本地没有历史数据，无法回测。请先执行历史数据回填。"}

        # 时间过滤（严格只使用区间内的本地历史）
        candles = self._filter_range(all_candles, config.start_date, config.end_date)
        if len(candles) < 60:
            return {"ok": False, "message": f"所选区间内可用日线不足（{len(candles)} 根），至少需要 60 根。"}

        # 验证模式分割
        split_idx = len(candles)
        if config.validation_mode in ("oos", "walk_forward") and config.oos_split > 0:
            split_idx = max(30, int(len(candles) * (1 - config.oos_split)))
            test_slice = candles[split_idx:]
        else:
            test_slice = candles

        result = self._simulate(candles[:split_idx] if config.validation_mode != "insample" else candles, config)
        if config.validation_mode != "insample":
            result["validation"] = {
                "mode": config.validation_mode,
                "train_days": split_idx,
                "test_days": len(test_slice),
                "train_period": self._date_str(candles[0]["ts"]) + " ~ " + self._date_str(candles[max(0, split_idx - 1)]["ts"]),
                "test_period": self._date_str(test_slice[0]["ts"]) + " ~ " + self._date_str(test_slice[-1]["ts"]),
                "note": "回测结果同时提供训练区间与样本外区间的表现，样本外更能反映真实能力。",
            }
            result["oos_result"] = self._simulate(test_slice, config, initial_state=None)

        run_id = await self._persist(config, result, candles)
        result["run_id"] = run_id
        result["model_version"] = self.VERSION
        return result

    # ---------------------------------------------------------------- 核心模拟
    def _simulate(
        self, candles: list[dict[str, Any]], config: BacktestConfig, initial_state: BacktestState | None = None
    ) -> dict[str, Any]:
        state = initial_state or BacktestState(cash=float(config.initial_capital))
        invested_total = float(config.initial_capital)
        equity_curve: list[list[Any]] = []
        monthly_returns: dict[str, float] = {}
        peak_equity = state.cash
        max_dd = 0.0
        max_dd_start_ts = int(candles[0]["ts"])
        max_dd_trough_ts = max_dd_start_ts
        max_dd_duration_days = 0
        dd_start_ts: int = max_dd_start_ts
        # 该次最大回撤「起点峰值」对应的权益绝对值：恢复天数以回到它为准，
        # 绝不使用事后才知道的全局最高权益，避免隐性未来函数。
        max_dd_peak_value = state.cash
        dd_start_equity = state.cash

        closes = np.array([c["close"] for c in candles], dtype=float)
        base_amount = (
            config.weekly_contribution
            if config.contribution_frequency == "weekly"
            else config.monthly_contribution
        )
        step_days = 7 if config.contribution_frequency == "weekly" else 30

        initial_price = float(closes[0])

        for i, candle in enumerate(candles):
            ts = int(candle["ts"])
            price = float(candle["close"])

            # ---- 1) 决策依据严格限制在「截至今天」的可见窗口
            history = closes[: i + 1]
            ath = float(np.max(history))
            drawdown_pct = (price - ath) / ath * 100 if ath else 0.0
            ma200 = float(np.mean(history[-200:])) if len(history) >= 200 else None
            percentile = float((history <= price).sum() / len(history) * 100) if len(history) > 0 else None

            # 风险/周期分类同样只用截至今日的价格历史，调用外部引擎会引入
            # 链上/情绪等回测切片里没有的数据，也会造成隐性未来函数。
            if len(history) >= 31:
                window = np.clip(history[-91:], 1e-9, None)
                annual_vol = float(np.std(np.diff(np.log(window))) * np.sqrt(365) * 100)
                change_30d = float((price / window[-31] - 1) * 100) if len(window) >= 31 else None
            else:
                annual_vol, change_30d = None, None
            dev_ma200 = (price - ma200) / ma200 if ma200 else None

            ctx = StrategyContext(
                ts=ts,
                price=price,
                drawdown_pct=drawdown_pct,
                ath=ath,
                price_percentile=percentile,
                ma200=ma200,
                risk_level=classify_risk_level(drawdown_pct, annual_vol),
                cycle_phase=classify_cycle_phase(drawdown_pct, dev_ma200, change_30d),
                cash_balance=state.cash,
            )
            multiplier = multiplier_for(config.strategy_code, config.strategy_params, ctx)

            # ---- 2) 初始资金：第一天一次性建仓
            to_buy = 0.0
            if i == 0 and state.cash > 0:
                to_buy = max(0.0, state.cash - config.cash_reserve)

            # ---- 3) 定期投入（每 step_days 一次，含第一天）
            if base_amount > 0 and (i % step_days) == 0:
                injected = base_amount * multiplier
                if config.max_single_contribution > 0:
                    injected = min(injected, config.max_single_contribution)
                state.cash += injected
                invested_total += injected
                cap = max(0.0, state.cash - config.cash_reserve)
                to_buy += min(injected, cap)

            # ---- 4) 执行买入（含手续费与滑点）
            if to_buy > 1e-8:
                exec_price = price * (1 + config.slippage)
                fee = to_buy * config.fee_rate
                net = max(0.0, to_buy - fee)
                state.btc += net / exec_price
                state.cash -= to_buy
                state.fees += fee
                state.trades += 1

            equity = state.cash + state.btc * price
            equity_curve.append([ts, round(equity, 2)])

            # ---- 5) 回撤统计
            if equity >= peak_equity:
                peak_equity = equity
                dd_start_ts = ts
                dd_start_equity = equity
            if peak_equity > 0:
                dd = (equity - peak_equity) / peak_equity * 100
                if dd < max_dd:
                    max_dd = dd
                    max_dd_start_ts = dd_start_ts
                    max_dd_trough_ts = ts
                    # 记录「本次回撤起点对应的峰值」。
                    # 恢复时间必须从谷底回到**这个**峰值，而不是事后才知道的全局最高权益 ——
                    # 否则等于拿未来信息定义过去，会把「还在坑里」误算成「已恢复」。
                    max_dd_peak_value = dd_start_equity
                    max_dd_duration_days = max(
                        max_dd_duration_days, int((max_dd_trough_ts - max_dd_start_ts) // DAY)
                    )

            # ---- 6) 记录每月最后一天的权益
            label = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m")
            monthly_returns[label] = equity

        # 计算月度收益率
        months = sorted(monthly_returns.keys())
        monthly_pct: list[dict[str, Any]] = []
        prev_value: float | None = None
        for month in months:
            value = monthly_returns[month]
            if prev_value is None:
                monthly_pct.append({"month": month, "value": round(value, 2), "return_pct": None})
            else:
                monthly_pct.append(
                    {
                        "month": month,
                        "value": round(value, 2),
                        "return_pct": round((value - prev_value) / prev_value * 100, 2) if prev_value else None,
                    }
                )
            prev_value = value

        final_price = float(closes[-1])
        final_value = state.cash + state.btc * final_price
        total_invested = invested_total
        net_profit = final_value - total_invested
        roi = (net_profit / total_invested * 100) if total_invested > 0 else 0.0

        days = int((candles[-1]["ts"] - candles[0]["ts"]) // DAY)
        years = days / 365.0 if days > 0 else 0.0
        cagr = ((final_value / total_invested) ** (1 / years) - 1) * 100 if years > 0 and total_invested > 0 else 0.0

        # Sharpe / Sortino（基于月度收益）
        returns = [m["return_pct"] for m in monthly_pct if m["return_pct"] is not None]
        sharpe, sortino, volatility = self._risk_metrics(returns)

        # 恢复时间：从最大回撤谷底到重新超过「该次回撤起点峰值」所需天数
        recovery_days = 0
        recovered = False
        for ts, eq in equity_curve:
            if ts > max_dd_trough_ts and eq >= max_dd_peak_value:
                recovery_days = int((ts - max_dd_trough_ts) // DAY)
                recovered = True
                break

        avg_cost = (total_invested / state.btc) if state.btc > 0 else 0.0

        yearly = self._yearly_summary(monthly_pct)

        warnings: list[str] = []
        if len(candles) < 365:
            warnings.append("回测区间不足 1 年，统计结果代表性有限")
        if state.trades < 30:
            warnings.append(f"交易次数仅 {state.trades} 次，样本较少")
        if max_dd < -70:
            warnings.append("历史最大回撤超过 70%，请确认自身风险承受能力")

        return {
            "ok": True,
            "final_value": round(final_value, 2),
            "total_invested": round(total_invested, 2),
            "net_profit": round(net_profit, 2),
            "roi": round(roi, 2),
            "cagr": round(cagr, 2),
            "max_drawdown": round(max_dd, 2),
            "max_drawdown_window": {
                "start_ts": max_dd_start_ts,
                "trough_ts": max_dd_trough_ts,
                "duration_days": max_dd_duration_days,
            },
            "recovery_days": recovery_days,
            "recovery_note": (
                f"最大回撤后已于 {recovery_days} 天回到回撤前峰值"
                if recovered else "截至回测结束仍未回到该次回撤前的高点"
            ),
            "sharpe": sharpe,
            "sortino": sortino,
            "volatility": volatility,
            "best_year": yearly.get("best"),
            "worst_year": yearly.get("worst"),
            "btc_amount": round(state.btc, 8),
            "avg_cost": round(avg_cost, 2),
            "final_price": round(final_price, 2),
            "total_fees": round(state.fees, 2),
            "trades": state.trades,
            "initial_price": round(initial_price, 2),
            "days": days,
            "years": round(years, 2),
            "equity_curve": equity_curve,
            "monthly_returns": monthly_pct,
            "warnings": warnings,
            "buy_and_hold": {
                "initial_btc": round(config.initial_capital / initial_price, 8) if initial_price else 0.0,
                "final_value": round(config.initial_capital / initial_price * final_price, 2) if initial_price else 0.0,
            },
        }

    # ---------------------------------------------------------------- 工具
    @staticmethod
    def _filter_range(candles: list[dict[str, Any]], start: str, end: str) -> list[dict[str, Any]]:
        def to_ts(date_str: str, default: int) -> int:
            if not date_str:
                return default
            try:
                return int(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
            except ValueError:
                return default

        start_ts = to_ts(start, candles[0]["ts"])
        end_ts = to_ts(end, candles[-1]["ts"]) + DAY
        return [c for c in candles if start_ts <= c["ts"] < end_ts]

    @staticmethod
    def _date_str(ts: int) -> str:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")

    @staticmethod
    def _risk_metrics(monthly_returns: list[float]) -> tuple[float, float, float]:
        if len(monthly_returns) < 3:
            return 0.0, 0.0, 0.0
        arr = np.array([r / 100 for r in monthly_returns], dtype=float)
        mean = float(np.mean(arr))
        std = float(np.std(arr)) or 1e-9
        downside = arr[arr < 0]
        downside_std = float(np.std(downside)) if len(downside) > 1 else std
        # 月频 -> 年化
        sharpe = float(mean / std * math.sqrt(12))
        sortino = float(mean / downside_std * math.sqrt(12)) if downside_std else 0.0
        volatility = float(std * math.sqrt(12) * 100)
        return round(sharpe, 3), round(sortino, 3), round(volatility, 2)

    @staticmethod
    def _yearly_summary(monthly: list[dict[str, Any]]) -> dict[str, Any]:
        yearly: dict[str, dict[str, Any]] = {}
        for row in monthly:
            year = row["month"][:4]
            yearly.setdefault(year, {"first": row["value"], "last": row["value"]})
            yearly[year]["last"] = row["value"]
        summary = {}
        for year, vals in yearly.items():
            pct = ((vals["last"] - vals["first"]) / vals["first"] * 100) if vals["first"] else 0.0
            summary[year] = round(pct, 2)
        if not summary:
            return {}
        best_year = max(summary.items(), key=lambda x: x[1])
        worst_year = min(summary.items(), key=lambda x: x[1])
        return {
            "best": {"year": best_year[0], "return_pct": best_year[1]},
            "worst": {"year": worst_year[0], "return_pct": worst_year[1]},
            "all": summary,
        }

    async def _persist(self, config: BacktestConfig, result: dict[str, Any], candles: list[dict[str, Any]]) -> int:
        factory = get_session_factory()
        async with factory() as session:
            coverage = await candle_coverage(session)
            run = BacktestRun(
                name=config.name or f"{config.strategy_code} {datetime.now(timezone.utc):%Y-%m-%d}",
                strategy_code=config.strategy_code,
                strategy_params=config.strategy_params,
                plan_snapshot={
                    "initial_capital": config.initial_capital,
                    "monthly_contribution": config.monthly_contribution,
                    "weekly_contribution": config.weekly_contribution,
                    "contribution_frequency": config.contribution_frequency,
                    "fee_rate": config.fee_rate,
                    "slippage": config.slippage,
                },
                start_date=self._date_str(candles[0]["ts"]),
                end_date=self._date_str(candles[-1]["ts"]),
                initial_capital=config.initial_capital,
                fee_rate=config.fee_rate,
                slippage=config.slippage,
                data_version=f"{coverage.get('count', 0)}bars_{coverage.get('start_ts')}_{coverage.get('end_ts')}",
                model_version=self.VERSION,
                validation_mode=config.validation_mode,
                status="done",
            )
            session.add(run)
            await session.flush()

            session.add(
                BacktestResult(
                    run_id=int(run.id),
                    final_value=result.get("final_value", 0.0),
                    total_invested=result.get("total_invested", 0.0),
                    net_profit=result.get("net_profit", 0.0),
                    roi=result.get("roi", 0.0),
                    cagr=result.get("cagr", 0.0),
                    max_drawdown=result.get("max_drawdown", 0.0),
                    max_drawdown_duration_days=result.get("max_drawdown_window", {}).get("duration_days", 0),
                    recovery_days=result.get("recovery_days", 0),
                    sharpe=result.get("sharpe", 0.0),
                    sortino=result.get("sortino", 0.0),
                    volatility=result.get("volatility", 0.0),
                    best_year=result.get("best_year"),
                    worst_year=result.get("worst_year"),
                    btc_amount=result.get("btc_amount", 0.0),
                    avg_cost=result.get("avg_cost", 0.0),
                    total_fees=result.get("total_fees", 0.0),
                    trades=result.get("trades", 0),
                    equity_curve=result.get("equity_curve", [])[-4000:],
                    monthly_returns=result.get("monthly_returns", []),
                    warnings=result.get("warnings", []),
                )
            )
            await session.commit()
            return int(run.id)

    async def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        from sqlalchemy import desc, select

        factory = get_session_factory()
        async with factory() as session:
            rows = (
                await session.execute(select(BacktestRun).order_by(desc(BacktestRun.created_at)).limit(limit))
            ).scalars().all()
        return [
            {
                "id": r.id,
                "name": r.name,
                "strategy_code": r.strategy_code,
                "start_date": r.start_date,
                "end_date": r.end_date,
                "validation_mode": r.validation_mode,
                "model_version": r.model_version,
                "data_version": r.data_version,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]
