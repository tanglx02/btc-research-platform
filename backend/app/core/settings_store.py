# -*- coding: utf-8 -*-
"""运行时配置中心：让所有配置都能在后台改，不必手工编辑 .env。

设计要点
--------
1. **元数据随代码走**。``SETTINGS_SPEC`` 定义每一项的中文名、说明、类型、
   是否敏感、是否需要重启；数据库表 ``app_settings`` **只存 key -> value**。
   这样不会出现「库里有值但代码不认识它」的幽灵配置，前端也能根据 spec 自动渲染表单。

2. **优先级**：数据库 > 环境变量/.env > 代码默认值。
   也就是说 .env 依然可用（适合无人值守部署），但在后台改过之后以后台为准。

3. **立即生效**。保存后会调用 :func:`apply_to_settings` 把值写回 settings 单例，
   于是所有原本读 ``settings.XXX`` 的代码自动拿到新值 —— 不需要重启，也不需要改各模块。

4. **敏感值只写不读明文**。标记为 ``sensitive`` 的项，列表接口只返回
   ``has_value`` 与掩码，绝不把密钥原文回传给前端。

不应放进来的东西
----------------
``DATABASE_URL`` 不暴露：改错会直接导致服务起不来，属于部署期配置，应走 .env。
``HOST`` / ``PORT`` / ``API_PREFIX`` 同样需要重启且改错即失联，标记为 requires_restart 并谨慎暴露。
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from ..db.base import get_session_factory
from ..db.models import AppSetting
from .config import get_settings
from .logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------- 元数据


@dataclass(frozen=True)
class SettingSpec:
    """一项配置的元数据。"""

    key: str
    label: str               # 中文名，直接显示在后台
    category: str
    value_type: str = "str"  # str | int | float | bool
    default: Any = ""
    sensitive: bool = False      # 密钥类：列表不返回明文
    requires_restart: bool = False
    help: str = ""
    placeholder: str = ""
    options: list[dict[str, str]] = field(default_factory=list)  # 枚举项（下拉框）


# 分类的展示顺序与中文名
CATEGORIES: list[dict[str, str]] = [
    {"code": "provider_keys", "name": "数据源密钥", "help": "留空即「未配置」，该数据源会如实报 NOT_CONFIGURED，绝不伪造数据。"},
    {"code": "smtp", "name": "邮件通知", "help": "用于智能预警的邮件发送。填完记得点「发送测试邮件」验证。"},
    {"code": "alerts", "name": "智能预警", "help": "预警引擎的检测周期、摘要邮件时间等。"},
    {"code": "collection", "name": "数据采集", "help": "各采集任务的执行周期。改小会更实时但更耗请求额度。"},
    {"code": "network", "name": "网络与代理", "help": "超时、重试、**代理**、限流。国内访问 Binance / Coinbase / Kraken / CoinGecko 等需要代理。"},
    {"code": "failover", "name": "故障切换", "help": "数据源健康判定与自动切换阈值。"},
    {"code": "ai", "name": "AI 助手", "help": "问答助手依赖的大模型配置。"},
    {"code": "general", "name": "基础设置", "help": "展示与运行相关的基础项。"},
]


def _opts(*pairs: tuple[str, str]) -> list[dict[str, str]]:
    return [{"value": v, "label": l} for v, l in pairs]


SETTINGS_SPEC: list[SettingSpec] = [
    # ---------------- 数据源密钥 ----------------
    SettingSpec("GLASSNODE_API_KEY", "Glassnode", "provider_keys", sensitive=True,
                help="链上高级指标（NUPL、SOPR、MVRV 精细口径、交易所流）。官网 glassnode.com 购买后获取。",
                placeholder="留空则该数据源不可用"),
    SettingSpec("CRYPTOQUANT_API_KEY", "CryptoQuant", "provider_keys", sensitive=True,
                help="交易所净流量、储备量、ETF 资金流的另一路来源。",
                placeholder="留空则该数据源不可用"),
    SettingSpec("COINGLASS_API_KEY", "Coinglass", "provider_keys", sensitive=True,
                help="合约持仓量、爆仓、资金费率与 ETF 资金流。",
                placeholder="留空则该数据源不可用"),
    SettingSpec("FRED_API_KEY", "FRED（美联储经济数据）", "provider_keys", sensitive=True,
                help="利率、M2 货币供应量等宏观序列。可在 fred.stlouisfed.org 免费申请。",
                placeholder="免费申请，留空则宏观数据少一路来源"),

    # ---------------- 邮件通知 ----------------
    SettingSpec("SMTP_HOST", "SMTP 服务器", "smtp", placeholder="例如 smtp.qq.com、smtp.gmail.com",
                help="发信服务器地址。"),
    SettingSpec("SMTP_PORT", "SMTP 端口", "smtp", "int", default=465,
                help="SSL 常用 465；STARTTLS 常用 587。"),
    SettingSpec("SMTP_ENCRYPTION", "加密方式", "smtp", "str", default="ssl",
                options=_opts(("ssl", "SSL（端口 465）"), ("starttls", "STARTTLS（端口 587）"), ("none", "不加密（仅内网）")),
                help="与端口要匹配，否则会连接失败。"),
    SettingSpec("SMTP_USER", "SMTP 账号", "smtp", placeholder="通常是完整邮箱地址",
                help="登录用户名。"),
    SettingSpec("SMTP_PASSWORD", "SMTP 密码 / 授权码", "smtp", sensitive=True,
                help="多数邮箱要用「授权码」而不是登录密码（QQ/163 尤其如此）。"),
    SettingSpec("SMTP_SENDER", "发件人地址", "smtp", placeholder="例如 btc@example.com",
                help="一般与 SMTP 账号相同。"),
    SettingSpec("SMTP_SENDER_NAME", "发件人显示名", "smtp", default="BTC 智能预警",
                help="收件人看到的发件人名称。"),
    SettingSpec("SMTP_TO", "收件人地址", "smtp", placeholder="多个用英文逗号分隔",
                help="接收预警邮件的邮箱。"),
    SettingSpec("SMTP_TIMEOUT_SECONDS", "连接超时（秒）", "smtp", "float", default=20.0),
    SettingSpec("PUBLIC_BASE_URL", "平台访问地址", "smtp", placeholder="例如 http://192.168.1.10:8787",
                help="邮件里「打开平台查看」按钮的链接前缀；留空则邮件不显示该按钮。"),

    # ---------------- 智能预警 ----------------
    SettingSpec("ALERTS_ENABLED", "启用智能预警", "alerts", "bool", default=True,
                help="关闭后预警引擎不再求值，已有规则保留但不会触发。"),
    SettingSpec("JOB_ALERT_ENGINE_SECONDS", "规则检测周期（秒）", "alerts", "int", default=60,
                help="每多少秒跑一轮检测。规则自身的检测周期还会再做一次过滤。"),
    SettingSpec("JOB_ALERT_RETRY_SECONDS", "失败邮件补发周期（秒）", "alerts", "int", default=300,
                help="发送失败的事件每隔多久重试一次。"),
    SettingSpec("ALERT_DIGEST_DAILY_HOUR", "每日摘要发送时刻（UTC 小时）", "alerts", "int", default=9,
                help="北京时间 = 该值 + 8。填 9 即北京时间 17:00。"),
    SettingSpec("ALERT_DIGEST_WEEKLY_WEEKDAY", "每周摘要在周几", "alerts", "int", default=0,
                options=_opts(("0", "周一"), ("1", "周二"), ("2", "周三"), ("3", "周四"),
                              ("4", "周五"), ("5", "周六"), ("6", "周日"))),
    SettingSpec("ALERT_DIGEST_WEEKLY_HOUR", "每周摘要发送时刻（UTC 小时）", "alerts", "int", default=9),
    SettingSpec("ALERT_MAX_RULES_PER_CYCLE", "单轮最多评估规则数", "alerts", "int", default=500,
                help="保护上限，超出部分下一轮继续。"),

    # ---------------- 数据采集 ----------------
    SettingSpec("SCHEDULER_ENABLED", "启用定时任务", "collection", "bool", default=True, requires_restart=True,
                help="关闭后所有自动采集停止，只能手动触发。"),
    SettingSpec("JOB_MARKET_TICK_SECONDS", "实时价格采集（秒）", "collection", "int", default=30),
    SettingSpec("JOB_MARKET_1H_SECONDS", "小时线采集（秒）", "collection", "int", default=300),
    SettingSpec("JOB_MARKET_1D_SECONDS", "日线采集（秒）", "collection", "int", default=1800),
    SettingSpec("JOB_DERIVATIVES_SECONDS", "合约数据采集（秒）", "collection", "int", default=300),
    SettingSpec("JOB_ONCHAIN_SECONDS", "链上数据采集（秒）", "collection", "int", default=3600),
    SettingSpec("JOB_SENTIMENT_SECONDS", "情绪指数采集（秒）", "collection", "int", default=3600),
    SettingSpec("JOB_MACRO_SECONDS", "宏观数据采集（秒）", "collection", "int", default=21600),
    SettingSpec("JOB_ETF_SECONDS", "ETF 资金流采集（秒）", "collection", "int", default=21600),
    SettingSpec("BACKFILL_START_DATE", "历史回填起始日期", "collection", default="2013-01-01",
                placeholder="YYYY-MM-DD"),

    # ---------------- 网络策略 ----------------
    SettingSpec("SOCKS_PROXY", "SOCKS5 代理（推荐）", "network", sensitive=True,
                placeholder="socks5://用户名:密码@IP:端口",
                help="填了它，所有数据源默认走这个代理；也可以直接贴 `用户名@IP:端口` 这种简写。"
                     "保存后下一次请求就生效，不用重启。填完请点页面上的「测试代理连通性」。"),
    SettingSpec("HTTPS_PROXY", "HTTPS 代理", "network", placeholder="http://用户名:密码@IP:端口",
                help="HTTP/HTTPS 类型的正向代理。同时填了 SOCKS5 时，**优先用 SOCKS5**。"),
    SettingSpec("HTTP_PROXY", "HTTP 代理", "network", placeholder="http://IP:端口",
                help="兜底项，一般填上面两个就够了。"),
    SettingSpec("HTTP_TRUST_ENV", "允许系统环境变量代理（HTTP_PROXY 等）", "network", "bool", default=False,
                help="默认关闭。关闭时平台只看这里的配置 —— 避免操作系统/Docker 里残留的 "
                     "HTTP_PROXY 悄悄接管请求，导致「后台显示直连、实际走了别的代理」。"
                     "只有在你的环境本来就靠环境变量配代理时才打开。"),
    SettingSpec("DNS_MODE", "DNS 解析方式", "network", default="system",
                options=_opts(("system", "跟随操作系统（默认）"), ("doh", "DoH：绕开本地 DNS 污染")),
                help="国内网络下很多「数据源时通时断」其实不是网络问题，而是本地 DNS 被污染"
                     "（同一域名反复解析出假 IP，常见特征是出现 2001:: 开头的地址）。"
                     "改成 doh 后平台会用 DNS-over-HTTPS 拿真实 IP，通常能直接恢复。"
                     "改完请在下方点「检测 DNS 污染」验证。注意：此项只对直连生效，走代理时由代理方解析。"),
    SettingSpec("DNS_DOH_ENDPOINTS", "DoH 服务器列表", "network",
                default="https://dns.alidns.com/dns-query,"
                        "https://doh.pub/dns-query,"
                        "https://cloudflare-dns.com/dns-query,"
                        "https://dns.google/dns-query",
                help="逗号分隔，按顺序尝试，前一个失败自动换下一个。"
                     "默认把阿里公共 DNS 排在最前：Cloudflare / Google 的 DoH 在国内"
                     "经常连不上（实测直接超时），排第一个会让「开了 DoH 没效果」。"
                     "留空则退回上面这份默认值。"),
    SettingSpec("DNS_CACHE_TTL_SECONDS", "DNS 缓存时间（秒）", "network", "int", default=300),
    SettingSpec("DEFAULT_TIMEOUT_CONNECT", "连接超时（秒）", "network", "float", default=4.0,
                help="网络不好时可调到 8~10。走代理建议 6~10。"),
    SettingSpec("DEFAULT_TIMEOUT_READ", "读取超时（秒）", "network", "float", default=10.0,
                help="数据源响应慢时可调到 20~30。"),
    SettingSpec("DEFAULT_RETRIES", "单源重试次数", "network", "int", default=2,
                help="同一数据源内的重试，不含切换到备用源。"),
    SettingSpec("RATE_LIMIT_DEFAULT_QPS", "默认请求频率（次/秒）", "network", "float", default=2.0),

    # ---------------- 故障切换 ----------------
    SettingSpec("FAILURE_THRESHOLD", "连续失败几次判定故障", "failover", "int", default=3),
    SettingSpec("RECOVERY_SUCCESS_STREAK", "连续成功几次恢复为主源", "failover", "int", default=3),
    SettingSpec("RECOVERY_COOLDOWN_SECONDS", "故障源冷却时间（秒）", "failover", "int", default=180),
    SettingSpec("STALE_AFTER_SECONDS", "数据多久算过期（秒）", "failover", "int", default=180),
    SettingSpec("CROSS_VALIDATION_TOLERANCE_PCT", "交叉验证容差（%）", "failover", "float", default=0.35,
                help="多源取值差异超过该百分比即判为冲突。"),

    # ---------------- AI 助手 ----------------
    SettingSpec("AI_ASSISTANT_ENABLED", "启用 AI 问答助手", "ai", "bool", default=True),
    SettingSpec("OPENAI_API_KEY", "大模型 API Key", "ai", sensitive=True,
                help="留空则问答助手不可用（不会用假答案糊弄）。"),
    SettingSpec("OPENAI_BASE_URL", "大模型接口地址", "ai", default="https://api.openai.com/v1",
                help="换成国内中转或自建兼容接口时填这里。"),
    SettingSpec("OPENAI_MODEL", "模型名称", "ai", default="gpt-4o-mini"),

    # ---------------- 基础 ----------------
    SettingSpec("TIMEZONE", "时区", "general", default="Asia/Shanghai"),
    SettingSpec("LOG_LEVEL", "日志级别", "general", default="INFO",
                options=_opts(("DEBUG", "DEBUG"), ("INFO", "INFO"), ("WARNING", "WARNING"), ("ERROR", "ERROR"))),
    SettingSpec("PORT", "服务端口", "general", "int", default=8787, requires_restart=True,
                help="改端口必须重启服务才生效。"),
    SettingSpec("SECRET_KEY", "系统密钥", "general", sensitive=True, requires_restart=True,
                help="用于备份包签名。生产环境务必改成强随机值，否则备份签名形同虚设。"),
    SettingSpec("ADMIN_TOKEN", "管理接口令牌", "general", sensitive=True,
                help="调用 /api/v1/system/* 管理接口的凭证。生产环境务必修改。"),
]

SPEC_BY_KEY: dict[str, SettingSpec] = {s.key: s for s in SETTINGS_SPEC}


# ---------------------------------------------------------------- 值转换


def coerce(spec: SettingSpec | None, raw: Any) -> Any:
    """把字符串/任意值转成该配置项声明的类型。转换失败时回退默认值，绝不抛异常到调用方。"""
    if raw is None or raw == "":
        return spec.default if spec else ""
    try:
        if spec is None or spec.value_type == "str":
            return str(raw)
        if spec.value_type == "int":
            return int(float(raw))
        if spec.value_type == "float":
            return float(raw)
        if spec.value_type == "bool":
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in ("1", "true", "yes", "on", "是")
    except (TypeError, ValueError):
        return spec.default if spec else ""
    return raw


def to_storage(value: Any) -> str:
    """写入数据库前的序列化：统一存字符串（SQLite 无原生 bool/json 差异问题）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def mask(value: str) -> str:
    """敏感值掩码：让人能确认「填过了」，又拿不到原文。"""
    if not value:
        return ""
    if len(value) <= 6:
        return "*" * len(value)
    return f"{value[:3]}{'*' * (len(value) - 6)}{value[-3:]}"


# ---------------------------------------------------------------- 存储


class SettingsStore:
    """进程内缓存 + 数据库持久化的配置存储。

    启动时 :meth:`load` 从库里全量读入缓存，之后同步读取都走内存，
    避免在请求路径上打数据库。
    """

    def __init__(self) -> None:
        self._cache: dict[str, str] = {}
        self._loaded = False
        self._lock = asyncio.Lock()
        # 首次应用前从 settings 抄一份「原始值」。
        # 用途：用户点了「恢复默认」后，能把被覆盖的 settings 属性还原回去 ——
        # 否则 setattr 已经把原值冲掉，删除库里的记录也变不回来。
        self._baseline: dict[str, Any] = {}

    # -------------------------------------------------- 加载

    async def load(self, *, force: bool = False) -> None:
        """从数据库加载全部配置到内存缓存，并回写到 settings 单例。"""
        async with self._lock:
            if self._loaded and not force:
                return
            try:
                factory = get_session_factory()
                async with factory() as session:
                    rows = (await session.execute(select(AppSetting))).scalars().all()
                    self._cache = {r.key: (r.value or "") for r in rows}
            except Exception as exc:  # noqa: BLE001
                # 表还没建好（首次启动）或数据库不可用时，退化为「全部用 .env / 默认值」。
                # 这里不能让配置中心把整个启动流程拖垮。
                logger.warning("settings.load_failed error=%s，将回退到 .env 与默认值", exc)
                self._cache = {}
            self._loaded = True
        self.apply_to_settings()

    def _snapshot_baseline(self, settings: Any) -> None:
        """记录未经本模块污染过的原始值（.env 或代码默认值）。只在首次调用时生效。"""
        if self._baseline:
            return
        for spec in SETTINGS_SPEC:
            if hasattr(settings, spec.key):
                self._baseline[spec.key] = getattr(settings, spec.key)

    def apply_to_settings(self) -> None:
        """把配置写回 settings 单例。

        这是「改完立即生效、不用重启」的关键：所有原本读 ``settings.XXX`` 的模块
        无需任何改动就能拿到后台配置的新值。

        注意这里**遍历 SETTINGS_SPEC 而不是只遍历缓存**：
        被删除（恢复默认）的配置项必须显式还原成基线值，
        否则 setattr 覆盖过的属性会一直停留在旧值上，出现「库里删了但运行时还是老值」。
        """
        settings = get_settings()
        self._snapshot_baseline(settings)
        applied = 0
        for spec in SETTINGS_SPEC:
            if not hasattr(settings, spec.key):
                continue
            raw = self._cache.get(spec.key)
            if raw is None or raw == "":
                target = self._baseline.get(spec.key, spec.default)
            else:
                target = coerce(spec, raw)
            try:
                setattr(settings, spec.key, target)
                applied += 1
            except Exception as exc:  # noqa: BLE001
                # pydantic 校验失败时不能中断整个应用，记录后继续
                logger.warning("settings.apply_failed key=%s error=%s", spec.key, exc)
        if applied:
            logger.info("settings.applied count=%d", applied)

    # -------------------------------------------------- 读

    def get_sync(self, key: str, default: Any = None) -> Any:
        """同步读取（供 Provider 等非异步上下文使用）。"""
        if key in self._cache and self._cache[key] != "":
            return self._cache[key]
        settings = get_settings()
        val = getattr(settings, key, None)
        if val is not None and val != "":
            return val
        spec = SPEC_BY_KEY.get(key)
        return default if default is not None else (spec.default if spec else "")

    async def get(self, key: str, default: Any = None) -> Any:
        await self.load()
        return self.get_sync(key, default)

    # -------------------------------------------------- 写

    async def set_many(self, items: dict[str, Any], *, updated_by: str = "admin") -> dict[str, Any]:
        """批量写入配置。返回每个 key 的写入结果。"""
        await self.load()
        results: dict[str, Any] = {}
        unknown: list[str] = []
        for key, value in items.items():
            spec = SPEC_BY_KEY.get(key)
            if spec is None:
                unknown.append(key)
                continue
            results[key] = to_storage(value)

        if unknown:
            raise KeyError(f"未识别的配置项：{', '.join(unknown)}")

        factory = get_session_factory()
        async with factory() as session:
            for key, stored in results.items():
                row = (await session.execute(
                    select(AppSetting).where(AppSetting.key == key)
                )).scalar_one_or_none()
                if row is None:
                    session.add(AppSetting(key=key, value=stored, updated_by=updated_by))
                else:
                    row.value = stored
                    row.updated_by = updated_by
            await session.commit()

        self._cache.update(results)
        self.apply_to_settings()
        logger.event("settings.updated", keys=sorted(results), by=updated_by)
        return {"updated": sorted(results), "count": len(results)}

    async def reset(self, keys: list[str]) -> dict[str, Any]:
        """把配置项恢复为「跟随 .env / 默认值」——即删除库里的覆盖值。"""
        await self.load()
        factory = get_session_factory()
        removed: list[str] = []
        async with factory() as session:
            for key in keys:
                row = (await session.execute(
                    select(AppSetting).where(AppSetting.key == key)
                )).scalar_one_or_none()
                if row is not None:
                    await session.delete(row)
                    removed.append(key)
            await session.commit()
        for key in removed:
            self._cache.pop(key, None)
        self.apply_to_settings()
        return {"removed": removed, "count": len(removed)}

    # -------------------------------------------------- 导出给前端

    def describe(self) -> dict[str, Any]:
        """返回「元数据 + 当前值」给前端渲染表单。敏感项只给掩码与是否已配置。"""
        settings = get_settings()
        groups: dict[str, list[dict[str, Any]]] = {c["code"]: [] for c in CATEGORIES}
        for spec in SETTINGS_SPEC:
            # 当前值：库里有就用库里的，否则 .env，再否则默认
            if spec.key in self._cache and self._cache[spec.key] != "":
                raw: Any = self._cache[spec.key]
                source = "database"
            else:
                env_val = getattr(settings, spec.key, None)
                if env_val is not None and env_val != "":
                    raw = env_val
                    source = "env"
                else:
                    raw = spec.default
                    source = "default"

            item = {
                "key": spec.key,
                "label": spec.label,
                "type": spec.value_type,
                "category": spec.category,
                "sensitive": spec.sensitive,
                "requires_restart": spec.requires_restart,
                "help": spec.help,
                "placeholder": spec.placeholder,
                "options": spec.options,
                "source": source,          # database | env | default —— 让用户知道当前值来自哪里
                "overridden": source == "database",
            }
            if spec.sensitive:
                text = to_storage(raw)
                item["value"] = ""                 # 敏感值绝不下发明文
                item["has_value"] = bool(text)
                item["masked"] = mask(text)
            else:
                item["value"] = coerce(spec, raw)
            groups.setdefault(spec.category, []).append(item)

        return {
            "categories": CATEGORIES,
            "groups": groups,
            "count": len(SETTINGS_SPEC),
        }


_store: SettingsStore | None = None


def get_settings_store() -> SettingsStore:
    global _store
    if _store is None:
        _store = SettingsStore()
    return _store
