# -*- coding: utf-8 -*-
"""结构化 JSON 日志：贯穿 request_id，绝不记录密钥、令牌与隐私数据。"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from .config import get_settings

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
provider_var: ContextVar[str] = ContextVar("provider", default="-")

_SENSITIVE_KEYS = {
    "api_key", "apikey", "token", "secret", "password", "authorization",
    "access_token", "refresh_token", "key", "passwd",
}


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def scrub(data: dict[str, Any]) -> dict[str, Any]:
    """移除敏感字段，避免日志泄露密钥。"""
    clean: dict[str, Any] = {}
    for k, v in data.items():
        lk = str(k).lower()
        if any(s in lk for s in _SENSITIVE_KEYS):
            clean[k] = "***REDACTED***"
        else:
            clean[k] = v
    return clean


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
            "provider": provider_var.get(),
        }
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(scrub(extra))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class _JsonLogger(logging.Logger):
    def event(self, msg: str, **fields: Any) -> None:
        self.info(msg, extra={"extra_fields": fields})


logging.setLoggerClass(_JsonLogger)


def setup_logging() -> None:
    settings = get_settings()
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)

    try:
        log_file = settings.abs_path(f"{settings.LOG_DIR}/platform.log")
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=20 * 1024 * 1024, backupCount=10, encoding="utf-8"
        )
        file_handler.setFormatter(JsonFormatter())
        root.addHandler(file_handler)
    except Exception:  # 日志落盘失败不应阻断启动
        pass

    root.setLevel(getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO))
    # 降噪
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
