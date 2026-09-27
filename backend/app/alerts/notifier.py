# -*- coding: utf-8 -*-
"""通知层。

第一阶段实现 Email（SMTP + SSL/TLS），但架构上不写死：
    NotificationProvider 抽象 -> EmailProvider / TelegramProvider(预留) / WebhookProvider(预留)
以后加 Telegram 不需要动 Alert Engine 与条件规则。

关键约束（来自需求）：
* 邮件发送失败**不能丢提醒**：AlertEvent 先落库，通知状态 PENDING -> SENDING -> SENT/FAILED，
  失败可重试，但有最大次数，绝不无限重试。
* 连续失败达阈值后熔断该渠道一段时间，并向日志与系统通知面暴露「邮件通知服务异常」。
* SMTP 密码**不落库、不回显**：只从环境变量 / .env 读取，数据库里最多存「引用名」。
"""

from __future__ import annotations

import smtplib
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Any, Protocol

from ..core.config import get_settings
from ..core.logging import get_logger

logger = get_logger(__name__)

MAX_SEND_ATTEMPTS = 3           # 单封邮件最多尝试次数（含首次）
CHANNEL_FAILURE_THRESHOLD = 5   # 连续失败达到该值 => 熔断并告警
CHANNEL_COOLDOWN_SECONDS = 1800  # 熔断时长


@dataclass(slots=True)
class NotifyResult:
    ok: bool
    channel: str
    target: str
    message_id: str | None = None
    smtp_response: str | None = None
    error: str | None = None
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "channel": self.channel,
            "target": self.target,
            "message_id": self.message_id,
            "smtp_response": self.smtp_response,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }


@dataclass(slots=True)
class OutgoingMessage:
    subject: str
    text_body: str
    html_body: str | None = None
    to: list[str] | None = None
    severity: str = "WARNING"


class NotificationProvider(Protocol):
    """所有通知渠道的统一协议。"""

    channel_type: str

    def available(self) -> tuple[bool, str]:
        """返回 (是否可用, 不可用原因)。配置缺失时明确说明缺什么。"""

    def send(self, msg: OutgoingMessage) -> NotifyResult:
        ...


# ------------------------------------------------------------------ SMTP 配置

@dataclass(slots=True)
class SmtpConfig:
    host: str
    port: int
    username: str
    password: str
    sender: str
    recipients: list[str]
    encryption: str = "starttls"   # ssl | starttls | none
    sender_name: str = "BTC 智能预警"
    timeout: float = 20.0

    @property
    def configured(self) -> bool:
        return bool(self.host and self.sender and self.recipients)

    def describe(self) -> dict[str, Any]:
        """用于界面回显。**绝不包含密码**。"""
        return {
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "sender": self.sender,
            "recipients": self.recipients,
            "encryption": self.encryption,
            "password_set": bool(self.password),
            "sender_name": self.sender_name,
        }


def load_smtp_config(overrides: dict[str, Any] | None = None) -> SmtpConfig:
    """从配置（.env / 环境变量）读取 SMTP；overrides 仅用于界面上的临时测试。

    注意：overrides 里的 password 只用于本次调用，不会写入数据库。
    """
    s = get_settings()
    o = overrides or {}

    def pick(key: str, default: str = "") -> str:
        val = o.get(key)
        if val is not None and val != "":
            return str(val)
        return str(getattr(s, key.upper(), default) or default)

    raw_to = pick("smtp_to") or pick("alert_email_to")
    recipients = [x.strip() for x in raw_to.replace(";", ",").split(",") if x.strip()]
    if not recipients:
        recipients = [pick("smtp_sender")] if pick("smtp_sender") else []

    return SmtpConfig(
        host=pick("smtp_host"),
        port=int(pick("smtp_port", "587") or 587),
        username=pick("smtp_user") or pick("smtp_username"),
        password=pick("smtp_password"),
        sender=pick("smtp_sender") or pick("smtp_from"),
        recipients=recipients,
        encryption=(pick("smtp_encryption", "starttls") or "starttls").lower(),
        sender_name=pick("smtp_sender_name", "BTC 智能预警") or "BTC 智能预警",
    )


class EmailProvider:
    """SMTP 邮件渠道。支持 SSL / STARTTLS / 明文（仅建议在内网使用）。"""

    channel_type = "email"

    def __init__(self, config: SmtpConfig) -> None:
        self.config = config

    def available(self) -> tuple[bool, str]:
        c = self.config
        if not c.host:
            return False, "未配置 SMTP 服务器（SMTP_HOST）"
        if not c.sender:
            return False, "未配置发件人（SMTP_SENDER）"
        if not c.recipients:
            return False, "未配置收件人（SMTP_TO）"
        if c.encryption not in ("ssl", "starttls", "none"):
            return False, f"不支持的加密方式：{c.encryption}"
        if c.encryption != "none" and not c.username:
            return False, "加密连接需要用户名（SMTP_USER）"
        return True, ""

    def _build_message(self, msg: OutgoingMessage) -> EmailMessage:
        c = self.config
        em = EmailMessage()
        em["Subject"] = msg.subject
        em["From"] = formataddr((c.sender_name, c.sender))
        em["To"] = ", ".join(msg.to or c.recipients)
        em["Date"] = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
        em["Message-ID"] = make_msgid(domain=(c.sender.split("@")[-1] or "localhost"))
        em["X-Alert-Severity"] = msg.severity
        em.set_content(msg.text_body)
        if msg.html_body:
            em.add_alternative(msg.html_body, subtype="html")
        return em

    def send(self, msg: OutgoingMessage, *, recipients: list[str] | None = None) -> NotifyResult:
        ok, reason = self.available()
        target = ", ".join(recipients or self.config.recipients)
        if not ok:
            return NotifyResult(False, self.channel_type, target, error=reason)

        c = self.config
        em = self._build_message(msg)
        to_list = recipients or c.recipients
        started = time.perf_counter()
        try:
            if c.encryption == "ssl":
                ctx = ssl.create_default_context()
                with smtplib.SMTP_SSL(c.host, c.port, timeout=c.timeout, context=ctx) as server:
                    if c.username:
                        server.login(c.username, c.password)
                    server.send_message(em, from_addr=c.sender, to_addrs=to_list)
            else:
                with smtplib.SMTP(c.host, c.port, timeout=c.timeout) as server:
                    server.ehlo()
                    if c.encryption == "starttls":
                        server.starttls(context=ssl.create_default_context())
                        server.ehlo()
                    if c.username:
                        server.login(c.username, c.password)
                    server.send_message(em, from_addr=c.sender, to_addrs=to_list)
            elapsed = int((time.perf_counter() - started) * 1000)
            logger.event("alert.email_sent", to=target, subject=msg.subject[:80], ms=elapsed)
            return NotifyResult(True, self.channel_type, target,
                                message_id=em["Message-ID"],
                                smtp_response="accepted", duration_ms=elapsed)
        except smtplib.SMTPAuthenticationError as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            err = f"SMTP 认证失败（{exc.smtp_code}）：请检查用户名/授权码。原始响应：{exc.smtp_error!r}"
            logger.error(f"alert.email_auth_failed: {err}")
            return NotifyResult(False, self.channel_type, target, error=err, duration_ms=elapsed)
        except smtplib.SMTPRecipientsRefused as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            err = f"收件人被拒绝：{exc.recipients}"
            logger.error(f"alert.email_recipients_refused: {err}")
            return NotifyResult(False, self.channel_type, target, error=err, duration_ms=elapsed)
        except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            err = f"{type(exc).__name__}: {exc}"
            logger.error(f"alert.email_failed: {err}")
            return NotifyResult(False, self.channel_type, target, error=err, duration_ms=elapsed)


class TelegramProvider:
    """预留：Telegram Bot 推送。当前未实现，明确抛错而不是静默成功。"""

    channel_type = "telegram"

    def available(self) -> tuple[bool, str]:
        return False, "Telegram 渠道尚未实现（本阶段仅支持 Email）"

    def send(self, msg: OutgoingMessage) -> NotifyResult:  # pragma: no cover - 预留
        return NotifyResult(False, self.channel_type, "", error="Telegram 渠道尚未实现")


class WebhookProvider:
    """预留：通用 Webhook。同样明确未实现。"""

    channel_type = "webhook"

    def available(self) -> tuple[bool, str]:
        return False, "Webhook 渠道尚未实现（本阶段仅支持 Email）"

    def send(self, msg: OutgoingMessage) -> NotifyResult:  # pragma: no cover - 预留
        return NotifyResult(False, self.channel_type, "", error="Webhook 渠道尚未实现")


_PROVIDERS: dict[str, type] = {
    "email": EmailProvider,
    "telegram": TelegramProvider,
    "webhook": WebhookProvider,
}


def build_provider(channel_type: str, overrides: dict[str, Any] | None = None) -> NotificationProvider:
    cls = _PROVIDERS.get(channel_type)
    if cls is None:
        raise ValueError(f"未知通知渠道：{channel_type}")
    if cls is EmailProvider:
        return EmailProvider(load_smtp_config(overrides))
    return cls()  # type: ignore[return-value]


def supported_channels() -> list[dict[str, Any]]:
    return [
        {"type": "email", "name": "邮件（SMTP）", "implemented": True},
        {"type": "telegram", "name": "Telegram（预留）", "implemented": False},
        {"type": "webhook", "name": "Webhook（预留）", "implemented": False},
    ]


def smtp_diagnostics(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """给「测试邮件」按钮用的诊断信息：连接 -> 加密 -> 登录 -> 发送，逐步给出真实响应。"""
    provider = build_provider("email", overrides)
    ok, reason = provider.available()
    cfg = provider.config.describe()  # type: ignore[attr-defined]
    steps: list[dict[str, Any]] = []

    if not ok:
        return {"ready": False, "config": cfg, "steps": [{"step": "配置检查", "ok": False, "detail": reason}]}

    steps.append({"step": "配置检查", "ok": True, "detail": f"{cfg['host']}:{cfg['port']} ({cfg['encryption']})"})

    c = provider.config  # type: ignore[attr-defined]
    try:
        if c.encryption == "ssl":
            with smtplib.SMTP_SSL(c.host, c.port, timeout=c.timeout) as server:
                steps.append({"step": "TCP + SSL 连接", "ok": True, "detail": server.ehlo()[1].decode()[:200]})
                if c.username:
                    server.login(c.username, c.password)
                    steps.append({"step": "登录", "ok": True, "detail": "认证通过"})
        else:
            with smtplib.SMTP(c.host, c.port, timeout=c.timeout) as server:
                code, banner = server.ehlo()
                steps.append({"step": "TCP 连接", "ok": True, "detail": str(banner.decode() if isinstance(banner, bytes) else banner)[:200]})
                if c.encryption == "starttls":
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                    steps.append({"step": "STARTTLS 升级", "ok": True, "detail": "已加密"})
                if c.username:
                    server.login(c.username, c.password)
                    steps.append({"step": "登录", "ok": True, "detail": "认证通过"})
    except smtplib.SMTPAuthenticationError as exc:
        steps.append({"step": "登录", "ok": False, "detail": f"认证失败 {exc.smtp_code}: {exc.smtp_error!r}"})
        return {"ready": False, "config": cfg, "steps": steps}
    except Exception as exc:  # noqa: BLE001 - 诊断场景要把真实原因展示给用户
        steps.append({"step": "连接", "ok": False, "detail": f"{type(exc).__name__}: {exc}"})
        return {"ready": False, "config": cfg, "steps": steps}

    return {"ready": True, "config": cfg, "steps": steps}


def parse_encryption(value: str) -> str:
    v = (value or "").strip().lower()
    if v in ("ssl", "smtps", "tls"):     # 465 通常叫 SSL；有些服务商把 TLS 指 465
        return "ssl"
    if v in ("starttls", "tls_starttls", "587"):
        return "starttls"
    if v in ("none", "plain", ""):
        return "none"
    return v
