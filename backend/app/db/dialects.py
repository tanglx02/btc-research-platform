# -*- coding: utf-8 -*-
"""数据库方言适配：URL 归一化、异步驱动映射、依赖检测与人工可读标签。

为什么要单独抽一个模块
----------------------
平台要同时跑在 SQLite / PostgreSQL / MySQL 上，而方言差异散落在五个地方：

1. SQLAlchemy **异步驱动**的 scheme（`sqlite+aiosqlite` / `postgresql+asyncpg` / `mysql+aiomysql`）
2. upsert 语法（PG 与 SQLite 用 `ON CONFLICT`，MySQL 只有 `ON DUPLICATE KEY UPDATE`）
3. 自增主键类型（SQLite 只对 `INTEGER PRIMARY KEY` 生成 rowid）
4. 新增列的地雷 PRAGMA vs `information_schema`
5. 需要额外 pip 安装的驱动包（``asyncpg`` / ``aiomysql``）

这些知识先前分别写在 `base.py`、`repo.py`、`migrate.py` 里，改一处漏一处：
最常见的事故是「换了数据库才发现某个查询用了 SQLite 专用语法」。
集中到本模块之后，新增一种方言只需要改这里加一张表。

一条重要的历史教训
------------------
`base.normalize_url` 原来把 ``postgresql://`` 改写成 ``postgresql+psycopg://``。
`psycopg` 是**同步**驱动，装到 asyncio 引擎上会在第一次查询时炸
（``InvalidRequestError: The asyncio extension requires an async driver``）；
也就是说「文档写着支持 PostgreSQL」，实际一配就崩，而且崩在运行时而非启动时。
现在统一由 `to_async_url()` 决定，并且只允许异步驱动。
"""

from __future__ import annotations

from importlib.util import find_spec
from typing import Any, Final
from urllib.parse import quote, urlsplit

# ---------------------------------------------------------------- 常量表

#: 支持的方言 -> (展示名, 一句话说明)
DIALECT_INFO: Final[dict[str, dict[str, str]]] = {
    "sqlite": {
        "label": "SQLite",
        "tagline": "单机文件数据库，零依赖、开箱即用",
        "best_for": "一台机器自己用；数据保存在本地文件里",
        "default_port": "",
        "default_file": "data/btc.db",
    },
    "postgresql": {
        "label": "PostgreSQL",
        "tagline": "多机共享同一份数据，支持 TimescaleDB 时序扩展",
        "best_for": "多台设备/多个同事要看到完全一致的数据",
        "default_port": "5432",
        "default_file": "",
    },
    "mysql": {
        "label": "MySQL / MariaDB",
        "tagline": "多机共享，运维生态成熟",
        "best_for": "已有 MySQL 实例，想直接复用",
        "default_port": "3306",
        "default_file": "",
    },
}

#: 方言 -> SQLAlchemy 异步 scheme
ASYNC_SCHEME: Final[dict[str, str]] = {
    "sqlite": "sqlite+aiosqlite",
    "postgresql": "postgresql+asyncpg",
    "mysql": "mysql+aiomysql",
}

#: 方言 -> 需要 pip 安装的驱动包名（SQLite 的 aiosqlite 随 SQLAlchemy[asyncio] 一起装，仍需显式判断）
DRIVER_PACKAGE: Final[dict[str, str]] = {
    "sqlite": "aiosqlite",
    "postgresql": "asyncpg",
    "mysql": "aiomysql",
}

#: 方言 -> 必装的 import 名。`asyncpg` / `aiomysql` 的包名与模块名一致；
#: 但 find_spec 判断的是 import 名，所以单独列一张表更稳。
DRIVER_MODULE: Final[dict[str, str]] = {
    "sqlite": "aiosqlite",
    "postgresql": "asyncpg",
    "mysql": "aiomysql",
}

#: 用户可能贴进来的各种写法 -> 标准方言名
SCHEME_ALIASES: Final[dict[str, str]] = {
    "sqlite": "sqlite",
    "sqlite3": "sqlite",
    "file": "sqlite",
    "postgresql": "postgresql",
    "postgres": "postgresql",
    "pgsql": "postgresql",
    "pg": "postgresql",
    "mysql": "mysql",
    "mariadb": "mysql",
}

#: 不受支持的常见写法 -> 给用户的解释
_REJECTED_SCHEMES: Final[dict[str, str]] = {
    "psycopg2": "这是同步驱动，异步引擎用不了；请删掉 psycopg2 这一段，直接写 postgresql://",
    "pymysql": "这是同步驱动，异步引擎用不了；请删掉 pymysql 这一段，直接写 mysql://",
    "mysqldb": "这是同步驱动，异步引擎用不了；请删掉 MySQLdb 这一段，直接写 mysql://",
}


# ---------------------------------------------------------------- 识别


def detect_dialect(url: str) -> str:
    """从 URL 的 scheme 判断方言（`sqlite` / `postgresql` / `mysql`）。

    无法识别时抛 `ValueError`，消息可直接展示给用户——宁可让他在「连接自检」
    这一步就看到问题，也不要拖到采集任务跑起来才炸。
    """
    raw = (url or "").strip()
    if not raw:
        raise ValueError("数据库连接地址为空")
    scheme = raw.split("://", 1)[0] if "://" in raw else ""
    if not scheme:
        raise ValueError(
            f"数据库连接地址缺少类型前缀（形如 postgresql:// 或 mysql://）：{raw[:60]}"
        )
    base = scheme.split("+", 1)[0].lower()

    hint = _REJECTED_SCHEMES.get(base)
    if hint:
        raise ValueError(hint)

    dialect = SCHEME_ALIASES.get(base)
    if dialect is None:
        raise ValueError(
            f"不支持的数据库类型「{base}」，"
            f"目前支持：{', '.join(sorted(set(SCHEME_ALIASES)))}"
        )
    return dialect


def normalize_to_async(url: str) -> str:
    """把任意写法的 URL 归一化为「方言 + 异步驱动」的形态。

    * ``postgres://user:pw@host/db`` -> ``postgresql+asyncpg://user:pw@host/db``
    * ``postgresql+psycopg://...``   -> ``postgresql+asyncpg://...``（纠正常见误配）
    * ``mysql+pymysql://...``        -> ``mysql+aiomysql://...``
    """
    dialect = detect_dialect(url)
    head, sep, tail = url.partition("://")
    if not sep:
        raise ValueError(f"数据库连接地址格式不对（缺少 ://）：{url[:60]}")
    return f"{ASYNC_SCHEME[dialect]}://{tail}"


def is_driver_installed(dialect: str) -> bool:
    """判断该方言的异步驱动是否已安装（只看能否 import，不实际导入）。"""
    module = DRIVER_MODULE.get(dialect)
    if not module:
        return False
    try:
        return find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def install_hint(dialect: str) -> str:
    """缺驱动时给一条能直接复制执行的命令。"""
    pkg = DRIVER_PACKAGE.get(dialect, "")
    if not pkg:
        return ""
    mirror = "https://mirrors.cloud.tencent.com/pypi/simple"
    return f"pip install {pkg} -i {mirror}"


def port_placeholder(dialect: str) -> str:
    return DIALECT_INFO.get(dialect, {}).get("default_port", "")


# ---------------------------------------------------------------- 构造


def build_url(
    dialect: str,
    *,
    host: str = "",
    port: str | int | None = None,
    database: str = "",
    username: str = "",
    password: str = "",
    file_path: str = "",
    sslmode: str = "",
) -> str:
    """按结构化字段拼出连接串。用户名与密码一律 URL 编码。

    为什么不能纯字符串拼接：密码里出现 `@` `:` `#` `/` 是常态
    （随机生成的口令几乎必然包含），不编码的话会把它截成主机或端口，
    报出来的错往往是「无法解析主机地址」，跟密码完全对不上号。
    """
    if dialect not in DIALECT_INFO:
        raise ValueError(f"不支持的数据库类型：{dialect}")
    scheme = ASYNC_SCHEME[dialect]

    if dialect == "sqlite":
        path = (file_path or DIALECT_INFO["sqlite"].get("default_file", "btc.db")).strip()
        if not path:
            raise ValueError("SQLite 需要指定数据库文件路径")
        return f"{scheme}:///{path.lstrip('/')}"

    host = (host or "").strip()
    if not host:
        raise ValueError("请填写数据库主机地址")
    database = (database or "").strip()
    if not database:
        raise ValueError("请填写数据库名")

    port_text = str(port).strip() if port not in (None, "") else ""
    if not port_text:
        port_text = port_placeholder(dialect)
    try:
        port_int = int(port_text)
    except ValueError as exc:
        raise ValueError(f"端口号不是数字：{port_text}") from exc
    if not 1 <= port_int <= 65535:
        raise ValueError(f"端口号超出范围（1~65535）：{port_int}")

    if ":" in host and not host.startswith("["):
        host = f"[{host}]"  # IPv6 必须包中括号，否则 `host:port` 会被截成两段

    userinfo = ""
    if username or password:
        user = quote(str(username or ""), safe="")
        pwd = quote(str(password or ""), safe="")
        userinfo = f"{user}:{pwd}@" if pwd else f"{user}@"

    query = f"?sslmode={quote(sslmode, safe='')}" if sslmode else ""
    return f"{scheme}://{userinfo}{host}:{port_int}/{database}{query}"


def mask_url(url: str) -> str:
    """对外展示用的掩码（隐藏口令）。"""
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except Exception:  # noqa: BLE001 - 地址残缺时也要能显示，不能连带整个自检崩掉
        return "***"
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    if parts.username:
        netloc = f"{parts.username}:***@{netloc}"
    elif parts.password:
        netloc = f"***@{netloc}"
    path = parts.path
    return f"{parts.scheme}://{netloc}{path}"


def describe(dialect: str) -> dict[str, Any]:
    """给前端渲染数据库类型卡片用的元信息。"""
    info = DIALECT_INFO.get(dialect, {})
    return {
        "dialect": dialect,
        "label": info.get("label", dialect),
        "tagline": info.get("tagline", ""),
        "best_for": info.get("best_for", ""),
        "default_port": info.get("default_port", ""),
        "default_file": info.get("default_file", ""),
        "driver_package": DRIVER_PACKAGE.get(dialect, ""),
        "driver_installed": is_driver_installed(dialect),
        "install_hint": install_hint(dialect),
        "example": build_url(
            dialect,
            host="127.0.0.1",
            database="btc_research",
            username="btc_user",
            password="换成你的密码",
        ) if dialect != "sqlite" else f"{ASYNC_SCHEME[dialect]}:///data/btc.db",
    }


def supported_dialects() -> list[dict[str, Any]]:
    return [describe(d) for d in ("sqlite", "postgresql", "mysql")]
