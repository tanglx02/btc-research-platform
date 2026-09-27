# -*- coding: utf-8 -*-
"""预警执行管线：一次取数 → 评估全部规则 → 状态机 → 事件落库 → 通知 → 重试。

这一层是需求里若干「不许」的落地处：

* **不许每条规则各请求一次 Provider**：所有规则共用同一个 ``MetricContext``；
  数据层的每个模块在 ``build_context()`` 里只取一次，指标在上下文里有解析缓存。
* **不许丢提醒**：事件先落库（PENDING），再发通知；发送成功才改成 SENT。
  发送失败也留 FAILED + error_message，并被重试任务捞起来继续发。
* **不许用 stale 数据触发关键提醒**：``require_fresh`` 与 ``min_quality`` 在任何
  通知动作之前拦截，并把拦截原因如实写进事件。
* **不许重复邮件**：冷却 + 状态机双保险；冷却期内条件持续满足不会重复通知。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.cache import get_cache
from ..db.base import get_session_factory
from ..db.models import (
    AlertCooldown,
    AlertEvent,
    AlertNotificationChannel,
    AlertNotificationLog,
    AlertRule,
)
from ..core.errors import AllProvidersFailedError
from ..services.market_service import MarketService
from . import rules as rules_mod
from .catalog import MetricContext
from .engine import QUALITY_CHOICES, EvalOutcome, RuleEngine, quality_rank
from .notifier import (
    CHANNEL_COOLDOWN_SECONDS,
    CHANNEL_FAILURE_THRESHOLD,
    MAX_SEND_ATTEMPTS,
    build_provider,
    load_smtp_config,
)
from .templates import build_body, build_subject

logger = logging.getLogger(__name__)

# 数据新鲜度上限（秒）。超过这个时间的数据视为过期，按 require_fresh 处理。
FRESH_LIMITS: dict[str, int] = {
    "price": 60 * 30,          # 价格：30 分钟
    "change_1h": 60 * 90,
    "default": 60 * 60 * 26,   # 日线类：26 小时
}


@dataclass
class ContextBundle:
    """一次评估所需的全部数据 + 元信息（谁提供的、质量如何、什么时候的）。"""

    ctx: MetricContext
    providers_used: list[str] = field(default_factory=list)
    used_fallback: bool = False
    quality: str = "SINGLE_SOURCE"
    data_updated_at: datetime | None = None
    module_errors: dict[str, str] = field(default_factory=dict)
    market_context: dict[str, Any] = field(default_factory=dict)
    provider_chain: str = ""


# ------------------------------------------------------------------ 取数

async def build_context(*, need_personal: bool = False) -> ContextBundle:
    """一次性收集所有模块数据，构造共享求值上下文。

    这里只做「取已有数据」，不直接调用任何 ``request``。第三方请求全部由
    ResilientRouter（含主备切换）与采集层负责，本函数只做聚合。
    """
    svc = MarketService()
    bundle = ContextBundle(ctx=MetricContext())
    ctx = bundle.ctx

    # --- 日线 + 小时线（本地优先）
    try:
        daily = await svc.candles("BTC", "1d", 4000)
        ctx.candles = daily.get("candles") or []
        if daily.get("source", {}).get("providers_used_historically"):
            bundle.providers_used.extend(daily["source"]["providers_used_historically"])
    except Exception as exc:  # noqa: BLE001
        bundle.module_errors["candles"] = str(exc)

    try:
        hourly = await svc.candles("BTC", "1h", 1000)
        ctx.intraday = hourly.get("candles") or []
    except Exception as exc:  # noqa: BLE001
        bundle.module_errors["intraday"] = str(exc)

    # --- 指标（纯本地计算）
    try:
        ind = await svc.indicators("BTC", "1d", 4000)
        ctx.indicators = ind if ind.get("available") else {}
        if not ind.get("available"):
            bundle.module_errors["indicators"] = ind.get("message", "指标不可用")
    except Exception as exc:  # noqa: BLE001
        bundle.module_errors["indicators"] = str(exc)

    # --- 实时价格（走 ResilientRouter，主备切换由它负责）
    try:
        price_block = await svc.current_price("BTC")
        ctx.price_block = price_block
        if price_block.get("available"):
            src = price_block.get("source") or {}
            if src.get("provider"):
                bundle.providers_used.append(str(src["provider"]))
            if src.get("used_fallback"):
                bundle.used_fallback = True
            q = str(src.get("quality") or "SINGLE_SOURCE")
            bundle.quality = _worse(bundle.quality, q)
            ts = _parse_ts(src.get("observation_time") or src.get("fetch_time"))
            if ts:
                bundle.data_updated_at = ts
    except Exception as exc:  # noqa: BLE001
        bundle.module_errors["price"] = str(exc)

    # --- 综合分析（估值/周期/风险/regime）
    try:
        analysis = await svc.analysis()
        ctx.analysis = analysis if analysis.get("available") else {}
        if not analysis.get("available"):
            bundle.module_errors["analysis"] = analysis.get("message", "综合分析不可用")
        else:
            bundle.market_context = _market_context(analysis)
            meta = analysis.get("meta") or {}
            if meta.get("bars_used"):
                bundle.market_context["bars_used"] = meta["bars_used"]
    except Exception as exc:  # noqa: BLE001
        bundle.module_errors["analysis"] = str(exc)

    # --- 链上 / 衍生品 / ETF / 情绪 / 宏观
    for name, coro in (
        ("onchain", _safe(svc.onchain)),
        ("derivatives", _safe(svc.derivatives)),
        ("etf", _safe(svc.etf)),
        ("sentiment", _safe(svc.sentiment)),
        ("macro", _safe(svc.macro)),
    ):
        try:
            block = await coro
            if block is None:
                bundle.module_errors[name] = "服务未提供该模块"
                continue
            setattr(ctx, name, block)
            if not block.get("available", True):
                bundle.module_errors[name] = block.get("message", "模块不可用")
            src = block.get("source") or {}
            if isinstance(src, dict) and src.get("provider"):
                bundle.providers_used.append(str(src["provider"]))
        except Exception as exc:  # noqa: BLE001
            bundle.module_errors[name] = str(exc)

    # --- 个人计划（仅当规则需要时才查，避免无谓开销）
    if need_personal:
        try:
            ctx.personal = await _personal_snapshot()
        except Exception as exc:  # noqa: BLE001
            bundle.module_errors["personal"] = str(exc)

    # 去重、保序
    seen: set[str] = set()
    bundle.providers_used = [p for p in bundle.providers_used if p and not (p in seen or seen.add(p))]
    bundle.provider_chain = " -> ".join(bundle.providers_used[:5]) or "unknown"
    return bundle


async def _safe(fn):
    """把同步取函数包成协程，并在不支持时返回 None 而不是炸掉整轮。"""
    try:
        return await fn()
    except AttributeError:
        return None
    except AllProvidersFailedError as exc:
        return {"available": False, "message": f"所有数据源均不可用：{exc}", "source": {}}


def _worse(a: str, b: str) -> str:
    """取两者里较差的数据质量。"""
    return a if quality_rank(a) <= quality_rank(b) else b


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _market_context(analysis: dict[str, Any]) -> dict[str, Any]:
    val = analysis.get("valuation") or {}
    cyc = analysis.get("cycle") or {}
    risk = analysis.get("risk") or {}
    reg = analysis.get("regime") or {}
    return {
        "valuation_state": val.get("state"),
        "valuation_state_cn": val.get("state_cn") or val.get("label"),
        "cycle_phase": cyc.get("phase"),
        "cycle_phase_cn": cyc.get("phase_cn") or cyc.get("label"),
        "risk_level": risk.get("level"),
        "risk_score": risk.get("overall_risk"),
        "regime": reg.get("state") or reg.get("label"),
    }


async def _personal_snapshot() -> dict[str, Any]:
    """个人资金计划的当前快照（只读）。"""
    from ..db.models import UserPlan
    from ..services.plan_service import PlanService

    factory = get_session_factory()
    async with factory() as session:
        plan = (
            await session.execute(select(UserPlan).order_by(desc(UserPlan.id)).limit(1))
        ).scalars().first()
        if plan is None:
            return {"available": False, "message": "还没有创建资金计划"}
        svc = PlanService()
        try:
            holdings = await svc.holdings(session, plan.id)  # type: ignore[arg-type]
        except TypeError:
            holdings = await svc.holdings(plan.id)  # type: ignore[call-arg]
        try:
            nxt = await svc.next_contribution(session, plan.id)  # type: ignore[arg-type]
        except TypeError:
            nxt = await svc.next_contribution(plan.id)  # type: ignore[call-arg]
        return {
            "available": True,
            "plan_id": plan.id,
            "plan_name": plan.name,
            "holdings": holdings,
            "next_contribution": nxt,
            "initial_capital": getattr(plan, "initial_capital", None),
            "monthly_income": getattr(plan, "monthly_income", None),
            "cash_reserve": getattr(plan, "cash_reserve", None),
            "max_drawdown_tolerance": getattr(plan, "max_drawdown_tolerance", None),
        }


# ------------------------------------------------------------------ 评估

class AlertPipeline:
    """规则求值与通知的执行者。"""

    def __init__(self) -> None:
        self.engine = RuleEngine()
        self.cache = get_cache()

    async def run_cycle(self, *, force: bool = False, rule_ids: list[int] | None = None) -> dict[str, Any]:
        """跑一轮完整的检测。Scheduler 周期性调用它。"""
        started = datetime.now(timezone.utc)
        factory = get_session_factory()

        async with factory() as session:
            stmt = select(AlertRule).where(AlertRule.enabled.is_(True), AlertRule.paused.is_(False))
            if rule_ids:
                stmt = stmt.where(AlertRule.id.in_(rule_ids))
            all_rules = list((await session.execute(stmt)).scalars().all())

            due = [r for r in all_rules if force or self._is_due(r, started)]
            if not due:
                return {
                    "started_at": started.isoformat(),
                    "checked": 0,
                    "total_rules": len(all_rules),
                    "message": "本轮没有到期的规则",
                    "events": [],
                }

            need_personal = any(r.plan_id for r in due)
            bundle = await build_context(need_personal=need_personal)

            results: list[dict[str, Any]] = []
            for rule in due:
                try:
                    results.append(await self._evaluate_rule(session, rule, bundle, started))
                except Exception as exc:  # noqa: BLE001 - 单条规则出错不应影响其他规则
                    logger.exception("规则 %s 求值失败", rule.id)
                    rule.last_error = f"{type(exc).__name__}: {exc}"
                    rule.last_checked_at = started
                    results.append({"rule_id": rule.id, "rule_name": rule.name, "error": rule.last_error})

            await session.commit()

        due_events = [r for r in results if r.get("event_id")]
        # 事件已落库 -> 再发通知（顺序不能反，否则发信失败会丢掉提醒）
        if due_events:
            await self.dispatch_pending(limit=200)

        return {
            "started_at": started.isoformat(),
            "checked": len(due),
            "total_rules": len(all_rules),
            "triggered": len(due_events),
            "module_errors": bundle.module_errors,
            "providers_used": bundle.providers_used,
            "used_fallback": bundle.used_fallback,
            "data_quality": bundle.quality,
            "results": results,
        }

    @staticmethod
    def _is_due(rule: AlertRule, now: datetime) -> bool:
        if rule.last_checked_at is None:
            return True
        last = rule.last_checked_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return (now - last).total_seconds() >= max(30, rule.check_interval_seconds or 60)

    async def _evaluate_rule(self, session: AsyncSession, rule: AlertRule,
                             bundle: ContextBundle, now: datetime) -> dict[str, Any]:
        tree = await rules_mod.load_tree(session, rule.id)
        if not tree:
            rule.last_checked_at = now
            rule.last_error = "规则没有任何条件，已跳过"
            return {"rule_id": rule.id, "rule_name": rule.name, "skipped": "no_conditions"}

        prev_state = self._prev_state(rule)
        outcome = self.engine.evaluate(tree, rule.logic or "AND", bundle.ctx, prev_state=prev_state)  # type: ignore[arg-type]

        rule.last_checked_at = now
        rule.last_error = None
        self._persist_eval_state(rule, outcome, prev_state, now)

        was_active = rule.state in ("TRIGGERED", "COOLDOWN")
        out: dict[str, Any] = {
            "rule_id": rule.id,
            "rule_name": rule.name,
            "satisfied": outcome.satisfied,
            "reason": outcome.reason,
            "unavailable": sorted(set(outcome.unavailable)),
            "state_before": rule.state,
        }

        if outcome.satisfied:
            gate_ok, gate_reason = self._quality_gate(rule, bundle)
            if not gate_ok:
                # 需求十七/十五：质量不足不发关键提醒；如果之前是触发态，反而要报「恢复」
                rule.state = "NORMAL" if not was_active else rule.state
                out["suppressed"] = gate_reason
                rule.last_eval = self._eval_payload(outcome, bundle, suppressed=gate_reason)
                return out

            if not was_active and not self._in_cooldown(rule, now):
                event = await self._emit_event(session, rule, outcome, bundle, "triggered", now)
                rule.state = "TRIGGERED"
                rule.last_trigger_at = now
                rule.trigger_count = (rule.trigger_count or 0) + 1
                expiry = now + timedelta(seconds=max(60, rule.cooldown_seconds or 86400))
                rule.cooldown_until = expiry
                session.add(AlertCooldown(rule_id=rule.id, event_id=event.id, started_at=now,
                                          expires_at=expiry, reason="触发后进入冷却，避免重复邮件"))
                out["event_id"] = event.id
                out["triggered"] = True
                rule.last_eval = self._eval_payload(outcome, bundle)
            else:
                if was_active:
                    rule.state = "COOLDOWN" if self._in_cooldown(rule, now) else rule.state
                    out["suppressed"] = "冷却中或已处于触发态，不重复通知"
                else:
                    out["suppressed"] = "冷却期未结束"
                rule.last_eval = self._eval_payload(outcome, bundle, suppressed=out.get("suppressed"))
        else:
            if was_active:
                was_triggered_before = rule.last_trigger_at is not None
                rule.state = "RECOVERED" if rule.notify_on_recover else "NORMAL"
                rule.last_recovered_at = now
                rule.cooldown_until = None
                out["recovered"] = True
                if rule.notify_on_recover:
                    event = await self._emit_event(session, rule, outcome, bundle, "recovered", now)
                    out["event_id"] = event.id
                if was_triggered_before:
                    # 恢复后允许再次触发：把连续计数复位，冷却清空
                    rule.consecutive_hits = 0
                    rule.condition_since = None
                rule.state = rule.state if rule.notify_on_recover else "NORMAL"
            else:
                rule.state = "NORMAL"
            rule.last_eval = self._eval_payload(outcome, bundle)

        out["state_after"] = rule.state
        return out

    # -------------------------------------------------- 状态机

    @staticmethod
    def _prev_state(rule: AlertRule) -> dict[str, Any]:
        """把上一轮的「连续次数 / 起算时间」还原出来给引擎用。"""
        st: dict[str, Any] = {}
        if rule.condition_since:
            st["__since__"] = rule.condition_since.isoformat()
        if rule.consecutive_hits:
            st["__hits__"] = rule.consecutive_hits
        last = rule.last_eval or {}
        for item in last.get("conditions") or []:
            cid = item.get("condition_id")
            if cid is None:
                continue
            if item.get("consecutive_hits") is not None:
                st[f"hits_{cid}"] = item["consecutive_hits"]
            if item.get("since"):
                st[f"since_{cid}"] = item["since"]
            if item.get("actual") is not None:
                st[f"prev_{cid}"] = item["actual"]
        return st

    @staticmethod
    def _persist_eval_state(rule: AlertRule, outcome: EvalOutcome,
                            prev_state: dict[str, Any], now: datetime) -> None:
        hits = int(prev_state.get("__hits__", 0))
        rule.consecutive_hits = hits + 1 if outcome.satisfied else 0
        if outcome.satisfied:
            if not rule.condition_since:
                rule.condition_since = now
        else:
            rule.condition_since = None

    @staticmethod
    def _in_cooldown(rule: AlertRule, now: datetime) -> bool:
        if not rule.cooldown_until:
            return False
        until = rule.cooldown_until
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return until > now

    # -------------------------------------------------- 数据质量门槛

    @staticmethod
    def _quality_gate(rule: AlertRule, bundle: ContextBundle) -> tuple[bool, str]:
        """需求十五 / 十七：数据不合格就不发关键提醒。"""
        actual = bundle.quality or "SINGLE_SOURCE"

        if rule.require_fresh:
            # 数据被标记为 STALE，本身就代表「这是上一次成功获取的旧值」，
            # 不允许用它触发关键提醒（需求明确禁止用过期数据触发）。
            if str(actual).upper() == "STALE":
                return False, (
                    "当前取到的是降级数据（数据源暂时不可用，显示的是最后一次成功获取的值）。"
                    "本条规则要求数据新鲜，因此不触发提醒。"
                )
            if bundle.data_updated_at is not None:
                limit = FRESH_LIMITS["default"]
                if "price" in (bundle.ctx.metric_sources or {}):
                    limit = FRESH_LIMITS["price"]
                age = (datetime.now(timezone.utc) - bundle.data_updated_at).total_seconds()
                if age > limit:
                    return False, (
                        f"数据已过期：最后一次有效更新在 {int(age / 60)} 分钟前，"
                        f"超过 {int(limit / 60)} 分钟的新鲜度上限。不会用过期数据触发提醒。"
                    )

        if rule.require_multi_source and quality_rank(actual) < quality_rank("VERIFIED"):
            return False, (
                f"本条规则要求至少两个独立数据源确认，当前数据质量为 {actual}，未达到要求。"
            )

        required = rule.min_quality or "SINGLE_SOURCE"
        if quality_rank(actual) < quality_rank(required):
            return False, f"当前数据质量为 {actual}，低于本条规则要求的 {required}。"
        return True, ""

    # -------------------------------------------------- 事件

    async def _emit_event(self, session: AsyncSession, rule: AlertRule, outcome: EvalOutcome,
                          bundle: ContextBundle, event_type: str, now: datetime) -> AlertEvent:
        event = AlertEvent(
            rule_id=rule.id,
            rule_name=rule.name,
            severity=rule.severity or "WARNING",
            event_type=event_type,
            trigger_time=now,
            metric_values=outcome.metric_values(),
            threshold=outcome.thresholds(),
            condition_result={"reason": outcome.reason,
                              "results": self._conditions_payload(outcome)},
            market_context=bundle.market_context,
            provider=bundle.providers_used[0] if bundle.providers_used else None,
            provider_chain=bundle.provider_chain,
            used_fallback=bundle.used_fallback,
            data_quality=bundle.quality,
            data_updated_at=bundle.data_updated_at,
            notification_status="PENDING",
        )
        session.add(event)
        await session.flush()
        return event

    @staticmethod
    def _conditions_payload(outcome: EvalOutcome) -> list[dict[str, Any]]:
        prev = {}
        return [r.to_dict() for r in outcome.results]

    def _eval_payload(self, outcome: EvalOutcome, bundle: ContextBundle,
                      suppressed: str | None = None) -> dict[str, Any]:
        return {
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
            "satisfied": outcome.satisfied,
            "reason": outcome.reason,
            "suppressed": suppressed,
            "conditions": [r.to_dict() for r in outcome.results],
            "metric_sources": dict(bundle.ctx.metric_sources),
            "metric_quality": dict(bundle.ctx.metric_quality),
            "providers_used": bundle.providers_used,
            "used_fallback": bundle.used_fallback,
            "data_quality": bundle.quality,
        }

    # -------------------------------------------------- 通知

    async def dispatch_pending(self, limit: int = 100) -> dict[str, Any]:
        """把还没发出去的提醒发掉（含重试）。"""
        factory = get_session_factory()
        sent = failed = 0
        async with factory() as session:
            stmt = (
                select(AlertEvent)
                .where(AlertEvent.notification_status.in_(("PENDING", "FAILED")),
                       AlertEvent.retry_count < MAX_SEND_ATTEMPTS)
                .order_by(AlertEvent.id.asc())
                .limit(limit)
            )
            events = list((await session.execute(stmt)).scalars().all())
            for event in events:
                event.retry_count = (event.retry_count or 0) + 1
                ok = await self._send_event(session, event)
                sent += 1 if ok else 0
                failed += 0 if ok else 1
            await session.commit()
        return {"attempted": len(events), "sent": sent, "failed": failed}

    async def _send_event(self, session: AsyncSession, event: AlertEvent) -> bool:
        channels = await self._channels_for(session, event.rule_id)
        if not channels:
            event.notification_status = "SKIPPED"
            event.error_message = "没有配置可用的通知渠道（请先在预警中心配置 SMTP）"
            event.retry_count = (event.retry_count or 0) + 1
            return False

        rule = await session.get(AlertRule, event.rule_id)
        # 邮件模板需要规则说明与「当时的市场状态」，这里组装成一个普通 dict，
        # 保证通知层与数据库模型解耦（模板只认字段名，不认 ORM）。
        event_dict = {
            "rule_name": event.rule_name,
            "severity": event.severity,
            "event_type": event.event_type,
            "trigger_time": event.trigger_time,
            "description": rule.description if rule else None,
            "condition_result": event.condition_result,
            "market_context": {
                **(event.market_context or {}),
                "provider": event.provider,
                "provider_chain": event.provider_chain,
                "used_fallback": event.used_fallback,
                "data_quality": event.data_quality,
                "data_updated_at": event.data_updated_at.isoformat() if event.data_updated_at else None,
            },
        }
        subjects_logged: set[str] = set()
        any_success = False

        for ch in channels:
            now = datetime.now(timezone.utc)
            if ch.disabled_until and ch.disabled_until > now:
                continue
            provider = build_provider(ch.channel_type, (ch.config or {}))
            if provider is None:
                continue
            avail, why = provider.available()
            if not avail:
                session.add(AlertNotificationLog(
                    event_id=event.id, rule_id=event.rule_id,
                    channel_type=ch.channel_type, target=ch.target,
                    status="FAILED", attempt=1, subject=None,
                    error_message=f"渠道不可用：{why}",
                ))
                event.notification_status = "FAILED"
                event.error_message = f"渠道不可用：{why}"
                continue

            event.notification_status = "SENDING"
            await session.flush()

            msg = self._build_message(rule, event_dict, ch)
            attempts = 0
            last_err = None
            sent_ok = False
            while attempts < MAX_SEND_ATTEMPTS:
                attempts += 1
                result = await asyncio.to_thread(provider.send, msg)
                session.add(AlertNotificationLog(
                    event_id=event.id, rule_id=event.rule_id,
                    channel_type=ch.channel_type, target=ch.target,
                    status="SENT" if result.ok else "FAILED",
                    attempt=attempts, subject=msg.subject,
                    smtp_response=result.smtp_response, error_message=result.error,
                    duration_ms=result.duration_ms,
                ))
                if result.ok:
                    sent_ok = True
                    any_success = True
                    ch.consecutive_failures = 0
                    ch.last_error = None
                    ch.last_success_at = datetime.now(timezone.utc)
                    event.notification_status = "SENT"
                    event.email_sent_at = datetime.now(timezone.utc)
                    event.email_message_id = result.message_id
                    event.error_message = None
                    subjects_logged.add(msg.subject)
                    break
                last_err = result.error
                # 认证 / 收件人被拒属于配置问题，重试没有意义，直接留给人工处理
                if _is_fatal(result.error):
                    break
                await asyncio.sleep(min(2 ** attempts, 8))

            if not sent_ok:
                ch.consecutive_failures = (ch.consecutive_failures or 0) + 1
                ch.last_error = last_err
                event.notification_status = "FAILED"
                event.error_message = last_err
                # 注意：这里**不**累计到 retry_count。retry_count 表示「后续补发轮次」，
                # 上面 while 里的 attempts 只是「同一时刻的连续尝试」。
                # 如果把两者混在一起，一次瞬时故障就会把补发预算用光，
                # 用户第二天网络恢复了也收不到这封提醒 —— 那就等于丢了提醒。
                if ch.consecutive_failures >= CHANNEL_FAILURE_THRESHOLD:
                    ch.disabled_until = datetime.now(timezone.utc) + timedelta(seconds=CHANNEL_COOLDOWN_SECONDS)
                    # 需求二十八：连续失败必须在日志里明确提示，不能静默
                    logger.error(
                        "通知渠道 %s 连续失败 %d 次，已暂停发送 %d 秒。最后一次错误：%s",
                        ch.channel_type, ch.consecutive_failures, CHANNEL_COOLDOWN_SECONDS, last_err,
                    )
            await session.flush()

        return any_success

    @staticmethod
    def _build_message(rule: AlertRule | None, event_dict: dict[str, Any],
                       ch: AlertNotificationChannel):
        from .notifier import OutgoingMessage

        try:
            from ..core.config import get_settings
            site = getattr(get_settings(), "public_base_url", "") or ""
        except Exception:  # noqa: BLE001
            site = ""

        subject = build_subject(event_dict.get("rule_name") or "监测提醒",
                                event_dict.get("severity") or "WARNING",
                                event_dict.get("event_type") or "triggered")
        text, html = build_body(event_dict, site_url=site)
        cfg = load_smtp_config()
        targets = [t.strip() for t in (ch.target or "").split(",") if t.strip()]
        if not targets:
            targets = list(cfg.recipients)
        return OutgoingMessage(
            subject=subject, text_body=text, html_body=html,
            to=targets, severity=event_dict.get("severity") or "WARNING",
        )

    async def _channels_for(self, session: AsyncSession,
                            rule_id: int) -> list[AlertNotificationChannel]:
        """规则专属渠道优先；没有就用全局渠道。"""
        rows = list((await session.execute(
            select(AlertNotificationChannel).where(
                AlertNotificationChannel.enabled.is_(True),
                AlertNotificationChannel.rule_id == rule_id,
            )
        )).scalars().all())
        if rows:
            return rows
        rows = list((await session.execute(
            select(AlertNotificationChannel).where(
                AlertNotificationChannel.enabled.is_(True),
                AlertNotificationChannel.rule_id.is_(None),
            )
        )).scalars().all())
        if rows:
            return rows
        # 库里没有渠道记录：用环境变量里的 SMTP 配置兜一个（保证「配了就能用」）
        cfg = load_smtp_config()
        if cfg.configured:
            ch = AlertNotificationChannel(
                rule_id=None, channel_type="email",
                target=",".join(cfg.recipients), config={"from_env": True}, enabled=True,
            )
            session.add(ch)
            await session.flush()
            return [ch]
        return []

    # -------------------------------------------------- 规则测试 / 试运行

    async def test_rule(self, rule_payload: dict[str, Any]) -> dict[str, Any]:
        """需求三十二：立刻告知「现在是否成立」。不写任何状态。"""
        data = rules_mod.validate_rule(rule_payload)
        groups_data = data["groups"]
        need_personal = bool(data.get("plan_id"))
        bundle = await build_context(need_personal=need_personal)

        # 把校验后的 dict 转成引擎结构（不落库、不分配 id）
        tree = _payload_to_tree(groups_data)
        outcome = self.engine.dry_run(tree, data["logic"], bundle.ctx)
        gate_ok, gate_reason = self._quality_gate(_TempRule(data), bundle)

        return {
            "satisfied": outcome.satisfied,
            "reason": outcome.reason,
            "expression": _readable(data),
            "conditions": [r.to_dict() for r in outcome.results],
            "unavailable": sorted(set(outcome.unavailable)),
            "quality_gate": {"passed": gate_ok, "reason": gate_reason},
            "would_notify": bool(outcome.satisfied and gate_ok),
            "market_context": bundle.market_context,
            "providers_used": bundle.providers_used,
            "used_fallback": bundle.used_fallback,
            "data_quality": bundle.quality,
            "module_errors": bundle.module_errors,
        }

    async def dry_run_rule_id(self, rule_id: int) -> dict[str, Any]:
        """对已存在的规则立刻判断一次。"""
        factory = get_session_factory()
        async with factory() as session:
            rule = await session.get(AlertRule, rule_id)
            if rule is None:
                raise rules_mod.RuleValidationError(f"规则 {rule_id} 不存在。", "id")
            tree = await rules_mod.load_tree(session, rule_id)
            bundle = await build_context(need_personal=bool(rule.plan_id))
            outcome = self.engine.dry_run(tree, rule.logic or "AND", bundle.ctx)  # type: ignore[arg-type]
            gate_ok, gate_reason = self._quality_gate(rule, bundle)
            expression = rules_mod.describe_rule(rule, tree)
        return {
            "rule_id": rule_id,
            "satisfied": outcome.satisfied,
            "reason": outcome.reason,
            "expression": expression,
            "conditions": [r.to_dict() for r in outcome.results],
            "would_notify": bool(outcome.satisfied and gate_ok),
            "quality_gate": {"passed": gate_ok, "reason": gate_reason},
            "market_context": bundle.market_context,
            "providers_used": bundle.providers_used,
            "used_fallback": bundle.used_fallback,
            "data_quality": bundle.quality,
        }


def _is_fatal(error: str | None) -> bool:
    """认证失败 / 收件人被拒 —— 重试也救不回来，属于配置问题，直接留给人工处理。"""
    if not error:
        return False
    return ("认证失败" in error) or ("收件人被拒绝" in error) or ("SMTPAuthenticationError" in error)


class _TempRule:
    """只用于 quality_gate 的临时对象（测试规则时规则还没落库）。"""

    def __init__(self, data: dict[str, Any]) -> None:
        self.min_quality = data.get("min_quality") or "SINGLE_SOURCE"
        self.require_fresh = bool(data.get("require_fresh", True))
        self.require_multi_source = bool(data.get("require_multi_source", False))


def _payload_to_tree(groups: list[dict[str, Any]]):
    from .engine import Condition, ConditionGroup

    out = []
    for g in groups:
        node = ConditionGroup(id=None, operator=g["operator"], label=g.get("label"),
                              position=g.get("position", 0))
        for c in g["conditions"]:
            node.conditions.append(Condition(
                id=None, metric_code=c["metric_code"], operator=c["operator"],
                threshold=c["threshold"], threshold_high=c["threshold_high"],
                compare_metric=c["compare_metric"], change_window=c["change_window"],
                percentile_window_days=c["percentile_window_days"],
                expected_state=c["expected_state"], duration_seconds=c["duration_seconds"],
                consecutive_count=c["consecutive_count"], min_quality=c["min_quality"],
                position=c["position"],
            ))
        for sub in g["groups"]:
            node.groups.extend(_payload_to_tree([sub]))
        out.append(node)
    return out


def _readable(data: dict[str, Any]) -> str:
    from .rules import _describe_group
    tree = _payload_to_tree(data["groups"])
    joiner = " 并且 " if data["logic"] == "AND" else " 或者 "
    return joiner.join(_describe_group(g) for g in tree)


# ------------------------------------------------------------------ 重试任务

async def retry_failed_notifications() -> dict[str, Any]:
    """Scheduler 周期调用：把 FAILED 且未超重试上限的提醒再发一次。"""
    return await AlertPipeline().dispatch_pending(limit=200)


# ------------------------------------------------------------------ 每日摘要 / 周报

async def send_digest(period: str = "daily") -> dict[str, Any]:
    """需求二十三/二十四：每日摘要与周报。"""
    from .templates import build_digest_body, build_digest_subject

    days = 1 if period == "daily" else 7
    factory = get_session_factory()
    since = datetime.now(timezone.utc) - timedelta(days=days)

    async with factory() as session:
        events = list((await session.execute(
            select(AlertEvent).where(AlertEvent.trigger_time >= since)
            .order_by(AlertEvent.trigger_time.desc()).limit(200)
        )).scalars().all())
        rules = list((await session.execute(select(AlertRule))).scalars().all())

    digest = {
        "period": period,
        "since": since.isoformat(),
        "event_count": len(events),
        "rule_count": len(rules),
        "events": [
            {
                "rule_name": e.rule_name,
                "severity": e.severity,
                "trigger_time": e.trigger_time.isoformat() if e.trigger_time else None,
                "metric_values": e.metric_values,
                "notification_status": e.notification_status,
                "data_quality": e.data_quality,
            }
            for e in events
        ],
        "by_severity": {
            s: sum(1 for e in events if e.severity == s)
            for s in ("INFO", "WARNING", "HIGH", "CRITICAL")
        },
    }

    cfg = load_smtp_config()
    if not cfg.configured:
        return {**digest, "sent": False, "message": "SMTP 未配置，摘要未发送"}

    provider = build_provider("email", {})
    if provider is None:
        return {**digest, "sent": False, "message": "邮件通道不可用"}
    avail, why = provider.available()
    if not avail:
        return {**digest, "sent": False, "message": f"邮件通道不可用：{why}"}

    from .notifier import OutgoingMessage

    subject = build_digest_subject(period, len(events))
    text, html = build_digest_body(digest, kind=period)
    result = await asyncio.to_thread(provider.send, OutgoingMessage(
        subject=subject, text_body=text, html_body=html,
        to=list(cfg.recipients), severity="INFO",
    ))

    async with factory() as session:
        session.add(AlertNotificationLog(
            event_id=None, rule_id=None, channel_type="email",
            target=",".join(cfg.recipients),
            status="SENT" if result.ok else "FAILED", attempt=1,
            subject=subject, smtp_response=result.smtp_response,
            error_message=result.error, duration_ms=result.duration_ms,
        ))
        await session.commit()

    return {**digest, "sent": result.ok, "error": result.error}


# ------------------------------------------------------------------ 历史触发回测
#
# 需求三十三：把规则放到历史数据上跑，看它过去何时成立、触发后市场怎么走。
#
# 防未来泄漏是这一节的核心约束，做法是「增量推进 + 因果切片」：
#   * 第 i 步只把 candles[:i+1] 交给求值器，绝不把整个序列一次性传进去；
#   * 指标在每个切点上用**当时的窗口**重算，而不是拿全历史算好再切片；
#   * 跨越类条件用的「上一期」严格取切片内的倒数第二个点；
#   * 触发后的表现（7/30/90/180 天）只在这些天数**确实已经存在**时才统计，
#     样本不足就返回 None 并说明，不拿不完整窗口凑数。
#
# 这与项目已有的「反事实检验」一脉相承：把未来数据追加进来，历史结论必须逐点不变。

_BACKTEST_WINDOWS = (7, 30, 90, 180)


def _indicator_snapshot_at(full_ind: dict[str, Any], ts: int) -> dict[str, Any]:
    """从「一次算完」的指标结果里，取出 ts 这一刻的指标快照。

    因为全部指标序列都是因果的（下标 i 只依赖 ≤ i 的数据），
    compute(candles) 在某个时间点上的值与 compute(candles[:i+1]) 的最后一根完全等价，
    所以这样取值不会引入未来数据，只是把 O(n²) 的重算换成了 O(1) 的查表。

    注意：IndicatorEngine 的 series 会跳过 NaN（指标还没成熟的早期点），
    所以序列长度与 K 线根数并不相等，不能按下标索引——必须按时间戳定位，
    并且对每个指标找出「时间戳 ≤ 当前时刻」的最后一个有效值（这就是因果对齐）。
    """
    idx = full_ind.get("_ts_index") or {}
    if not idx:
        return {}

    out: dict[str, Any] = {}
    for key, table in idx.items():
        # table: {ts -> value}，且 key 有序；用二分找 ≤ ts 的最后一个
        import bisect

        keys = table["keys"]
        vals = table["vals"]
        pos = bisect.bisect_right(keys, ts) - 1
        if pos < 0:
            continue
        out[key] = vals[pos]

    if not out:
        return {}
    price = out.get("price")
    atr14 = out.get("atr14")
    if price and atr14 is not None:
        out["atr_pct"] = atr14 / price * 100
    return out


def _build_ts_index(series: dict[str, Any]) -> dict[str, dict[str, list]]:
    """把 [[ts, value], ...] 形式的指标序列整理成可按时间二分查找的结构。"""
    idx: dict[str, dict[str, list]] = {}
    for key, arr in (series or {}).items():
        if not arr:
            continue
        keys: list[int] = []
        vals: list[Any] = []
        for point in arr:
            if isinstance(point, (list, tuple)) and len(point) == 2:
                keys.append(int(point[0]))
                vals.append(point[1])
        if keys:
            idx[key] = {"keys": keys, "vals": vals}
    return idx


async def backtest_rule(rule_id: int, *, max_bars: int = 3000) -> dict[str, Any]:
    """用本地历史逐个时点回放一条规则的成立情况。"""
    from sqlalchemy import select

    from ..db.models import Candle
    from ..services.market_service import MarketService

    factory = get_session_factory()
    async with factory() as session:
        rule = await session.get(AlertRule, rule_id)
        if rule is None:
            return {"available": False, "message": f"规则 {rule_id} 不存在"}
        tree = await rules_mod.load_tree(session, rule_id)
        if not tree:
            return {"available": False, "message": "该规则没有任何条件，无法回测"}

        # 只取日线做回测（其他周期历史太短，结论不可靠）
        rows = list((await session.execute(
            select(Candle).where(Candle.symbol == "BTC", Candle.interval == "1d")
            .order_by(Candle.ts.asc()).limit(max_bars)
        )).scalars().all())

    if len(rows) < 60:
        return {"available": False,
                "message": f"本地只有 {len(rows)} 根日线，不足以回测（至少需要 60 根）。请先执行历史回填。"}

    candles = [
        {
            "symbol": r.symbol, "interval": r.interval, "ts": int(r.ts),
            "open": float(r.open), "high": float(r.high),
            "low": float(r.low), "close": float(r.close),
            "volume": float(r.volume or 0),
            "source_id": r.source_id, "quality_status": r.quality_status,
            "observation_time": r.observation_time,
        }
        for r in rows
    ]

    engine = RuleEngine()
    from ..engines.indicators import IndicatorEngine

    ind_engine = IndicatorEngine()

    hits: list[int] = []
    prev_state: dict[str, Any] = {}
    WARMUP = 60  # 前 60 根只用于堆指标，不判定（否则等于用了未来的数据）

    # ------------------------------------------------------------------
    # 性能：一次性把指标序列算完，再逐点取值
    #
    # 早先的实现是「每个时点都调一次 IndicatorEngine.compute(candles[:i+1])」，
    # 3000 根日线上要跑 75 秒，接口直接超时。
    #
    # 这样做之所以等价、而且依然严格因果，是因为本项目的全部指标序列都是
    # **因果的**：第 i 个下标的值只由下标 ≤ i 的数据决定（SMA/EMA/RSI/MACD/
    # ATR/Bollinger/已实现波动率/历史回撤/累计 VWAP 都满足这条）。
    # 于是：
    #     compute(candles[:i+1])[-1]  ≡  compute(candles)[i]
    # 逐点重算和一次算完再索引，得到的是同一个数。
    #
    # 唯一带「全历史统计」性质的量（历史分位、ATH/ATL、距 ATH 距离）由
    # MetricContext 从 candles 自己算，它拿到的仍然是 candles[:i+1]，不受影响。
    # ------------------------------------------------------------------
    try:
        full_ind = ind_engine.compute(candles, include_series=True)
    except Exception:  # noqa: BLE001 - 整体算不出来就没法回测，如实报错
        return {"available": False, "message": "指标计算失败，无法进行历史回测。"}

    # 把指标序列整理成「按时间戳二分查找」的表，逐点取值时 O(log n)
    ts_index = _build_ts_index(full_ind.get("series") or {})
    if not ts_index:
        return {"available": False, "message": "指标计算无结果，无法进行历史回测。"}
    full_ind["_ts_index"] = ts_index

    for i in range(WARMUP, len(candles)):
        bar_ts = int(candles[i]["ts"])
        ind = _indicator_snapshot_at(full_ind, bar_ts)
        if not ind:
            continue

        # MetricContext 拿到的是因果切片 candles[:i+1]（价格/ATH/分位要用），
        # 而指标取的是「同一时间点」的快照，两者严格对齐。
        window = candles[: i + 1]
        ctx = MetricContext(
            candles=window,
            indicators=ind,
        )
        ctx.mark_source("price", "historical_replay", "SINGLE_SOURCE")
        # 回测的语义是「历史上这个条件成不成立」，因此把数据质量门槛放开；
        # 质量门槛是给实时提醒用的（防止用坏数据打扰用户），不是用来改写历史的。
        outcome = engine.evaluate(tree, rule.logic or "AND", ctx, prev_state=prev_state)
        if outcome.satisfied:
            hits.append(i)

        # 让「回归检测」类条件（上穿/下穿/状态变化）在下一步能拿到本期值
        for r in outcome.results:
            if r.actual is not None:
                prev_state[f"prev_{r.condition.id}"] = r.actual

    if not hits:
        return {
            "available": True,
            "rule_id": rule_id,
            "rule_name": rule.name,
            "trigger_count": 0,
            "bars_tested": len(candles) - WARMUP,
            "from_ts": candles[WARMUP]["ts"],
            "to_ts": candles[-1]["ts"],
            "message": "在这段历史里，该规则从未成立过。",
            "aftermath": [],
        }

    # ---- 触发后的表现（严格只用已经存在的未来数据）
    aftermath = []
    for days in _BACKTEST_WINDOWS:
        rets: list[float] = []
        max_gains: list[float] = []
        max_dds: list[float] = []
        vols: list[float] = []
        for i in hits:
            if i + days >= len(candles):
                continue  # 未来数据不够，这个样本直接不用（不补、不猜）
            base = candles[i]["close"]
            future = candles[i + 1 : i + 1 + days]
            if not base or not future:
                continue
            closes = [c["close"] for c in future]
            rets.append((closes[-1] - base) / base * 100.0)
            max_gains.append((max(closes) - base) / base * 100.0)
            max_dds.append((min(closes) - base) / base * 100.0)
            mean = sum(closes) / len(closes)
            var = sum((c - mean) ** 2 for c in closes) / len(closes)
            vols.append((var ** 0.5) / base * 100.0)
        if not rets:
            aftermath.append({
                "days": days, "available": False, "sample_size": 0,
                "note": f"没有足够的 {days} 天后续数据，这一档无法统计",
            })
            continue
        aftermath.append({
            "days": days,
            "available": True,
            "sample_size": len(rets),
            "avg_return_pct": round(sum(rets) / len(rets), 3),
            "avg_max_gain_pct": round(sum(max_gains) / len(max_gains), 3),
            "avg_max_drawdown_pct": round(sum(max_dds) / len(max_dds), 3),
            "avg_volatility_pct": round(sum(vols) / len(vols), 3),
            "positive_ratio_pct": round(sum(1 for r in rets if r > 0) / len(rets) * 100, 1),
        })

    # ---- 平均间隔
    gaps = [hits[k] - hits[k - 1] for k in range(1, len(hits))]
    avg_gap = round(sum(gaps) / len(gaps), 2) if gaps else None

    return {
        "available": True,
        "rule_id": rule_id,
        "rule_name": rule.name,
        "expression": rules_mod.describe_rule(rule, tree),
        "trigger_count": len(hits),
        "bars_tested": len(candles) - WARMUP,
        "from_ts": candles[WARMUP]["ts"],
        "to_ts": candles[-1]["ts"],
        "first_trigger": candles[hits[0]]["ts"],
        "last_trigger": candles[hits[-1]]["ts"],
        "avg_interval_days": avg_gap,
        "trigger_dates": [candles[i]["ts"] for i in hits[-50:]],
        "aftermath": aftermath,
        "caveats": [
            "以上全部是历史统计，只说明过去发生过什么，不代表未来会重复。",
            "回测按时间顺序逐点推进：每个时点只用截至该时点为止的数据，不会用到未来信息。",
            "样本数越少，结论越不可靠；请重点看 sample_size。",
            "系统不提供买卖建议，也不对任何未来涨跌做承诺。",
        ],
    }
