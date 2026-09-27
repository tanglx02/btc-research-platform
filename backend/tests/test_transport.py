# -*- coding: utf-8 -*-
"""网络故障分类 + 重试/退避策略测试（不访问真实网络）。

对应需求：
  * 429 降频策略          -> rate_limited（backoff，不在本轮重试）
  * 500/502/503           -> server_error（retry_failover）
  * 403                   -> forbidden（临时停用，避免无效请求）
  * 401                   -> auth_error（提醒管理员，不无限重试）
  * DNS 失败              -> dns_error（failover）
  * 超时                  -> timeout（failover，硬超时不拖垮服务）
"""

from __future__ import annotations

import httpx
import pytest

from app.core.errors import FAILURE_POLICY
from app.providers.transport import ProviderTransport


def transport() -> ProviderTransport:
    return ProviderTransport("unit_test", base_url="https://example.invalid", qps=100.0)


@pytest.mark.parametrize("exc_factory,expected", [
    (lambda: httpx.ConnectTimeout("timeout"), "timeout"),
    (lambda: httpx.ReadTimeout("timeout"), "timeout"),
    (lambda: httpx.ConnectError("[Errno -2] Name or service not known"), "dns_error"),
    (lambda: httpx.ConnectError("Proxy connection failed"), "proxy_error"),
    (lambda: httpx.ConnectError("connection refused"), "connect_error"),
    (lambda: httpx.RemoteProtocolError("bad response"), "data_format_error"),
])
def test_classify_transport_errors(exc_factory, expected):
    ftype, _ = ProviderTransport.classify(exc_factory())
    assert ftype == expected


@pytest.mark.parametrize("status,expected", [
    (401, "auth_error"),
    (403, "forbidden"),
    (429, "rate_limited"),
    (418, "rate_limited"),
    (500, "server_error"),
    (502, "server_error"),
    (503, "server_error"),
    (504, "timeout"),
])
def test_classify_http_status(status, expected):
    request = httpx.Request("GET", "https://example.invalid/x")
    response = httpx.Response(status, request=request)
    ftype, _ = ProviderTransport.classify(httpx.HTTPStatusError("err", request=request, response=response))
    assert ftype == expected


def test_retry_policy_matches_documented_strategy():
    """故障分类 -> 处置策略表必须与设计文档一致。"""
    assert FAILURE_POLICY["rate_limited"] == "backoff"
    assert FAILURE_POLICY["server_error"] == "retry_failover"
    assert FAILURE_POLICY["forbidden"] == "disable_temporary"
    assert FAILURE_POLICY["auth_error"] == "alert_admin"
    assert FAILURE_POLICY["dns_error"] == "failover"
    assert FAILURE_POLICY["timeout"] == "failover"
    # 「不支持的输入」只切源，不停用 Provider
    assert FAILURE_POLICY["unsupported_input"] == "failover"
    assert FAILURE_POLICY["range_unsupported"] == "failover"


def test_should_not_retry_on_credentials_or_format():
    t = transport()
    assert t._should_retry("auth_error", 401) is False
    assert t._should_retry("forbidden", 403) is False
    assert t._should_retry("data_format_error", None) is False
    assert t._should_retry("rate_limited", 429) is False   # 429 走退避降频
    assert t._should_retry("server_error", 500) is True


def test_per_provider_proxy_and_timeout_configuration():
    """每个 Provider 必须能单独配置代理/超时/重试 —— 国内网络环境的硬需求。"""
    t = ProviderTransport(
        "unit_test",
        base_url="https://example.invalid",
        timeout_connect=1.5,
        timeout_read=2.5,
        retries=4,
        proxy="socks5://127.0.0.1:1080",
        qps=1.0,
    )
    assert t.timeout_connect == 1.5
    assert t.timeout_read == 2.5
    assert t.retries == 4
    assert t.proxy_url == "socks5://127.0.0.1:1080"


def test_token_bucket_rate_limit():
    import asyncio

    from app.providers.transport import TokenBucket

    async def run() -> None:
        bucket = TokenBucket(rate=10.0, capacity=2.0)
        # 桶内已有 2 个令牌，前两次应立即通过
        await bucket.acquire()
        await bucket.acquire()
        # 第三次必须等待补充
        import time

        started = time.perf_counter()
        await bucket.acquire()
        assert time.perf_counter() - started < 0.5

    asyncio.run(run())
