"""跨方言数据库可移植性校验（upsert 语法、建表 DDL、连接串归一化）。

为什么用编译而不是真连库
------------------------
没有哪台开发机上必然装了 PostgreSQL 和 MySQL，但「语句语法对不对」这件事
必须被自动化守住 —— 否则「支持 MySQL」只是一句写在 README 里的承诺。

SQLAlchemy 允许脱离连接直接把 statement 编译成某个方言的 SQL 文本，
所以这里逐方言断言产出的 SQL 形状：

* SQLite / PostgreSQL 必须出现 ``ON CONFLICT ... DO UPDATE``
* MySQL 必须出现 ``ON DUPLICATE KEY UPDATE``，且**绝不能**出现 ``ON CONFLICT``
* MySQL 更新值来源是 ``VALUES(col)``（`inserted`），不是 PG 的 ``excluded``
* 「冲突即跳过」在 MySQL 下要变成 ``INSERT IGNORE``

这组断言同时防住一种很隐蔽的回归：有人图省事把 MySQL 也走 SQLite 那段代码，
编译阶段不报错，第一次真正连上 MySQL 才炸。
"""

from __future__ import annotations

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy.dialects import mysql, postgresql, sqlite  # noqa: E402

from app.db.dialects import (  # noqa: E402
    build_url,
    detect_dialect,
    install_hint,
    is_driver_installed,
    mask_url,
    normalize_to_async,
)
from app.db.models import Base, Candle  # noqa: E402
from app.db.upsert import build_upsert  # noqa: E402

CONFLICT = ["symbol", "interval", "ts", "source_id"]
UPDATABLE = ["open", "high", "low", "close", "volume"]

ROW = {
    "symbol": "BTC", "interval": "1d", "ts": 1700000000, "source_id": "binance_vision",
    "open": 100.0, "high": 110.0, "low": 90.0, "close": 105.0, "volume": 12.5,
}

COMPILERS = {
    "sqlite": sqlite.dialect(),
    "postgresql": postgresql.dialect(),
    "mysql": mysql.dialect(),
}


def compile_sql(dialect_name: str, update_cols=None, **kwargs) -> str:
    cols = UPDATABLE if update_cols is None else update_cols
    stmt = build_upsert(dialect_name, Candle, [ROW], CONFLICT, cols, **kwargs)
    return str(stmt.compile(dialect=COMPILERS[dialect_name])).lower()


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_on_conflict_dialects_use_on_conflict(dialect: str) -> None:
    sql = compile_sql(dialect)
    assert "on conflict" in sql, f"{dialect} 应使用 ON CONFLICT 语法：{sql}"
    assert "do update set" in sql, f"{dialect} 冲突时应更新已有行：{sql}"


def test_mysql_uses_on_duplicate_key_update() -> None:
    sql = compile_sql("mysql")
    assert "on duplicate key update" in sql, f"MySQL 必须使用 ON DUPLICATE KEY UPDATE：{sql}"
    # 这是关键的反向断言：MySQL 服务端不认识 ON CONFLICT，混用会在执行时才炸
    assert "on conflict" not in sql, f"MySQL 生成了不支持的 ON CONFLICT 子句：{sql}"
    assert "excluded" not in sql, f"MySQL 没有 excluded 伪表，应改用 VALUES(col)：{sql}"


def test_conflict_columns_are_never_updated() -> None:
    """冲突列不能被写进 SET —— 否则 MySQL 会更新成的行再度冲突，PG 会报语法错。"""
    for dialect in COMPILERS:
        sql = compile_sql(dialect, update_cols=UPDATABLE + ["ts", "source_id"])
        if dialect == "mysql":
            assert "ts=values(ts)" not in sql.replace(" ", ""), (
                f"{dialect} 把冲突列 ts 放进了更新列表：{sql}"
            )
        else:
            assert "(symbol, interval, ts, source_id) do update" in sql, (
                f"{dialect} 的冲突列列表不完整：{sql}"
            )


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql", "mysql"])
def test_do_nothing_never_updates(dialect: str) -> None:
    sql = compile_sql(dialect, do_nothing=True)
    if dialect == "mysql":
        assert sql.strip().startswith("insert ignore"), f"MySQL 应用 INSERT IGNORE：{sql}"
    else:
        assert "do nothing" in sql, f"{dialect} 应使用 ON CONFLICT DO NOTHING：{sql}"


def test_empty_update_cols_falls_back_to_do_nothing() -> None:
    """没有可更新列时必须退化成跳过，而不是退化成普通 INSERT（那会撞唯一约束）。"""
    for dialect in COMPILERS:
        sql = compile_sql(dialect, update_cols=[])
        if dialect == "mysql":
            assert "ignore" in sql
        else:
            assert "do nothing" in sql


def test_build_upsert_compiles_for_every_supported_dialect() -> None:
    """兼容性兜底：任何方言都不能在构造阶段抛异常。"""
    for dialect in COMPILERS:
        sql = compile_sql(dialect)
        assert "insert into candles" in sql, f"{dialect} 生成的表名不对：{sql}"


# ============================================================ 建表 DDL


@pytest.mark.parametrize(
    "dialect_name",
    ["sqlite", "postgresql", "mysql"],
    ids=lambda d: f"ddl-{d}",
)
def test_all_tables_ddl_compiles(dialect_name: str) -> None:
    """全部表结构在该方言下都能编译成 CREATE TABLE。

    这条用例挡的是「模型里混进了某方言不支持的定义」，典型几种：

    * MySQL 不允许 TEXT / JSON / BLOB 列带 DEFAULT
    * MySQL 要求 VARCHAR 必须有长度（否则索引建不出来）
    * SQLite 要求自增主键写 INTEGER，写 BIGINT 会变成普通列

    这些都不是 Python 层的错误，只有真正建表时才暴露 —— 而那时候用户已经
    填完连接信息、点完「完成安装」了，报错成本极高。所以放在编译阶段挡住。
    """
    from sqlalchemy.schema import CreateTable

    compiler = COMPILERS[dialect_name]
    failures: list[str] = []
    for table in Base.metadata.sorted_tables:
        try:
            sql = str(CreateTable(table).compile(dialect=compiler))
        except Exception as exc:  # noqa: BLE001 - 收集全部失败再一次性报出来
            failures.append(f"{table.name}: {exc}")
            continue
        assert f"CREATE TABLE {table.name}" in sql, f"{dialect_name} 建表语句缺表名：{sql}"
    assert not failures, f"{dialect_name} 下 {len(failures)} 张表无法建：" + "; ".join(failures[:5])


def test_indexed_string_columns_have_explicit_length() -> None:
    """MySQL 必须知道 VARCHAR 有多长，无长度的 String 会让索引建表失败。"""
    offenders: list[str] = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if col.type.__class__.__name__ != "String":
                continue
            if getattr(col.type, "length", None) is None:
                offenders.append(f"{table.name}.{col.name}")
    assert not offenders, (
        "以下 String 列没有指定长度，MySQL 会拒绝在其上建索引：" + ", ".join(offenders)
    )


# ============================================================ 连接串


@pytest.mark.parametrize(
    "raw,dialect,expected",
    [
        ("postgresql://u:p@h:5432/db", "postgresql", "postgresql+asyncpg://u:p@h:5432/db"),
        ("postgres://u:p@h:5432/db", "postgresql", "postgresql+asyncpg://u:p@h:5432/db"),
        ("postgresql+psycopg://u:p@h/db", "postgresql", "postgresql+asyncpg://u:p@h/db"),
        ("mysql://u:p@h:3306/db", "mysql", "mysql+aiomysql://u:p@h:3306/db"),
        ("mysql+pymysql://u:p@h/db", "mysql", "mysql+aiomysql://u:p@h/db"),
        ("mariadb://u:p@h/db", "mysql", "mysql+aiomysql://u:p@h/db"),
    ],
)
def test_normalize_to_async_always_picks_an_async_driver(raw: str, dialect: str, expected: str) -> None:
    """同步驱动（psycopg / pymysql）装到 asyncio 引擎上必然报错，这里强制纠正。"""
    assert detect_dialect(raw) == dialect
    assert normalize_to_async(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("postgresql+psycopg2://u:p@h/db", "postgresql+asyncpg://u:p@h/db"),
        ("mysql+mysqldb://u:p@h/db", "mysql+aiomysql://u:p@h/db"),
    ],
)
def test_sync_driver_suffix_is_auto_corrected(raw: str, expected: str) -> None:
    """每种方言只认一种异步驱动，用户在 scheme 里写什么都一律纠正过来。

    这里选择「纠正」而不是「报错」：一个人写下 postgresql+psycopg2，
    他的意图毫无疑问是连 PostgreSQL，拿异步驱动接上去就是他要的结果。
    真正的同步连接请求到 asyncio 引擎上才崩，那种崩排查起来要命。
    """
    assert normalize_to_async(raw) == expected


@pytest.mark.parametrize("raw", ["psycopg2://u:p@h/db", "pymysql://u:p@h/db"])
def test_bare_driver_scheme_gets_actionable_message(raw: str) -> None:
    """把驱动名当数据库类型写（`psycopg2://...`）必须提示改成 `postgresql://`。"""
    with pytest.raises(ValueError, match="同步驱动"):
        detect_dialect(raw)


@pytest.mark.parametrize("raw", ["", "mssql://u:p@h/db", "oracle://x", "没有前缀"])
def test_unknown_scheme_raises_readable_error(raw: str) -> None:
    with pytest.raises(ValueError):
        detect_dialect(raw)


def test_build_url_encodes_credential_special_characters() -> None:
    """密码里的 @ : / # 必须编码，否则会被当成主机/端口，报的错与真因完全无关。"""
    url = build_url(
        "postgresql", host="db.internal", port=5432, database="btc",
        username="btc_user", password="p@ss:w/rd#1",
    )
    # 形如 postgresql+asyncpg://<userinfo>@db.internal:5432/btc
    head, _, tail = url.partition("@db.internal:5432/btc")
    assert tail == ""
    assert head.startswith("postgresql+asyncpg://btc_user:")
    encoded = head.split("btc_user:", 1)[1]
    for special in ("@", ":", "/", "#"):
        assert special not in encoded, f"特殊字符 {special} 没有被编码：{url}"
    assert "%40" in encoded and "%2F" in encoded


def test_mask_url_hides_password() -> None:
    url = build_url("mysql", host="1.2.3.4", database="btc", username="root", password="sup3r-secret")
    masked = mask_url(url)
    assert "sup3r-secret" not in masked
    assert "1.2.3.4" in masked and "btc" in masked


@pytest.mark.parametrize("dialect,package", [("sqlite", "aiosqlite"),
                                            ("postgresql", "asyncpg"),
                                            ("mysql", "aiomysql")])
def test_install_hint_mentions_required_package(dialect: str, package: str) -> None:
    """缺驱动时给的必须是一条能直接复制的命令，而不是「请安装对应驱动」。"""
    hint = install_hint(dialect)
    assert package in hint
    assert hint.startswith("pip install ")
    # 驱动状态不一致时提示同样要给出（装没装是环境问题，提示本身必须对）
    assert isinstance(is_driver_installed(dialect), bool)
