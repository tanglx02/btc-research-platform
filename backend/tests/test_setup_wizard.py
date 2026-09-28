"""首次启动安装向导的回归用例。

覆盖三件事：

1. `.env` 的读写 —— 配置被 AUTOMATIC 写坏比功能缺失更糟糕，
   用户自己写的中文注释不能被打平，写入必须幂等。
2. 连接自检 —— 必须给出可照做的结论，而不是「连接失败」四个字；
   而且**任何情况下都不能抛异常**（这是给用户看的第一步东西）。
3. 安装闸门 —— 没装好时 `/api/v1/**` 必须被 503 挡下，
   否则用户看到的是一堆与「数据库没配」无关的报错。

注意：所有会写 `.env` 的用例都必须先把 `ENV_PATH` 指向临时目录，
谁都不想在跑一次测试之后发现自己仓库里的配置文件被改写了。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core.config import get_settings


def _reload_env(**pairs: str) -> None:
    for key, value in pairs.items():
        os.environ[key] = value
    get_settings.cache_clear()


# ============================================================ .env 读写


@pytest.fixture()
def temp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 ENV_PATH 指到临时文件，测试期间绝不碰仓库里真实的 .env。"""
    from app.core import setup as setup_core

    target = tmp_path / ".env"
    monkeypatch.setattr(setup_core, "ENV_PATH", target)
    return target


def test_upsert_env_preserves_comments_and_order(temp_env: Path) -> None:
    from app.core.setup import upsert_env_vars

    temp_env.write_text(
        "# 我自己写的注释\n"
        "APP_ENV=development\n"
        "\n"
        "# 下面是我手工加的\n"
        "TIMEZONE=Asia/Shanghai\n",
        encoding="utf-8",
    )
    upsert_env_vars({"APP_ENV": "production", "SETUP_COMPLETED": "true"})

    lines = temp_env.read_text(encoding="utf-8").splitlines()
    assert "# 我自己写的注释" in lines, "用户写的注释不能被打平"
    assert "# 下面是我手工加的" in lines
    assert lines.index("APP_ENV=production") > lines.index("# 我自己写的注释")
    # 原有键在原地更新，不能挪位置、也不能重复出现
    assert [i for i, ln in enumerate(lines) if ln.startswith("APP_ENV=")] == [1]
    assert lines.count("SETUP_COMPLETED=true") == 1
    assert "TIMEZONE=Asia/Shanghai" in lines


def test_upsert_env_is_idempotent(temp_env: Path) -> None:
    from app.core.setup import upsert_env_vars

    for _ in range(3):
        upsert_env_vars({"DATABASE_URL": "sqlite+aiosqlite:///x.db"})
    text = temp_env.read_text(encoding="utf-8")
    assert text.count("DATABASE_URL=") == 1, "同一个 key 反复写只能有一行"


def test_upsert_env_creates_file_when_missing(temp_env: Path) -> None:
    from app.core.setup import upsert_env_vars

    assert not temp_env.exists()
    upsert_env_vars({"SETUP_COMPLETED": "true"})
    assert temp_env.exists()
    assert "SETUP_COMPLETED=true" in temp_env.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "line,expected",
    [
        ("APP_ENV=dev", "APP_ENV"),
        ("  ADMIN_TOKEN  =  abc  ", "ADMIN_TOKEN"),
        ("# 注释里有等号=也没用", ""),
        ("随便一行没有等号", ""),
        ("", ""),
    ],
)
def test_env_key_of(line: str, expected: str) -> None:
    from app.core.setup import _env_key_of

    assert _env_key_of(line) == expected


def test_needs_setup_follows_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import setup as setup_core

    monkeypatch.setenv("SETUP_COMPLETED", "false")
    get_settings.cache_clear()
    assert setup_core.needs_setup() is True

    monkeypatch.setenv("SETUP_COMPLETED", "true")
    get_settings.cache_clear()
    assert setup_core.needs_setup() is False


# ============================================================ 连接自检


async def test_probe_sqlite_reports_version_and_write_permission(isolated_db: Path) -> None:
    from app.core.setup import probe_database

    target = isolated_db.parent / f"probe_{isolated_db.stem}.db"
    result = await probe_database(f"sqlite:///{target.as_posix()}")

    assert result["ok"] is True, result["summary"]
    assert result["dialect"] == "sqlite"
    assert result["writable"] is True
    assert result["existing_tables"] == 0
    assert result["latency_ms"] > 0, "摘要里的耗时必须真的量过，不能恒为 0"
    names = [c["name"] for c in result["checks"]]
    assert names == ["连接串格式", "异步驱动", "建立连接", "已有数据对比", "读写权限"]
    assert all(c["ok"] for c in result["checks"])


async def test_probe_never_raises_on_garbage_url() -> None:
    """自检是第一道门面：任何输入都必须返回一份报告，不能把 500 甩给前端。"""
    from app.core.setup import probe_database

    for bad in ["", "mssql://a:b@c/d", "这不是连接串", "postgresql://", "mysql://u@:0/"]:
        result = await probe_database(bad, timeout=2)
        assert isinstance(result, dict) and result["ok"] is False
        assert result["summary"], f"{bad!r} 必须给出一句可读的失败原因"
        assert result["checks"], f"{bad!r} 至少要有一条检查项"


async def test_probe_unreachable_host_gives_actionable_reason() -> None:
    from app.core.setup import probe_database

    result = await probe_database("postgresql://u:p@127.0.0.1:5599/none", timeout=2)
    assert result["ok"] is False
    # 「连接失败：...」这种没有下文的话不该出现在一个装机界面上
    assert "连接失败" not in result["summary"]
    # 连不上的表现有两种（端口没监听会给 RST；被防火墙丢弃则是超时），
    # 但无论哪一种都不能退化成一句干巴巴的失败，必须给出下一步排查方向。
    assert any(word in result["summary"] for word in ("端口", "拒绝", "超时")), result["summary"]


async def test_probe_missing_driver_returns_pip_command(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import setup as setup_core
    from app.db import dialects

    monkeypatch.setattr(dialects, "is_driver_installed", lambda _d: False)
    # set_ups 里是从 dialects 现取的函数，patch 模块属性即可生效
    result = await setup_core.probe_database("postgresql://u:p@1.2.3.4:5432/db")

    assert result["ok"] is False
    assert "asyncpg" in (result["hint"] or ""), "缺驱动时必须给出 pip 命令而不是只说失败"
    assert any(not c["ok"] and c["name"] == "异步驱动" for c in result["checks"])


# ============================================================ 落盘与闸门


async def test_complete_setup_writes_env_then_runs_startup(temp_env: Path) -> None:
    from app.core import setup as setup_core

    _reload_env(SETUP_COMPLETED="false")
    url = f"sqlite:///{temp_env.parent.as_posix()}/after_setup.db"
    result = await setup_core.complete_setup(url)

    assert result["completed"] is True
    assert result["dialect"] == "sqlite"
    assert "SETUP_COMPLETED=true" in temp_env.read_text(encoding="utf-8")
    assert temp_env.read_text(encoding="utf-8").count("DATABASE_URL=") == 1
    # 完成后必须就地可用，不能要求用户再去重启一次
    assert (result.get("startup") or {}).get("providers", 0) > 0
    assert setup_core.needs_setup() is False


async def test_complete_setup_refuses_to_write_when_probe_fails(temp_env: Path) -> None:
    """自检没过就不许落盘 —— 「先存再说」的结果一定是服务起不来。"""
    from app.core import setup as setup_core

    bad = "postgresql://u:p@127.0.0.1:5599/none"
    result = await setup_core.complete_setup(bad)
    assert result["completed"] is False
    assert not temp_env.exists(), "自检失败时一个字节都不该写进配置文件"


@pytest.mark.parametrize(
    "spec,dialect",
    [
        ({"dialect": "sqlite", "file_path": "data/x.db"}, "sqlite"),
        ({"dialect": "postgresql", "host": "h", "database": "d", "username": "u"}, "postgresql"),
        ({"dialect": "mysql", "host": "h", "database": "d", "username": "u"}, "mysql"),
    ],
)
def test_url_from_spec_builds_correct_scheme(spec: dict, dialect: str) -> None:
    from app.api.setup import url_from_spec

    url = url_from_spec(spec)
    assert {
        "sqlite": "sqlite+aiosqlite",
        "postgresql": "postgresql+asyncpg",
        "mysql": "mysql+aiomysql",
    }[dialect] in url


def test_url_from_spec_prefers_explicit_connection_string() -> None:
    from app.api.setup import url_from_spec

    explicit = "postgresql+asyncpg://u:p@10.0.0.5:5432/db"
    assert url_from_spec({"database_url": explicit, "dialect": "sqlite"}) == explicit


def test_setup_gate_blocks_business_api_but_allows_wizard(
    monkeypatch: pytest.MonkeyPatch, isolated_db: Path
) -> None:
    """未安装时闸门必须生效：业务接口 503，引导接口照常可用。"""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("SETUP_COMPLETED", "false")
    monkeypatch.delenv("SETUP_COMPLETED", raising=False)
    os.environ["SETUP_COMPLETED"] = "false"
    get_settings.cache_clear()

    from app.main import create_app

    with TestClient(create_app()) as client:
        status = client.get("/api/v1/system/setup/status")
        assert status.status_code == 200
        assert status.json()["needs_setup"] is True

        blocked = client.get("/api/v1/system/health")
        assert blocked.status_code == 503
        assert blocked.json()["error"]["code"] == "setup_required"

        dialects = client.get("/api/v1/system/setup/dialects")
        assert dialects.status_code == 200

    _reload_env(SETUP_COMPLETED="true")


def test_openapi_schema_builds_and_lists_setup_routes() -> None:
    """OpenAPI 必须能完整生成。

    这条用例守的是一个很容易漏掉的坑：路由函数里写了 `request: Request`
    但模块没 import `Request`，而模块顶部又有 `from __future__ import annotations`。
    FastAPI 会把无法解析的 ForwardRef 当成普通请求体字段去建 Pydantic 模型，
    结果路由本身能注册成功、**只有生成 schema（也就是 /docs）时才炸**。
    平时跑单测碰不到 /docs，于是这个雷会一直埋到用户打开文档页才响。
    """
    from app.main import create_app

    spec = create_app().openapi()
    setup_paths = [p for p in spec["paths"] if p.endswith("/system/setup/status")]
    assert setup_paths, "安装引导接口没有出现在 OpenAPI 里"

    proxy = spec["paths"].get("/api/v1/system/proxy/test", {})
    assert "post" in proxy, "代理测试接口丢失（很可能是注解解析失败）"


async def test_test_endpoint_never_returns_raw_password() -> None:
    """自检报告里绝不能回显原始连接串 —— 里面有明文口令。"""
    from app.core import setup as setup_core

    from app.api.setup import test_connection

    result = await test_connection(
        {"database_url": "postgresql://u:topsecret@127.0.0.1:5599/db"},
    )
    assert result["database_url"] == ""
    assert "topsecret" not in str(result)
