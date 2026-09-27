# -*- coding: utf-8 -*-
"""个人资金计划与资产账本。

默认只做记录与模拟，绝不自动下单。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, select

from ..core.logging import get_logger
from ..db.base import get_session_factory
from ..db.models import PortfolioSnapshot, UserHolding, UserPlan, UserTransaction
from ..db.repo import latest_candles
from ..providers.types import QualityStatus
from .strategies import StrategyContext, list_strategies, multiplier_for

logger = get_logger(__name__)

DAY = 86400


class PlanService:
    def __init__(self) -> None:
        pass

    # ---------------------------------------------------------------- 计划 CRUD
    async def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        factory = get_session_factory()
        async with factory() as session:
            plan = UserPlan(
                name=payload.get("name") or "我的 BTC 计划",
                currency=payload.get("currency", "CNY"),
                initial_capital=float(payload.get("initial_capital") or 0),
                monthly_income=float(payload.get("monthly_income") or 0),
                monthly_contribution=float(payload.get("monthly_contribution") or 0),
                weekly_contribution=float(payload.get("weekly_contribution") or 0),
                contribution_frequency=payload.get("contribution_frequency", "monthly"),
                start_date=payload.get("start_date") or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                end_date=payload.get("end_date"),
                cash_reserve=float(payload.get("cash_reserve") or 0),
                max_single_contribution=float(payload.get("max_single_contribution") or 0),
                max_drawdown_tolerance=float(payload.get("max_drawdown_tolerance") or 50),
                strategy_code=payload.get("strategy_code", "dca_fixed"),
                strategy_params=payload.get("strategy_params") or {},
                notes=payload.get("notes"),
            )
            session.add(plan)
            await session.commit()
            await session.refresh(plan)
            return self._plan_dict(plan)

    async def list(self) -> list[dict[str, Any]]:
        factory = get_session_factory()
        async with factory() as session:
            rows = (await session.execute(select(UserPlan).order_by(desc(UserPlan.created_at)))).scalars().all()
        return [self._plan_dict(r) for r in rows]

    async def get(self, plan_id: int) -> dict[str, Any] | None:
        factory = get_session_factory()
        async with factory() as session:
            row = await session.get(UserPlan, plan_id)
            return self._plan_dict(row) if row else None

    async def update(self, plan_id: int, payload: dict[str, Any]) -> dict[str, Any] | None:
        factory = get_session_factory()
        async with factory() as session:
            row = await session.get(UserPlan, plan_id)
            if not row:
                return None
            for key in (
                "name", "currency", "initial_capital", "monthly_income", "monthly_contribution",
                "weekly_contribution", "contribution_frequency", "start_date", "end_date",
                "cash_reserve", "max_single_contribution", "max_drawdown_tolerance",
                "strategy_code", "strategy_params", "notes",
            ):
                if key in payload and payload[key] is not None:
                    setattr(row, key, payload[key])
            row.updated_at = datetime.now(timezone.utc)
            await session.commit()
            return self._plan_dict(row)

    async def delete(self, plan_id: int) -> dict[str, Any]:
        """删除计划。

        必须连同它的交易流水一起清掉：plan_id 上没有外键 ON DELETE CASCADE，
        只删计划会把流水变成「指向已不存在计划的孤儿记录」，
        它们仍会被全局持仓/流水统计算进去 —— 明明删了计划，资产却一点没变。
        因此这里显式级联，并把删除数量如实返回。
        """
        factory = get_session_factory()
        async with factory() as session:
            row = await session.get(UserPlan, plan_id)
            if not row:
                return {"deleted": False, "transactions_removed": 0}
            txs = (await session.execute(
                select(UserTransaction).where(UserTransaction.plan_id == plan_id)
            )).scalars().all()
            removed = len(txs)
            for tx in txs:
                await session.delete(tx)
            await session.delete(row)
            await session.commit()
            return {"deleted": True, "transactions_removed": removed}

    # ---------------------------------------------------------------- 交易账本
    async def add_transaction(self, plan_id: int | None, payload: dict[str, Any]) -> dict[str, Any]:
        factory = get_session_factory()
        async with factory() as session:
            date_str = payload.get("date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
            ts = int(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
            price = float(payload["price"])
            amount_fiat = float(payload.get("amount_fiat") or 0)
            amount_btc = float(payload.get("amount_btc") or 0)
            side = payload.get("side", "buy")
            if amount_btc <= 0 and amount_fiat > 0 and price > 0:
                amount_btc = amount_fiat / price
            if amount_fiat <= 0 and amount_btc > 0:
                amount_fiat = amount_btc * price
            tx = UserTransaction(
                plan_id=plan_id,
                side=side,
                ts=ts,
                date=date_str,
                price=price,
                amount_btc=amount_btc,
                amount_fiat=amount_fiat,
                fee=float(payload.get("fee") or 0),
                currency=payload.get("currency", "CNY"),
                note=payload.get("note"),
                source=payload.get("source", "manual"),
            )
            session.add(tx)
            await session.commit()
            await session.refresh(tx)
            return {"id": tx.id, "date": tx.date, "price": tx.price, "amount_btc": round(tx.amount_btc, 8),
                    "amount_fiat": round(tx.amount_fiat, 2), "side": tx.side}

    async def transactions(self, plan_id: int | None = None, limit: int = 500) -> list[dict[str, Any]]:
        factory = get_session_factory()
        async with factory() as session:
            stmt = select(UserTransaction).order_by(desc(UserTransaction.ts)).limit(limit)
            if plan_id:
                stmt = stmt.where(UserTransaction.plan_id == plan_id)
            rows = (await session.execute(stmt)).scalars().all()
        return [
            {
                "id": r.id, "plan_id": r.plan_id, "date": r.date, "side": r.side,
                "price": r.price, "amount_btc": r.amount_btc, "amount_fiat": r.amount_fiat,
                "fee": r.fee, "currency": r.currency, "note": r.note, "source": r.source,
            }
            for r in rows
        ]

    async def delete_transaction(self, tx_id: int) -> bool:
        factory = get_session_factory()
        async with factory() as session:
            row = await session.get(UserTransaction, tx_id)
            if not row:
                return False
            await session.delete(row)
            await session.commit()
            return True

    # ---------------------------------------------------------------- 持仓与快照
    async def holdings(
        self, plan_id: int | None = None, usd_to_plan_rate: float | None = None
    ) -> dict[str, Any]:
        """基于真实交易记录计算持仓、平均成本与浮盈亏。

        币种口径：本地 K 线价格是 USD，交易记录却可以是 CNY/USD 任意一种（由用户录入时选择）。
        两者混在一起相加是没有意义的，而系统里没有任何可靠的实时汇率数据源
        （DataCategory 里没有 FX 类别），所以这里的策略是：
          * 不同币种混用时，明确标注 mixed_currency=True，并在 fx_conversion 里说明没有换算；
          * 只有当调用方显式给出 usd_to_plan_rate（1 USD = ? 计划币种）时才做换算，
            换算比例会原样回显，便于核对；
          * 绝不使用任何拍脑袋的固定汇率充当默认值。
        """
        factory = get_session_factory()
        async with factory() as session:
            stmt = select(UserTransaction).order_by(UserTransaction.ts)
            if plan_id:
                stmt = stmt.where(UserTransaction.plan_id == plan_id)
            txs = (await session.execute(stmt)).scalars().all()
            target_currency = "CNY"
            if plan_id is not None and (plan := await session.get(UserPlan, plan_id)) is not None:
                target_currency = plan.currency or "CNY"
            elif txs:
                target_currency = txs[0].currency or "CNY"

        def to_target_currency(amount_fiat: float, fee: float, currency: str) -> tuple[float, float]:
            """把一笔金额折成计划币种。没有汇率时原样返回，由上层如实标注。"""
            if currency == target_currency:
                return amount_fiat, fee
            if usd_to_plan_rate and usd_to_plan_rate > 0:
                return amount_fiat * usd_to_plan_rate, fee * usd_to_plan_rate
            return amount_fiat, fee

        currencies = sorted({str(tx.currency or "CNY") for tx in txs})
        mixed_currency = len(currencies) > 1 or (
            len(currencies) == 1 and currencies[0] != target_currency
        )
        converted = bool(usd_to_plan_rate and usd_to_plan_rate > 0) and mixed_currency

        total_btc = 0.0
        invested = 0.0      # 累计投入本金
        realized = 0.0
        cost_basis = 0.0    # 当前仍持有的这部分 BTC 对应的成本（卖出时按比例结转）
        for tx in txs:
            if tx.side == "buy":
                total_btc += tx.amount_btc
                fiat, tx_fee = to_target_currency(tx.amount_fiat, tx.fee, tx.currency or "CNY")
                spent = fiat + tx_fee
                invested += spent
                cost_basis += spent
            else:
                sold = min(tx.amount_btc, total_btc) if total_btc > 0 else 0.0
                fiat, tx_fee = to_target_currency(tx.amount_fiat, tx.fee, tx.currency or "CNY")
                if total_btc > 0 and sold > 0:
                    # 卖出部分是这一段持仓对应的成本，必须从「已实现盈亏」里扣除。
                    # 早期实现把卖出毛收入当成已实现盈亏（realized += 收入），
                    # 于是按原价卖一半会被记成赚了 25000，盈亏完全失真。
                    cost_of_sold = cost_basis * (sold / total_btc)
                    realized += fiat - tx_fee - cost_of_sold
                    # 剩余持仓按同比例结转成本基数。
                    # 不结转的话有卖单后均价被系统性抬高、浮盈被系统性低估。
                    cost_basis -= cost_of_sold
                else:
                    realized += fiat - tx_fee
                total_btc -= tx.amount_btc

        # 极端情况（如记录缺卖出前的买入）下持仓为负，归零以显示真实可用余额
        if total_btc < 0:
            total_btc = 0.0
            cost_basis = 0.0
        avg_cost = (cost_basis / total_btc) if total_btc > 0 else 0.0

        # 当前市值（使用本地最近一次可信价格）
        async with factory() as session:
            candles = await latest_candles(session, "BTC", "1d", 1)
        price = candles[-1]["close"] if candles else None
        price_provider = candles[-1]["provider"] if candles else ""

        # 市值按本地最新收评价 USD 计；折算成计划币种同样需要调用方提供汇率
        market_value_usd = total_btc * price if (price and total_btc) else 0.0
        market_value = market_value_usd
        price_in_plan = price
        if price and converted:
            market_value = market_value_usd * usd_to_plan_rate
            price_in_plan = price * usd_to_plan_rate
        unrealized = market_value - cost_basis

        return {
            "plan_id": plan_id,
            "currency": target_currency,
            "currencies_in_records": currencies,
            "mixed_currency": mixed_currency,
            "fx_conversion": {
                "applied": converted,
                "rate_used": round(usd_to_plan_rate, 6) if converted else None,
                "message": (
                    f"已按 1 USD = {usd_to_plan_rate} {target_currency} 换算"
                    if converted else
                    ("记录中存在非本位币交易（" + "/".join(currencies) + "），系统没有可靠汇率数据源，"
                     "因此未做换算 —— 金额相加存在口径混用，请勿据此直接做资金决策。")
                ),
            },
            "total_btc": round(total_btc, 8),
            "total_invested": round(invested, 2),
            "cost_basis": round(cost_basis, 2),
            "realized_pnl": round(realized, 2),
            "avg_cost": round(avg_cost, 2),
            "current_price": price,
            "current_price_in_plan_currency": round(price_in_plan, 2) if price_in_plan else None,
            "price_currency": "USD",
            "price_provider": price_provider,
            "market_value": round(market_value, 2),
            "unrealized_pnl": round(unrealized, 2),
            "total_value": round(market_value + realized, 2),
            "roi": round((market_value - invested) / invested * 100, 2) if invested > 0 else 0.0,
            "transaction_count": len(txs),
            "quality": QualityStatus.VERIFIED.value if candles else QualityStatus.MISSING.value,
            "note": "默认手动记录，不接入任何交易 API，系统不会自动下单。",
        }

    async def snapshot(self, plan_id: int | None = None) -> dict[str, Any]:
        """记录组合快照：同一自然日只保留一条（后一次的估值覆盖前一次）。

        不去重的话，页面上每点一次「记录快照」就多一行同日不同值的记录，
        画出来的净值曲线会出现同一天多个互相矛盾的点位 —— 看起来像 bug，实际是数据重复。
        """
        holdings = await self.holdings(plan_id)
        # 以 UTC 自然日为桶：跨时区的机器上同一天不会因为时区差被拆成两天
        day_bucket = int(datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp())
        factory = get_session_factory()
        async with factory() as session:
            existing = (await session.execute(
                select(PortfolioSnapshot).where(
                    PortfolioSnapshot.plan_id == plan_id,
                    PortfolioSnapshot.date >= day_bucket,
                    PortfolioSnapshot.date < day_bucket + 86400,
                ).order_by(desc(PortfolioSnapshot.date))
            )).scalars().first()
            mode = "updated" if existing else "created"
            row = existing or PortfolioSnapshot(plan_id=plan_id, date=day_bucket)
            row.market_value = holdings["market_value"]
            row.invested = holdings["total_invested"]
            row.unrealized_pnl = holdings["unrealized_pnl"]
            row.roi = holdings["roi"]
            row.price_used = holdings["current_price"] or 0.0
            row.price_provider = holdings["price_provider"]
            if not existing:
                session.add(row)
            await session.commit()
        return {"ok": True, "mode": mode, "date": day_bucket, "snapshot": holdings}

    # ---------------------------------------------------------------- 计划建议
    async def next_contribution(self, plan_id: int) -> dict[str, Any]:
        """计算下次投入时间与建议金额（基于当前策略规则，不构成投资建议）。"""
        plan = await self.get(plan_id)
        if not plan:
            return {"available": False, "message": "计划不存在"}

        factory = get_session_factory()
        async with factory() as session:
            candles = await latest_candles(session, "BTC", "1d", 500)
        if not candles:
            return {"available": False, "message": "本地历史数据不足"}

        closes = [c["close"] for c in candles]
        now_price = closes[-1]
        ath = max(closes)
        drawdown_pct = (now_price - ath) / ath * 100
        percentile = sum(1 for c in closes if c <= now_price) / len(closes) * 100

        ctx = StrategyContext(
            ts=int(candles[-1]["ts"]),
            price=now_price,
            drawdown_pct=drawdown_pct,
            ath=ath,
            price_percentile=percentile,
        )
        multiplier = multiplier_for(plan["strategy_code"], plan["strategy_params"], ctx)
        base = plan["weekly_contribution"] if plan["contribution_frequency"] == "weekly" else plan["monthly_contribution"]
        suggested = base * multiplier
        if plan["max_single_contribution"] > 0:
            suggested = min(suggested, plan["max_single_contribution"])

        # 下次投入日期（从最后一次交易推算）
        txs = await self.transactions(plan_id, limit=1)
        if txs:
            last_ts = int(datetime.strptime(txs[0]["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
            step = 7 * DAY if plan["contribution_frequency"] == "weekly" else 30 * DAY
            next_ts = last_ts + step
        else:
            start_raw = plan.get("start_date")
            if not start_raw:
                return {"available": False, "message": "计划没有设置起始日期，无法推算下次投入"}
            next_ts = int(datetime.strptime(start_raw, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
        next_date_str = datetime.fromtimestamp(next_ts, tz=timezone.utc).strftime("%Y-%m-%d")

        warnings: list[str] = []
        guards: dict[str, Any] = {}

        # ---- end_date：计划到期后不应继续给出「下一笔」建议
        plan_status = "active"
        end_raw = plan.get("end_date")
        if end_raw:
            end_ts = int(datetime.strptime(end_raw, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
            if next_ts > end_ts:
                plan_status = "ended"
                warnings.append(
                    f"计划已于 {end_raw} 到期（下次投入推算日 {next_date_str} 超出截止日），"
                    "系统不再建议继续加码；如需继续请延长或修改计划的截止日期。"
                )

        # ---- max_drawdown_tolerance：回撤超过自己设定的容忍度时提示
        beyond_tolerance = False
        if plan.get("max_drawdown_tolerance", 0) > 0:
            beyond_tolerance = drawdown_pct < -float(plan["max_drawdown_tolerance"])
            guards["max_drawdown_tolerance"] = plan["max_drawdown_tolerance"]
            guards["drawdown_within_tolerance"] = not beyond_tolerance
            if beyond_tolerance:
                warnings.append(
                    f"当前回撤 {drawdown_pct:.1f}% 已超过你设定的容忍度 "
                    f"{plan['max_drawdown_tolerance']:.0f}%，是否继续加码请自行判断；系统不会替你决定。"
                )

        # ---- 初始资金口径：进度只对初始本金有意义
        if plan.get("initial_capital", 0) > 0:
            guards["initial_capital"] = plan["initial_capital"]

        # ---- 现金流安全垫：月收入扣除留存的钱才是可投金额上限（如实给出，不替用户削减建议额）
        monthly_income = float(plan.get("monthly_income") or 0)
        cash_reserve = float(plan.get("cash_reserve") or 0)
        if monthly_income > 0:
            guards["monthly_income"] = monthly_income
            guards["cash_reserve"] = cash_reserve
            guards["disposable"] = round(monthly_income - cash_reserve, 2)
            if cash_reserve > 0 and suggested > monthly_income - cash_reserve:
                warnings.append(
                    f"本期建议 {suggested:,.2f} {plan['currency']} 高于「月收入 {monthly_income:,.0f} "
                    f"- 留存 {cash_reserve:,.0f}」的可投余额 {monthly_income - cash_reserve:,.2f}，"
                    "金额已照实给出，是否执行取决于你的现金流安排。"
                )

        # ---- 已投入进度（用于判断离初始本金的差距，只读展示）
        invested_now = await self.holdings(plan_id)
        if float(plan.get("initial_capital") or 0) > 0:
            guards["invested_to_date"] = invested_now.get("total_invested", 0.0)

        return {
            "available": True,
            "plan_id": plan_id,
            "plan_name": plan["name"],
            "plan_status": plan_status,
            "base_amount": base,
            "multiplier": round(multiplier, 2),
            "suggested_amount": round(suggested, 2),
            "currency": plan["currency"],
            "next_date": next_date_str,
            "beyond_drawdown_tolerance": beyond_tolerance,
            "guards": guards,
            "warnings": warnings,
            "reason": {
                "drawdown_pct": round(drawdown_pct, 2),
                "price_percentile": round(percentile, 1),
                "strategy": plan["strategy_code"],
            },
            "disclaimer": "建议金额由你设定的策略规则与计划参数计算得出，仅供参考，不构成投资建议。",
        }

    def strategies(self) -> list[dict[str, Any]]:
        return list_strategies()

    @staticmethod
    def _plan_dict(row: UserPlan) -> dict[str, Any]:
        return {
            "id": row.id,
            "name": row.name,
            "currency": row.currency,
            "initial_capital": row.initial_capital,
            "monthly_income": row.monthly_income,
            "monthly_contribution": row.monthly_contribution,
            "weekly_contribution": row.weekly_contribution,
            "contribution_frequency": row.contribution_frequency,
            "start_date": row.start_date,
            "end_date": row.end_date,
            "cash_reserve": row.cash_reserve,
            "max_single_contribution": row.max_single_contribution,
            "max_drawdown_tolerance": row.max_drawdown_tolerance,
            "strategy_code": row.strategy_code,
            "strategy_params": row.strategy_params,
            "notes": row.notes,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
