"""代理地址归一化与展示。

这个模块存在的唯一理由：**人粘贴出来的代理地址千奇百怪**。

真实世界里大家会这样给：

    socks5://user:pass@1.2.3.4:1080     # 标准写法
    socks5h://user:pass@1.2.3.4:1080    # curl 习惯（远程解析 DNS）
    proxy:2048507772t@43.213.7.34:1080  # 带个 label 前缀
    socks5，user@host:1080              # 中文标点、只有用户名没有密码
    user@host:1080                      # 干脆不带 scheme

底层 `python-socks` 只认第一种写法，且**不认 socks5h**。所以必须在进入传输层之前统一：
无论用户怎么贴，内部一律是 `scheme://[user[:pass]@]host:port`。

另一个硬约束：**明文密码绝不能出现在日志、接口响应、错误消息里**。
所以这里同时提供 `mask_proxy()`——对外一律显示 `socks5://user:***@host:port`。
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

# python-socks（httpx-socks 底层）实际支持的 scheme
KNOWN_SCHEMES = ("socks5", "socks4", "http", "https")
# 会被折叠成 socks5 的写法：socks5h 表示「让代理端解析域名」，而 httpx-socks 默认就是远端解析
ALIAS_SCHEMES = {"socks5h": "socks5", "socks": "socks5", "socks4a": "socks4"}
DEFAULT_PORTS = {"socks5": 1080, "socks4": 1080, "http": 8080, "https": 8080}

# 中文标点 / 常见前缀清洗
_CHAR_MAP = {
    "：": ":", "＠": "@", "，": ",", "；": ";", "／": "/",
    "　": " ", "“": "", "”": "", "'": "", "\"": "", "`": "",
}
# 贴地址时常见的引导词/标签，例如「socks5，proxy:user@host:port」里的 proxy
_LABEL_RE = re.compile(r"^(?:proxy|代理|地址|proxies|http_proxy|https_proxy|socks)\s*[:：]\s*", re.I)
# 「socks5」「socks5h」这类只有类型的片段（后面可能跟 : 或 , 或直接贴地址）
# `(?!//)` 是为了放过标准写法 socks5:// —— 那种由下面的 "://" 分支处理
_SCHEME_PREFIX_RE = re.compile(
    r"^(socks5h?|socks4a?|https?)\s*[:：,，]\s*(?!//)", re.I
)
_HOST_RE = re.compile(r"^[a-zA-Z0-9.\-_]+$")


def _clean(raw: str) -> str:
    text = (raw or "").strip()
    for bad, good in _CHAR_MAP.items():
        text = text.replace(bad, good)
    # 去掉首尾包裹的引号与空白
    text = text.strip().strip("'\"` ，,")
    # 「proxy:」「代理:」这类标签前缀
    prev = None
    while prev != text:
        prev = text
        text = _LABEL_RE.sub("", text).strip()
    return text


def normalize_proxy_url(raw: str, *, default_scheme: str = "socks5") -> str:
    """把任何人类写法归一化成 `scheme://[user[:pass]@]host:port`。

    失败抛 `ValueError`，消息是可直接展示给用户看的中文。
    """
    text = _clean(raw)
    if not text:
        return ""

    scheme = default_scheme
    # 反复剥离「类型片段」与「标签」，直到剩下 [user[:pass]@]host[:port]。
    # 例如：「socks5，proxy:abc@1.2.3.4:1080」要先去掉 socks5 段、再去掉 proxy 标签。
    for _ in range(5):
        m = _SCHEME_PREFIX_RE.match(text)
        if m:
            scheme = m.group(1).lower()
            text = text[m.end():].strip().lstrip(",，:：").strip()
            continue
        stripped = _LABEL_RE.sub("", text).strip()
        if stripped != text:
            text = stripped
            continue
        break

    if "://" in text:
        head, _, tail = text.partition("://")
        scheme = head.strip().lower()
        text = tail.strip()

    scheme = ALIAS_SCHEMES.get(scheme, scheme)
    if scheme not in KNOWN_SCHEMES:
        raise ValueError(f"不支持的代理类型「{scheme}」，只支持 {', '.join(KNOWN_SCHEMES)}")

    # 到这里剩下的形态是 [user[:pass]@]host[:port]
    userinfo = ""
    if "@" in text:
        userinfo, _, text = text.rpartition("@")
        userinfo = userinfo.strip()
    hostport = text.strip().rstrip("/")
    if not hostport:
        raise ValueError("代理地址缺少主机部分")

    host, port = _split_host_port(hostport)
    port = port or DEFAULT_PORTS[scheme]
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"  # IPv6 输出时必须补回中括号，否则下游解析会错位

    # 用户名/密码里的特殊字符原样保留即可，URL 解析交给下游
    prefix = f"{userinfo}@" if userinfo else ""
    return f"{scheme}://{prefix}{host}:{port}"


def _split_host_port(hostport: str) -> tuple[str, int | None]:
    """拆 host 与 port。支持 IPv4、域名与 `[::1]:1080` 形式的 IPv6。"""
    host, port = hostport, None
    bracketed = False
    if hostport.startswith("["):  # [::1]:1080
        end = hostport.find("]")
        if end == -1:
            raise ValueError("IPv6 地址缺少右中括号")
        host = hostport[1:end]
        bracketed = True
        rest = hostport[end + 1:]
        if rest.startswith(":"):
            port = _parse_port(rest[1:])
    elif ":" in hostport:
        left, _, right = hostport.rpartition(":")
        if right.isdigit():
            host, port = left, _parse_port(right)
        else:
            raise ValueError(f"端口号不是数字：{right}")
    if not host or (not bracketed and not _HOST_RE.match(host)):
        if ":" in host:
            raise ValueError(f"IPv6 地址请用中括号包裹：[{host}]")
        raise ValueError(f"主机地址不合法：{host}")
    return host, port


def _parse_port(text: str) -> int:
    if not text.isdigit():
        raise ValueError(f"端口号不是数字：{text}")
    port = int(text)
    if not 1 <= port <= 65535:
        raise ValueError(f"端口号超出范围（1~65535）：{port}")
    return port


def describe_proxy(url: str) -> dict[str, object]:
    """把代理地址拆成可安全展示的字段（**不含密码明文**）。"""
    empty = {"set": False, "scheme": None, "host": None, "port": None,
             "username": None, "has_password": False, "masked": None}
    if not url:
        return empty
    try:
        parts = urlsplit(url)
    except Exception:
        return empty
    username = parts.username or None
    return {
        "set": True,
        "scheme": parts.scheme or None,
        "host": parts.hostname,
        "port": parts.port,
        "username": username,
        "has_password": bool(parts.password),
        "masked": mask_proxy(url),
    }


def mask_proxy(url: str) -> str:
    """对外展示用的掩码：`socks5://user:***@1.2.3.4:1080`。"""
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except Exception:
        return "***"
    user = parts.username or ""
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    if user:
        netloc = f"{user}:***@{netloc}"
    elif parts.password:
        netloc = f"***@{netloc}"
    return f"{parts.scheme}://{netloc}"


def resolve_preferred(*candidates: str) -> str:
    """按优先级挑第一个非空的代理，并归一化。

    优先级由调用方按传参顺序给出（本项目是：Provider 独立代理 > SOCKS > HTTPS > HTTP）。
    单个候选写错时给出明确错误，而不是静默继续——宁可让用户看到「代理地址写错了」，
    也不要悄悄直连导致他以为是数据源坏了。
    """
    for item in candidates:
        if item and item.strip():
            return normalize_proxy_url(item)
    return ""
