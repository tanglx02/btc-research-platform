# -*- coding: utf-8 -*-
"""首次启动的安装引导：选择数据库、连接自检、落盘配置。

为什么需要它
------------
原来装这套系统得先手工改 `.env` 里的 `DATABASE_URL`，改完还得自己想办法验证
到底连没连上 —— 一次 landing 不好就表现为「页面一片降级」，而用户完全不知道
是因为数据库没配通。

更实际的需求是：**多台设备要看到同一份数据**。SQLite 是本地文件，放在几台机器上
就是几份互不相关的库；只有把库放到一台大家都能连的 PostgreSQL / MySQL 上才一致。
所以把「选哪种数据库」做成首次启动的必经一步，并且在保存之前先真连一下。

设计硬约束
----------
1. **没测通不许保存。** 「先存再说」的后果是服务起不来，而错误信息还在日志里。
2. **自检必须给药方。** 缺驱动给 pip 命令，密码错说密码错，库不存在说库不存在；
   「连接失败」四个字对一个正在装机的人来说毫无用处。
3. **口令不进日志。** 所有对外输出一律走 `mask_url()`。
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from .logging import get_logger

logger = get_logger(__name__)

#: `.env` 的位置跟随仓库根目录：便携版里就是解压目录，换了机器也跟着走
ENV_PATH = Path(__file__).resolve().parents[3] / ".env"

_ENV_HEADER = (
    "# BTC 全市场智能研究平台 —— 运行环境配置\n"
    "# 本文件由「首次启动安装向导」写入，也可以手工编辑；编辑后重启服务生效。\n"
    "# 完整可选项见 .env.example，未列出的项一律使用代码默认值。\n"
)


# ---------------------------------------------------------------- .env 读写


def read_env_file(path: Path | str | None = None) -> str:
    p = Path(path or ENV_PATH)
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8")
    except OSError as exc:  # 只读异常不至于让安装向导整体失败
        logger.event("setup.env_read_failed", error=str(exc)[:200])
        return ""


def upsert_env_vars(pairs: dict[str, str], path: Path | str | None = None) -> list[str]:
    """按 key 更新 `.env`，**保留注释与原有顺序**，缺失的追加到文件末尾。

    为什么不能用 `python-dotenv` 覆写整个文件：那样会把用户自己写的中文注释
    和分组空行全部打平，第二次进来看到的是一份面目全非的配置文件。

    返回被写到的文件路径，便于安装向导把「配置写在哪了」如实告诉用户。
    """
    p = Path(path or ENV_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    remaining = dict(pairs)

    lines = read_env_file(p).splitlines() if p.exists() else []
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        key = _env_key_of(stripped)
        if key and key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)

    if remaining:
        if out and out[-1].strip() and not out[-1].strip().startswith("#"):
            out.append("")
        for key, value in remaining.items():
            out.append(f"{key}={value}")

    text = "\n".join(out).rstrip("\n")
    if not text:
        text = _ENV_HEADER.rstrip("\n")
    p.write_text(text + "\n", encoding="utf-8")
    return [str(p)]


def _env_key_of(line: str) -> str:
    """从一行文本里取出 env key；注释行、无 `=` 的行返回空串。"""
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return ""
    key = stripped.split("=", 1)[0].strip()
    if key.lower().startswith("export "):
        key = key[7:].strip()
    return key


# ---------------------------------------------------------------- 状态判定


def needs_setup(settings: Any = None) -> bool:
    """是否需要先过一遍安装向导。

    判据只有一条：`SETUP_COMPLETED` 是否为真。这个值由向导在连接自检通过之后写入 `.env`。
    """
    s = settings or _settings()
    return not bool(getattr(s, "SETUP_COMPLETED", False))


def _settings() -> Any:
    from .config import get_settings

    return get_settings()


async def ensure_adopted_existing_install(settings: Any = None, *, min_tables: int = 5) -> bool:
    """给「升级上来的老部署」自动补上已安装标记。

    `SETUP_COMPLETED` 是这次才加的配置项。已经跑着一套库的人如果也被逼着
    重走一遍向导，第一反应一定是「我数据还在吗」。所以这里做一次判定：
    当前连得上的库里如果已经有相当数量的本平台表，就认为已经装过了，
    把这个结论写进 `.env`，用户毫无感知。

    Args:
        min_tables: 至少命中多少张本平台的表才算「已经装过」。
            不能只看 1~2 张：万一有人把本平台接到一个恰好也有同名表的业务库上，
            判定成「已安装」会让向导永远不再出现，反而更难挽回。
    """
    s = settings or _settings()
    if getattr(s, "SETUP_COMPLETED", False):
        return True

    from sqlalchemy import text

    from ..db.base import get_engine
    from ..db.dialects import detect_dialect
    from ..db.models import ALL_TABLES

    known = {t.__tablename__ for t in ALL_TABLES}
    try:
        dialect = detect_dialect(s.DATABASE_URL)
        engine = get_engine(s)
        async with engine.connect() as conn:
            rows = (await conn.execute(text(_table_list_sql(dialect)))).scalars().all()
    except Exception:  # noqa: BLE001 - 连不上就是「没装过」，这正是要判定的事情
        return False

    if len(set(rows) & known) < min_tables:
        return False

    from .config import get_settings

    os.environ["SETUP_COMPLETED"] = "true"
    upsert_env_vars({"SETUP_COMPLETED": "true"})
    get_settings.cache_clear()
    logger.event("setup.adopted_existing", tables=len(set(rows) & known))
    return True


# ---------------------------------------------------------------- 连接自检


def _version_sql(dialect: str) -> str:
    return {
        "sqlite": "SELECT sqlite_version()",
        "postgresql": "SELECT version()",
        "mysql": "SELECT VERSION()",
    }.get(dialect, "SELECT 1")


def _table_list_sql(dialect: str) -> str:
    if dialect == "sqlite":
        return "SELECT name FROM sqlite_master WHERE type = 'table'"
    if dialect == "mysql":
        return (
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = DATABASE()"
        )
    return (
        "SELECT tablename FROM pg_tables "
        "WHERE schemaname NOT IN ('pg_catalog', 'information_schema')"
    )


def _connect_args(dialect: str, timeout: float) -> dict[str, Any]:
    """每个方言的超时参数名都不一样，写错一个就是「连不上但不知道为什么」。

    * asyncpg 用 ``timeout``（秒，float）
    * aiomysql 底层是 pymysql，用 ``connect_timeout``（秒，int）
    * aiosqlite 不需要
    """
    if dialect == "postgresql":
        return {"timeout": float(timeout)}
    if dialect == "mysql":
        return {"connect_timeout": max(1, int(timeout))}
    return {"timeout": 30}


def friendly_db_error(dialect: str, exc: BaseException) -> str:
    """把驱动抛出的底层异常翻译成「照着做就能解决」的中文提示。

    这一函数存在的理由很具体：``asyncpg`` 的原始错误是英文而且极其简略
    （``password authentication failed for user "btc"``），
    MySQL 更是只给一个数字码（``(1045, "Access denied...")``）。
    装机的人看到这些会直接怀疑是平台有问题。
    """
    message = str(exc).strip()
    cls = type(exc).__name__
    module = type(exc).__module__ or ""
    low = message.lower()

    if isinstance(exc, asyncio.TimeoutError) or isinstance(exc, TimeoutError):
        return (
            "连接超时：主机没有在预期时间内响应。按顺序检查三件事："
            "① 数据库是否已启动；"
            "② 是否监听了对外地址（很多发行版默认只监听 127.0.0.1，其它机器连不上）；"
            "③ 防火墙或云厂商安全组是否放行了端口。"
        )

    if "gaierror" in cls.lower() or "getaddrinfo" in low or "name or service not known" in low:
        return "主机名解析失败：请把主机换成 IP 地址，或检查 DNS 设置。"

    # --- 认证 / 库不存在（按驱动的异常类名识别，不 import 具体包，装没装都能用）
    auth_markers = ("InvalidPasswordError", "OperationalError 1045", "(1045,", "1045")
    missing_db_markers = ("InvalidCatalogNameError", "(1049,", "1049")
    permission_markers = ("InsufficientPrivilege", "(1044,", "(1142,")

    blob = f"{module}.{cls} {message}"
    if any(m in blob for m in auth_markers):
        return "用户名或密码不正确：请检查填错了没有，注意密码区分大小写。"
    if any(m in blob for m in missing_db_markers):
        return (
            "数据库不存在：请用管理员账号先建一个空库"
            "（PostgreSQL：CREATE DATABASE xxx；MySQL：CREATE DATABASE xxx CHARACTER SET utf8mb4），"
            "本向导只连库、不建库。"
        )
    if any(m in blob for m in permission_markers):
        return "当前账号没有访问该数据库的权限：请让管理员授权，或换一个账号试试。"

    if isinstance(exc, ConnectionRefusedError) or "connection refused" in low or "(2003," in message:
        return (
            "连接被拒绝：目标主机上这个端口没有程序在监听。"
            "检查端口号是否填错，以及数据库服务是否真的启动了。"
        )
    if "network is unreachable" in low or "no route to host" in low:
        return "网络不可达：本机到目标主机之间没有可用路由，检查 IP 是否在同一网段。"
    if "ssl" in low and ("not support" in low or "refused" in low or "unexpected" in low):
        return "SSL 协商失败：该数据库可能没开启 SSL，去掉 sslmode 参数再试一次。"
    if dialect == "sqlite" and ("unable to open" in low or "no such file" in low):
        return "SQLite 文件打不开：路径不存在或当前账号没有写权限，换一个有写权限的目录。"
    return f"连接失败：{message[:220]}"


async def probe_database(raw_url: str, *, timeout: float = 8.0) -> dict[str, Any]:
    """真的去连一次数据库，把每一步的结果逐项报回来。

    为什么不只做一句 `SELECT 1`：能连上不等于能用。装下去之后第一件事就是建 45 张表，
    所以这里还要顺手验证「有没有建表权限」和「现有库里已经有哪些表」，
    免得用户填了一个别人的业务库，把我们的表插进去。
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from ..db.base import normalize_url
    from ..db.dialects import detect_dialect, install_hint, is_driver_installed, mask_url
    from ..db.models import ALL_TABLES

    started = time.perf_counter()
    checks: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "ok": False, "checks": checks, "hint": None,
        "latency_ms": 0.0, "masked_url": "", "dialect": "",
        "server_version": None, "existing_tables": 0, "expected_tables": len(ALL_TABLES),
        "writable": False, "summary": "",
    }

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    # ---- 步骤 1：连接串能不能看懂
    try:
        dialect = detect_dialect(raw_url)
    except ValueError as exc:
        add("连接串格式", False, str(exc))
        result["hint"] = "连接串必须以 sqlite:// / postgresql:// / mysql:// 开头。"
        result["summary"] = str(exc)
        _finish(result, started)
        return result
    result["dialect"] = dialect

    try:
        url = normalize_url(raw_url)
    except Exception as exc:  # noqa: BLE001
        detail = friendly_db_error(dialect, exc)
        add("连接串格式", False, detail)
        result["hint"] = detail
        result["summary"] = detail
        _finish(result, started)
        return result
    result["masked_url"] = mask_url(url)
    add("连接串格式", True, f"识别为 {dialect}，将使用异步驱动连接")

    # ---- 步骤 2：驱动装了没有
    if not is_driver_installed(dialect):
        hint = install_hint(dialect)
        add("异步驱动", False, f"缺少驱动，请安装：{hint}")
        result["hint"] = f"在本机执行下面这条命令装好驱动，再回来点「测试连接」：{hint}"
        result["summary"] = "缺少数据库驱动"
        _finish(result, started)
        return result
    add("异步驱动", True, "已安装")

    engine = None
    probe_table = "_btc_setup_probe"
    try:
        engine = create_async_engine(
            url,
            connect_args=_connect_args(dialect, timeout),
            poolclass=NullPool,
            future=True,
        )
        try:
            async with asyncio.timeout(max(2.0, timeout + 3.0)):
                async with engine.connect() as conn:
                    version = (await conn.execute(text(_version_sql(dialect)))).scalar()
                    add("建立连接", True, f"服务端版本：{str(version)[:80]}")
                    result["server_version"] = str(version)[:120]

                    # 已有表：判断这是不是一个干净的新库
                    rows = (await conn.execute(text(_table_list_sql(dialect)))).scalars().all()
                    known = {t.__tablename__ for t in ALL_TABLES}
                    hit = sorted(set(rows) & known)
                    result["existing_tables"] = len(hit)
                    add("已有数据对比", True,
                        f"本平台所需的 {len(known)} 张表中有 {len(hit)} 张已存在"
                        + (f"（{', '.join(hit[:4])}{'…' if len(hit) > 4 else ''}）" if hit else "，这是一个干净的新库"))

                    # 写权限：安不安全、能不能建表，不实际试一次根本不知道
                    await conn.execute(text(f"DROP TABLE IF EXISTS {probe_table}"))
                    await conn.execute(text(f"CREATE TABLE {probe_table} (id INTEGER PRIMARY KEY, v INTEGER)"))
                    try:
                        await conn.execute(text(f"INSERT INTO {probe_table} (id, v) VALUES (1, 42)"))
                        cur = await conn.execute(text(f"SELECT v FROM {probe_table} WHERE id = 1"))
                        assert cur.scalar() == 42
                    finally:
                        await conn.execute(text(f"DROP TABLE IF EXISTS {probe_table}"))
                    await conn.commit()
                    add("读写权限", True, "可建表、可写入、可删除")
                    result["writable"] = True
        except asyncio.TimeoutError:
            add("建立连接", False, friendly_db_error(dialect, TimeoutError()))
            result["hint"] = "先确认主机和端口是对的：在本机执行 ping 或 telnet 试一下。"
            result["summary"] = "连接超时"
            _finish(result, started)
            return result

        result["ok"] = True
        # latency 必须在拼 summary 之前算好，否则摘要里会永远是 0 ms
        result["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
        result["summary"] = (
            f"连接正常：{(result['server_version'] or dialect).split(',')[0]}"
            f"，已存在 {result['existing_tables']}/{result['expected_tables']} 张表"
            f"，读写权限正常，往返耗时 {result['latency_ms'] or 0:.0f} ms"
        )
    except Exception as exc:  # noqa: BLE001 - 自检接口必须永远返回结果，不抛给前端一个 500
        logger.event("setup.probe_failed", dialect=dialect, error=str(exc)[:200])
        detail = friendly_db_error(dialect, exc)
        # 已经连上才可能卡在建表上，按当前进展决定是哪一步失败，别一律报「连不上」
        connected = any(c["name"] == "建立连接" and c["ok"] for c in checks)
        failed_name = "读写权限" if connected else "建立连接"
        if any(c["name"] == failed_name for c in checks):
            failed_name += "（第二阶段）"
        add(failed_name, False, detail)
        result["hint"] = detail
        result["summary"] = detail
    finally:
        if engine is not None:
            try:
                await engine.dispose()
            except Exception:  # noqa: BLE001
                pass

    _finish(result, started)
    return result


def _finish(result: dict[str, Any], started: float) -> None:
    result["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
    if not result.get("summary"):
        failed = [c for c in result["checks"] if not c["ok"]]
        result["summary"] = failed[0]["detail"] if failed else "自检未通过"


# ---------------------------------------------------------------- 完成安装


async def complete_setup(raw_url: str) -> dict[str, Any]:
    """连接自检 -> 写 `.env` -> 重建引擎 -> 就地完成初始化。

    只有**自检通过**才会落盘。写完立刻在本进程里跑一遍启动动作，
    用户不需要重启服务就能直接进页面。
    """
    from ..db.base import normalize_url, reset_engine
    from ..db.dialects import detect_dialect, mask_url
    from .bootstrap import run_startup_tasks
    from .config import get_settings

    probe = await probe_database(raw_url)
    if not probe["ok"]:
        return {"completed": False, "probe": probe, "written_files": [], "written_keys": []}

    dialect = detect_dialect(raw_url)
    url = normalize_url(raw_url)

    written_keys = ["DATABASE_URL", "SETUP_COMPLETED"]
    # env 变量的优先级高于 .env 文件：两个都写，否则同一次进程里 reload 不生效。
    os.environ["DATABASE_URL"] = url
    os.environ["SETUP_COMPLETED"] = "true"
    files = upsert_env_vars({"DATABASE_URL": url, "SETUP_COMPLETED": "true"})

    await reset_engine()
    s = get_settings()

    from .bootstrap import run_startup_tasks

    summary = await run_startup_tasks(s)

    logger.event("setup.completed", dialect=dialect, url=mask_url(url))
    return {
        "completed": True,
        "dialect": dialect,
        "dialect_label": {"sqlite": "SQLite", "postgresql": "PostgreSQL", "mysql": "MySQL"}.get(dialect, dialect),
        "masked_url": mask_url(url),
        "written_files": files,
        "written_keys": written_keys,
        "probe": probe,
        "startup": summary,
        "needs_restart": False,
        "hint": (
            "配置已写入 .env（只有本机可读），以后启动会直接读取。"
            if dialect != "sqlite"
            else "数据将保存在本地文件里，换到别的机器不会自动同步。"
        ),
    }
