# -*- coding: utf-8 -*-
"""DNS 解析策略：绕开本地 DNS 污染。

存在理由
--------
在国内（以及部分企业/校园网）直连海外 API 时，**最常见的失败其实不是网络不通，而是 DNS 被污染**：
同一个域名每次解析出的 IP 都不一样，混入了 Teredo 保留段 ``2001::/32`` 的假 IPv6、
或完全不相干的地址（实测 ``api.binance.com`` 被解析到 Facebook 的地址段）。
表现是「同一个源一会儿通、一会儿超时」，非常难定位。

DNS-over-HTTPS（DoH）把查询包放进 HTTPS 隧道里发出去，污染者抢答不了，因此能拿到真实 IP。

边界（很重要，别越界）
----------------------
1. **走代理时不生效** —— 有代理时代理方负责解析目的地，本地改 DNS 没有意义还会坏事。
2. **DoH 不可用必须静默回退到系统 DNS** —— 本模块的目的是「把坏的变好」，
   绝不能因为 DoH 服务器自身连不上，反而把本来能用的数据源搞挂。
3. 解析结果有 TTL 缓存，避免每个请求都多耗一次 DNS 往返。
"""
from __future__ import annotations

import asyncio
import base64
import ipaddress
import socket
import struct
import time
from typing import Iterable

import httpx

from .logging import get_logger

logger = get_logger(__name__)

# 默认端点按「国内可达优先」排序：阿里公共 DNS 实测可达（约 0.4 秒），
# 而 Cloudflare / Google 在国内部署上经常直接连不上。后者保留作兜底。
DEFAULT_ENDPOINTS: tuple[str, ...] = (
    "https://dns.alidns.com/dns-query",
    "https://doh.pub/dns-query",
    "https://cloudflare-dns.com/dns-query",
    "https://dns.google/dns-query",
)
DEFAULT_TTL_SECONDS = 300.0
DEFAULT_TIMEOUT = 4.0

# 一看就不该出现在正常 A/AAAA 应答里的地址类型。命中即判定为污染
_SUSPICIOUS_V6_PREFIXES = (
    "2001::",      # Teredo 隧道，实测是污染重灾区
    "2002::",      # 6to4
    "3ffe::",
    "5f00::",
)


def _looks_like_ip(host: str) -> bool:
    """判断是不是 IP 字面量。是的话没有解析的必要（也不会有 DNS 污染问题）。"""
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def _is_poisoned(ip: str) -> bool:
    """粗粒度判定一个 IP 是否可能是污染产物。

    只做「一看就不对」的判断（保留段），不做严格校验 —— 保守些，
    漏掉一点没关系，把正常 IP 误判成污染才是灾难。
    """
    try:
        parsed = ipaddress.ip_address(ip)
    except ValueError:
        return True
    if parsed.version == 6:
        text = ip.lower()
        return any(text.startswith(prefix) for prefix in _SUSPICIOUS_V6_PREFIXES)
    return parsed.is_unspecified or parsed.is_loopback or parsed.is_reserved


def _encode_dns_query(host: str, qtype: str) -> str:
    """构造 RFC 1035 查询报文，并按 RFC 8484 编成 URL 参数。

    为什么要有这条路：各家 DoH 服务支持的格式并不统一。多数支持 JSON 形式
    （``?name=&type=``），但**阿里公共 DNS 只认二进制报文**，问 JSON 会回 400。
    两条路都留着，用户贴什么端点进来都能直接用。
    """
    labels: list[bytes] = []
    # 先把主机名规范掉，三件事都不能省：
    # - 大小写：DNS 大小写不敏感，不归一会让缓存键被同一个域名打散；
    # - 尾点：FQDN 的写法（example.com.）拆出来会多一个空标签，
    #   而空标签的长度字节是 0，解析器会当成「名字到此结束」，后面的部分直接变垃圾字节；
    # - 截断：单个标签最多 63 字节，超了必须裁掉而不是让它抛 UnicodeEncodeError
    #   （注意是**编码前**裁 —— 'a'*70 在 encode('idna') 这一步就炸了，编码后再裁已经来不及）。
    for label in str(host).strip().lower().rstrip(".").split("."):
        try:
            raw = label[:63].encode("idna")
        except UnicodeError:
            raw = label.encode("utf-8", "ignore")[:63]
        if raw:
            labels.append(raw[:63])
    if not labels:
        raise ValueError(f"不是合法的主机名: {host!r}")

    qtype_code = 1 if qtype == "A" else 28
    packet = struct.pack(">HHHHHH", 0xABCD, 0x0100, 1, 0, 0, 0)
    for raw in labels:
        packet += bytes([len(raw)]) + raw
    packet += b"\x00" + struct.pack(">HH", qtype_code, 1)
    return base64.urlsafe_b64encode(packet).decode("ascii").rstrip("=")


def _skip_dns_name(data: bytes, offset: int) -> int:
    """跳过报文里的一个域名，返回新的偏移量。支持 DNS 压缩指针（0xC0 开头）。"""
    pos = offset
    end = len(data)
    landing: int | None = None  # 第一次遇到压缩指针时，名字在原位置到此为止
    for _ in range(256):
        if pos >= end:
            break
        length = data[pos]
        if length == 0:
            return landing if landing is not None else pos + 1
        if length & 0xC0 == 0xC0:
            if pos + 1 >= end:
                break
            if landing is None:
                landing = pos + 2
            pos = ((length & 0x3F) << 8) | data[pos + 1]
            continue
        pos += 1 + length
    return pos


def _decode_dns_answer(payload: bytes, qtype: str) -> tuple[list[str], int | None]:
    """解析 RFC 1035 应答报文，返回（IP 列表，其中最小的 TTL）。"""
    wanted = 1 if qtype == "A" else 28
    if len(payload) < 12:
        return [], None

    qdcount, ancount = struct.unpack(">HH", payload[4:8])
    offset = 12
    for _ in range(qdcount):
        offset = _skip_dns_name(payload, offset)
        offset += 4  # QTYPE + QCLASS

    answers: list[str] = []
    ttl_min: int | None = None
    for _ in range(ancount):
        offset = _skip_dns_name(payload, offset)
        if offset + 10 > len(payload):
            break
        rtype, rclass, ttl, rdlength = struct.unpack(">HHIH", payload[offset:offset + 10])
        offset += 10
        rdata = payload[offset:offset + rdlength]
        offset += rdlength
        if rclass != 1 or rtype != wanted or not rdata:
            continue
        try:
            ip = ipaddress.ip_address(rdata)
        except ValueError:
            continue  # 长度不对 => 不是 A/AAAA，跳过
        answers.append(str(ip))
        if ttl_min is None or ttl < ttl_min:
            ttl_min = int(ttl)
    return answers, ttl_min


def _worth_retrying_as_wire(exc: BaseException) -> bool:
    """判断这次失败值不值得换成二进制报文再问一次。

    只有「服务器听懂了、但不接受 JSON 形式」才值得 —— 典型就是阿里公共 DNS 回的
    ``400 no 'dns' query parameter found``。
    端点连不上（TransportError）或自身在报 5xx 时，报文形式一样发到同一个坏服务器上，
    重试只是把连接超时再付一遍：4 个端点 × 2 种类型下，最坏要多出 8 次徒劳往返。
    """
    if isinstance(exc, httpx.TransportError):  # 连接/读超时：端点不可达
        return False
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500:
        return False
    return True


class DohResolver:
    """DoH 解析器，带 TTL 缓存与多端点故障切换。"""

    def __init__(
        self,
        endpoints: Iterable[str] = DEFAULT_ENDPOINTS,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.endpoints: tuple[str, ...] = tuple(e for e in (str(x).strip() for x in endpoints) if e)
        self.ttl = max(30.0, ttl_seconds)
        self.timeout = timeout
        self._cache: dict[str, tuple[str | None, float]] = {}
        self._cache_time: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._failure_until: dict[str, float] = {}  # 端点级熔断，避免每次请求都卡在坏端点上
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------ 查询
    async def _client_get(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                trust_env=False,  # 内部查询自身不能被环境变量代理劫持
                timeout=httpx.Timeout(connect=self.timeout, read=self.timeout,
                                      write=self.timeout, pool=5.0),
                headers={"accept": "application/dns-json"},
            )
        return self._client

    async def _query_json(self, client: httpx.AsyncClient, endpoint: str,
                          host: str, qtype: str) -> tuple[list[str], int | None]:
        """JSON 形式（RFC 8427 的 `application/dns-json`）：Cloudflare / Google / DNSPod 都支持。"""
        resp = await client.get(endpoint, params={"name": host, "type": qtype})
        resp.raise_for_status()
        payload = resp.json()
        wanted = 1 if qtype == "A" else 28
        answers: list[str] = []
        ttl: int | None = None
        for item in payload.get("Answer", []) or []:
            if item.get("type") != wanted:
                continue
            data = str(item.get("data") or "").strip()
            if not data:
                continue
            if isinstance(item.get("TTL"), int):
                value = max(30, min(int(item["TTL"]), 3600))
                ttl = value if ttl is None else min(ttl, value)
            answers.append(data)
        return answers, ttl

    async def _query_wire(self, client: httpx.AsyncClient, endpoint: str,
                          host: str, qtype: str) -> tuple[list[str], int | None]:
        """RFC 8484 二进制报文形式：阿里公共 DNS 只认这一种。"""
        resp = await client.get(
            endpoint,
            params={"dns": _encode_dns_query(host, qtype)},
            headers={"accept": "application/dns-message"},
        )
        resp.raise_for_status()
        return _decode_dns_answer(resp.content, qtype)

    async def _query_one(self, endpoint: str, host: str, qtype: str) -> list[str]:
        if time.monotonic() < self._failure_until.get(endpoint, 0.0):
            return []
        client = await self._client_get()
        ttl = self.ttl
        answers: list[str] = []
        try:
            answers, wire_ttl = await self._query_json(client, endpoint, host, qtype)
        except Exception as exc:  # noqa: BLE001 - 端点可能不支持 JSON 形式
            if not _worth_retrying_as_wire(exc):
                raise
            answers, wire_ttl = await self._query_wire(client, endpoint, host, qtype)
        if wire_ttl:
            ttl = max(30.0, min(float(wire_ttl), 3600.0))
        self._cache_time[host] = ttl
        return answers

    async def _resolve_uncached(self, host: str) -> str | None:
        """按端点顺序逐个尝试，取第一个「看起来可信」的答案。"""
        async with self._lock:
            for endpoint in self.endpoints:
                for qtype in ("A", "AAAA"):
                    try:
                        ips = await self._query_one(endpoint, host, qtype)
                    except Exception as exc:  # noqa: BLE001 - 端点故障属于预期，换下一个
                        self._failure_until[endpoint] = time.monotonic() + 60.0
                        logger.event("dns.doh_endpoint_failed", endpoint=endpoint,
                                     error=type(exc).__name__, detail=str(exc)[:120])
                        continue
                    clean = [ip for ip in ips if not _is_poisoned(ip)]
                    if clean:
                        # IPv4 优先：多数被墙/被污染环境里 IPv6 可用性更差
                        clean.sort(key=lambda ip: 0 if ":" not in ip else 1)
                        logger.event("dns.doh_resolved", host=host, endpoint=endpoint,
                                     qtype=qtype, ip=clean[0])
                        return clean[0]
            return None

    async def resolve(self, host: str) -> str | None:
        """返回该域名通过 DoH 得到的 IP；拿不到就返回 None（调用方会退回系统 DNS）。"""
        if not host or _looks_like_ip(host):
            return None
        now = time.monotonic()
        cached = self._cache.get(host)
        if cached and cached[1] > now:
            return cached[0]
        ip = await self._resolve_uncached(host)
        # 失败也要缓存一小段时间，避免每个请求都去做一轮注定失败的 DoH 查询
        ttl = self._cache_time.get(host, self.ttl)
        self._cache[host] = (ip, now + (ttl if ip else 60.0))
        return ip

    def peek(self, host: str) -> str | None:
        """同步地取缓存值（不改写成协程的场景用），未命中返回 None。"""
        cached = self._cache.get(host)
        return cached[0] if cached and cached[1] > time.monotonic() else None

    async def aclose(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    # ------------------------------------------------------------------ 诊断
    def system_ips(self, host: str) -> list[str]:
        """系统 DNS 的答案。失败返回空列表而不是抛异常 —— 诊断接口不能崩。"""
        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        except Exception as exc:  # noqa: BLE001
            logger.event("dns.system_lookup_failed", host=host, error=type(exc).__name__)
            return []
        seen: list[str] = []
        for info in infos:
            ip = str(info[4][0])
            if ip not in seen:
                seen.append(ip)
        return seen

    async def diagnose(self, host: str) -> dict[str, object]:
        """对比系统 DNS 与 DoH 的答案，给出「是否被污染」的判断。

        这是给用户排障用的：与其让他猜「为什么这个源时好时坏」，
        不如直接告诉他本地 DNS 返回了什么、真实答案是什么。
        """
        system = self.system_ips(host)
        doh: list[str] = []
        endpoint_used: str | None = None
        for endpoint in self.endpoints:
            try:
                doh = await self._query_one(endpoint, host, "A")
            except Exception:  # noqa: BLE001
                continue
            if doh:
                endpoint_used = endpoint
                break
        doh_clean = [ip for ip in doh if not _is_poisoned(ip)]
        poisoned = [ip for ip in system if _is_poisoned(ip)]
        mismatched = [ip for ip in system if ip not in doh_clean]

        verdict = "unknown"
        advice = "本地 DNS 未返回异常地址，但 DoH 也取不到权威答案，" \
                 "无法交叉验证；若数据源确实时通时断，请检查网络或更换 DoH 服务器。"
        if poisoned:
            # 保留地址段本身就是铁证，不需要等 DoH 交叉验证 —— 等的话会因为
            # DoH 恰好连不上而把明显的污染判成 unknown。
            verdict = "poisoned"
            advice = ("本地 DNS 返回了保留地址段（典型的污染特征）。"
                      "建议把「DNS 解析方式」改成 DoH。" +
                      ("" if doh_clean else "（本次 DoH 查询未成功，但上述证据已足以判定。）"))
        elif not doh_clean:
            pass  # 保持 unknown：既无污染证据也无权威答案可供比对
        elif mismatched and not set(system).intersection(doh_clean):
            verdict = "suspicious"
            advice = ("本地 DNS 的答案与权威答案完全不一致，可能被劫持；"
                      "若该数据源时通时断，建议改用 DoH。")
        else:
            verdict = "clean"
            advice = "本地 DNS 与 DoH 答案一致，不需要改动。"

        return {
            "host": host,
            "system_ips": system,
            "doh_ips": doh_clean,
            "doh_endpoint": endpoint_used,
            "poisoned_ips": poisoned,
            "mismatched_ips": mismatched,
            "verdict": verdict,
            "advice": advice,
        }


# ---------------------------------------------------------------------- 进程级单例


_resolver: DohResolver | None = None
_resolver_endpoints: tuple[str, ...] = ()


def get_resolver(endpoints: Iterable[str] = DEFAULT_ENDPOINTS,
                 ttl_seconds: float = DEFAULT_TTL_SECONDS) -> DohResolver:
    """取进程级共享的解析器。

    端点配置变了就重建 —— 用户在后台改了 DoH 服务器要能立刻生效。
    """
    global _resolver, _resolver_endpoints
    wanted = tuple(str(x).strip() for x in endpoints if str(x).strip()) or DEFAULT_ENDPOINTS
    if _resolver is None or wanted != _resolver_endpoints:
        _resolver_endpoints = wanted
        _resolver = DohResolver(endpoints=wanted, ttl_seconds=ttl_seconds)
    return _resolver


def reset_resolver() -> None:
    """测试用：丢弃单例，让下一次 get_resolver 重建。"""
    global _resolver, _resolver_endpoints
    _resolver = None
    _resolver_endpoints = ()


class PinnedDnsTransport(httpx.AsyncHTTPTransport):
    """把 URL 里的域名替换成 DoH 解出的 IP，同时保留原域名的 Host 头与 TLS SNI。

    TLS 证书校验依赖这两件事：
    - ``Host`` 头：让对端知道要服务哪个虚拟主机；
    - ``sni_hostname``：让 TLS 握手带上正确的 SNI，否则证书对不上。
    两者缺一，https 请求就会失败，所以这里必须显式补上。
    """

    def __init__(self, resolver: DohResolver, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._resolver = resolver

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        origin_host = request.url.host
        ip = await self._resolver.resolve(origin_host)
        if ip:
            # 注意用 netloc 而不是 host：非默认端口（如 https://example.test:8443）
            # 的 Host 头必须带端口，否则对端虚拟主机匹配不上。
            origin_netloc = request.url.netloc.decode("ascii")
            literal = f"[{ip}]" if ":" in ip else ip
            request.url = request.url.copy_with(host=literal)
            request.headers["Host"] = origin_netloc
            request.extensions["sni_hostname"] = origin_host
        return await super().handle_async_request(request)
