# -*- coding: utf-8 -*-
"""Resilient Router —— 自动故障切换引擎。

    Provider A 失败 -> 自动切换 B -> 成功 -> 使用 B -> 记录 Failover Event
    全部失败        -> 抛 AllProvidersFailedError（由 service 层回落到本地最近可信数据，标记 stale）

绝不生成假数据；所有尝试、原因、延迟、HTTP 状态都会被记录并可追溯。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

from ..core.config import get_settings
from ..core.errors import AllProvidersFailedError, ProviderError
from ..core.logging import get_logger
from .base import DataProvider
from .registry import CATEGORY_CAPABILITY, ProviderRegistry, get_registry
from .types import (
    DataCategory,
    ProviderAttempt,
    ProviderResult,
    QualityStatus,
    RoutedResult,
)
from .validation import SourceValue, cross_validate

logger = get_logger(__name__)

T = TypeVar("T")


class ResilientRouter:
    """按数据类别执行「主源 -> 备用源」链路的弹性取数。"""

    def __init__(self, registry: ProviderRegistry | None = None) -> None:
        self.s = get_settings()
        self.registry = registry or get_registry()
        self.health = self.registry.health

    # ------------------------------------------------------------------ 核心
    async def _try_one(self, provider: DataProvider, capability: str, priority: int, kwargs: dict[str, Any]) -> tuple[ProviderResult | None, ProviderAttempt]:
        started = time.perf_counter()
        try:
            # 单 Provider 硬超时兜底：不能超过超时太多，否则拖垮整条链路
            timeout = provider.transport.timeout_read + provider.transport.timeout_connect + 5
            result = await asyncio.wait_for(provider.call(capability, **kwargs), timeout=timeout)
            latency = (time.perf_counter() - started) * 1000
            result.latency_ms = result.latency_ms or latency
            ok, reason = self._sanity(provider, capability, result)
            if not ok:
                raise ProviderError(
                    reason,
                    provider=provider.name,
                    failure_type="data_quality_error",
                )
            await self.health.record_success(
                provider.name,
                latency_ms=result.latency_ms,
                http_status=(result.trace.http_status if result.trace else None),
            )
            self.registry.maybe_promote_recovered()
            return result, ProviderAttempt(provider=provider.name, ok=True, latency_ms=result.latency_ms, priority=priority)
        except asyncio.TimeoutError as exc:
            latency = (time.perf_counter() - started) * 1000
            await self.health.record_failure(provider.name, "timeout", "整体调用超时", latency_ms=latency)
            self.registry.provider_moved_to_backup(provider.name)
            return None, ProviderAttempt(provider=provider.name, ok=False, latency_ms=latency,
                                         failure_type="timeout", message="调用超时", priority=priority)
        except ProviderError as exc:
            latency = (time.perf_counter() - started) * 1000
            await self.health.record_failure(
                provider.name, exc.failure_type, exc.message, latency_ms=latency, http_status=exc.http_status_code
            )
            if exc.failure_type in ("auth_error", "forbidden", "data_format_error"):
                provider.enabled = False
                provider.disabled_reason = f"自动停用：{exc.failure_type} - {exc.message[:120]}"
                logger.event("provider.auto_disable", provider=provider.name, reason=exc.failure_type)
            else:
                self.registry.provider_moved_to_backup(provider.name)
            return None, ProviderAttempt(provider=provider.name, ok=False, latency_ms=latency,
                                         failure_type=exc.failure_type,
                                         http_status=exc.http_status_code,
                                         message=exc.message[:300], priority=priority)
        except Exception as exc:  # noqa: BLE001 - 兜底，绝不让单个 Provider 异常冒泡炸掉系统
            latency = (time.perf_counter() - started) * 1000
            await self.health.record_failure(provider.name, "unknown", str(exc), latency_ms=latency)
            self.registry.provider_moved_to_backup(provider.name)
            return None, ProviderAttempt(provider=provider.name, ok=False, latency_ms=latency,
                                         failure_type="unknown", message=str(exc)[:300], priority=priority)

    @staticmethod
    def _sanity(provider: DataProvider, capability: str, result: ProviderResult) -> tuple[bool, str]:
        """数据出口处的最后一道防线：坏数据不允许进入业务层。"""
        if result is None or result.data is None:
            return False, "返回空数据"
        if capability in ("price",):
            price = float(result.data)
            if price <= 0 or price > 10_000_000:
                return False, f"价格越界: {price}"
        if capability == "ohlcv":
            if not isinstance(result.data, list) or len(result.data) == 0:
                return False, "K 线数据为空"
        return True, ""

    # ------------------------------------------------------------------ 取数
    async def fetch(self, category: DataCategory, **kwargs: Any) -> RoutedResult:
        """按优先级链自动故障切换取数。全部失败抛 AllProvidersFailedError。"""
        capability = CATEGORY_CAPABILITY[category]
        chain = self.registry.providers_for(category)
        primary_name = chain[0].name if chain else ""

        if not chain:
            raise AllProvidersFailedError(category.value, [], None)

        # ---- 熔断：跳过近期连续失败的 Provider，避免被黑洞源拖慢整条链路
        healthy = [p for p in chain if p.enabled and p.is_configured() and not self.health.in_cooldown(p.name)]
        cooled_down = [
            p for p in chain
            if p.enabled and p.is_configured() and self.health.in_cooldown(p.name)
        ]
        ordered = healthy or chain  # 全部熔断时才勉强全量尝试一次

        attempts: list[ProviderAttempt] = []
        first_failure_type = ""

        # ---- 被跳过的数据源也要在 attempts 里留痕。
        # 前端要能回答「为什么没用它」：是被管理员停用、没配 Key，还是正在熔断中。
        # 没有痕迹的话，用户只能看到一个结果，无从判断是否有人为干预。
        for provider in chain:
            if provider in ordered:
                continue
            if not provider.enabled:
                failure_type, msg = "disabled", provider.disabled_reason or "已禁用"
            elif not provider.is_configured():
                failure_type, msg = "not_configured", "数据源未配置（缺少 Key）"
            elif self.health.in_cooldown(provider.name):
                failure_type = "circuit_open"
                msg = f"熔断中（{self.health.cooldown_remaining(provider.name):.0f}s 内暂不重试）"
            else:
                continue
            attempts.append(
                ProviderAttempt(provider=provider.name, ok=False, failure_type=failure_type,
                                message=msg, priority=99)
            )

        for idx, provider in enumerate(ordered):
            if not provider.enabled:
                attempts.append(ProviderAttempt(provider=provider.name, ok=False, failure_type="disabled",
                                                message="已禁用", priority=idx))
                continue
            if not provider.is_configured():
                attempts.append(ProviderAttempt(provider=provider.name, ok=False, failure_type="not_configured",
                                                message="数据源未配置", priority=idx))
                continue
            result, attempt = await self._try_one(provider, capability, idx, kwargs)
            attempts.append(attempt)
            if result is not None:
                used_fallback = idx > 0
                if used_fallback:
                    await self.health.record_failover(
                        category=category.value,
                        from_provider=primary_name,
                        to_provider=provider.name,
                        reason=first_failure_type or attempt.failure_type or "unknown",
                    )
                return RoutedResult(
                    data=result.data,
                    provider=provider.name,
                    category=category,
                    primary_provider=primary_name,
                    used_fallback=used_fallback,
                    failover_reason=first_failure_type if used_fallback else "",
                    failover_time=datetime.now(timezone.utc) if used_fallback else None,
                    quality=result.quality,
                    confidence=result.confidence * (0.9 if used_fallback else 1.0),
                    observation_time=result.observation_time,
                    fetch_time=result.fetch_time,
                    latency_ms=result.latency_ms,
                    attempts=attempts,
                    trace=result.trace,
                    meta=result.meta,
                )
            if not first_failure_type and attempt.failure_type:
                first_failure_type = attempt.failure_type

        # 已经在上文为「被跳过」的源留过痕，这里只补充「熔断但本轮被迫重试」的情况
        seen = {a.provider for a in attempts}
        for p in cooled_down:
            if p.name in seen:
                continue
            attempts.append(
                ProviderAttempt(
                    provider=p.name,
                    ok=False,
                    failure_type="circuit_open",
                    message=f"熔断中（{self.health.cooldown_remaining(p.name):.0f}s 内暂不重试）",
                    priority=99,
                )
            )

        raise AllProvidersFailedError(category.value, [a.__dict__ for a in attempts], None)

    async def fetch_many(self, category: DataCategory, **kwargs: Any) -> list[ProviderResult]:
        """并行向所有可用 Provider 取数（用于交叉验证/补洞），失败源自动忽略。"""
        capability = CATEGORY_CAPABILITY[category]
        providers = self.registry.providers_for(category)
        tasks = [self._try_one(p, capability, i, kwargs) for i, p in enumerate(providers)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out: list[ProviderResult] = []
        for item in results:
            if isinstance(item, BaseException):
                continue
            res, _ = item
            if res is not None:
                out.append(res)
        return out

    async def fetch_validated(
        self,
        category: DataCategory,
        *,
        extractor: Callable[[Any], float] | None = None,
        tolerance_pct: float | None = None,
        max_sources: int = 3,
        **kwargs: Any,
    ) -> RoutedResult:
        """主源成功后再并行采样其他源做交叉验证。

        - 一致 -> VERIFIED / CROSS_VERIFIED，使用主源值（并记录 median）
        - 冲突 -> CONFLICT，使用 median，置信度大幅下调，前端必须提示
        """
        extract = extractor or (lambda x: float(x))
        main = await self.fetch(category, **kwargs)
        values: list[SourceValue] = [
            SourceValue(
                provider=main.provider,
                value=extract(main.data),
                latency_ms=main.latency_ms,
                observation_time=main.observation_time.isoformat() if main.observation_time else None,
            )
        ]

        others = [p for p in self.registry.providers_for(category)
                  if p.name != main.provider and p.enabled and p.is_configured()][: max_sources - 1]
        if others:
            gathered = await asyncio.gather(
                *[self._try_one(p, CATEGORY_CAPABILITY[category], 99, kwargs) for p in others],
                return_exceptions=True,
            )
            for item in gathered:
                if isinstance(item, BaseException):
                    continue
                res, attempt = item
                if res is None:
                    main.attempts.append(attempt)
                    continue
                try:
                    val = extract(res.data)
                except Exception:  # noqa: BLE001
                    continue
                values.append(
                    SourceValue(
                        provider=res.provider,
                        value=val,
                        latency_ms=res.latency_ms,
                        observation_time=res.observation_time.isoformat() if res.observation_time else None,
                    )
                )
                main.attempts.append(ProviderAttempt(provider=res.provider, ok=True,
                                                     latency_ms=res.latency_ms, priority=99))

        outcome = cross_validate(values, tolerance_pct=tolerance_pct)
        for v in outcome.values:
            await self.health.record_cross_validation(
                v.provider, outcome.verdict != "CONFLICT", outcome.max_deviation_pct
            )

        main.cross_validation = outcome.to_dict()
        main.quality = outcome.quality
        if outcome.verdict == "CONFLICT":
            main.data = outcome.median
            main.confidence = min(main.confidence, 0.55)
        elif outcome.verdict in ("VERIFIED", "CROSS_VERIFIED"):
            main.confidence = min(1.0, main.confidence + (0.05 if outcome.source_count >= 3 else 0.0))
        else:  # SINGLE_SOURCE
            main.confidence = min(main.confidence, 0.8)
        return main

    async def probe_all(self) -> list[dict[str, Any]]:
        """一键测试全部 Provider。"""
        async def probe(p: DataProvider) -> dict[str, Any]:
            started = time.perf_counter()
            try:
                info = await p.health_check()
                latency = (time.perf_counter() - started) * 1000
                if info.get("ok"):
                    await self.health.record_success(p.name, latency_ms=latency)
                else:
                    await self.health.record_failure(
                        p.name, info.get("failure_type", "unknown"), info.get("message", "")
                    )
                return {
                    "provider": p.name,
                    "display_name": p.display_name,
                    "ok": bool(info.get("ok")),
                    "latency_ms": round(latency, 2),
                    "configured": p.is_configured(),
                    "enabled": p.enabled,
                    "message": info.get("message", ""),
                    "failure_type": info.get("failure_type", ""),
                }
            except Exception as exc:  # noqa: BLE001
                return {
                    "provider": p.name,
                    "display_name": p.display_name,
                    "ok": False,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                    "configured": p.is_configured(),
                    "enabled": p.enabled,
                    "message": str(exc)[:200],
                    "failure_type": getattr(exc, "failure_type", "unknown"),
                }

        providers = self.registry.all()
        return await asyncio.gather(*[probe(p) for p in providers])


_router_singleton: ResilientRouter | None = None


def get_router() -> ResilientRouter:
    global _router_singleton
    if _router_singleton is None:
        _router_singleton = ResilientRouter(get_registry())
    return _router_singleton
