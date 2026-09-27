# -*- coding: utf-8 -*-
"""DNS 策略测试。

这里的重点不是「DoH 协议实现得对不对」（那是 httpx 和 DNS 服务器的事），而是三件容易踩坏的事：

1. **改写 URL 成 IP 后，Host 头和 TLS SNI 必须保留** —— 缺一个 https 请求就会失败，
   而且失败信息（证书不匹配）会把人往完全错误的方向带。这条用真实本地服务验证。
2. **DoH 挂了必须静默退回系统 DNS** —— 新功能的目的是「把坏的变好」，
   绝不能因为 DoH 服务器自身不可用，反而把本来能用的数据源搞坏。
3. **走代理时不应该启用** —— 有代理时解析发生在代理侧，本地改写纯属添乱。
"""
from __future__ import annotations

import asyncio
import base64
import http.server
import ipaddress
import struct
import threading

import httpx
import pytest

from app.core.config import get_settings
from app.core.dns_resolver import (
    DEFAULT_ENDPOINTS,
    DohResolver,
    PinnedDnsTransport,
    _decode_dns_answer,
    _encode_dns_query,
    _is_poisoned,
    _looks_like_ip,
    _skip_dns_name,
    _worth_retrying_as_wire,
    get_resolver,
    reset_resolver,
)
from app.providers.transport import ProviderTransport


# ------------------------------------------------------------------ 纯函数


@pytest.mark.parametrize("ip,expected", [
    ("2001::80f2:fa9d", True),   # Teredo 保留段 —— 实测污染包最爱用它
    ("2002::1", True),
    ("2409:8a6c:3b1:a760::1", False),  # 正常家庭宽带 IPv6，不能误杀
    ("2606:4700::1111", False),        # Cloudflare DNS
    ("108.160.172.204", False),
    ("0.0.0.0", True),
    ("127.0.0.1", True),
    ("不是IP", True),
])
def test_is_poisoned(ip: str, expected: bool) -> None:
    assert _is_poisoned(ip) is expected


@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", True),
    ("::1", True),
    ("[::1]", True),
    ("api.binance.com", False),
])
def test_looks_like_ip(host: str, expected: bool) -> None:
    assert _looks_like_ip(host) is expected


def test_ip_literals_are_not_resolved() -> None:
    """IP 字面量没有 DNS 污染问题，解析纯属浪费一次往返。"""
    async def run() -> None:
        resolver = DohResolver(endpoints=["http://127.0.0.1:1/dns-query"])
        assert await resolver.resolve("127.0.0.1") is None

    asyncio.run(run())


# ------------------------------------------------------------------ DoH 失败降级


def test_resolver_returns_none_when_all_endpoints_dead() -> None:
    """所有 DoH 端点不可用时必须返回 None（调用方据此退回系统 DNS），而不是抛异常。"""
    async def run() -> None:
        resolver = DohResolver(endpoints=["http://127.0.0.1:1/a", "http://127.0.0.1:1/b"],
                               timeout=0.5)
        assert await resolver.resolve("api.example.com") is None
        # 失败也要有短期缓存，避免每个请求都卡一轮连接尝试
        assert resolver.peek("api.example.com") is None

    asyncio.run(run())


def test_get_resolver_rebuilds_when_endpoints_change() -> None:
    """用户在后台改了 DoH 服务器，必须能立刻生效，不用重启进程。"""
    reset_resolver()
    try:
        a = get_resolver(endpoints=["https://a.example/dns-query"])
        assert get_resolver(endpoints=["https://a.example/dns-query"]) is a
        b = get_resolver(endpoints=["https://b.example/dns-query"])
        assert b is not a
        assert b.endpoints == ("https://b.example/dns-query",)
    finally:
        reset_resolver()


# ------------------------------------------------------------------ 本地目标服务


class _EchoHandler(http.server.BaseHTTPRequestHandler):
    captured: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802
        _EchoHandler.captured = {
            "host_header": str(self.headers.get("Host") or ""),
            "path": self.path,
        }
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        return None


@pytest.fixture
def http_target() -> "tuple[str, int]":
    _EchoHandler.captured = {}
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _EchoHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "127.0.0.1", srv.server_address[1]
    srv.shutdown()
    srv.server_close()


class _FakeResolver:
    """固定返回某个 IP 的假解析器 —— 用来验证改写逻辑本身，不掺 DoH 网络。"""

    def __init__(self, ip: str | None) -> None:
        self.ip = ip
        self.calls: list[str] = []

    async def resolve(self, host: str) -> str | None:
        self.calls.append(host)
        return self.ip


# ------------------------------------------------------------------ 改写正确性（最关键的几条）


async def test_pinned_transport_keeps_host_header_and_sni(http_target: tuple[str, int]) -> None:
    """URL 换成 IP 后，Host 头必须还是原域名 —— 否则虚拟主机/CND 会直接拒绝。"""
    host, port = http_target
    fake = _FakeResolver(host)
    transport = PinnedDnsTransport(fake)
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        resp = await client.get(f"http://market-data.example.com:{port}/api/v3/ping")
    assert resp.status_code == 200
    # 尽管我们用 IP 建连，对端看到的 Host 必须是原始域名
    assert _EchoHandler.captured["host_header"] == f"market-data.example.com:{port}"
    assert fake.calls == ["market-data.example.com"]


def _capture_rewrite(monkeypatch: pytest.MonkeyPatch, ip: str | None, url: str) -> dict[str, object]:
    """跑一次真实的改写流程，但把最底层的发送动作拦下来。

    这样能同时断言三件事：URL 被换成了 IP、Host 头保留域名、SNI 扩展被设置。
    不真的发包是为了让测试与外网无关，稳定可重复。
    """
    captured: dict[str, object] = {}

    async def fake_handle(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        captured["url_host"] = request.url.host
        captured["url_port"] = request.url.port
        captured["host_header"] = request.headers.get("Host")
        captured["sni"] = request.extensions.get("sni_hostname")
        return httpx.Response(200, request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake_handle)

    async def run() -> None:
        transport = PinnedDnsTransport(_FakeResolver(ip))
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            await client.get(url)

    asyncio.run(run())
    return captured


def test_rewrite_sets_ip_host_sni(monkeypatch: pytest.MonkeyPatch) -> None:
    """IPv4：URL 换成 IP，Host 头和 SNI 必须还是原域名。缺一个 https 就会失败。"""
    got = _capture_rewrite(monkeypatch, "65.9.184.122", "https://api.binance.com/api/v3/ping")
    assert got["url_host"] == "65.9.184.122"
    assert got["host_header"] == "api.binance.com"
    assert got["sni"] == "api.binance.com"


def test_rewrite_brackets_ipv6(monkeypatch: pytest.MonkeyPatch) -> None:
    """IPv6 必须加中括号，否则 host:port 会被解析错位。"""
    got = _capture_rewrite(monkeypatch, "2409:8a6c::1", "https://example.test/ping")
    assert got["url_host"] == "2409:8a6c::1"
    assert got["host_header"] == "example.test"
    assert got["sni"] == "example.test"


def test_rewrite_keeps_port(monkeypatch: pytest.MonkeyPatch) -> None:
    got = _capture_rewrite(monkeypatch, "93.184.216.34", "https://example.test:8443/x")
    assert got["url_host"] == "93.184.216.34"
    assert got["url_port"] == 8443
    assert got["host_header"] == "example.test:8443"


def test_no_rewrite_when_resolver_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """解析器给不出 IP 时，请求必须原样发出 —— 这就是「退回系统 DNS」的实现方式。"""
    got = _capture_rewrite(monkeypatch, None, "https://example.test/ping")
    assert got["url_host"] == "example.test"
    assert got["sni"] is None


async def test_falling_back_to_system_dns_when_resolver_returns_none(
    http_target: tuple[str, int],
) -> None:
    """解析器返回 None 时请求必须照常发出 —— 这是「不能把能用的搞坏」的底线。"""
    host, port = http_target
    transport = PinnedDnsTransport(_FakeResolver(None))
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        resp = await client.get(f"http://{host}:{port}/ping")
    assert resp.status_code == 200


# ------------------------------------------------------------------ 与 ProviderTransport 的集成


def _isolate_network_state(settings: object) -> None:
    """把代理配置清空再断言。

    不清会踩两个坑：一是运行环境可能被注入 HTTP_PROXY（命令行沙箱常见），
    二是同一个 pytest 会话里别的用例可能残留代理值。两者都会让 transport 走代理分支，
    从而根本不会启用 DoH —— 于是测试断言的是别的东西。
    """
    settings.HTTP_PROXY = ""  # type: ignore[attr-defined]
    settings.HTTPS_PROXY = ""  # type: ignore[attr-defined]
    settings.SOCKS_PROXY = ""  # type: ignore[attr-defined]


def test_doh_applies_only_when_direct() -> None:
    """走代理时不挂 DoH transport；直连 + DNS_MODE=doh 时才挂。"""
    settings = get_settings()
    old_mode = settings.DNS_MODE
    settings.DNS_MODE = "doh"
    _isolate_network_state(settings)
    try:
        direct = ProviderTransport(provider_name="t")
        client = asyncio.run(direct.client())
        assert isinstance(client._transport, PinnedDnsTransport)
        asyncio.run(direct.close())

        # 有代理 => 解析发生在代理侧，不该再改写
        proxied = ProviderTransport(provider_name="t", proxy="http://127.0.0.1:65533")
        pclient = asyncio.run(proxied.client())
        assert not isinstance(pclient._transport, PinnedDnsTransport)
        asyncio.run(proxied.close())
    finally:
        settings.DNS_MODE = old_mode


def test_dns_mode_change_rebuilds_connection() -> None:
    """后台把 DNS_MODE 从 system 改成 doh，已有连接池必须重建。

    不重建的话，改完配置后旧连接还在沿用旧策略，表现为「改了没效果」——
    和代理那个坑是同一类问题。
    """
    settings = get_settings()
    old_mode = settings.DNS_MODE
    _isolate_network_state(settings)
    settings.DNS_MODE = "system"
    try:
        transport = ProviderTransport(provider_name="t")
        first = asyncio.run(transport.client())
        assert not isinstance(first._transport, PinnedDnsTransport)

        settings.DNS_MODE = "doh"
        second = asyncio.run(transport.client())
        assert second is not first
        assert first.is_closed, "旧连接必须关掉，否则会继续走旧的解析策略"
        assert isinstance(second._transport, PinnedDnsTransport)
        asyncio.run(transport.close())
    finally:
        settings.DNS_MODE = old_mode


def test_doh_disabled_by_default() -> None:
    """默认必须走系统 DNS —— 不能因为加功能就改变既有行为。"""
    settings = get_settings()
    old_mode = settings.DNS_MODE
    settings.DNS_MODE = "system"
    _isolate_network_state(settings)
    try:
        transport = ProviderTransport(provider_name="t")
        client = asyncio.run(transport.client())
        assert not isinstance(client._transport, PinnedDnsTransport)
        asyncio.run(transport.close())
    finally:
        settings.DNS_MODE = old_mode


# ------------------------------------------------------------------ 诊断输出


def test_diagnose_reports_poisoning() -> None:
    """本地 DNS 给出 Teredo 假地址、DoH 给出真地址时，必须如实判为 polluted。"""
    resolver = DohResolver(endpoints=["https://cloudflare-dns.com/dns-query"])

    async def run() -> dict[str, object]:
        resolver.system_ips = lambda host: ["2001::9f6a:794b", "31.13.80.169"]  # type: ignore[assignment]
        resolver._query_one = lambda endpoint, host, qtype: asyncio.sleep(  # type: ignore[assignment]
            0, result=["65.9.184.122"])
        return await resolver.diagnose("api.binance.com")

    result = asyncio.run(run())
    assert result["verdict"] == "poisoned"
    assert result["poisoned_ips"] == ["2001::9f6a:794b"]
    assert result["doh_ips"] == ["65.9.184.122"]
    assert "DoH" in str(result["advice"])


def test_diagnose_reports_poisoning_even_without_doh() -> None:
    """保留段假 IP 本身就是铁证，不能因为 DoH 恰好连不上就判成 unknown。

    实测遇到过：本地 DNS 返回 2001::xxx，但当时 DoH 服务器不可达，
    若按「必须有 DoH 才能判定」来写，会把明显的污染报成「无法判定」。
    """
    resolver = DohResolver(endpoints=["https://x/dns-query"])

    async def run() -> dict[str, object]:
        resolver.system_ips = lambda host: ["173.236.212.42", "2001::adfc:6c03"]  # type: ignore[assignment]
        resolver._query_one = lambda endpoint, host, qtype: asyncio.sleep(  # type: ignore[assignment]
            0, result=[])
        return await resolver.diagnose("api.binance.com")

    result = asyncio.run(run())
    assert result["verdict"] == "poisoned"
    assert result["poisoned_ips"] == ["2001::adfc:6c03"]
    assert result["doh_ips"] == []
    assert "足以判定" in str(result["advice"])


def test_diagnose_says_clean_when_consistent() -> None:
    resolver = DohResolver(endpoints=["https://x/dns-query"])

    async def run() -> dict[str, object]:
        resolver.system_ips = lambda host: ["65.9.184.122"]  # type: ignore[assignment]
        resolver._query_one = lambda endpoint, host, qtype: asyncio.sleep(  # type: ignore[assignment]
            0, result=["65.9.184.122"])
        return await resolver.diagnose("api.binance.com")

    result = asyncio.run(run())
    assert result["verdict"] == "clean"
    assert result["mismatched_ips"] == []


# ================================================================== wire 报文形式（RFC 8484）
#
# 这一节测的是「端点只认二进制报文」时必须走通的那条路。
# 起因是实测：阿里公共 DNS 对 JSON 形式回 ``400 no 'dns' query parameter found``，
# 而它又是国内少数可达的 DoH 端点 —— 当时默认端点正好把它排在第一位，
# 于是整个 DoH 静默失效（既不报错，也拿不到 IP），表现和没配一样。

_TEST_ENDPOINT = "https://dns.example/dns-query"


def _b64url_decode(text: str) -> bytes:
    """补回 padding 再解 —— 编码器按 RFC 8484 要求是**不带** ``=`` 的。"""
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _build_answer(records: list[tuple[int, bytes, int]], qtype_code: int = 1) -> bytes:
    """拼一个应答报文。records 是 (rtype, rdata, ttl)，名字一律用压缩指针写。

    用指针不是图省事：真实服务器的应答几乎都用压缩指针（尤其有 CNAME 时），
    自己拼包若只用裸名字，就测不到「指针 exemptions」这条最容易写错的分支。
    """
    qname = b"\x07example\x03com\x00"
    header = struct.pack(">HHHHHH", 0xABCD, 0x8180, 1, len(records), 0, 0)
    body = qname + struct.pack(">HH", qtype_code, 1)  # QTYPE + QCLASS
    for rtype, rdata, ttl in records:
        body += b"\xC0\x0C" + struct.pack(">HHIH", rtype, 1, ttl, len(rdata)) + rdata
    return header + body


# ------------------------------------------------------------------ 编码

def test_encode_dns_query_matches_rfc1035_layout() -> None:
    """报文布局是和服务器对过的，改错一个字段对方就回 FORMERR —— 必须钉死。"""
    raw = _b64url_decode(_encode_dns_query("example.com", "A"))
    # 头 12 字节：随机 ID + 标准查询标志 + QDCOUNT=1
    assert raw[:12] == struct.pack(">HHHHHH", 0xABCD, 0x0100, 1, 0, 0, 0)
    assert raw[12:] == b"\x07example\x03com\x00" + struct.pack(">HH", 1, 1)


def test_encode_dns_query_is_unpadded_base64url() -> None:
    """RFC 8484 要求 base64url 且无 padding；URL 里的 ``=`` 会被部分服务器当成参数值的一部分。"""
    text = _encode_dns_query("example.com", "AAAA")
    assert "=" not in text
    assert not (set(text) & set("+/"))
    assert struct.unpack(">HH", _b64url_decode(text)[-4:]) == (28, 1)  # AAAA=28, CLASS IN=1


@pytest.mark.parametrize("host", ["example.com", "EXAMPLE.COM", "example.com.", " example.com ",
                                 "example.com.."])
def test_encode_dns_query_normalizes_host_forms(host: str) -> None:
    """带尾点、大小写、空格、连续点的写法必须编出同一份报文。

    否则同一个域名会被当成不同的查询发出去，而 ``example.com..`` 拆出的空标签
    会让报文提前以 0 结束，服务器看到的后半段全是垃圾字节。
    """
    assert _encode_dns_query(host, "A") == _encode_dns_query("example.com", "A")


def test_encode_dns_query_truncates_overlong_label() -> None:
    """超过 63 字节的标签要裁剪，不能让 encode('idna') 抛异常。

    这条曾经是隐患：``'a'*70`` 在 ``encode('idna')`` 这一步就抛 UnicodeEncodeError，
    写在后面的 ``[:63]`` 根本轮不到执行 —— 编码之后才裁已经晚了。

    注意这里有两层防御（编码前截断 + 异常兜底），互为备份；
    变异测试要是只废掉其中一层，本用例仍会通过，这是特性不是漏洞。
    """
    raw = _b64url_decode(_encode_dns_query("a" * 70 + ".com", "A"))
    assert raw[12] == 63, "标签长度字段最多 63"
    assert b"a" * 63 in raw


def test_encode_dns_query_supports_punycode() -> None:
    raw = _b64url_decode(_encode_dns_query("例子.example.com", "A"))
    assert b"xn--fsqu00a" in raw  # idna 编码后的中文标签


def test_encode_dns_query_rejects_empty_host() -> None:
    with pytest.raises(ValueError):
        _encode_dns_query("...", "A")


# ------------------------------------------------------------------ 报文解析

def test_skip_dns_name_reads_uncompressed_name() -> None:
    packet = _build_answer([])
    assert _skip_dns_name(packet, 12) == 25  # 12 + len("\x07example\x03com\x00")


def test_skip_dns_name_consumes_two_bytes_for_pointer() -> None:
    """指针自己只占 2 字节 —— 必须返回「指针之后」的位置，不是被指到的目标地址。

    返回成目标地址（12）的话，后续按 sizeof 去解 rdlength 会把整个应答读错。
    """
    packet = _build_answer([(1, bytes([93, 184, 216, 34]), 300)])
    answer_offset = 12 + len(b"\x07example\x03com\x00") + 4
    assert packet[answer_offset:answer_offset + 2] == b"\xC0\x0C"
    assert _skip_dns_name(packet, answer_offset) == answer_offset + 2


@pytest.mark.parametrize("exc,expected", [
    (httpx.HTTPStatusError("no 'dns' query parameter found", request=None,  # type: ignore[arg-type]
                           response=httpx.Response(400)), True),   # 阿里 DNS 的日常：值得降级
    (httpx.HTTPStatusError("bad", request=None,  # type: ignore[arg-type]
                           response=httpx.Response(415)), True),   # 不支持的媒体类型，同理
    (httpx.HTTPStatusError("down", request=None,  # type: ignore[arg-type]
                           response=httpx.Response(503)), False),  # 端点自身故障，换格式也没用
    (httpx.ConnectError("refused"), False),       # 连不上：TransportError 分支
    (httpx.ReadTimeout("slow"), False),
    (ValueError("not json"), True),               # 回了非 JSON：换报文格式值得一试
])
def test_worth_retrying_as_wire(exc: Exception, expected: bool) -> None:
    """只在该降级的场景降级 —— 端点宕机时不重试，能省掉一半的徒劳往返。"""
    assert _worth_retrying_as_wire(exc) is expected


def test_skip_dns_name_terminates_on_pointer_loop() -> None:
    """自己指自己的畸形报文必须能停下来，不能死循环 —— 外部服务器返回什么都有可能。"""
    assert _skip_dns_name(b"\xC0\x00", 0) == 0


def test_decode_dns_answer_reads_a_records() -> None:
    packet = _build_answer([(1, bytes([93, 184, 216, 34]), 300)])
    ips, ttl = _decode_dns_answer(packet, "A")
    assert ips == ["93.184.216.34"]
    assert ttl == 300


def test_decode_dns_answer_skips_cname_and_takes_min_ttl() -> None:
    """真实应答常常是 CNAME 链 + 多个 A：只取类型匹配的，TTL 取最小的一个（最快过期的说了算）。"""
    cname = b"\x03cdn\x07example\x03com\x00"
    packet = _build_answer([
        (5, cname, 600),                            # CNAME：不是我们要的类型
        (1, bytes([93, 184, 216, 34]), 120),
        (1, bytes([93, 184, 216, 35]), 45),
    ])
    ips, ttl = _decode_dns_answer(packet, "A")
    assert ips == ["93.184.216.34", "93.184.216.35"]
    assert ttl == 45


def test_decode_dns_answer_reads_aaaa_and_ignores_other_qtype() -> None:
    v6 = ipaddress.IPv6Address("2606:4700::1111").packed
    packet = _build_answer([(28, v6, 300)], qtype_code=28)
    assert _decode_dns_answer(packet, "AAAA") == (["2606:4700::1111"], 300)
    # 问 A 的时候不能把 AAAA 混进来 —— 后面会拿它去建 IPv4 连接
    assert _decode_dns_answer(packet, "A") == ([], None)


def test_decode_dns_answer_ignores_wrong_length_rdata() -> None:
    """rdata 长度凑不出合法 IP 时跳过，不能把畸形数据塞进 IP 列表。"""
    assert _decode_dns_answer(_build_answer([(1, b"\x01\x02\x03", 300)]), "A") == ([], None)


@pytest.mark.parametrize("payload", [b"", b"\x00" * 11, b"\xC0\x0C" * 20])
def test_decode_dns_answer_survives_malformed_packets(payload: bytes) -> None:
    """畸形报文返回空结果即可 —— 上层会退回系统 DNS，绝不能在这里抛异常或卡住。"""
    assert _decode_dns_answer(payload, "A")[0] == []


# ------------------------------------------------------------------ HTTP 层：JSON 与报文的自动降级

def _mount(resolver: DohResolver, handler: object) -> None:
    """给解析器装一个假 transport，让测试不依赖外网又能检查真实的请求形态。"""
    resolver._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        trust_env=False,
        timeout=httpx.Timeout(2.0),
    )


async def test_query_one_prefers_json_and_skips_wire() -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"Answer": [{"type": 1, "data": "93.184.216.34",
                                                     "TTL": 120}]})

    resolver = DohResolver(endpoints=[_TEST_ENDPOINT], timeout=1.0)
    _mount(resolver, handler)
    ips = await resolver._query_one(_TEST_ENDPOINT, "example.com", "A")
    assert ips == ["93.184.216.34"]
    assert len(calls) == 1, "JSON 端点正常时不应再多发一次报文请求"
    assert "name=example.com" in str(calls[0].url)
    assert "dns=" not in str(calls[0].url)
    await resolver.aclose()


async def test_query_one_falls_back_to_wire_when_json_rejected() -> None:
    """阿里公共 DNS 的实际表现：JSON 形式回 400 ``no 'dns' query parameter found``。

    这条不通的话，国内用户把默认端点一填上，DoH 就变成「配了等于没配」——
    而且不报错，最难排查的那种。
    """
    calls: list[httpx.Request] = []
    kinds: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.params.get("dns"):
            kinds.append("wire")
            return httpx.Response(200, content=_build_answer([(1, bytes([93, 184, 216, 34]), 300)]))
        kinds.append("json")
        return httpx.Response(400, text="no 'dns' query parameter found")

    resolver = DohResolver(endpoints=[_TEST_ENDPOINT], timeout=1.0)
    _mount(resolver, handler)
    assert await resolver._query_one(_TEST_ENDPOINT, "example.com", "A") == ["93.184.216.34"]
    assert kinds == ["json", "wire"], "必须先把 JSON 问一遍失败了才降级，顺序不能反"
    assert calls[-1].headers["accept"] == "application/dns-message"
    # 报文形式的 URL 参数是 dns=<base64url>，且查询 A 的报文尾部是 (1, 1)
    param = calls[-1].url.params.get("dns") or ""
    assert struct.unpack(">HH", _b64url_decode(param)[-4:]) == (1, 1)
    await resolver.aclose()


async def test_query_one_clamps_tiny_ttl() -> None:
    """服务器通告的 TTL 可能极小甚至为 0 —— 照单全收会变成每请求一次 DNS 往返。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_build_answer([(1, bytes([93, 184, 216, 34]), 1)]))

    resolver = DohResolver(endpoints=[_TEST_ENDPOINT], timeout=1.0)
    _mount(resolver, handler)
    await resolver._query_one(_TEST_ENDPOINT, "example.com", "A")
    assert resolver._cache_time["example.com"] >= 30.0
    await resolver.aclose()


# ------------------------------------------------------------------ 端到端选型与降级

async def test_resolve_drops_poisoned_answer() -> None:
    """同一个 A 应答里混了保留地址时，要挑出能用的那个，而不是整个源作废。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("dns")  # 本例强制走报文形式
        return httpx.Response(200, content=_build_answer([
            (1, bytes([240, 0, 0, 1]), 300),      # 240.0.0.0/4 保留段 => 判定污染
            (1, bytes([93, 184, 216, 34]), 300),
        ]))

    resolver = DohResolver(endpoints=[_TEST_ENDPOINT], timeout=1.0)
    resolver._query_json = _raise_json  # type: ignore[assignment]  # 逼它走报文路径
    _mount(resolver, handler)
    assert await resolver.resolve("example.com") == "93.184.216.34"
    await resolver.aclose()


async def test_resolve_accepts_ipv4_before_ipv6() -> None:
    """A 与 AAAA 都能用时必须选 IPv4（国内多数网络 IPv6 可用性更差）。"""
    asked: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url.params.get("type")))
        if request.url.params.get("type") == "A":
            return httpx.Response(200, json={"Answer": [{"type": 1, "data": "93.184.216.34"}]})
        return httpx.Response(200, json={"Answer": [
            {"type": 28, "data": "2606:4700::1111"}]})

    resolver = DohResolver(endpoints=[_TEST_ENDPOINT], timeout=1.0)
    _mount(resolver, handler)
    assert await resolver.resolve("example.com") == "93.184.216.34"
    assert asked == ["A"], "IPv4 拿到答案就不该再去问 AAAA"
    await resolver.aclose()


async def test_resolve_falls_back_to_ipv6_when_no_a_record() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("type") == "A":
            return httpx.Response(200, json={"Answer": []})  # NXDOMAIN 之外的「无 A 记录」
        return httpx.Response(200, json={"Answer": [{"type": 28, "data": "2606:4700::1111"}]})

    resolver = DohResolver(endpoints=[_TEST_ENDPOINT], timeout=1.0)
    _mount(resolver, handler)
    assert await resolver.resolve("example.com") == "2606:4700::1111"
    await resolver.aclose()


async def test_broken_endpoint_is_circuited_and_short_cached() -> None:
    """端点整体不可用时：标记熔断 + 短期缓存，别让每个请求都重来一轮注定失败的连接。"""
    attempts = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(503, text="down")

    resolver = DohResolver(endpoints=[_TEST_ENDPOINT], timeout=1.0)
    _mount(resolver, handler)
    assert await resolver.resolve("example.com") is None
    assert attempts["n"] == 1, "5xx 是端点自身故障，不该再换报文格式重试一次"
    assert resolver._failure_until.get(_TEST_ENDPOINT, 0.0) > 0.0

    assert await resolver.resolve("example.com") is None
    assert attempts["n"] == 1, "失败结果要缓存住，否则每个业务请求都会卡一轮连接超时"
    await resolver.aclose()


def test_default_endpoints_put_china_reachable_first() -> None:
    """端点顺序是实测结论，不是随手排的 —— 改回去会让国内部署的 DoH 整体失效。"""
    assert DEFAULT_ENDPOINTS[0] == "https://dns.alidns.com/dns-query"
    assert DEFAULT_ENDPOINTS[1] == "https://doh.pub/dns-query"
    # 国外端点留作兜底，但不能排在前面
    assert DEFAULT_ENDPOINTS[-2:] == ("https://cloudflare-dns.com/dns-query",
                                      "https://dns.google/dns-query")


async def _raise_json(*args: object, **kwargs: object) -> tuple[list[str], int | None]:
    """让 _query_one 必定降级到报文形式（模拟只支持 RFC 8484 的端点）。"""
    raise RuntimeError("JSON form not supported")
