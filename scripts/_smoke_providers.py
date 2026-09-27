# -*- coding: utf-8 -*-
"""临时验证脚本：检查 Provider 注册与自动故障切换是否真的能拿到真实数据。"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.config import reload_settings
from app.core.logging import setup_logging
from app.providers.registry import get_registry
from app.providers.router import get_router
from app.providers.types import DataCategory


async def main() -> None:
    reload_settings()
    setup_logging()
    reg = get_registry()
    print(f"[registry] providers = {len(reg.all())}")
    for c in DataCategory:
        chain = reg.providers_for(c)
        print(f"  {c.value:<24} {len(chain):>2} -> {[p.name for p in chain][:6]}")

    router = get_router()
    print("\n[fetch] market_price with cross validation ...")
    res = await router.fetch_validated(DataCategory.MARKET_PRICE)
    print(f"  price = {res.data}  provider = {res.provider}  quality = {res.quality.value}  conf={res.confidence:.2f}")
    print(f"  attempts = {[(a.provider, a.ok, a.failure_type) for a in res.attempts]}")
    print(f"  cross = {res.cross_validation}")


if __name__ == "__main__":
    asyncio.run(main())
