# -*- coding: utf-8 -*-
"""跨方言 upsert 构造器。

为什么必须有这个模块
--------------------
「重复跑一次的写入必须幂等」是本平台的硬要求（采集任务会被反复调度），
而三种数据库的 upsert 语法**互不相同**：

* SQLite / PostgreSQL :: ``INSERT ... ON CONFLICT (cols) DO UPDATE SET ...``
* MySQL               :: ``INSERT ... ON DUPLICATE KEY UPDATE ...``

MySQL 既没有 ``ON CONFLICT``，也没有 ``excluded`` 伪表（它用的是 ``VALUES(col)``）。
早前的写法是先 `dialect_insert()` 拿一个方言 Insert 对象，再在外面统一调
``on_conflict_do_update()`` —— 这在 MySQL 上根本走不通：不是编译期报错，
就是生成一句 MySQL 不认识的 SQL 语句。

所以这里一步到位：给方言、给 rows、给冲突列与待更新列，返回**已经完整构造好的**
statement。调用方不再需要知道方言差异。

离线可测
--------
构造 statement 不需要真的连数据库 —— 用 SQLAlchemy 的方言对象直接 compile 即可
（见 ``tests/test_upsert_dialects.py``）。没有 PostgreSQL / MySQL 实例的环境也能
保证「语法对不对」这件事被守住。
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from sqlalchemy.sql.dml import Insert


def build_upsert(
    dialect_name: str,
    model: Any,
    rows: Sequence[dict[str, Any]],
    index_elements: Sequence[str],
    update_cols: Sequence[str],
    *,
    do_nothing: bool = False,
) -> Insert:
    """构造一条方言无关的 upsert 语句。

    Args:
        dialect_name: ``sqlite`` / ``postgresql`` / ``mysql``
        model: ORM 模型类
        rows: 待写入的行
        index_elements: 判定冲突的列（必须与唯一约束/主键一致）
        update_cols: 冲突时要覆盖的列（不能包含冲突列本身）
        do_nothing: True 表示「冲突即跳过」，不更新已有行
    """
    index_elements = list(index_elements)
    update_cols = [c for c in update_cols if c not in set(index_elements)]

    if dialect_name == "mysql":
        from sqlalchemy.dialects.mysql import insert as mysql_insert

        stmt = mysql_insert(model).values(list(rows))
        if do_nothing or not update_cols:
            # MySQL 没有 DO NOTHING，等价写法是 INSERT IGNORE：
            # 撞唯一键的那一行被静默丢弃，其余照常写入，语义一致。
            return stmt.prefix_with("IGNORE")
        return stmt.on_duplicate_key_update(
            **{name: getattr(stmt.inserted, name) for name in update_cols}
        )

    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_ctor
    else:
        from sqlalchemy.dialects.sqlite import insert as dialect_ctor

    stmt = dialect_ctor(model).values(list(rows))
    if do_nothing or not update_cols:
        return stmt.on_conflict_do_nothing(index_elements=index_elements)
    return stmt.on_conflict_do_update(
        index_elements=index_elements,
        set_={name: getattr(stmt.excluded, name) for name in update_cols},
    )


def upsert_for_session(
    session: Any,
    model: Any,
    rows: Iterable[dict[str, Any]],
    index_elements: Sequence[str],
    update_cols: Sequence[str],
    *,
    do_nothing: bool = False,
    default_dialect: str = "sqlite",
) -> Insert:
    """按 session 绑定的方言构造 upsert。

    `session.get_bind()` 偶尔会返回 None（例如刚建好还没真正用过的 session），
    这时退回 `default_dialect` 而不是抛异常 —— upsert 是写入主链路，不该因为
    拿不到 bind 就让整个采集任务失败。
    """
    rows = list(rows)
    dialect_name = default_dialect
    try:
        bind = session.get_bind()
        if bind is not None and getattr(bind, "dialect", None) is not None:
            dialect_name = bind.dialect.name
    except Exception:  # noqa: BLE001 - bind 取不到时退回默认方言，由调用方继续
        pass
    return build_upsert(
        dialect_name, model, rows, index_elements, update_cols, do_nothing=do_nothing
    )
