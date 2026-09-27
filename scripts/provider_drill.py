# -*- coding: utf-8 -*-
"""Provider 全源演练脚本。

用途（对应 docs/27-测试与验收清单.md 的「数据源演练」章节）：

1. **全源探测**：对注册表里的每一个 Provider 做一次真实 health_check，
   记录是否可用、是否配置、延迟、失败类型。
2. **全类别取数**：对 14 个数据类别逐个走 ResilientRouter.fetch，
   记录实际命中的 Provider、质量状态、是否发生 fallback。
3. **主源失效演练**：把每个类别的主源临时禁用，重新取数，
   验证「主 Provider 失效后 Alert / 取数是否仍能工作」。
4. **恢复**：演练结束后原样恢复被禁用的 Provider，避免污染运行态。

输出：控制台摘要 + ``data/reports/provider_drill_<时间戳>.json``

用法::

    python scripts/provider_drill.py                # 全量演练
    python scripts/provider_drill.py --no-failover  # 只探测，不做主源失效演练
    python scripts/provider_drill.py --categories market_price,ohlcv
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import reload_settings  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402
from app.providers.registry import get_registry  # noqa: E402
from app.providers.router import get_router  # noqa: E402
from app.providers.types import DataCategory  # noqa: E402

REPORT_DIR = ROOT / "data" / "reports"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _short(value: Any, limit: int = 160) -> Any:
    """把取到的原始数据压成可入报告的小摘要，避免报告文件过大。"""
    if isinstance(value, (int, float, str, bool)) or value is None:
        text = str(value)
        return text if len(text) <= limit else text[: limit - 3] + "..."
    if isinstance(value, dict):
        return {k: _short(v, 60) for k, v in list(value.items())[:8]}
    if isinstance(value, list):
        head = [_short(v, 60) for v in value[:3]]
        return {"_type": "list", "_len": len(value), "_head": head}
    return {"_type": type(value).__name__}


async def probe_all_providers(router) -> list[dict[str, Any]]:
    """阶段 1：逐个 Provider 做真实健康探测。"""
    results = await router.probe_all()
    return sorted(results, key=lambda r: (not r["ok"], r["provider"]))


async def fetch_all_categories(router, categories: list[DataCategory]) -> list[dict[str, Any]]:
    """阶段 2：每个类别走一次主/备链路取数。"""
    out: list[dict[str, Any]] = []
    for cat in categories:
        chain = router.registry.providers_for(cat)
        started = time.perf_counter()
        entry: dict[str, Any] = {
            "category": cat.value,
            "chain": [p.name for p in chain],
            "chain_size": len(chain),
        }
        try:
            res = await router.fetch(cat)
            entry.update(
                ok=True,
                provider=res.provider,
                primary_provider=res.primary_provider,
                quality=res.quality.value,
                confidence=round(res.confidence, 3),
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
                fallback=bool(res.used_fallback),
                failover_reason=res.failover_reason,
                attempts=[
                    {"provider": a.provider, "ok": a.ok, "failure_type": a.failure_type}
                    for a in res.attempts
                ],
                sample=_short(res.data),
            )
        except Exception as exc:  # noqa: BLE001
            entry.update(
                ok=False,
                error=type(exc).__name__,
                message=str(exc)[:300],
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
                attempts=[],
            )
        out.append(entry)
    return out


async def failover_drill(router, categories: list[DataCategory]) -> list[dict[str, Any]]:
    """阶段 3：主源失效演练。

    对每个类别临时禁用链首 Provider，重新取数，验证能否自动切到备用源。
    无论成功失败都会在 finally 中恢复原状态。
    """
    out: list[dict[str, Any]] = []
    for cat in categories:
        chain = router.registry.providers_for(cat)
        if not chain:
            out.append({"category": cat.value, "skipped": "该类别没有可用 Provider 链"})
            continue
        primary = chain[0]
        was_enabled = primary.enabled
        entry: dict[str, Any] = {
            "category": cat.value,
            "disabled_primary": primary.name,
            "chain": [p.name for p in chain],
        }
        try:
            router.registry.set_enabled(primary.name, False, reason="provider_drill")
            started = time.perf_counter()
            try:
                res = await router.fetch(cat)
                entry.update(
                    survived=True,
                    new_provider=res.provider,
                    switched=bool(res.provider != primary.name),
                    quality=res.quality.value,
                    confidence=round(res.confidence, 3),
                    latency_ms=round((time.perf_counter() - started) * 1000, 1),
                    attempts=[
                        {"provider": a.provider, "ok": a.ok, "failure_type": a.failure_type}
                        for a in res.attempts
                    ],
                )
            except Exception as exc:  # noqa: BLE001
                entry.update(
                    survived=False,
                    error=type(exc).__name__,
                    message=str(exc)[:300],
                    latency_ms=round((time.perf_counter() - started) * 1000, 1),
                )
        finally:
            router.registry.set_enabled(primary.name, was_enabled, reason="provider_drill_restore")
        out.append(entry)
    return out


def _print_summary(report: dict[str, Any]) -> None:
    print("\n" + "=" * 78)
    print(f"Provider 全源演练报告   {report['generated_at']}")
    print("=" * 78)

    probes = report["providers"]
    ok = [p for p in probes if p["ok"]]
    print(f"\n[1/3] Provider 探测：{len(ok)}/{len(probes)} 可用")
    for p in probes:
        flag = "OK  " if p["ok"] else "FAIL"
        cfg = "configured" if p["configured"] else "NOT-CONFIGURED"
        extra = "" if p["ok"] else f"  <- {p['failure_type']}: {p['message'][:60]}"
        print(f"  {flag} {p['provider']:<22} {p['latency_ms']:>8.1f}ms  {cfg:<15}{extra}")

    print("\n[2/3] 全类别取数")
    for c in report["categories"]:
        if c.get("ok"):
            fb = " (FALLBACK)" if c.get("fallback") else ""
            print(
                f"  OK   {c['category']:<24} via {c['provider']:<20} "
                f"{c['quality']:<15} conf={c['confidence']:<5}{fb}"
            )
        else:
            print(f"  FAIL {c['category']:<24} {c.get('error')}: {str(c.get('message'))[:70]}")

    print("\n[3/3] 主源失效演练（禁用链首 Provider 后重新取数）")
    if not report["failover"]:
        print("  （已跳过）")
    for f in report["failover"]:
        if f.get("skipped"):
            print(f"  SKIP {f['category']:<24} {f['skipped']}")
        elif f.get("survived"):
            sw = "已切换" if f.get("switched") else "未切换(可能熔断跳过)"
            print(
                f"  PASS {f['category']:<24} 禁用 {f['disabled_primary']:<18} "
                f"-> {f['new_provider']:<20} {sw}"
            )
        else:
            print(
                f"  FAIL {f['category']:<24} 禁用 {f['disabled_primary']:<18} "
                f"-> {f.get('error')}: {str(f.get('message'))[:60]}"
            )

    s = report["summary"]
    print("\n" + "-" * 78)
    print(
        f"Provider 可用 {s['providers_ok']}/{s['providers_total']} | "
        f"类别取数成功 {s['categories_ok']}/{s['categories_total']} | "
        f"主源失效存活 {s['failover_survived']}/{s['failover_total']}"
    )
    print("-" * 78)


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Provider 全源演练")
    parser.add_argument("--no-failover", action="store_true", help="跳过主源失效演练")
    parser.add_argument("--categories", default="", help="只演练指定类别，逗号分隔")
    args = parser.parse_args(argv)

    reload_settings()
    setup_logging()

    if args.categories:
        wanted = {c.strip() for c in args.categories.split(",") if c.strip()}
        categories = [c for c in DataCategory if c.value in wanted]
        unknown = wanted - {c.value for c in categories}
        if unknown:
            print(f"[warn] 未知类别已忽略：{sorted(unknown)}")
    else:
        categories = list(DataCategory)

    router = get_router()
    registry = router.registry
    print(f"[registry] 共 {len(registry.all())} 个 Provider，演练 {len(categories)} 个类别")

    print("\n[1/3] 逐个探测 Provider（真实网络请求，请稍候）...")
    providers = await probe_all_providers(router)

    print("[2/3] 全类别主/备链路取数...")
    cats = await fetch_all_categories(router, categories)

    print("[3/3] 主源失效演练..." if not args.no_failover else "[3/3] 已跳过主源失效演练")
    drill = [] if args.no_failover else await failover_drill(router, categories)

    report = {
        "generated_at": _now(),
        "providers": providers,
        "categories": cats,
        "failover": drill,
        "summary": {
            "providers_total": len(providers),
            "providers_ok": sum(1 for p in providers if p["ok"]),
            "categories_total": len(cats),
            "categories_ok": sum(1 for c in cats if c.get("ok")),
            "categories_fallback": sum(1 for c in cats if c.get("fallback")),
            "failover_total": len(drill),
            "failover_survived": sum(1 for f in drill if f.get("survived")),
            "failover_switched": sum(1 for f in drill if f.get("switched")),
        },
    }

    _print_summary(report)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = REPORT_DIR / f"provider_drill_{stamp}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已写入：{path}")

    await registry.close_all()
    # 只要类别取数没有全灭，就认为演练通过；Provider 本身受网络/限流影响不作硬断言。
    return 0 if report["summary"]["categories_ok"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
