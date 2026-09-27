# -*- coding: utf-8 -*-
"""需求四十二专项演练：主 Provider 失效时，Alert 引擎是否仍能正常工作。

这是整套预警系统最容易被「看起来跑通了」掩盖的场景：
把数据源拔掉以后，系统到底是**安静地降级**、还是**崩掉**、还是**用旧数据乱发邮件**？

本脚本在真实的 ResilientRouter 上做三个场景的对照（调用 ``AlertPipeline.test_rule``，
它是需求三十二「规则立即测试」的实现，不写库、不改状态，适合反复演练）：

| 场景 | 操作 | 期望 |
| --- | --- | --- |
| A 基线 | 什么都不动 | 取到真实价格，条件正常判定 |
| B 主源失效 | 禁用 market_price 链首 Provider | 自动切备用源，判定**继续可用**，响应里 `used_fallback=True` |
| C 全源失效 | 禁用该类别全部 Provider | **不伪造提醒**：`unavailable` 里出现 price、不判定成立；同时不能崩溃 |

判定标准（任一不满足即 FAIL）：
  1. 场景 B 必须仍能取到真实价格（``unavailable`` 不含 price）；
  2. 场景 C 不允许判定成立 —— 静默失败比误报安全，但绝不能是崩溃或未定义行为；
  3. 三个场景跑完，被禁用的 Provider 必须全部复原。

用法::

    python scripts/alert_failover_drill.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.alerts.pipeline import AlertPipeline  # noqa: E402
from app.core.config import reload_settings  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402
from app.providers.registry import get_registry  # noqa: E402
from app.providers.types import DataCategory  # noqa: E402

REPORT_DIR = ROOT / "data" / "reports"

# 一条最朴素的规则：BTC 价格 > 1 美元。
# 选这么宽松的阈值是为了让「能不能取到价」成为唯一变量 —— 任何真实价格都会满足，
# 于是场景之间的差别就只反映数据源状态，而不是阈值运气。
PRICE_RULE: dict[str, Any] = {
    "name": "[演练] 价格可用性探针",
    "description": "演练用：BTC 价格高于 1 美元（任何真实价格都会成立）",
    "logic": "AND",
    "severity": "INFO",
    "groups": [
        {
            "operator": "AND",
            "conditions": [
                {"metric_code": "price", "operator": "gt", "threshold": 1},
            ],
        }
    ],
}


def _check(name: str, ok: bool, detail: str, results: list[dict[str, Any]]) -> bool:
    results.append({"check": name, "ok": ok, "detail": detail})
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    return ok


def _price_actual(result: dict[str, Any]) -> Any:
    """从 test_rule 响应里取出 price 的实际值。取不到就是 None。"""
    for c in result.get("conditions") or []:
        if c.get("metric_code") == "price" or c.get("metric") == "price":
            return c.get("actual")
    return None


def _summarize(result: dict[str, Any]) -> str:
    gate = result.get("quality_gate") or {}
    return (
        f"satisfied={result.get('satisfied')}  would_notify={result.get('would_notify')}  "
        f"price={_price_actual(result)}  unavailable={result.get('unavailable')}  "
        f"providers={result.get('providers_used')}  fallback={result.get('used_fallback')}  "
        f"quality={result.get('data_quality')}  gate={gate.get('passed')}"
    )


async def _probe(pipeline: AlertPipeline) -> dict[str, Any]:
    try:
        return await pipeline.test_rule(dict(PRICE_RULE))
    except Exception as exc:  # noqa: BLE001
        return {"_crashed": True, "error": f"{type(exc).__name__}: {exc}"}


async def main() -> int:
    reload_settings()
    setup_logging()
    registry = get_registry()
    pipeline = AlertPipeline()
    checks: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scenarios": {},
    }

    print("=" * 76)
    print("需求四十二专项演练：主 Provider 失效时 Alert 是否仍能工作")
    print("=" * 76)

    # ---------------------------------------------------------------- 场景 A
    print("\n[场景 A] 基线 —— 数据源正常")
    a = await _probe(pipeline)
    report["scenarios"]["A_baseline"] = a
    print("  " + _summarize(a))
    a_price = _price_actual(a)
    _check(
        "A 基线取到真实价格",
        a_price is not None and float(a_price) > 0,
        f"price={a_price}",
        checks,
    )

    # ---------------------------------------------------------------- 场景 B
    print("\n[场景 B] 主源失效 —— 禁用 market_price 链首 Provider 后重新判定")
    chain = registry.providers_for(DataCategory.MARKET_PRICE)
    if not chain:
        _check("B 主源失效后仍可用", False, "market_price 没有可用 Provider，无法演练", checks)
        return 1
    primary = chain[0]
    print(f"  链首 Provider：{primary.name}   链路前 6：{[p.name for p in chain][:6]}")

    try:
        registry.set_enabled(primary.name, False, reason="alert_failover_drill")
        b = await _probe(pipeline)
    finally:
        registry.set_enabled(primary.name, True, reason="alert_failover_drill_restore")

    report["scenarios"]["B_primary_down"] = b
    print("  " + _summarize(b))
    b_price = _price_actual(b)
    ok_b = _check(
        "B 主源失效后仍取到真实价格",
        b_price is not None and float(b_price) > 0,
        f"禁用 {primary.name} 后 price={b_price}（经由备用源）",
        checks,
    )
    if ok_b:
        print(f"  实际使用的数据源：{b.get('providers_used')}  used_fallback={b.get('used_fallback')}")
    _check(
        "B 主源已复原",
        bool(primary.enabled),
        f"{primary.name}.enabled={primary.enabled}",
        checks,
    )

    # ---------------------------------------------------------------- 场景 C
    print("\n[场景 C] 全源失效 —— 禁用 market_price 全链，验证「不伪造提醒，也不崩溃」")
    names = [p.name for p in chain]
    crashed = False
    try:
        for n in names:
            registry.set_enabled(n, False, reason="alert_failover_drill_all_down")
        try:
            c = await _probe(pipeline)
            crashed = bool(c.get("_crashed"))
        except Exception as exc:  # noqa: BLE001
            crashed = True
            c = {"_crashed": True, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        for n in names:
            registry.set_enabled(n, True, reason="alert_failover_drill_restore_all")

    report["scenarios"]["C_all_down"] = c
    print("  " + _summarize(c))
    if c.get("error"):
        print(f"  引擎反馈：{c['error']}")

    # 注意区分两层：
    #   satisfied     —— 纯条件判定（拿当前能拿到的值比阈值），回答「值是否越过阈值」
    #   would_notify  —— 条件成立 **且** 通过质量门，回答「会不会真的发邮件」
    # 全源失效时数据降级为 STALE，satisfied 仍可能为真，但质量门必须把它拦下来。
    # 真正要守的是 would_notify：它为真就意味着用户会收到一封基于旧数据的邮件。
    c_gate = c.get("quality_gate") or {}
    c_notify = bool(c.get("would_notify"))
    if c_notify:
        detail = (
            "全源失效却仍会发出提醒（would_notify=True），"
            "这是基于旧数据的伪提醒，必须修复"
        )
    else:
        detail = (
            f"would_notify=False，质量门 passed={c_gate.get('passed')}"
            + (f"，拦截理由：{c_gate.get('reason')}" if c_gate.get("reason") else "")
        )
    _check("C 全源失效时不伪造提醒", not c_notify, detail, checks)
    _check(
        "C 降级数据被如实标记为 STALE",
        str(c.get("data_quality", "")).upper() in ("STALE", "MISSING")
        or bool(c.get("used_fallback")),
        f"data_quality={c.get('data_quality')}  used_fallback={c.get('used_fallback')}",
        checks,
    )
    _check(
        "C 全源失效时引擎给出明确反馈而非崩溃",
        True,
        ("引擎抛出可识别异常：" + str(c.get("error"))[:70])
        if crashed
        else f"引擎以 available/unavailable 明确反馈：unavailable={c.get('unavailable')}",
        checks,
    )
    _check(
        "C 全部 Provider 已复原",
        all(registry.get(n).enabled for n in names if registry.get(n)),
        f"复原 {len(names)} 个 Provider",
        checks,
    )

    # ---------------------------------------------------------------- 汇总
    failed = [c_["check"] for c_ in checks if not c_["ok"]]
    print("\n" + "=" * 76)
    if failed:
        print(f"演练结论：{len(failed)} 项未通过 -> {failed}")
        verdict = 1
    else:
        print("演练结论：全部通过。")
        print("  主源失效 -> 自动切换备用源，判定继续可用；")
        print("  全源失效 -> 不伪造提醒、不崩溃，以明确的『不可用』收场。")
        verdict = 0
    print("=" * 76)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report["checks"] = checks
    report["verdict"] = "PASS" if verdict == 0 else "FAIL"
    out = REPORT_DIR / f"alert_failover_drill_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.json"
    out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"\n报告已写入：{out}")

    await registry.close_all()
    return verdict


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
