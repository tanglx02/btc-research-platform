# -*- coding: utf-8 -*-
"""测试基础设施。

关键点：
1. 测试必须使用**独立的临时数据库**，绝不能动到 `data/btc.db`（那是有真实历史数据的库）；
2. 测试不访问网络 —— Provider 全部使用本地 Fake 实现；
3. Mock Provider 与生产 Provider 严格隔离（生产注册表默认跳过 `mock_*` 模块）；
4. 所有异步测试通过 pytest-asyncio 运行。
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"

for p in (str(ROOT), str(BACKEND), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

# 必须在 import app.* 之前设置，避免加载到真实数据库
os.environ["APP_ENV"] = "test"
os.environ["SCHEDULER_ENABLED"] = "false"
# 未完成「首次启动安装向导」时，/api/v1/** 会被 503 闸门统一挡下。
# 测试必须显式声明「已安装」，否则整套 API 契约用例拿到的都是 setup_required，
# 而这个报错跟被测接口毫无关系，排查时会浪费很多时间。
os.environ["SETUP_COMPLETED"] = "true"
os.environ.setdefault("LOG_LEVEL", "WARNING")

_TMP_DB_DIR = Path(os.environ.get("BTC_TEST_TMP", str(ROOT / "data" / "_test_tmp")))
_TMP_DB_DIR.mkdir(parents=True, exist_ok=True)
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{(_TMP_DB_DIR / 'test_btc.db').as_posix()}"


@pytest.fixture(scope="session")
def event_loop() -> Iterator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


def _reset_db_state(db_file: Path) -> None:
    """切换到一个全新的测试库文件，并重置引擎/配置单例。

    三个单例必须一起重置，否则「看起来隔离，实则共用一个库」：
      * `get_settings()` 有 lru_cache，DATABASE_URL 改了也不生效；
      * `app.db.base._engine` 是模块级单例，指向旧库；
      * `app.db.base._session_factory` 绑定了旧 engine。
    """
    from app.core.config import get_settings
    from app.db import base as db_base

    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    get_settings.cache_clear()
    asyncio.run(db_base.dispose_engine())


def _remove_db_files(db_file: Path) -> None:
    """清理一个测试库及其 SQLite 伴生文件（-wal / -shm）。

    两处写法都是刻意保守的：

    1. **先 exists() 再删** —— 某些运行环境会拦下 ``Path.unlink`` 改走回收站，
       对不存在的文件照样搬一次然后报错（Windows 上表现为 ``SHFileOperationW 失败: 0x2``），
       此时 ``missing_ok=True`` 并不起作用，因为它是在 wrapper 之后才判定。
    2. **捕获 OSError** —— SQLite 句柄没释放时删不掉是常态，
       清理失败绝不该让用例被判为失败（它不影响任何断言）。
    """
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(db_file) + suffix)
        if not path.exists():
            continue
        try:
            path.unlink()
        except OSError:
            pass  # 删不掉就留给下一次运行，测试判定不能挂在清理上


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """清理陈旧的临时测试库。

    每个用例一个独立库，一旦某次 teardown 失败（Windows 上 SQLite 句柄没释放时
    很常见），文件就会留在 data/_test_tmp 里越积越多。这里按「最后修改时间超过
    一天」来做兜底清扫，既不误删本次运行可能还在用的文件，也不会让目录无限膨胀。
    """
    import time

    deadline = time.time() - 86400
    for path in _TMP_DB_DIR.glob("*.db*"):
        if not path.exists():
            continue
        try:
            if path.stat().st_mtime < deadline:
                path.unlink()
        except OSError:
            pass  # 被占用就下一次再清，清理失败不该让测试判定为失败


@pytest.fixture(autouse=True)
def isolated_db(request: pytest.FixtureRequest) -> Iterator[Path]:
    """每个用例一个独立数据库文件 —— 用例之间绝不串数据，也绝不碰 data/btc.db。"""
    db_file = _TMP_DB_DIR / f"test_{uuid.uuid4().hex}.db"
    _remove_db_files(db_file)
    _reset_db_state(db_file)
    yield db_file
    if "no_db_cleanup" not in (getattr(request, "fixturenames", []) or []):
        _reset_db_state(_TMP_DB_DIR / "test_btc.db")
        _remove_db_files(db_file)


@pytest.fixture()
def tmp_db_path(isolated_db: Path) -> Path:
    return isolated_db


@pytest.fixture()
async def db_ready(tmp_db_path: Path) -> Any:
    """初始化临时库并返回 factory。"""
    from app.db.migrate import init_db
    from app.db.base import get_session_factory

    await init_db()
    return get_session_factory()


@pytest.fixture()
def make_price_provider_factory():
    """返回一个可构造 Fake Provider 的工厂，用于 Failover 链路测试。"""
    from app.providers.base import DataProvider
    from app.providers.types import DataCategory, FetchTrace, ProviderResult, QualityStatus

    class FakePriceProvider(DataProvider):
        """可控失败的价格 Provider（仅测试用，不参与生产扫描）。"""

        def __init__(self, name: str, price: float = 60000.0, failures: int = 0,
                     failure_type: str = "server_error", priority: int = 100, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.name = name
            self.display_name = f"Fake {name}"
            self.categories = {DataCategory.MARKET_PRICE}
            self.capabilities = {"price"}
            self.default_priority = priority
            self._price = price
            self._remaining_failures = failures
            self._failure_type = failure_type
            self.calls = 0

        async def fetch_price(self, symbol: str = "BTC") -> ProviderResult:
            self.calls += 1
            if self._remaining_failures > 0:
                self._remaining_failures -= 1
                from app.core.errors import ProviderError

                raise ProviderError(
                    f"fake failure ({self._failure_type})",
                    provider=self.name,
                    failure_type=self._failure_type,
                )
            return ProviderResult(
                data=float(self._price),
                provider=self.name,
                category=DataCategory.MARKET_PRICE,
                quality=QualityStatus.SINGLE_SOURCE,
                trace=FetchTrace(provider=self.name, endpoint="fake://price", http_status=200),
            )

    return FakePriceProvider
