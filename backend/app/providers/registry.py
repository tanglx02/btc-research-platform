# -*- coding: utf-8 -*-
"""Provider 注册表：自动发现、分类、优先级编排、动态启用/禁用。

同一份「数据类别 -> Provider 优先级链」就是整个系统的高可用骨架：
  主源 -> 备 1 -> 备 2 -> ... -> 备 N
链的顺序可由健康评分自动微调，但管理员锁定的条目永远优先。
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import pkgutil
from threading import RLock
from typing import Any, Iterable

from ..core.config import get_settings
from ..core.logging import get_logger
from .base import CAPABILITY_METHODS, DataProvider, discover_provider_classes
from .health import get_health_center
from .types import DataCategory, QualityStatus

logger = get_logger(__name__)

# 数据类别 -> 所需能力
CATEGORY_CAPABILITY: dict[DataCategory, str] = {
    DataCategory.MARKET_PRICE: "price",
    DataCategory.MARKET_TICKER_24H: "ticker_24h",
    DataCategory.OHLCV: "ohlcv",
    DataCategory.ORDERBOOK: "orderbook",
    DataCategory.MARKET_CAP: "market_cap",
    DataCategory.DERIVATIVES_FUNDING: "funding",
    DataCategory.DERIVATIVES_OI: "open_interest",
    DataCategory.DERIVATIVES_LIQUIDATION: "liquidation",
    DataCategory.ONCHAIN_BASIC: "onchain_basic",
    DataCategory.ONCHAIN_ADVANCED: "onchain_advanced",
    DataCategory.EXCHANGE_FLOW: "exchange_flow",
    DataCategory.ETF_FLOW: "etf_flow",
    DataCategory.MACRO_SERIES: "macro_series",
    DataCategory.SENTIMENT_INDEX: "sentiment",
}


class ProviderRegistry:
    def __init__(self, *, include_mocks: bool = False) -> None:
        self.s = get_settings()
        self.health = get_health_center()
        self._providers: dict[str, DataProvider] = {}
        self._lock = RLock()
        self._chain_cache: dict[DataCategory, list[str]] = {}
        self.include_mocks = include_mocks

    # ---------------------------------------------------------------- 发现与注册
    def autodiscover(self) -> int:
        """扫描 `app.providers.impl` 包，实例化所有 Provider。"""
        import app.providers.impl as impl_pkg  # noqa: PLC0415

        count = 0
        for _, mod_name, _ in pkgutil.iter_modules(impl_pkg.__path__):
            if mod_name.startswith("mock_") and not self.include_mocks:
                continue
            if mod_name.startswith("_"):
                continue
            module = importlib.import_module(f"app.providers.impl.{mod_name}")
            for cls in discover_provider_classes(module):
                self.register(cls(settings=self.s))
                count += 1
        logger.event("registry.autodiscover", count=count)
        self._apply_overrides()
        self._chain_cache.clear()
        return count

    def register(self, provider: DataProvider) -> None:
        if not inspect.isclass(provider) and provider.name:
            with self._lock:
                self._providers[provider.name] = provider
            self.health.snapshot(
                provider.name,
                display_name=provider.display_name,
                enabled=provider.enabled,
                configured=provider.is_configured(),
                priority=provider.manual_priority or provider.default_priority,
            )

    # ---------------------------------------------------------------- 查询
    def all(self) -> list[DataProvider]:
        with self._lock:
            return list(self._providers.values())

    def get(self, name: str) -> DataProvider | None:
        with self._lock:
            return self._providers.get(name)

    def require(self, name: str) -> DataProvider:
        p = self.get(name)
        if not p:
            raise KeyError(f"未注册的 Provider: {name}")
        return p

    def _effective_priority(self, p: DataProvider) -> tuple[int, float]:
        """排序键：(优先级, -健康分)。管理员锁定优先级的 Provider 排前面。"""
        snap = self.health.snapshot(p.name)
        priority = p.manual_priority if p.manual_priority is not None else p.default_priority
        # 未锁定时允许健康分微调（同优先级组内按健康分降序）
        return (0 if p.locked else 1, priority, -snap.score)

    def providers_for(self, category: DataCategory) -> list[DataProvider]:
        """返回该类别可用 Provider 的有序列表（主 -> 备 N）。"""
        cap = CATEGORY_CAPABILITY[category]
        with self._lock:
            candidates = [p for p in self._providers.values() if category in p.categories and p.supports(cap)]
            candidates.sort(key=self._effective_priority)
            return candidates

    def chain_names(self, category: DataCategory) -> list[str]:
        return [p.name for p in self.providers_for(category)]

    def primary_for(self, category: DataCategory) -> DataProvider | None:
        chain = self.providers_for(category)
        return chain[0] if chain else None

    # ---------------------------------------------------------------- 运行时运维
    def set_enabled(self, name: str, enabled: bool, reason: str = "") -> None:
        p = self.require(name)
        p.enabled = enabled
        p.disabled_reason = "" if enabled else (reason or "管理员手动禁用")
        self.health.reset_stats(name)
        self._chain_cache.clear()
        logger.event("registry.set_enabled", provider=name, enabled=enabled, reason=p.disabled_reason)

    def set_priority(self, name: str, priority: int, *, lock: bool = True) -> None:
        p = self.require(name)
        p.manual_priority = priority
        p.locked = lock
        self._chain_cache.clear()
        logger.event("registry.set_priority", provider=name, priority=priority, locked=lock)

    def unlock(self, name: str) -> None:
        p = self.require(name)
        p.locked = False
        p.manual_priority = None
        self._chain_cache.clear()

    def provider_moved_to_backup(self, name: str) -> None:
        """Provider 故障后降权：-5 但不直接拉黑，保留其恢复可能。"""
        p = self.require(name)
        if p.locked:
            return
        base = p.manual_priority if p.manual_priority is not None else p.default_priority
        p.manual_priority = base + 5
        self._chain_cache.clear()

    def maybe_promote_recovered(self) -> list[str]:
        """检查是否有 Provider 满足恢复阈值，恢复其原有优先级。返回恢复的 Provider 列表。"""
        promoted: list[str] = []
        with self._lock:
            for p in self._providers.values():
                if p.locked or p.manual_priority is None:
                    continue
                if p.manual_priority != p.default_priority and self.health.can_promote(p.name):
                    p.manual_priority = p.default_priority
                    promoted.append(p.name)
                    logger.event("registry.promote_recovered", provider=p.name)
        if promoted:
            self._chain_cache.clear()
        return promoted

    # ---------------------------------------------------------------- 配置覆盖
    def _apply_overrides(self) -> None:
        overrides = self.s.provider_overrides
        for category_key, cfg in overrides.items():
            try:
                category = DataCategory(category_key)
            except ValueError:
                continue
            for name in cfg.get("disabled", []):
                if self.get(name):
                    self.set_enabled(name, False, "配置显式禁用")
            for idx, name in enumerate(cfg.get("order", [])):
                p = self.get(name)
                if p:
                    self.set_priority(name, idx, lock=True)
            primary = cfg.get("primary")
            if primary and self.get(primary):
                self.set_priority(primary, 0, lock=True)

    # ---------------------------------------------------------------- 导出
    def describe_all(self) -> list[dict[str, Any]]:
        out = []
        for p in sorted(self.all(), key=lambda x: (x.manual_priority if x.manual_priority is not None else x.default_priority)):
            d = p.describe()
            d["health"] = self.health.snapshot(
                p.name,
                display_name=p.display_name,
                enabled=p.enabled,
                configured=p.is_configured(),
                priority=p.manual_priority if p.manual_priority is not None else p.default_priority,
                locked=p.locked,
                categories=sorted(c.value for c in p.categories),
            ).to_dict()
            out.append(d)
        return out

    def category_matrix(self) -> list[dict[str, Any]]:
        """每个数据类别的 Provider 链及其健康情况（数据质量中心使用）。"""
        matrix = []
        for cat in DataCategory:
            chain = self.providers_for(cat)
            unconfigured = [
                p.name
                for p in self.all()
                if cat in p.categories and not p.is_configured()
            ]
            disabled = [p.name for p in self.all() if cat in p.categories and not p.enabled]
            rows = []
            for idx, p in enumerate(chain):
                snap = self.health.snapshot(
                    p.name,
                    display_name=p.display_name,
                    enabled=p.enabled,
                    configured=p.is_configured(),
                    priority=idx,
                    is_backup=idx > 0,
                    locked=p.locked,
                    categories=sorted(c.value for c in p.categories),
                )
                rows.append({"role": "primary" if idx == 0 else f"backup_{idx}", **snap.to_dict()})
            matrix.append(
                {
                    "category": cat.value,
                    "capability": CATEGORY_CAPABILITY[cat],
                    "provider_count": len(chain),
                    "providers": rows,
                    "unconfigured": unconfigured,
                    "disabled": disabled,
                    "status": rows[0]["status"] if rows else "NO_PROVIDER",
                    "degraded_ok": True,  # 该类别可在无 Provider 时降级运行
                }
            )
        return matrix

    async def close_all(self) -> None:
        for p in self.all():
            try:
                await p.close()
            except Exception:  # noqa: BLE001
                pass


_registry_singleton: ProviderRegistry | None = None


def get_registry() -> ProviderRegistry:
    global _registry_singleton
    if _registry_singleton is None:
        _registry_singleton = ProviderRegistry()
        _registry_singleton.autodiscover()
    return _registry_singleton


def reset_registry() -> ProviderRegistry:
    """测试用：重建注册表。"""
    global _registry_singleton
    _registry_singleton = ProviderRegistry(include_mocks=True)
    _registry_singleton.autodiscover()
    return _registry_singleton
