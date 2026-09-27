# -*- coding: utf-8 -*-
"""全局配置：全部来自环境变量，启动时集中校验，缺失即快速失败。

设计原则：
1. 任何密钥只出现在环境/`.env` 中，代码库只提交 `.env.example`（占位值）。
2. 所有网络相关参数（代理、超时、重试、退避）均按 Provider 单独可配。
3. 生产环境的 CORS 必须使用显式来源列表，禁止通配符。
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[3]  # 仓库根目录


class Settings(BaseSettings):
    """系统集中配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------- 基础 ----------------
    APP_NAME: str = "BTC Intelligence Platform"
    APP_ENV: str = Field(default="development")  # development | production | test
    DEBUG: bool = False
    API_PREFIX: str = "/api/v1"
    HOST: str = "127.0.0.1"
    PORT: int = 8787
    TIMEZONE: str = "Asia/Shanghai"
    LOG_LEVEL: str = "INFO"
    LOG_DIR: str = "logs"
    SECRET_KEY: str = "change-me-in-production-please-use-long-random-string"
    ADMIN_TOKEN: str = "admin-change-me"

    # ---------------- 存储 ----------------
    # 默认 SQLite（Windows/Linux 零依赖一键启动）；生产可切换 PostgreSQL(+TimescaleDB)
    DATABASE_URL: str = "sqlite+aiosqlite:///data/btc.db"
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_ECHO: bool = False
    DATA_DIR: str = "data"
    BACKUP_DIR: str = "backups"
    RAW_RETENTION_DAYS: int = 3650  # 原始数据保留 10 年

    # ---------------- 缓存 ----------------
    # 内存缓存（默认）；若配置了 REDIS_URL 则自动使用 Redis
    REDIS_URL: str = ""
    CACHE_TTL_SECONDS: int = 60
    CACHE_MAX_ENTRIES: int = 5000

    # ---------------- Provider 全局默认网络策略 ----------------
    DEFAULT_TIMEOUT_CONNECT: float = 4.0
    DEFAULT_TIMEOUT_READ: float = 10.0
    DEFAULT_RETRIES: int = 2           # 同一 Provider 内重试次数（不含 failover）
    DEFAULT_BACKOFF_BASE: float = 0.6  # 指数退避基数（秒）
    DEFAULT_BACKOFF_MAX: float = 8.0
    USER_AGENT: str = "BTC-Intelligence-Platform/1.0 (+research)"
    HTTP_PROXY: str = ""
    HTTPS_PROXY: str = ""
    SOCKS_PROXY: str = ""
    # False = 忽略操作系统环境变量里的 HTTP_PROXY/HTTPS_PROXY，只认平台自己的配置。
    # 这样「后台显示直连」就一定是直连，排障时不会被隐藏的环境变量带偏。
    HTTP_TRUST_ENV: bool = False
    # system = 用操作系统 DNS；doh = 用 DNS-over-HTTPS 绕开本地 DNS 污染。
    # 对直连海外 API 的国内部署很有用：多数「时通时断」其实是 DNS 抢答污染，不是网络不通。
    DNS_MODE: str = "system"
    # 默认以国内可达的端点开头：实测 Cloudflare / Google 的 DoH 在国内部署上经常直接连不上
    # （表现为「开了 DoH 却毫无效果」，用户会误以为功能坏了），而阿里公共 DNS 可达。
    # 排在后面的作为兜底，前面全部失败时会依次尝试。
    DNS_DOH_ENDPOINTS: str = (
        "https://dns.alidns.com/dns-query,"
        "https://doh.pub/dns-query,"
        "https://cloudflare-dns.com/dns-query,"
        "https://dns.google/dns-query"
    )
    DNS_CACHE_TTL_SECONDS: int = 300
    DNS_SERVERS: str = ""  # 逗号分隔，例如 223.5.5.5,119.29.29.29
    MAX_CONNECTIONS_PER_PROVIDER: int = 8
    RATE_LIMIT_DEFAULT_QPS: float = 2.0

    # ---------------- Failover / Health ----------------
    FAILURE_THRESHOLD: int = 3          # 连续失败达到该值判定为故障
    DEGRADED_LATENCY_MS: float = 2500.0 # 超过该延迟判定 DEGRADED
    SLOW_LATENCY_MS: float = 1500.0
    RECOVERY_SUCCESS_STREAK: int = 3    # 恢复为主源所需连续成功次数
    RECOVERY_COOLDOWN_SECONDS: int = 180
    HEALTH_WINDOW_24H: int = 24 * 3600
    STALE_AFTER_SECONDS: int = 180      # 超过该时间未更新即标记 stale
    CROSS_VALIDATION_TOLERANCE_PCT: float = 0.35  # 价格交叉验证容差（%）
    CROSS_VALIDATION_MIN_SOURCES: int = 2

    # ---------------- 采集任务 ----------------
    SCHEDULER_ENABLED: bool = True
    JOB_MARKET_TICK_SECONDS: int = 30
    JOB_MARKET_1H_SECONDS: int = 300
    JOB_MARKET_1D_SECONDS: int = 1800
    JOB_ONCHAIN_SECONDS: int = 3600
    JOB_SENTIMENT_SECONDS: int = 3600
    JOB_MACRO_SECONDS: int = 21600
    JOB_ETF_SECONDS: int = 21600      # ETF 资金流单独调频（不要与宏观共用）
    JOB_RETENTION_SECONDS: int = 86400  # 原始报文留档清理周期（每天一次）
    JOB_DERIVATIVES_SECONDS: int = 300
    JOB_QUALITY_SECONDS: int = 3600
    JOB_HEALTH_SECONDS: int = 120
    BACKFILL_START_DATE: str = "2013-01-01"
    BACKFILL_BATCH_DAYS: int = 500

    # ---------------- 智能预警（监测 / 条件 / 邮件通知）----------------
    ALERTS_ENABLED: bool = True
    JOB_ALERT_ENGINE_SECONDS: int = 60     # 规则求值周期（每条规则自身的检测周期还会再过滤）
    JOB_ALERT_RETRY_SECONDS: int = 300     # 失败通知补发周期
    JOB_ALERT_DIGEST_SECONDS: int = 900    # 检查「是否到点发摘要」的周期
    ALERT_DIGEST_DAILY_HOUR: int = 9       # 每日摘要在 UTC 几点发
    ALERT_DIGEST_WEEKLY_WEEKDAY: int = 0   # 周报在周几发（0=周一）
    ALERT_DIGEST_WEEKLY_HOUR: int = 9
    ALERT_MAX_RULES_PER_CYCLE: int = 500   # 单轮最多评估多少条规则

    # SMTP（邮件通知）。全部可选，未配置时「测试邮件」会明确报出缺哪一项。
    SMTP_HOST: str = ""
    SMTP_PORT: int = 465
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_SENDER: str = ""
    SMTP_SENDER_NAME: str = "BTC 智能预警"
    SMTP_TO: str = ""
    SMTP_ENCRYPTION: str = "ssl"           # ssl | starttls | none
    SMTP_TIMEOUT_SECONDS: float = 20.0
    PUBLIC_BASE_URL: str = ""              # 邮件里「查看完整分析」的链接前缀

    # ---------------- 可选第三方 Key（未配置 => Provider 状态 NOT_CONFIGURED，绝不伪造数据）----------------
    GLASSNODE_API_KEY: str = ""
    COINGLASS_API_KEY: str = ""
    FRED_API_KEY: str = ""
    CRYPTOQUANT_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"
    OPENAI_MODEL: str = "gpt-4o-mini"
    AI_ASSISTANT_ENABLED: bool = True

    # ---------------- Provider 覆盖配置（JSON）----------------
    # 形如 {"market_price": {"primary": "binance_vision", "order": [...], "disabled": [...]}}
    PROVIDER_OVERRIDES: str = "{}"

    # ---------------- CORS ----------------
    CORS_ORIGINS: str = "*"

    @field_validator("CORS_ORIGINS")
    @classmethod
    def _validate_cors(cls, v: str, info: Any) -> str:
        return v

    @property
    def is_production(self) -> bool:
        return self.APP_ENV.lower() == "production"

    @property
    def cors_origin_list(self) -> list[str]:
        raw = [s.strip() for s in self.CORS_ORIGINS.split(",") if s.strip()]
        if not raw:
            raw = ["*"]
        if self.is_production and "*" in raw:
            # 生产环境禁止通配符：回退到本机地址，避免误开放
            raw = [s for s in raw if s != "*"] or [f"http://{self.HOST}:{self.PORT}"]
        return raw

    @property
    def provider_overrides(self) -> dict[str, Any]:
        try:
            return json.loads(self.PROVIDER_OVERRIDES or "{}")
        except json.JSONDecodeError:
            return {}

    def abs_path(self, relative: str) -> Path:
        p = Path(relative)
        if not p.is_absolute():
            p = ROOT_DIR / p
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def validate_for_startup(self) -> None:
        """启动时关键校验，快速失败。"""
        if self.is_production and self.SECRET_KEY.startswith("change-me"):
            raise RuntimeError("生产环境必须设置 SECRET_KEY 环境变量")
        if self.is_production and self.ADMIN_TOKEN.startswith("admin-change-me"):
            raise RuntimeError("生产环境必须设置 ADMIN_TOKEN 环境变量")
        self.abs_path(self.DATA_DIR)
        self.abs_path(self.LOG_DIR)
        self.abs_path(self.BACKUP_DIR)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reload_settings() -> Settings:
    get_settings.cache_clear()
    s = get_settings()
    # 允许 .env 中的代理写入进程环境变量，便于底层库感知
    if s.HTTP_PROXY:
        os.environ.setdefault("HTTP_PROXY", s.HTTP_PROXY)
    if s.HTTPS_PROXY:
        os.environ.setdefault("HTTPS_PROXY", s.HTTPS_PROXY)
    return s
