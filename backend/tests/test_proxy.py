# -*- coding: utf-8 -*-
"""代理相关测试。

这里刻意不用 mock 假装「代理通了」：用一个**真的本地 SOCKS5 中继**（`tests/socks5_stub.py`）
把请求转发到本机的 HTTP 服务，于是握手、认证、CONNECT、数据中转全是真的。
只有「改了代理要不要重建连接」「密码会不会泄漏」这类才用属性断言。
"""
from __future__ import annotations

import http.server
import json
import threading

import pytest

from app.core.config import get_settings
from app.core.errors import ProviderError
from app.core.proxy import describe_proxy, mask_proxy, normalize_proxy_url, resolve_preferred
from app.providers.transport import ProviderTransport
from tests.socks5_stub import start_socks5


# ------------------------------------------------------------------ 归一化


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("socks5://u:p@1.2.3.4:1080", "socks5://u:p@1.2.3.4:1080"),
        ("socks5h://u:p@h.example:1080", "socks5://u:p@h.example:1080"),
        ("SOCKS5://u:p@h.example:1080", "socks5://u:p@h.example:1080"),
        # 用户真实粘贴形态：带 label、中文标点、只有用户名没有密码
        ("proxy:2048507772t@43.213.7.34:1080", "socks5://2048507772t@43.213.7.34:1080"),
        ("socks5，proxy:2048507772t@43.213.7.34:1080", "socks5://2048507772t@43.213.7.34:1080"),
        ("socks5, 2048507772t@43.213.7.34:1080", "socks5://2048507772t@43.213.7.34:1080"),
        ("2048507772t@43.213.7.34:1080", "socks5://2048507772t@43.213.7.34:1080"),
        ("43.213.7.34:1080", "socks5://43.213.7.34:1080"),
        ("  socks5://a:b@h.example:1080  ", "socks5://a:b@h.example:1080"),
        # 端口省略时按类型补默认端口
        ("socks5://u@h.example", "socks5://u@h.example:1080"),
        ("http://u:p@proxy.example:8080", "http://u:p@proxy.example:8080"),
        ("https://proxy.example:3128", "https://proxy.example:3128"),
        ("[::1]:1080", "socks5://[::1]:1080"),
        ("", ""),
    ],
)
def test_normalize_accepts_human_forms(raw: str, expected: str) -> None:
    assert normalize_proxy_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["bad://x", "h:abc", "h:99999", ":// missing-host", "socks5://", "只有中文没有地址"],
)
def test_normalize_rejects_bad_input(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_proxy_url(raw)


def test_mask_never_leaks_password() -> None:
    url = normalize_proxy_url("socks5://alice:supersecret@1.2.3.4:1080")
    masked = mask_proxy(url)
    assert "supersecret" not in masked
    assert masked == "socks5://alice:***@1.2.3.4:1080"

    info = describe_proxy(url)
    # 结构体里也只能看到「有没有密码」，不能看到密码本身
    assert info["has_password"] is True
    assert info["username"] == "alice"
    assert info["host"] == "1.2.3.4"
    assert info["port"] == 1080
    assert "supersecret" not in json.dumps(info, ensure_ascii=False)


def test_resolve_preferred_order() -> None:
    # Provider 独立代理 > SOCKS > HTTPS > HTTP
    assert resolve_preferred("socks5://a@1.1.1.1:1080", "socks5://b@2.2.2.2:1080",
                             "http://c@3.3.3.3:8080") == "socks5://a@1.1.1.1:1080"
    assert resolve_preferred("", "socks5://b@2.2.2.2:1080",
                             "http://c@3.3.3.3:8080") == "socks5://b@2.2.2.2:1080"
    assert resolve_preferred("", "", "http://c@3.3.3.3:8080",
                             "http://d@4.4.4.4:8080") == "http://c@3.3.3.3:8080"
    assert resolve_preferred("", "", "", "") == ""


# ------------------------------------------------------------------ 本地 HTTP 目标服务


class _JsonHandler(http.server.BaseHTTPRequestHandler):
    payload = {"hello": "proxy"}

    def do_GET(self) -> None:  # noqa: N802
        body = json.dumps({**self.payload, "path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # 静音，避免污染测试输出
        return None


@pytest.fixture
def http_target() -> "tuple[str, int]":
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _JsonHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield "127.0.0.1", srv.server_address[1]
    srv.shutdown()
    srv.server_close()


# ------------------------------------------------------------------ 传输层


def test_transport_proxy_priority() -> None:
    settings = get_settings()
    _clear_proxies()
    settings.HTTP_PROXY = "http://http@9.9.9.9:8080"
    settings.HTTPS_PROXY = "http://https@8.8.8.8:8080"
    settings.SOCKS_PROXY = "socks5://socks@7.7.7.7:1080"
    try:
        assert ProviderTransport(provider_name="t").proxy_url == "socks5://socks@7.7.7.7:1080"

        # Provider 自己的代理优先级最高
        own = ProviderTransport(provider_name="t", proxy="socks5://mine@6.6.6.6:1080")
        assert own.proxy_url == "socks5://mine@6.6.6.6:1080"

        # 优先级第二档：没有 SOCKS 才轮到 HTTPS/HTTP
        settings.SOCKS_PROXY = ""
        assert ProviderTransport(provider_name="t").proxy_url == "http://https@8.8.8.8:8080"
        settings.HTTPS_PROXY = ""
        assert ProviderTransport(provider_name="t").proxy_url == "http://http@9.9.9.9:8080"

        # 全都没配 -> 直连
        settings.HTTP_PROXY = ""
        assert ProviderTransport(provider_name="t").proxy_url is None
    finally:
        settings.HTTP_PROXY = settings.HTTPS_PROXY = settings.SOCKS_PROXY = ""


def _clear_proxies() -> None:
    """把三种代理都清空。

    注意：进程环境变量里可能本来就有 HTTP_PROXY（有些终端/沙箱会注入），
    pydantic Settings 会把它读进来，所以必须显式置空而不是依赖默认值。
    """
    s = get_settings()
    s.HTTP_PROXY = s.HTTPS_PROXY = s.SOCKS_PROXY = ""


async def test_allow_global_proxy_false_keeps_direct(http_target: tuple[str, int]) -> None:
    """``allow_global_proxy=False`` 必须能无视全局代理，强制直连。

    这条是为了「直连对照测试」：测试环境本身可能被注入 HTTP_PROXY，
    不绕过的话所谓的对照组其实也走了代理，两边结果一样，看不出代理到底有没有用。
    """
    host, port = http_target
    seen: list[tuple[str, int]] = []
    server, socks_port = start_socks5(user="carol", password="pwd",
                                      on_connect=lambda h, p: seen.append((h, p)))
    settings = get_settings()
    _clear_proxies()
    settings.SOCKS_PROXY = f"socks5://carol:pwd@127.0.0.1:{socks_port}"
    try:
        # 全局代理已配 -> 普通 transport 会走代理
        normal = ProviderTransport(provider_name="normal", timeout_connect=5.0,
                                   timeout_read=8.0, retries=0, qps=50.0)
        resp = await normal.request("GET", f"http://{host}:{port}/normal")
        assert resp.status_code == 200
        assert seen == [(host, port)]
        await normal.close()

        # allow_global_proxy=False -> 代理端什么都收不到
        seen.clear()
        forced = ProviderTransport(provider_name="forced_direct",
                                   allow_global_proxy=False,
                                   timeout_connect=5.0, timeout_read=8.0,
                                   retries=0, qps=50.0)
        assert forced.proxy_url is None
        resp2 = await forced.request("GET", f"http://{host}:{port}/forced")
        assert resp2.status_code == 200
        assert seen == [], "强制直连时不该有流量经过代理"
        await forced.close()

        # 但 Provider 自己的独立代理仍然优先于「强制直连」开关之上吗？
        # 不会 —— allow_global_proxy 只管全局，独立代理照样生效，这是刻意的：
        # 独立代理是显式给某个源配的，绕过它会让「给某个源单独走代理」失效。
        own = ProviderTransport(provider_name="own", proxy="这不是地址",
                                allow_global_proxy=False, retries=0)
        info = own.describe_proxy()
        assert info["invalid"] is True
    finally:
        settings.SOCKS_PROXY = ""
        server.shutdown()
        server.server_close()


def test_describe_proxy_reports_source() -> None:
    """source 决定前端提示文案：「跟随全局」还是「本源独占」。"""
    settings = get_settings()
    _clear_proxies()
    try:
        assert ProviderTransport(provider_name="t").describe_proxy()["source"] == "none"
        settings.SOCKS_PROXY = "socks5://g@1.1.1.1:1080"
        assert ProviderTransport(provider_name="t").describe_proxy()["source"] == "global"
        own = ProviderTransport(provider_name="t", proxy="socks5://p@2.2.2.2:1080")
        assert own.describe_proxy()["source"] == "provider"
        # 强制直连时，即使全局有配置，也不该被算成这条源的代理
        forced = ProviderTransport(provider_name="t", allow_global_proxy=False)
        assert forced.describe_proxy()["source"] == "none"
    finally:
        settings.SOCKS_PROXY = ""


async def test_transport_rebuilds_client_when_proxy_changes() -> None:
    settings = get_settings()
    _clear_proxies()
    t = ProviderTransport(provider_name="t")
    first = await t.client()
    assert t._client_proxy is None

    settings.SOCKS_PROXY = "socks5://alice:secret@127.0.0.1:65533"
    second = await t.client()  # 后台改了代理，下一次取连接必须换新的
    assert second is not first
    assert t._client_proxy == "socks5://alice:secret@127.0.0.1:65533"
    assert first.is_closed  # 旧连接必须关掉，否则会有幽灵连接继续直连
    await t.close()


async def test_invalid_proxy_raises_clear_error_instead_of_direct() -> None:
    settings = get_settings()
    _clear_proxies()
    settings.SOCKS_PROXY = "这不是代理地址"
    t = ProviderTransport(provider_name="demo_provider")
    with pytest.raises(ProviderError) as exc:
        await t.client()
    assert exc.value.failure_type == "proxy_error"
    assert "代理地址无法识别" in exc.value.message
    settings.SOCKS_PROXY = ""


async def test_describe_proxy_never_raises_and_masks() -> None:
    settings = get_settings()
    _clear_proxies()
    settings.SOCKS_PROXY = "socks5://alice:supersecret@1.2.3.4:1080"
    info = ProviderTransport(provider_name="t").describe_proxy()
    assert info["set"] is True
    assert info["masked"] == "socks5://alice:***@1.2.3.4:1080"
    assert "supersecret" not in str(info)
    assert "supersecret" not in mask_proxy(str(settings.SOCKS_PROXY))

    settings.SOCKS_PROXY = "坏地址"
    bad = ProviderTransport(provider_name="t").describe_proxy()
    assert bad["invalid"] is True  # 展示场景要给出原因，而不是整页 500
    settings.SOCKS_PROXY = ""


# ------------------------------------------------------------------ 真实 SOCKS5 穿透


async def test_request_really_goes_through_socks5(http_target: tuple[str, int]) -> None:
    """端到端：httpx -> SOCKS5 握手/认证 -> 中继 -> 目标 HTTP 服务。"""
    host, port = http_target
    seen: list[tuple[str, int]] = []
    server, socks_port = start_socks5(user="alice", password="secret",
                                      on_connect=lambda h, p: seen.append((h, p)))
    try:
        transport = ProviderTransport(
            provider_name="socks_test",
            proxy=f"socks5://alice:secret@127.0.0.1:{socks_port}",
            timeout_connect=5.0, timeout_read=8.0, retries=0, qps=50.0,
        )
        try:
            resp = await transport.request("GET", f"http://{host}:{port}/ping")
            assert resp.status_code == 200
            assert json.loads(resp.text)["hello"] == "proxy"
        finally:
            await transport.close()
    finally:
        server.shutdown()
        server.server_close()

    # 目标地址是被代理端解析出来的 —— 证明请求确实走了代理，而不是绕过
    assert seen == [(host, port)], f"代理端记录到的目标是 {seen}"


async def test_wrong_proxy_password_is_proxy_error(http_target: tuple[str, int]) -> None:
    host, port = http_target
    server, socks_port = start_socks5(user="alice", password="secret")
    try:
        transport = ProviderTransport(
            provider_name="socks_test",
            proxy=f"socks5://alice:WRONG@127.0.0.1:{socks_port}",
            timeout_connect=5.0, timeout_read=5.0, retries=0, qps=50.0,
        )
        try:
            with pytest.raises(ProviderError) as exc:
                await transport.request("GET", f"http://{host}:{port}/ping")
        finally:
            await transport.close()
    finally:
        server.shutdown()
        server.server_close()

    # 认证失败必须归类为代理故障 —— 它既不是「数据源坏了」，也不该让用户误以为值不对
    assert exc.value.failure_type == "proxy_error"


async def test_global_socks_proxy_switches_at_runtime(http_target: tuple[str, int]) -> None:
    """后台改 SOCKS_PROXY 之后，不需要重启，下一次请求就走新代理。"""
    host, port = http_target
    seen: list[tuple[str, int]] = []
    server, socks_port = start_socks5(user="bob", password="pwd",
                                      on_connect=lambda h, p: seen.append((h, p)))
    settings = get_settings()
    _clear_proxies()
    try:
        transport = ProviderTransport(provider_name="runtime_switch",
                                      timeout_connect=5.0, timeout_read=8.0, retries=0, qps=50.0)
        # 1) 没配代理时也能取到数（直连）
        first = await transport.request("GET", f"http://{host}:{port}/direct")
        assert first.status_code == 200
        assert seen == []  # 代理端什么都没看到 => 确实是直连

        # 2) 后台改了代理，下一次请求应该走代理，且不用重建 transport
        settings.SOCKS_PROXY = f"socks5://bob:pwd@127.0.0.1:{socks_port}"
        second = await transport.request("GET", f"http://{host}:{port}/via-proxy")
        assert second.status_code == 200
        assert seen == [(host, port)]
        await transport.close()
    finally:
        settings.SOCKS_PROXY = ""
        server.shutdown()
        server.server_close()
