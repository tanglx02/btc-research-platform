# -*- coding: utf-8 -*-
"""Provider 抽象基类与统一接口标准。

新增一个数据源的正确姿势：
1. 在 `app/providers/impl/` 下新建独立文件；
2. 继承 :class:`DataProvider`，声明 `name / display_name / categories / capabilities`；
3. 实现对应能力方法（返回 :class:`ProviderResult`）；
4. 在本包 `__init__.py` 中 import —— 注册表自动发现，业务层零改动。

删除/禁用一个 Provider：在后台管理将其 disabled 即可，
业务层、数据库、前端、指标引擎、回测系统均不需要修改。
"""

from __future__ import annotations

import inspect
from abc import ABC
from datetime import datetime, timezone
from typing import Any, Iterable

from ..core.config import get_settings
from ..core.errors import ProviderError, ProviderNotConfiguredError
from ..core.logging import get_logger
from .transport import ProviderTransport
from .types import (
    Candle,
    DataCategory,
    FetchTrace,
    ProviderResult,
    QualityStatus,
)

logger = get_logger(__name__)

# ------------------------------------------------------------------ 能力契约

CAPABILITY_METHODS: dict[str, str] = {
    "price": "fetch_price",
    "ticker_24h": "fetch_ticker_24h",
    "ohlcv": "fetch_ohlcv",
    "orderbook": "fetch_orderbook",
    "market_cap": "fetch_market_cap",
    "funding": "fetch_funding",
    "open_interest": "fetch_open_interest",
    "liquidation": "fetch_liquidation",
    "long_short_ratio": "fetch_long_short_ratio",
    "onchain_basic": "fetch_onchain_basic",
    "onchain_advanced": "fetch_onchain_advanced",
    "exchange_reserve": "fetch_exchange_reserve",
    "exchange_flow": "fetch_exchange_flow",
    "etf_flow": "fetch_etf_flow",
    "macro_series": "fetch_macro_series",
    "sentiment": "fetch_sentiment",
}


class DataProvider(ABC):
    """所有数据源的统一抽象。

    子类不得直接被业务代码引用；只能通过 `registry` + `ResilientRouter` 调用。
    """

    # ---- 元信息（子类必须声明）----
    name: str = "abstract"
    display_name: str = "抽象数据源"
    homepage: str = ""
    categories: set[DataCategory] = set()
    capabilities: set[str] = set()
    base_url: str = ""
    requires_api_key: bool = False
    api_key_env: str = ""
    default_priority: int = 100          # 数字越小优先级越高
    rate_limit_qps: float = 2.0
    notes: str = ""
    region_hint: str = "global"          # global | cn-friendly | cn-hostile

    # ---- 运行时状态（由 registry/health 管理，运行时可改）----
    enabled: bool = True
    disabled_reason: str = ""
    locked: bool = False                  # 管理员手动锁定优先级
    manual_priority: int | None = None

    def __init__(self, settings: Any = None, **transport_kwargs: Any) -> None:
        self.settings = settings or get_settings()
        self.transport = ProviderTransport(
            self.name,
            base_url=self.base_url,
            qps=self.rate_limit_qps,
            settings=self.settings,
            **transport_kwargs,
        )
        self.logger = get_logger(f"provider.{self.name}")

    # -------------------------------------------------------------- 配置检查
    def api_key(self) -> str:
        """读取该 Provider 所需密钥；未配置返回空字符串。

        读取顺序：**后台配置（数据库）> 环境变量/.env**。
        这样在「系统设置 → 数据源密钥」里填完就能立刻用，不必改 .env、也不必重启。
        """
        if not self.api_key_env:
            return ""
        # 后台配置优先。这里刻意调用同步接口：Provider 的调用链多处是同步上下文，
        # 而配置中心在启动时已把全量配置载入内存，同步读缓存不会打数据库。
        try:
            from ..core.settings_store import get_settings_store

            stored = get_settings_store().get_sync(self.api_key_env)
        except Exception:  # noqa: BLE001 - 配置中心不可用时必须退回 .env，不能让数据源崩掉
            stored = ""
        if stored:
            return str(stored).strip()
        return (getattr(self.settings, self.api_key_env, "") or "").strip()

    def is_configured(self) -> bool:
        if not self.requires_api_key:
            return True
        return bool(self.api_key())

    def assert_configured(self) -> None:
        """未配置时抛错：由上层显示为「数据源未配置」，绝不生成假数据。"""
        if not self.is_configured():
            raise ProviderNotConfiguredError(self.name, f"{self.api_key_env}")

    # -------------------------------------------------------------- 能力
    def supports(self, capability: str) -> bool:
        """能力可用性 = 声明 + 已实现 + 凭据齐备。"""
        if capability not in self.capabilities:
            return False
        method = CAPABILITY_METHODS.get(capability)
        if not method or not hasattr(self, method):
            return False
        return self.is_configured()

    def available_capabilities(self) -> list[str]:
        return [c for c in CAPABILITY_METHODS if self.supports(c)]

    async def call(self, capability: str, **kwargs: Any) -> ProviderResult:
        """统一能力调用入口（Router 通过它驱动任意 Provider）。"""
        if capability not in CAPABILITY_METHODS:
            raise ProviderError(
                f"未知能力 {capability}", provider=self.name, failure_type="data_format_error"
            )
        method_name = CAPABILITY_METHODS[capability]
        func = getattr(self, method_name, None)
        if func is None:
            raise ProviderError(
                f"{self.name} 未实现能力 {capability}",
                provider=self.name,
                failure_type="data_format_error",
            )
        # 缺凭据要在真正请求之前就暴露
        if self.requires_api_key and not self.is_configured():
            raise ProviderNotConfiguredError(self.name, self.api_key_env)
        result: ProviderResult = await func(**kwargs)
        if result.trace is None:
            result.trace = FetchTrace(
                provider=self.name, endpoint=method_name, request_params=kwargs, fetch_time=result.fetch_time
            )
        if result.provider != self.name:
            result.provider = self.name
        return result

    async def close(self) -> None:
        await self.transport.close()

    # -------------------------------------------------------------- 健康检查
    async def health_check(self) -> dict[str, Any]:
        """默认健康检查：取一次当前价格类能力。返回 dict 供 HealthCenter 汇总。"""
        started = datetime.now(timezone.utc)
        try:
            if self.supports("price"):
                res = await self.call("price", symbol="BTC")
            elif self.supports("sentiment"):
                res = await self.call("sentiment")
            elif self.supports("onchain_basic"):
                res = await self.call("onchain_basic")
            elif self.supports("macro_series"):
                res = await self.call("macro_series", series_id="DXY", limit=1)
            else:
                # 没有任何可用于探活的能力：只判定凭据是否正常
                self.assert_configured()
                return {"ok": True, "note": "无可用探活能力，仅校验配置"}
            delay = (datetime.now(timezone.utc) - (res.observation_time or started)).total_seconds()
            return {"ok": True, "latency_ms": res.latency_ms, "delay": max(0.0, delay)}
        except Exception as exc:  # noqa: BLE001
            failure_type = exc.failure_type if isinstance(exc, ProviderError) else "unknown"
            return {"ok": False, "failure_type": failure_type, "message": str(exc)}

    # -------------------------------------------------------------- 便捷构造器
    def ok(
        self,
        data: Any,
        category: DataCategory,
        *,
        observation_time: datetime | None = None,
        latency_ms: float = 0.0,
        quality: QualityStatus = QualityStatus.SINGLE_SOURCE,
        confidence: float = 1.0,
        endpoint: str = "",
        params: dict[str, Any] | None = None,
        raw: Any = None,
        **meta: Any,
    ) -> ProviderResult:
        """构造标准 ProviderResult，强制补齐来源信息。"""
        now = datetime.now(timezone.utc)
        return ProviderResult(
            data=data,
            provider=self.name,
            category=category,
            observation_time=observation_time or now,
            fetch_time=now,
            latency_ms=latency_ms,
            quality=quality,
            confidence=confidence,
            trace=FetchTrace(
                provider=self.name,
                endpoint=endpoint,
                request_params=params or {},
                latency_ms=latency_ms,
                fetch_time=now,
                raw_payload=raw if self.settings.APP_ENV != "production" else None,
            ),
            meta={"display_name": self.display_name, **meta},
        )

    # -------------------------------------------------------------- 默认实现
    async def fetch_price(self, symbol: str = "BTC", **_: Any) -> ProviderResult[float]:
        raise ProviderError(
            f"{self.name} 不支持实时价格", provider=self.name, failure_type="data_format_error"
        )

    async def fetch_ohlcv(
        self, symbol: str = "BTC", interval: str = "1d", limit: int = 500, **_: Any
    ) -> ProviderResult[list[Candle]]:
        raise ProviderError(
            f"{self.name} 不支持 K 线", provider=self.name, failure_type="data_format_error"
        )

    async def fetch_ticker_24h(self, symbol: str = "BTC", **_: Any) -> ProviderResult[dict[str, Any]]:
        raise ProviderError(
            f"{self.name} 不支持 24H 行情", provider=self.name, failure_type="data_format_error"
        )

    async def fetch_orderbook(self, symbol: str = "BTC", limit: int = 20, **_: Any) -> ProviderResult[dict]:
        raise ProviderError(
            f"{self.name} 不支持订单簿", provider=self.name, failure_type="data_format_error"
        )

    async def fetch_market_cap(self, symbol: str = "BTC", **_: Any) -> ProviderResult[dict[str, Any]]:
        raise ProviderError(
            f"{self.name} 不支持市值数据", provider=self.name, failure_type="data_format_error"
        )

    async def fetch_funding(self, symbol: str = "BTC", **_: Any) -> ProviderResult[dict[str, Any]]:
        raise ProviderError(
            f"{self.name} 不支持资金费率", provider=self.name, failure_type="data_format_error"
        )

    async def fetch_open_interest(self, symbol: str = "BTC", **_: Any) -> ProviderResult[dict[str, Any]]:
        raise ProviderError(
            f"{self.name} 不支持持仓量", provider=self.name, failure_type="data_format_error"
        )

    async def fetch_liquidation(self, symbol: str = "BTC", **_: Any) -> ProviderResult[dict[str, Any]]:
        raise ProviderError(
            f"{self.name} 不支持清算数据", provider=self.name, failure_type="data_format_error"
        )

    async def fetch_long_short_ratio(self, symbol: str = "BTC", **_: Any) -> ProviderResult[dict[str, Any]]:
        raise ProviderError(
            f"{self.name} 不支持多空比", provider=self.name, failure_type="data_format_error"
        )

    async def fetch_onchain_basic(self, **_: Any) -> ProviderResult[dict[str, Any]]:
        raise ProviderError(
            f"{self.name} 不支持基础链上数据", provider=self.name, failure_type="data_format_error"
        )

    async def fetch_onchain_advanced(self, **_: Any) -> ProviderResult[dict[str, Any]]:
        raise ProviderError(
            f"{self.name} 不支持高级链上指标", provider=self.name, failure_type="data_format_error"
        )

    async def fetch_exchange_reserve(self, **_: Any) -> ProviderResult[dict[str, Any]]:
        raise ProviderError(
            f"{self.name} 不支持交易所储备数据", provider=self.name, failure_type="data_format_error"
        )

    async def fetch_exchange_flow(self, **_: Any) -> ProviderResult[dict[str, Any]]:
        raise ProviderError(
            f"{self.name} 不支持交易所资金流", provider=self.name, failure_type="data_format_error"
        )

    async def fetch_etf_flow(self, **_: Any) -> ProviderResult[dict[str, Any]]:
        raise ProviderError(
            f"{self.name} 不支持 ETF 资金流", provider=self.name, failure_type="data_format_error"
        )

    async def fetch_macro_series(self, series_id: str = "DXY", limit: int = 365, **_: Any) -> ProviderResult[list]:
        raise ProviderError(
            f"{self.name} 不支持宏观序列", provider=self.name, failure_type="data_format_error"
        )

    async def fetch_sentiment(self, **_: Any) -> ProviderResult[dict[str, Any]]:
        raise ProviderError(
            f"{self.name} 不支持情绪指标", provider=self.name, failure_type="data_format_error"
        )

    # -------------------------------------------------------------- 元信息导出
    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "homepage": self.homepage,
            "categories": sorted(c.value for c in self.categories),
            "capabilities": self.available_capabilities(),
            "declared_capabilities": sorted(self.capabilities),
            "requires_api_key": self.requires_api_key,
            "api_key_env": self.api_key_env,
            "configured": self.is_configured(),
            "default_priority": self.default_priority,
            "current_priority": self.manual_priority if self.manual_priority is not None else self.default_priority,
            "enabled": self.enabled,
            "disabled_reason": self.disabled_reason,
            "locked": self.locked,
            "base_url": self.base_url,
            "proxy": provider_proxy_label(self.transport),
            "timeout": f"{self.transport.timeout_connect}s/{self.transport.timeout_read}s",
            "retries": self.transport.retries,
            "qps": self.rate_limit_qps,
            "region_hint": self.region_hint,
            "notes": self.notes,
        }


def provider_proxy_label(transport: ProviderTransport) -> str:
    """Provider 清单里展示「走不走代理」——只给掩码，密码绝不下发。"""
    info = transport.describe_proxy()
    if info.get("invalid"):
        return f"代理地址有误：{info.get('error')}"
    if not info.get("set"):
        return "直连"
    return str(info.get("masked") or "直连")


def discover_provider_classes(module: Any) -> Iterable[type[DataProvider]]:
    """从一个模块中发现所有具体 Provider 类。"""
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if issubclass(obj, DataProvider) and obj is not DataProvider and getattr(obj, "name", "") not in ("abstract", ""):
            yield obj
