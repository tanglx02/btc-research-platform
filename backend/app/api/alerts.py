# -*- coding: utf-8 -*-
"""智能监测与条件预警 API。

分组：
* ``/catalog``  —— 可监测的指标目录、可用条件类型、可用模板（前端下拉菜单的数据源）
* ``/rules``    —— 规则的增删改查、复制、暂停/恢复、测试、立即执行
* ``/events``   —— 触发历史（可按 7/30/90 天、1 年、全部查询）
* ``/channels`` —— 通知渠道
* ``/notify``   —— SMTP 配置、测试邮件、诊断
* ``/digest``   —— 每日摘要 / 周报
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from ..alerts import rules as rules_mod
from ..alerts.notifier import (
    build_provider,
    load_smtp_config,
    smtp_diagnostics,
    supported_channels,
)
from ..alerts.pipeline import AlertPipeline, send_digest
from ..alerts.templates import BUILTIN_TEMPLATES
from ..core.errors import NotFoundError, ValidationError
from ..db.base import get_session_factory
from ..db.models import (
    AlertEvent,
    AlertNotificationChannel,
    AlertNotificationLog,
    AlertRule,
)
from .deps import AdminDep, RequestIdDep, WriteDep

router = APIRouter()


# ------------------------------------------------------------------ 目录

@router.get("/catalog", summary="可监测指标目录（含通俗解释）")
async def catalog() -> dict[str, Any]:
    metrics = rules_mod.catalog_for_frontend()
    groups: dict[str, list[dict[str, Any]]] = {}
    for m in metrics:
        groups.setdefault(m["group"], []).append(m)
    return {
        "metrics": metrics,
        "by_group": groups,
        "group_count": len(groups),
        "operators": rules_mod.operators_for_frontend(),
        "severities": [
            {"code": s, "label": rules_mod.SEVERITY_CN[s]} for s in rules_mod.SEVERITIES
        ],
        "change_windows": ["1h", "4h", "12h", "24h", "7d", "30d"],
        "quality_levels": [
            {"code": "ANY", "label": "不限（只要有数据）"},
            {"code": "SINGLE_SOURCE", "label": "单源可用即可"},
            {"code": "VERIFIED", "label": "需通过交叉验证"},
            {"code": "CROSS_VERIFIED", "label": "需多个独立数据源一致"},
        ],
        "channels": supported_channels(),
        "templates": BUILTIN_TEMPLATES,
    }


@router.get("/templates", summary="条件模板（普通模式用的预设）")
async def templates() -> dict[str, Any]:
    return {"templates": BUILTIN_TEMPLATES}


# ------------------------------------------------------------------ 规则

class ConditionIn(BaseModel):
    metric_code: str
    operator: str
    threshold: float | None = None
    threshold_high: float | None = None
    compare_metric: str | None = None
    change_window: str | None = None
    percentile_window_days: int | None = None
    expected_state: str | None = None
    duration_seconds: int = 0
    consecutive_count: int = 1
    min_quality: str | None = None
    position: int = 0


class GroupIn(BaseModel):
    operator: Literal["AND", "OR", "NOT"] = "AND"
    label: str | None = None
    position: int = 0
    conditions: list[ConditionIn] = Field(default_factory=list)
    groups: list["GroupIn"] = Field(default_factory=list)


GroupIn.model_rebuild()


class RuleIn(BaseModel):
    name: str
    description: str | None = None
    logic: Literal["AND", "OR"] = "AND"
    severity: Literal["INFO", "WARNING", "HIGH", "CRITICAL"] = "WARNING"
    cooldown_seconds: int = 86400
    notify_on_recover: bool = False
    min_quality: str = "VERIFIED"
    require_fresh: bool = True
    require_multi_source: bool = False
    check_interval_seconds: int = 60
    plan_id: int | None = None
    template_code: str | None = None
    enabled: bool = True
    paused: bool = False
    groups: list[GroupIn] = Field(default_factory=list)


class ChannelIn(BaseModel):
    rule_id: int | None = None
    channel_type: str = "email"
    target: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class SmtpTestIn(BaseModel):
    """测试邮件：不传就用 .env 里的配置；传了只用于本次发送，不落库。"""

    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_sender: str | None = None
    smtp_to: str | None = None
    smtp_encryption: str | None = None
    subject: str | None = None


@router.get("/rules", summary="规则列表")
async def list_rules(enabled_only: bool = False, limit: int = Query(200, ge=1, le=1000),
                     offset: int = Query(0, ge=0)) -> dict[str, Any]:
    factory = get_session_factory()
    async with factory() as session:
        rows = await rules_mod.list_rules(session, enabled_only=enabled_only,
                                          limit=limit, offset=offset)
        items = [await rules_mod.rule_to_dict(session, r, include_tree=False) for r in rows]
    return {"rules": items, "count": len(items)}


@router.post("/rules", summary="新建规则")
async def create_rule(payload: RuleIn, _write: WriteDep, rid: RequestIdDep) -> dict[str, Any]:
    factory = get_session_factory()
    async with factory() as session:
        try:
            rule = await rules_mod.create_rule(session, payload.model_dump())
            await session.commit()
        except rules_mod.RuleValidationError as exc:
            raise ValidationError(exc.message) from exc
        return await rules_mod.rule_to_dict(session, rule)


@router.get("/rules/{rule_id}", summary="规则详情")
async def get_rule(rule_id: int) -> dict[str, Any]:
    factory = get_session_factory()
    async with factory() as session:
        rule = await rules_mod.get_rule(session, rule_id)
        if rule is None:
            raise NotFoundError(f"规则 {rule_id} 不存在")
        return await rules_mod.rule_to_dict(session, rule)


@router.put("/rules/{rule_id}", summary="更新规则")
async def update_rule(rule_id: int, payload: RuleIn, _write: WriteDep) -> dict[str, Any]:
    factory = get_session_factory()
    async with factory() as session:
        try:
            rule = await rules_mod.update_rule(session, rule_id, payload.model_dump())
            await session.commit()
        except rules_mod.RuleValidationError as exc:
            raise ValidationError(exc.message) from exc
        return await rules_mod.rule_to_dict(session, rule)


@router.delete("/rules/{rule_id}", summary="删除规则（保留历史事件）")
async def delete_rule(rule_id: int, _write: WriteDep) -> dict[str, Any]:
    factory = get_session_factory()
    async with factory() as session:
        result = await rules_mod.delete_rule(session, rule_id)
        await session.commit()
    if not result.get("deleted"):
        raise NotFoundError(f"规则 {rule_id} 不存在")
    return {"available": True, **result,
            "note": "历史触发记录已保留，便于事后追溯"}


@router.post("/rules/{rule_id}/duplicate", summary="复制规则")
async def duplicate_rule(rule_id: int, _write: WriteDep,
                         name: str | None = Query(None, max_length=128)) -> dict[str, Any]:
    factory = get_session_factory()
    async with factory() as session:
        try:
            rule = await rules_mod.duplicate_rule(session, rule_id, name)
            await session.commit()
        except rules_mod.RuleValidationError as exc:
            raise ValidationError(exc.message) from exc
        return await rules_mod.rule_to_dict(session, rule)


@router.post("/rules/{rule_id}/pause", summary="暂停规则")
async def pause_rule(rule_id: int, _write: WriteDep) -> dict[str, Any]:
    return await _set_paused(rule_id, True)


@router.post("/rules/{rule_id}/resume", summary="恢复规则")
async def resume_rule(rule_id: int, _write: WriteDep) -> dict[str, Any]:
    return await _set_paused(rule_id, False)


async def _set_paused(rule_id: int, paused: bool) -> dict[str, Any]:
    factory = get_session_factory()
    async with factory() as session:
        try:
            rule = await rules_mod.set_paused(session, rule_id, paused)
            await session.commit()
        except rules_mod.RuleValidationError as exc:
            raise NotFoundError(exc.message) from exc
        return {"available": True, "id": rule.id, "paused": rule.paused,
                "message": "已暂停监测" if paused else "已恢复监测"}


@router.post("/rules/test", summary="测试规则（不保存，立刻告知当前是否成立）")
async def test_rule(payload: RuleIn, _write: WriteDep) -> dict[str, Any]:
    try:
        return await AlertPipeline().test_rule(payload.model_dump())
    except rules_mod.RuleValidationError as exc:
        raise ValidationError(exc.message) from exc


@router.post("/rules/{rule_id}/test", summary="测试已保存的规则")
async def test_saved_rule(rule_id: int, _write: WriteDep) -> dict[str, Any]:
    try:
        return await AlertPipeline().dry_run_rule_id(rule_id)
    except rules_mod.RuleValidationError as exc:
        raise NotFoundError(exc.message) from exc


@router.post("/rules/{rule_id}/run", summary="立即执行一次该规则的检测")
async def run_rule_now(rule_id: int, _write: WriteDep) -> dict[str, Any]:
    factory = get_session_factory()
    async with factory() as session:
        rule = await rules_mod.get_rule(session, rule_id)
        if rule is None:
            raise NotFoundError(f"规则 {rule_id} 不存在")
    result = await AlertPipeline().run_cycle(force=True, rule_ids=[rule_id])
    return {**result, "note": "本次为手动立即执行，会真正按规则触发并发送通知"}


@router.get("/rules/{rule_id}/versions", summary="规则的历史版本（改了什么可追溯）")
async def rule_versions(rule_id: int, limit: int = Query(20, ge=1, le=200)) -> dict[str, Any]:
    from sqlalchemy import desc, select

    from ..db.models import AlertRuleVersion

    factory = get_session_factory()
    async with factory() as session:
        rows = list((await session.execute(
            select(AlertRuleVersion).where(AlertRuleVersion.rule_id == rule_id)
            .order_by(desc(AlertRuleVersion.version)).limit(limit)
        )).scalars().all())
    return {
        "versions": [
            {"id": v.id, "version": v.version, "change_note": v.change_note,
             "created_at": v.created_at.isoformat() if v.created_at else None,
             "snapshot": v.snapshot}
            for v in rows
        ]
    }


# ------------------------------------------------------------------ 事件

@router.get("/events", summary="触发历史")
async def list_events(
    days: str = Query("30", description="7 / 30 / 90 / 365 / all"),
    rule_id: int | None = None,
    severity: str | None = None,
    status: str | None = Query(None, description="PENDING/SENDING/SENT/FAILED/SKIPPED"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    from sqlalchemy import desc, func, select

    stmt = select(AlertEvent)
    count_stmt = select(func.count()).select_from(AlertEvent)
    since = _since_from_days(days)
    for condition in _event_filters(since, rule_id, severity, status):
        stmt = stmt.where(condition)
        count_stmt = count_stmt.where(condition)

    factory = get_session_factory()
    async with factory() as session:
        rows = list((await session.execute(
            stmt.order_by(desc(AlertEvent.trigger_time)).limit(limit).offset(offset)
        )).scalars().all())
        total = int((await session.execute(count_stmt)).scalar() or 0)

    return {
        "events": [_event_to_dict(e) for e in rows],
        "total": total,
        "range": days,
        "since": since.isoformat() if since else None,
        "count": len(rows),
    }


def _since_from_days(days: str) -> datetime | None:
    if str(days).lower() in ("all", "全部", "0"):
        return None
    try:
        n = int(days)
    except (TypeError, ValueError):
        n = 30
    return datetime.now(timezone.utc) - timedelta(days=max(1, n))


def _event_filters(since: datetime | None, rule_id: int | None,
                   severity: str | None, status: str | None):
    out = []
    if since is not None:
        out.append(AlertEvent.trigger_time >= since)
    if rule_id is not None:
        out.append(AlertEvent.rule_id == rule_id)
    if severity:
        out.append(AlertEvent.severity == severity.upper())
    if status:
        out.append(AlertEvent.notification_status == status.upper())
    return out


def _event_to_dict(e: AlertEvent) -> dict[str, Any]:
    return {
        "id": e.id,
        "rule_id": e.rule_id,
        "rule_name": e.rule_name,
        "severity": e.severity,
        "severity_cn": rules_mod.SEVERITY_CN.get(e.severity, e.severity),
        "event_type": e.event_type,
        "trigger_time": e.trigger_time.isoformat() if e.trigger_time else None,
        "metric_values": e.metric_values,
        "threshold": e.threshold,
        "condition_result": e.condition_result,
        "market_context": e.market_context,
        # 需求二十三/二十五：必须能回答「这条提醒用的是哪个数据源、质量如何」
        "provider": e.provider,
        "provider_chain": e.provider_chain,
        "used_fallback": e.used_fallback,
        "data_quality": e.data_quality,
        "data_updated_at": e.data_updated_at.isoformat() if e.data_updated_at else None,
        "notification_status": e.notification_status,
        "email_sent_at": e.email_sent_at.isoformat() if e.email_sent_at else None,
        "email_message_id": e.email_message_id,
        "retry_count": e.retry_count or 0,
        "error_message": e.error_message,
        "recovered_at": e.recovered_at.isoformat() if e.recovered_at else None,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }


@router.get("/events/{event_id}", summary="事件详情（含逐条件明细）")
async def get_event(event_id: int) -> dict[str, Any]:
    factory = get_session_factory()
    async with factory() as session:
        event = await session.get(AlertEvent, event_id)
        if event is None:
            raise NotFoundError(f"事件 {event_id} 不存在")
        logs = await _logs_for(session, event_id)
    return {**_event_to_dict(event), "notification_logs": logs}


async def _logs_for(session, event_id: int) -> list[dict[str, Any]]:
    from sqlalchemy import desc, select

    rows = list((await session.execute(
        select(AlertNotificationLog).where(AlertNotificationLog.event_id == event_id)
        .order_by(desc(AlertNotificationLog.id)).limit(50)
    )).scalars().all())
    return [
        {
            "id": x.id,
            "channel_type": x.channel_type,
            "target": x.target,
            "status": x.status,
            "attempt": x.attempt,
            "subject": x.subject,
            "smtp_response": x.smtp_response,
            "error_message": x.error_message,
            "duration_ms": x.duration_ms,
            "created_at": x.created_at.isoformat() if x.created_at else None,
        }
        for x in rows
    ]


@router.post("/events/{event_id}/resend", summary="重发某条提醒")
async def resend_event(event_id: int, _write: WriteDep) -> dict[str, Any]:
    """手工重发。用于「邮件确实丢了」时的一次性补救。"""
    factory = get_session_factory()
    async with factory() as session:
        event = await session.get(AlertEvent, event_id)
        if event is None:
            raise NotFoundError(f"事件 {event_id} 不存在")
        event.notification_status = "PENDING"
        event.retry_count = 0
        await session.commit()
    result = await AlertPipeline().dispatch_pending(limit=1)
    return {"available": True, **result,
            "note": "已在后台重新排队发送，发送结果见通知日志"}


@router.get("/events/{event_id}/logs", summary="某条提醒的发送日志")
async def event_logs(event_id: int) -> dict[str, Any]:
    factory = get_session_factory()
    async with factory() as session:
        return {"logs": await _logs_for(session, event_id)}


@router.get("/stats", summary="预警中心概览（首页用）")
async def stats(days: int = Query(7, ge=1, le=365)) -> dict[str, Any]:
    from sqlalchemy import func, select

    since = datetime.now(timezone.utc) - timedelta(days=days)
    factory = get_session_factory()
    async with factory() as session:
        total_rules = int((await session.execute(
            select(func.count()).select_from(AlertRule)
        )).scalar() or 0)
        active_rules = int((await session.execute(
            select(func.count()).select_from(AlertRule)
            .where(AlertRule.enabled.is_(True), AlertRule.paused.is_(False))
        )).scalar() or 0)
        recent_events = list((await session.execute(
            select(AlertEvent).where(AlertEvent.trigger_time >= since)
            .order_by(AlertEvent.trigger_time.desc()).limit(200)
        )).scalars().all())
        failed = int((await session.execute(
            select(func.count()).select_from(AlertEvent)
            .where(AlertEvent.notification_status == "FAILED")
        )).scalar() or 0)

    by_sev: dict[str, int] = {}
    for e in recent_events:
        by_sev[e.severity] = by_sev.get(e.severity, 0) + 1

    cfg = load_smtp_config()
    return {
        "rules_total": total_rules,
        "rules_active": active_rules,
        "events_in_window": len(recent_events),
        "events_by_severity": by_sev,
        "failed_notifications": failed,
        "window_days": days,
        "smtp": cfg.describe(),
        "smtp_ready": cfg.configured,
        "latest_event": _event_to_dict(recent_events[0]) if recent_events else None,
    }


# ------------------------------------------------------------------ 渠道

@router.get("/channels", summary="通知渠道列表")
async def list_channels() -> dict[str, Any]:
    from sqlalchemy import select

    factory = get_session_factory()
    async with factory() as session:
        rows = list((await session.execute(
            select(AlertNotificationChannel).order_by(AlertNotificationChannel.id.asc())
        )).scalars().all())
    return {
        "channels": [
            {
                "id": c.id,
                "rule_id": c.rule_id,
                "channel_type": c.channel_type,
                "target": c.target,
                "config": {k: v for k, v in (c.config or {}).items() if "password" not in k.lower()},
                "enabled": c.enabled,
                "consecutive_failures": c.consecutive_failures or 0,
                "last_error": c.last_error,
                "last_success_at": c.last_success_at.isoformat() if c.last_success_at else None,
                "disabled_until": c.disabled_until.isoformat() if c.disabled_until else None,
            }
            for c in rows
        ],
        "supported": supported_channels(),
    }


@router.post("/channels", summary="新增/更新通知渠道")
async def upsert_channel(payload: ChannelIn, _write: WriteDep) -> dict[str, Any]:
    if payload.channel_type not in {c["type"] for c in supported_channels()}:
        raise ValidationError(f"不支持的通知渠道：{payload.channel_type}")
    # 渠道配置里绝不允许出现明文密码（密码只放 .env 的 SMTP_PASSWORD）
    for key in (payload.config or {}):
        if "password" in key.lower() or "secret" in key.lower():
            raise ValidationError(
                f"渠道配置中不允许保存「{key}」。密码请放在 .env 的 SMTP_PASSWORD 中。"
            )

    factory = get_session_factory()
    async with factory() as session:
        rule_id = payload.rule_id
        existing = None
        if rule_id is not None:
            from sqlalchemy import select
            existing = (await session.execute(
                select(AlertNotificationChannel).where(
                    AlertNotificationChannel.rule_id == rule_id,
                    AlertNotificationChannel.channel_type == payload.channel_type,
                )
            )).scalars().first()
        if existing:
            existing.target = payload.target
            existing.config = payload.config
            existing.enabled = payload.enabled
            ch = existing
        else:
            ch = AlertNotificationChannel(
                rule_id=rule_id, channel_type=payload.channel_type,
                target=payload.target, config=payload.config, enabled=payload.enabled,
            )
            session.add(ch)
        await session.flush()
        await session.commit()
        return {"available": True, "id": ch.id, "channel_type": ch.channel_type,
                "target": ch.target, "enabled": ch.enabled}


@router.delete("/channels/{channel_id}", summary="删除通知渠道")
async def delete_channel(channel_id: int, _write: WriteDep) -> dict[str, Any]:
    factory = get_session_factory()
    async with factory() as session:
        ch = await session.get(AlertNotificationChannel, channel_id)
        if ch is None:
            raise NotFoundError(f"渠道 {channel_id} 不存在")
        await session.delete(ch)
        await session.commit()
    return {"available": True, "deleted": channel_id}


@router.post("/channels/{channel_id}/reset", summary="解除渠道熔断（恢复发送）")
async def reset_channel(channel_id: int, _write: WriteDep) -> dict[str, Any]:
    factory = get_session_factory()
    async with factory() as session:
        ch = await session.get(AlertNotificationChannel, channel_id)
        if ch is None:
            raise NotFoundError(f"渠道 {channel_id} 不存在")
        ch.consecutive_failures = 0
        ch.disabled_until = None
        ch.last_error = None
        await session.commit()
    return {"available": True, "id": channel_id, "message": "已解除熔断"}


# ------------------------------------------------------------------ 通知 / SMTP

@router.get("/notify/smtp", summary="当前 SMTP 配置（不含密码）")
async def smtp_config(_admin: AdminDep) -> dict[str, Any]:
    cfg = load_smtp_config()
    return {"config": cfg.describe(), "configured": cfg.configured}


@router.post("/notify/smtp/test", summary="发送测试邮件（含逐步诊断）")
async def test_smtp(payload: SmtpTestIn, _write: WriteDep) -> dict[str, Any]:
    """需求十九：必须显示成功/失败/错误原因/SMTP 响应。"""
    from email.utils import make_msgid

    from ..alerts.notifier import OutgoingMessage

    overrides = {k: v for k, v in payload.model_dump().items() if v not in (None, "")}
    subject_override = overrides.pop("subject", None)

    diag = smtp_diagnostics(overrides)
    cfg = load_smtp_config(overrides)
    if not cfg.configured:
        return {"ok": False, "stage": "config",
                "message": "SMTP 尚未配置完整，请先填写服务器、发件人与收件人",
                "config": cfg.describe(), "diagnostics": diag}

    provider = build_provider("email", overrides)
    avail, why = provider.available()
    if not avail:
        return {"ok": False, "stage": "available", "message": why,
                "config": cfg.describe(), "diagnostics": diag}

    now = datetime.now(timezone.utc)
    subject = subject_override or f"【BTC 智能监测】测试邮件 {now.strftime('%Y-%m-%d %H:%M:%S')} UTC"
    text = (
        "这是一封测试邮件。\n"
        "如果你收到了它，说明预警中心的邮件通道是通的，规则触发后就能把提醒发到这个邮箱。\n\n"
        f"发送时间：{now.isoformat()}\n"
        f"服务器：{cfg.host}:{cfg.port}（{cfg.encryption}）\n"
        f"收件人：{', '.join(cfg.recipients)}\n\n"
        "这封邮件只用于验证配置，不代表任何市场判断，也不构成买卖建议。"
    )
    html = (
        "<p>这是一封测试邮件。</p>"
        "<p>如果你收到了它，说明预警中心的邮件通道是通的，规则触发后就能把提醒发到这个邮箱。</p>"
        f"<ul><li>发送时间：{now.isoformat()}</li>"
        f"<li>服务器：{cfg.host}:{cfg.port}（{cfg.encryption}）</li>"
        f"<li>收件人：{', '.join(cfg.recipients)}</li></ul>"
        "<p style='color:#718096'>这封邮件只用于验证配置，不代表任何市场判断，也不构成买卖建议。</p>"
    )

    msg = OutgoingMessage(subject=subject, text_body=text, html_body=html,
                          to=list(cfg.recipients), severity="INFO")
    result = await _send(provider, msg)

    factory = get_session_factory()
    async with factory() as session:
        session.add(AlertNotificationLog(
            event_id=None, rule_id=None, channel_type="email",
            target=",".join(cfg.recipients),
            status="SENT" if result.ok else "FAILED", attempt=1, subject=subject,
            smtp_response=result.smtp_response, error_message=result.error,
            duration_ms=result.duration_ms,
        ))
        await session.commit()

    return {
        "ok": result.ok,
        "message": "测试邮件已发送，请查收（含垃圾箱）" if result.ok else f"发送失败：{result.error}",
        "smtp_response": result.smtp_response,
        "error": result.error,
        "duration_ms": result.duration_ms,
        "message_id": result.message_id,
        "recipients": cfg.recipients,
        "config": cfg.describe(),
        "diagnostics": diag,
    }


async def _send(provider, msg):
    import asyncio

    return await asyncio.to_thread(provider.send, msg)


@router.post("/notify/smtp/diagnose", summary="只做连通性诊断，不发邮件")
async def diagnose_smtp(payload: SmtpTestIn, _admin: AdminDep) -> dict[str, Any]:
    overrides = {k: v for k, v in payload.model_dump().items() if v not in (None, "")}
    overrides.pop("subject", None)
    return smtp_diagnostics(overrides)


# ------------------------------------------------------------------ 摘要 / 周报

@router.post("/rules/{rule_id}/backtest", summary="历史触发模拟（严格防未来数据泄漏）")
async def backtest_rule_endpoint(
    rule_id: int,
    max_bars: int = Query(3000, ge=200, le=20000, description="最多回看多少根K线"),
) -> dict[str, Any]:
    """在历史数据上逐点重放规则。

    每一步只把「截至该时刻」的K线喂给指标引擎（因果切片），
    因此不会出现用未来数据判断过去的情况。触发后的表现只统计已经发生的未来数据。
    """
    from ..alerts.pipeline import backtest_rule

    try:
        return await backtest_rule(rule_id, max_bars=max_bars)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


@router.post("/digest/{period}", summary="立即发送摘要（daily / weekly）")
async def trigger_digest(period: Literal["daily", "weekly"], _write: WriteDep) -> dict[str, Any]:
    if period not in ("daily", "weekly"):
        raise ValidationError("period 只能是 daily 或 weekly")
    return await send_digest(period)


@router.get("/digest/preview", summary="预览摘要内容（不发邮件）")
async def preview_digest(period: str = Query("daily")) -> dict[str, Any]:
    """把摘要内容渲染出来给用户先看，确认没问题再发给邮箱。"""
    from sqlalchemy import select

    from ..alerts.templates import build_digest_body

    days = 1 if period == "daily" else 7
    since = datetime.now(timezone.utc) - timedelta(days=days)
    factory = get_session_factory()
    async with factory() as session:
        events = list((await session.execute(
            select(AlertEvent).where(AlertEvent.trigger_time >= since)
            .order_by(AlertEvent.trigger_time.desc()).limit(200)
        )).scalars().all())

    digest = {
        "period": period,
        "since": since.isoformat(),
        "event_count": len(events),
        "events": [
            {"rule_name": e.rule_name, "severity": e.severity,
             "trigger_time": e.trigger_time.isoformat() if e.trigger_time else None,
             "metric_values": e.metric_values,
             "notification_status": e.notification_status,
             "data_quality": e.data_quality}
            for e in events
        ],
        "by_severity": {s: sum(1 for e in events if e.severity == s)
                        for s in ("INFO", "WARNING", "HIGH", "CRITICAL")},
    }
    text, html = build_digest_body(digest, kind=period)
    return {"period": period, "text": text, "html": html, "event_count": len(events)}
