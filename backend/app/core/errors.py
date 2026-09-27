# -*- coding: utf-8 -*-
"""统一类型化错误体系：客户端只看到规范化结构，绝不返回堆栈或内部细节。"""

from __future__ import annotations

from typing import Any

# Provider 故障分类 -> 处置策略映射（见设计文档「API 故障策略」）
FAILURE_POLICY: dict[str, str] = {
    "timeout": "failover",
    "connect_error": "failover",
    "dns_error": "failover",
    "proxy_error": "failover",
    "tls_error": "failover",
    "rate_limited": "backoff",          # 429：降低频率，必要时 failover
    "server_error": "retry_failover",   # 500/502/503
    "forbidden": "disable_temporary",   # 403
    "auth_error": "alert_admin",        # 密钥无效：不无限重试
    "not_configured": "alert_admin",    # 未配置：显示「数据源未配置」
    "data_format_error": "disable_temporary",
    "data_quality_error": "failover",
    "empty_data": "failover",
    "stale_data": "failover",
    # 「这个数据源本来就不提供这个序列/周期」≠ 故障：只切下一个源，不能因此停用该 Provider，
    # 否则一个不支持的序列会把整个健康的数据源连坐下线。
    "unsupported_input": "failover",
    "range_unsupported": "failover",
    "unknown": "failover",
}


class AppError(Exception):
    """所有业务/基础错误基类。"""

    http_status: int = 500
    code: str = "internal_error"
    retryable: bool = False

    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
                "detail": self.detail,
            }
        }


# ---------------- 通用 ----------------
class ValidationError(AppError):
    http_status, code = 400, "validation_error"


class NotFoundError(AppError):
    http_status, code = 404, "not_found"


class UnauthorizedError(AppError):
    http_status, code = 401, "unauthorized"


class ForbiddenError(AppError):
    http_status, code = 403, "forbidden"


class RateLimitedError(AppError):
    http_status, code, retryable = 429, "rate_limited", True


class ServiceUnavailableError(AppError):
    http_status, code, retryable = 503, "service_unavailable", True


# ---------------- Provider 领域 ----------------
class ProviderError(AppError):
    """Provider 调用失败。携带 failure_type 供 Failover 引擎做策略决策。"""

    http_status = 502
    code = "provider_error"

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        failure_type: str = "unknown",
        http_status_code: int | None = None,
        retryable: bool = True,
        **detail: Any,
    ) -> None:
        super().__init__(message, **detail)
        self.provider = provider
        self.failure_type = failure_type
        self.http_status_code = http_status_code
        self.retryable = retryable

    @property
    def policy(self) -> str:
        return FAILURE_POLICY.get(self.failure_type, "failover")

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "provider": self.provider,
                "failure_type": self.failure_type,
                "policy": self.policy,
                "retryable": self.retryable,
                "detail": self.detail,
            }
        }


class ProviderNotConfiguredError(ProviderError):
    """Provider 缺少必要凭据：必须明确显示「数据源未配置」，绝不伪造数据。"""

    code = "provider_not_configured"

    def __init__(self, provider: str, what: str = "API Key") -> None:
        super().__init__(
            f"数据源 {provider} 未配置（缺少 {what}），当前无法提供该数据",
            provider=provider,
            failure_type="not_configured",
            retryable=False,
        )


class AllProvidersFailedError(AppError):
    """所有候选 Provider 均失败：系统必须返回最近一次可信数据并标记 stale，而不是假数据。"""

    http_status = 503
    code = "all_providers_failed"

    def __init__(self, category: str, attempts: list[dict[str, Any]], last_known: Any = None) -> None:
        super().__init__(f"数据类别 {category} 的所有数据源均不可用，已使用最近一次可信数据")
        self.category = category
        self.attempts = attempts
        self.last_known = last_known

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "category": self.category,
                "attempts": self.attempts,
                "stale_data_available": self.last_known is not None,
            }
        }


class DataQualityError(AppError):
    http_status, code = 422, "data_quality_error"


class NoDataError(AppError):
    """真实缺失：明确「无数据」，不用占位值伪装。"""

    http_status, code = 404, "no_data"
