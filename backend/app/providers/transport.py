# -*- coding: utf-8 -*-
"""网络传输层：每个 Provider 独立的代理 / 超时 / 重试 / 退避 / 限流 / 连接池。

故障分类原则（决定后续 Failover 行为）：
- DNS/连接/TLS/读超时  -> failover（换源最快）
- 429                  -> 指数退避 + 降频
- 500/502/503          -> 重试后 failover
- 401/403              -> 标记不可用/提示admin，不无限重试
- 返回格式异常          -> 停用该 Provider 并告警
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

import httpx

from ..core.config import Settings, get_settings
from ..core.errors import ProviderError
from ..core.logging import get_logger
from ..core.proxy import describe_proxy, mask_proxy, normalize_proxy_url

logger = get_logger(__name__)


class TokenBucket:
    """简单令牌桶，避免触发对方限流。"""

    def __init__(self, rate: float, capacity: float | None = None) -> None:
        self.rate = max(rate, 0.05)
        self.capacity = capacity or max(2.0, rate * 2)
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._updated
            self._updated = now
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            if self._tokens < 1:
                wait = (1 - self._tokens) / self.rate
                await asyncio.sleep(wait)
                self._tokens = 0.0
            else:
                self._tokens -= 1


class ProviderTransport:
    """单个 Provider 独占的 HTTP 传输配置与执行器。"""

    def __init__(
        self,
        provider_name: str,
        *,
        base_url: str = "",
        timeout_connect: float | None = None,
        timeout_read: float | None = None,
        retries: int | None = None,
        backoff_base: float | None = None,
        proxy: str = "",
        headers: dict[str, str] | None = None,
        qps: float | None = None,
        max_connections: int | None = None,
        settings: Settings | None = None,
        # False = 忽略全局代理，强制直连。只有「对照测试」这类场景才用。
        allow_global_proxy: bool = True,
    ) -> None:
        self.s = settings or get_settings()
        self.provider_name = provider_name
        self.base_url = base_url.rstrip("/")
        self.timeout_connect = timeout_connect or self.s.DEFAULT_TIMEOUT_CONNECT
        self.timeout_read = timeout_read or self.s.DEFAULT_TIMEOUT_READ
        self.retries = self.s.DEFAULT_RETRIES if retries is None else retries
        self.backoff_base = backoff_base or self.s.DEFAULT_BACKOFF_BASE
        self.headers = {
            "User-Agent": self.s.USER_AGENT,
            "Accept": "application/json,text/csv,*/*",
            **(headers or {}),
        }
        self.bucket = TokenBucket(qps or self.s.RATE_LIMIT_DEFAULT_QPS)
        self.max_connections = max_connections or self.s.MAX_CONNECTIONS_PER_PROVIDER
        self._client: httpx.AsyncClient | None = None
        self._client_proxy: str | None = None  # 建连接时用的代理，用于感知「代理被改了」
        self._client_dns: str = ""             # 建连接时的 DNS 指纹，用于感知「DNS 策略被改了」
        self.proxy = proxy or ""
        self._allow_global_proxy = allow_global_proxy

    # ------------------------------------------------------------------ client
    @property
    def proxy_url(self) -> str | None:
        """当前生效的代理（已归一化），没有则返回 None。

        优先级：**Provider 独立代理 > 全局 SOCKS > 全局 HTTPS > 全局 HTTP**。
        取值在每次建立连接时重新计算 —— 后台改了代理，下一次请求就会走新代理。

        ``allow_global_proxy=False`` 时只认 Provider 独立代理，忽略全局配置。
        这是给「直连对照测试」用的：本机环境可能被注入 HTTP_PROXY，
        不绕过的话对照组根本不是真的直连。
        """
        raw = self.proxy
        if not raw and self._allow_global_proxy:
            raw = self.s.SOCKS_PROXY or self.s.HTTPS_PROXY or self.s.HTTP_PROXY
        if not raw or not str(raw).strip():
            return None
        try:
            return normalize_proxy_url(str(raw))
        except ValueError as exc:
            # 地址写错时宁可明确报错，也不要悄悄直连（用户会误以为是数据源坏了）
            raise ProviderError(
                f"代理地址无法识别：{exc}（请检查「系统设置 → 网络与代理」，"
                "标准写法 socks5://用户名:密码@IP:端口）",
                provider=self.provider_name,
                failure_type="proxy_error",
            ) from exc

    def _dns_mode(self) -> str:
        return str(getattr(self.s, "DNS_MODE", "system") or "system").strip().lower()

    def _doh_endpoints(self) -> list[str]:
        raw = str(getattr(self.s, "DNS_DOH_ENDPOINTS", "") or "")
        return [x.strip() for x in raw.split(",") if x.strip()]

    def describe_proxy(self) -> dict[str, object]:
        """对外展示代理信息：**不抛异常、不含密码明文**。

        展示场景（Provider 清单、健康快照）绝不能因为地址写错而整个接口报错。
        """
        try:
            url = self.proxy_url
        except ProviderError as exc:
            return {"set": True, "invalid": True, "error": str(exc), "masked": None,
                    "scheme": None, "host": None, "port": None, "username": None,
                    "has_password": False}
        info = describe_proxy(url)
        # 便于前端提示「这条是数据源独占代理」还是「跟随全局」
        info["source"] = "provider" if (url and self.proxy) else ("global" if url else "none")
        if info["source"] == "none":
            env_proxy = self._env_proxy()
            if env_proxy:
                info.update(set=True, source="env", masked=mask_proxy(env_proxy))
        return info

    def _env_proxy(self) -> str | None:
        """操作系统环境变量里的代理。只有显式打开 HTTP_TRUST_ENV 时才可能生效。

        写在这里是为了让 UI 能提示「你其实被环境变量代理接管了」——不加这一段的话，
        后台会显示「直连」而请求实际走了别的路径，排障时会被彻底带偏。
        """
        if not getattr(self.s, "HTTP_TRUST_ENV", False) or not self._allow_global_proxy:
            return None
        import os

        for key in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy",
                    "HTTP_PROXY", "http_proxy"):
            value = (os.environ.get(key) or "").strip()
            if value:
                try:
                    return normalize_proxy_url(value)
                except ValueError:
                    continue
        return None

    def _build_client(self, proxy: str | None) -> httpx.AsyncClient:
        timeout = httpx.Timeout(
            connect=self.timeout_connect, read=self.timeout_read, write=self.timeout_read, pool=5.0
        )
        limits = httpx.Limits(
            max_connections=self.max_connections,
            max_keepalive_connections=max(2, self.max_connections // 2),
            keepalive_expiry=30.0,
        )
        transport: httpx.AsyncBaseTransport | None = None

        if proxy and proxy.startswith(("socks",)):
            try:
                from httpx_socks import AsyncProxyTransport  # type: ignore
            except ImportError as exc:  # 依赖缺失 => 明确报错，不让请求裸奔直连
                raise ProviderError(
                    f"SOCKS 代理依赖缺失（{exc}）：请执行 "
                    "pip install httpx-socks python-socks socksio",
                    provider=self.provider_name,
                    failure_type="proxy_error",
                ) from exc
            try:
                transport = AsyncProxyTransport.from_url(proxy)
            except Exception as exc:
                raise ProviderError(
                    f"SOCKS 代理地址无法解析（{mask_proxy(proxy)}）：{exc}",
                    provider=self.provider_name,
                    failure_type="proxy_error",
                ) from exc

        if transport is None and not proxy and self._dns_mode() == "doh":
            # 只有直连时才需要 DoH：走代理时代理方负责解析目的地，本地改写毫无意义。
            # 若 DoH 服务器连不上，resolve() 返回 None，transport 会原样发请求，
            # 即自动退回系统 DNS，不会把本来能用的数据源搞坏。
            try:
                from ..core.dns_resolver import PinnedDnsTransport, get_resolver

                transport = PinnedDnsTransport(get_resolver(
                    endpoints=self._doh_endpoints(),
                    ttl_seconds=float(getattr(self.s, "DNS_CACHE_TTL_SECONDS", 300) or 300),
                ))
            except Exception as exc:  # noqa: BLE001 - DNS 策略不该导致整个数据源起不来
                logger.warning("dns.doh_transport_unavailable error=%s", exc)

        client_kwargs: dict[str, Any] = dict(
            timeout=timeout, limits=limits, headers=self.headers, follow_redirects=True
        )
        if transport is not None:
            client_kwargs["transport"] = transport
        elif proxy:
            client_kwargs["proxy"] = proxy
        # 关键：httpx 默认 trust_env=True，会静默吃下操作系统里的 HTTP_PROXY。
        # 那会让「后台明明显示直连，请求却走了别的代理」，排障时极难发现，所以默认关掉。
        if not getattr(self.s, "HTTP_TRUST_ENV", False):
            client_kwargs["trust_env"] = False
        return httpx.AsyncClient(**client_kwargs)

    def _dns_fingerprint(self) -> str:
        """影响建连方式的 DNS 配置指纹。

        和代理一样：这些值变了就必须重建连接池，否则已有的长连接还会沿用旧策略，
        表现为「后台改了 DNS 解析方式却没效果」。
        """
        return f"{self._dns_mode()}|{','.join(self._doh_endpoints())}"

    async def client(self) -> httpx.AsyncClient:
        """取连接池。代理或 DNS 策略变化时会丢弃旧连接重建 —— 这是「后台改完立即生效」的关键。"""
        proxy = self.proxy_url  # 地址写错时这里会抛 proxy_error，由上层归类
        dns = self._dns_fingerprint()
        if self._client is None or self._client.is_closed:
            self._client = self._build_client(proxy)
        elif self._client_proxy != proxy or self._client_dns != dns:
            if not self._client.is_closed:
                await self._client.aclose()
            self._client = self._build_client(proxy)
        self._client_proxy = proxy
        self._client_dns = dns
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
            self._client_proxy = None

    # ------------------------------------------------------------------ helpers
    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    @staticmethod
    def classify(exc: Exception, http_status: int | None = None) -> tuple[str, str]:
        """把底层异常映射为标准化 failure_type。"""
        if isinstance(exc, httpx.ConnectTimeout) or isinstance(exc, httpx.ReadTimeout) or isinstance(
            exc, httpx.WriteTimeout
        ) or isinstance(exc, httpx.PoolTimeout):
            return "timeout", str(exc) or "请求超时"
        if isinstance(exc, httpx.ProxyError):
            return "proxy_error", f"代理返回错误：{exc}"
        if isinstance(exc, httpx.ConnectError):
            msg = str(exc).lower()
            if "socks" in msg or "proxy" in msg:
                return "proxy_error", "代理连接失败（检查代理地址、账号与网络）"
            if "name or service not known" in msg or "getaddrinfo" in msg or "nodename" in msg:
                return "dns_error", "DNS 解析失败"
            return "connect_error", "连接失败"
        if isinstance(exc, httpx.RemoteProtocolError):
            return "data_format_error", "响应格式异常"
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            mapping = {
                401: "auth_error",
                403: "forbidden",
                404: "data_format_error",
                418: "rate_limited",
                429: "rate_limited",
                451: "forbidden",
                500: "server_error",
                502: "server_error",
                503: "server_error",
                504: "timeout",
            }
            return mapping.get(status, "server_error"), f"HTTP {status}"
        if isinstance(exc, httpx.TransportError):
            return "network_error", str(exc)
        if isinstance(exc, ProviderError):
            return exc.failure_type, exc.message
        # python-socks 抛的异常不在 httpx 体系内，不接住会退化成 unknown
        if type(exc).__module__.startswith("python_socks") or "socks" in str(exc).lower():
            return "proxy_error", f"代理故障：{exc}"
        return "unknown", f"{type(exc).__name__}: {exc}"

    def _should_retry(self, failure_type: str, status_code: int | None) -> bool:
        if failure_type in ("auth_error", "forbidden", "not_configured", "data_format_error"):
            return False  # 凭据/格式问题：重试无意义，避免耗尽资源
        if failure_type == "rate_limited":
            return False  # 429 走退避+降频，不在本轮重试
        if status_code and 400 <= status_code < 500:
            return False
        return True

    # ------------------------------------------------------------------ request
    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        timeout_read: float | None = None,
        label: str = "request",
    ) -> httpx.Response:
        """执行请求，内置限流、重试与指数退避。抛出 :class:`ProviderError`。"""
        url = self._url(path)
        client = await self.client()
        await self.bucket.acquire()

        last_error: Exception | None = None
        last_type = "unknown"
        last_status: int | None = None
        started = time.perf_counter()

        for attempt in range(self.retries + 1):
            try:
                req_headers = {**self.headers, **(headers or {})}
                resp = await client.request(
                    method,
                    url,
                    params=params,
                    headers=req_headers,
                    json=json_body,
                    timeout=httpx.Timeout(
                        connect=self.timeout_connect,
                        read=timeout_read or self.timeout_read,
                        write=timeout_read or self.timeout_read,
                        pool=5.0,
                    ),
                )
                if resp.status_code >= 400:
                    last_status = resp.status_code
                    raise httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}", request=resp.request, response=resp
                    )
                return resp

            except Exception as exc:  # noqa: BLE001 - 统一归一化
                last_error = exc
                last_type, msg = self.classify(exc, last_status)
                if attempt >= self.retries or not self._should_retry(last_type, last_status):
                    break
                delay = min(self.s.DEFAULT_BACKOFF_MAX, self.backoff_base * (2**attempt))
                delay += random.uniform(0, self.backoff_base * 0.5)
                logger.event(
                    "provider.retry",
                    provider=self.provider_name,
                    attempt=attempt + 1,
                    failure_type=last_type,
                    delay=round(delay, 3),
                    label=label,
                )
                await asyncio.sleep(delay)

        elapsed_ms = (time.perf_counter() - started) * 1000
        if isinstance(last_error, ProviderError):
            raise last_error
        raise ProviderError(
            msg,
            provider=self.provider_name,
            failure_type=last_type,
            http_status_code=last_status,
            retryable=last_type
            not in ("auth_error", "forbidden", "not_configured", "data_format_error"),
            url=url,
            elapsed_ms=round(elapsed_ms, 2),
        )

    async def get_json(self, path: str, **kwargs: Any) -> Any:
        resp = await self.request("GET", path, **kwargs)
        try:
            return resp.json()
        except Exception as exc:
            raise ProviderError(
                "响应不是合法 JSON",
                provider=self.provider_name,
                failure_type="data_format_error",
                http_status_code=resp.status_code,
                preview=resp.text[:200],
            ) from exc

    async def get_text(self, path: str, **kwargs: Any) -> str:
        resp = await self.request("GET", path, **kwargs)
        return resp.text
