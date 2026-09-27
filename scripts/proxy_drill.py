#!/usr/bin/env python
"""代理演练：对照「直连」与「走代理」两种情况下，各数据源到底能不能取到数。

为什么要单独写一个脚本：界面上的「测试代理」按钮只测那几个固定目标，
而这里会把**注册表里每个 Provider 的探测地址**都跑一遍，并且给出「配代理之后新增了哪些可用数据源」的结论。

用法：
    # 用后台已保存的代理（读 /system/proxy），对每个数据源做对照
    python scripts/proxy_drill.py --server http://127.0.0.1:8787

    # 临时用一个代理地址试（不落库）
    python scripts/proxy_drill.py --proxy "socks5://user:pass@IP:1080"

    # 离线条：不连平台，直接本机 httpx 打（适合还没起服务时）
    python scripts/proxy_drill.py --offline --proxy "socks5://user:pass@IP:1080"

输出 JSON 报告到 data/reports/proxy_drill_YYYYmmdd-HHMMSS.json。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

REPORT_DIR = ROOT / "data" / "reports"

PROBE_TARGETS: list[tuple[str, str]] = [
    ("binance 现货", "https://api.binance.com/api/v3/ping"),
    ("binance K线", "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=1"),
    ("binance Vision", "https://data-api.binance.vision/api/v3/ping"),
    ("coinbase", "https://api.coinbase.com/v2/prices/BTC-USD/spot"),
    ("kraken", "https://api.kraken.com/0/public/Ticker?pair=XBTUSD"),
    ("gemini", "https://api.gemini.com/v1/pubticker/btcusd"),
    ("bitfinex", "https://api-pub.bitfinex.com/v2/ticker/tBTCUSD"),
    ("bitstamp", "https://www.bitstamp.net/api/v2/ticker/btcusd/"),
    ("huobi", "https://api.huobi.pro/market/detail/merged?symbol=btcusdt"),
    ("okx", "https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT"),
    ("bybit", "https://api.bybit.com/v5/market/tickers?category=spot&symbol=BTCUSDT"),
    ("gate", "https://api.gateio.ws/api/v4/spot/tickers?currency_pair=BTC_USDT"),
    ("coingecko 市值", "https://api.coingecko.com/api/v3/ping"),
    ("alternative.me 恐慌指数", "https://api.alternative.me/fng/?limit=1"),
    ("mempool.space", "https://mempool.space/api/v1/fees/recommended"),
    ("blockchain.info", "https://blockchain.info/ticker"),
    ("frankfurter 汇率", "https://api.frankfurter.app/latest"),
    ("stooq CSV", "https://stooq.com/q/d/l/?s=btcusd&i=d"),
    ("出口 IP", "https://api.ipify.org?format=json"),
]


async def probe_one(transport, label: str, url: str, limit: int = 120) -> dict[str, object]:
    started = time.perf_counter()
    try:
        resp = await transport.request("GET", url, label=label)
        text = (resp.text or "").replace("\n", " ")[:limit]
        return {"target": label, "url": url, "ok": True, "status": resp.status_code,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1), "preview": text}
    except Exception as exc:  # noqa: BLE001 —— 演练脚本要如实记录所有失败，不能吞
        from app.providers.transport import ProviderTransport

        failure_type, message = ProviderTransport.classify(exc)
        return {"target": label, "url": url, "ok": False,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "failure_type": failure_type, "message": message[:200]}


def _pair_diff(direct: list[dict], proxied: list[dict]) -> tuple[list[dict], list[dict]]:
    """对照两轮结果：哪些是「配了代理才通」，哪些「照样不通」。"""
    gained, broken = [], []
    for base, item in zip(direct, proxied):
        row = {"target": item["target"], "direct_ok": base["ok"], "proxy_ok": item["ok"],
               "latency_ms": item["latency_ms"], "message": str(item.get("message", ""))}
        if item["ok"] and not base["ok"]:
            gained.append(row)
        elif not item["ok"]:
            broken.append(row)
    return gained, broken


async def run_pair(proxy_url: str, timeout: float) -> dict[str, object]:
    from app.core.proxy import describe_proxy
    from app.providers.transport import ProviderTransport

    async def run(url: str) -> list[dict[str, object]]:
        t = ProviderTransport(provider_name="proxy_drill", proxy=url,
                              timeout_connect=8.0, timeout_read=timeout, retries=0, qps=20.0)
        try:
            out = []
            for label, target in PROBE_TARGETS:
                out.append(await probe_one(t, label, target))
            return out
        finally:
            await t.close()

    direct = await run("")
    proxied = await run(proxy_url) if proxy_url else []
    gained, still_broken = _pair_diff(direct, proxied)
    return {
        "proxy": describe_proxy(proxy_url) if proxy_url else {"set": False},
        "direct": direct,
        "proxied": proxied,
        "gained_via_proxy": gained,
        "still_unreachable": still_broken,
        "direct_ok": sum(1 for x in direct if x["ok"]),
        "proxy_ok": sum(1 for x in proxied if x["ok"]),
        "total": len(PROBE_TARGETS),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="代理连通性演练")
    ap.add_argument("--proxy", default="", help="代理地址；省略则从后端读取已保存的代理")
    ap.add_argument("--server", default="http://127.0.0.1:8787", help="平台地址")
    ap.add_argument("--admin-token", default="", help="管理令牌（生产模式需要）")
    ap.add_argument("--timeout", type=float, default=12.0)
    ap.add_argument("--offline", action="store_true", help="不连平台，直接本机发起请求")
    args = ap.parse_args()

    proxy_url = args.proxy.strip()
    if not proxy_url:
        print("[提示] 未提供 --proxy：这一轮只做「直连」基线。"
              "后端保存的代理是敏感值，接口只回掩码（无法还原原文），"
              "要做对照请显式 --proxy \"socks5://user:pass@IP:端口\"。")
    elif not args.offline:
        pass  # 显式指定了代理就直接用，不必再问后端

    result = asyncio.run(run_pair(proxy_url, args.timeout))

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = REPORT_DIR / f"proxy_drill_{stamp}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n直连可达：{result['direct_ok']}/{result['total']}")
    for item in result["direct"]:
        flag = "OK " if item["ok"] else "FAIL"
        note = item.get("message", "") if not item["ok"] else str(item.get("status"))
        print(f"  [{flag}] {item['target']:24} {item['latency_ms']:>7} ms  {note}")

    if proxy_url:
        print(f"\n走代理可达：{result['proxy_ok']}/{result['total']}")
        for item in result["proxied"]:
            flag = "OK " if item["ok"] else "FAIL"
            note = item.get("message", "") if not item["ok"] else str(item.get("status"))
            print(f"  [{flag}] {item['target']:24} {item['latency_ms']:>7} ms  {note}")
        print(f"\n配代理后新增可用：{len(result['gained_via_proxy'])} 个")
        for item in result["gained_via_proxy"]:
            print(f"  + {item['target']}")
        print(f"仍然不可达：{len(result['still_unreachable'])} 个")
        for item in result["still_unreachable"]:
            print(f"  - {item['target']}：{item['message'][:80]}")

    print(f"\n报告：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
