# -*- coding: utf-8 -*-
"""btcctl —— 统一运维命令行工具（Windows / Linux 通用）。

用法：
    python scripts/btcctl.py backfill      [--start 2013-01-01] [--end 2025-12-31] [--interval 1d] [--reset]
    python scripts/btcctl.py incremental   [--interval 1d]
    python scripts/btcctl.py tick
    python scripts/btcctl.py gap-repair    [--interval 1d]
    python scripts/btcctl.py quality       [--interval 1d]
    python scripts/btcctl.py collect-all
    python scripts/btcctl.py init-db
    python scripts/btcctl.py stats
    python scripts/btcctl.py coverage      [--interval 1d]
    python scripts/btcctl.py health
    python scripts/btcctl.py probe
    python scripts/btcctl.py backup        [--label weekly]
    python scripts/btcctl.py restore       --file backups/xxx.tar.gz
    python scripts/btcctl.py replay        --date 2021-11-10
    python scripts/btcctl.py backtest      --strategy dca_valuation --start 2018-01-01

设计约束：
    * 所有命令都不允许产生假数据；取不到就是取不到，命令会明确报告覆盖范围。
    * 长时间任务支持 Ctrl+C 中断，中断处会写入 checkpoint，下次继续。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
# 直接以 `python scripts/btcctl.py` 方式启动时，sys.path[0] 是 scripts/，
# 这会让 `from scripts.backup import ...` 找不到父包 —— 必须显式把根目录加进来。
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(BACKEND))


def _ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def _warn(msg: str) -> None:
    print(f"  [WARN] {msg}")


def _fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")


def _info(msg: str) -> None:
    print(f"  [..]   {msg}")


# --------------------------------------------------------------------- 命令


async def cmd_init_db(_args: argparse.Namespace) -> int:
    from app.core.logging import setup_logging
    from app.db.migrate import enable_timescaledb, init_db, table_stats
    from app.db.seed import seed_all

    setup_logging()
    await init_db()
    await enable_timescaledb()
    counts = await seed_all()
    _ok(f"数据库已初始化，种子数据：{counts}")
    stats = await table_stats()
    for name, count in sorted(stats.items()):
        if count:
            print(f"        {name:<28} {count}")
    return 0


async def cmd_backfill(args: argparse.Namespace) -> int:
    from app.collectors.market_collector import MarketCollector
    from app.core.logging import setup_logging

    setup_logging()
    collector = MarketCollector(interval=args.interval)
    _info(f"开始回填 {args.interval} 历史数据，起点 {args.start}，截止 {args.end or '今天'}（断点续传，可随时中断）")
    result = await collector.backfill(
        start_date=args.start, end_date=args.end, reset=bool(args.reset)
    )
    if result.get("ok"):
        _ok(f"回填完成：新增 {result.get('rows', 0)} 根，覆盖至 {result.get('until')}")
        uncovered = result.get("uncovered_windows") or []
        if uncovered:
            _warn(f"以下时间窗口所有数据源均无数据（已如实记录，未用假数据填补）：")
            for w in uncovered[:20]:
                print(f"          {w}")
            if len(uncovered) > 20:
                print(f"          …还有 {len(uncovered) - 20} 个窗口")
    else:
        _warn(
            f"回填中断于 {result.get('stopped_at')}，已写入 {result.get('rows', 0)} 根，"
            f"原因：{result.get('reason') or result.get('error')}"
        )
        _info("断点已保存，重新执行本命令即可继续。")
    await _print_coverage(args.interval)
    return 0 if result.get("ok") else 2


async def cmd_incremental(args: argparse.Namespace) -> int:
    from app.collectors.market_collector import MarketCollector
    from app.core.logging import setup_logging

    setup_logging()
    result = await MarketCollector(interval=args.interval).sync_incremental()
    _ok(f"增量同步完成：{result}")
    return 0


async def cmd_tick(_args: argparse.Namespace) -> int:
    from app.collectors.market_collector import MarketCollector
    from app.core.logging import setup_logging

    setup_logging()
    result = await MarketCollector().collect_tick()
    if result.get("ok"):
        _ok(
            f"最新价 {result['price']} 来自 {result['provider']}"
            f"（质量 {result['quality']}，置信度 {result['confidence']}）"
        )
    else:
        _fail(f"采集失败：{result.get('reason')}（不会写入任何假数据）")
        return 2
    return 0


async def cmd_gap_repair(args: argparse.Namespace) -> int:
    from app.collectors.market_collector import GapRepairCollector
    from app.core.logging import setup_logging

    setup_logging()
    result = await GapRepairCollector(interval=args.interval).repair()
    if result.get("repaired", 0) or not result.get("failed"):
        _ok(f"补洞完成：{result}")
    else:
        _warn(f"部分缺口未能补齐：{result}")
    return 0


async def cmd_quality(args: argparse.Namespace) -> int:
    from app.collectors.market_collector import DataQualityScanner
    from app.core.logging import setup_logging

    setup_logging()
    result = await DataQualityScanner().scan(interval=args.interval)
    if not result.get("days"):
        _warn("本地还没有任何历史数据，请先执行 backfill。")
        return 2
    from app.core.timeutils import fmt_local

    _ok(f"完整性 {result['completeness']:.2%}，共 {result['days']} 根，缺失 {result['missing']} 根")
    _info(f"覆盖区间 {fmt_local(result['start_ts'])} ~ {fmt_local(result['end_ts'])}")
    return 0


async def cmd_collect_all(_args: argparse.Namespace) -> int:
    """跑一遍所有非行情类采集（链上/衍生品/情绪/宏观/ETF）。"""
    from app.collectors.data_collectors import (
        DerivativesCollector,
        EtfCollector,
        MacroCollector,
        OnchainCollector,
        SentimentCollector,
    )
    from app.core.logging import setup_logging

    setup_logging()
    tasks = [
        ("衍生品（资金费率/未平仓）", DerivativesCollector()),
        ("链上指标", OnchainCollector()),
        ("市场情绪（恐慌贪婪）", SentimentCollector()),
        ("宏观指标", MacroCollector()),
        ("ETF 资金流", EtfCollector()),
    ]
    for label, collector in tasks:
        try:
            result = await collector.run()
            status = result.get("result", result)
            if isinstance(status, dict) and status.get("ok") is False:
                _warn(f"{label}：{status.get('reason', status)}")
            else:
                _ok(f"{label}：{status}")
        except Exception as exc:  # noqa: BLE001
            _fail(f"{label} 异常：{str(exc)[:120]}")
    return 0


async def cmd_stats(_args: argparse.Namespace) -> int:
    from app.core.logging import setup_logging
    from app.db.migrate import table_stats

    setup_logging()
    stats = await table_stats()
    for name, count in sorted(stats.items()):
        print(f"  {name:<28} {count}")
    return 0


async def _print_coverage(interval: str) -> None:
    from app.core.timeutils import fmt_local
    from app.db.base import get_session_factory
    from app.db.repo import candle_coverage

    factory = get_session_factory()
    async with factory() as session:
        cov = await candle_coverage(session, "BTC", interval)
    if cov.get("count"):
        _info(
            f"本地 {interval} 数据：{cov['count']} 根，{fmt_local(cov['start_ts'])} ~ {fmt_local(cov['end_ts'])}"
        )
    else:
        _warn(f"本地暂无 {interval} 数据")


async def cmd_coverage(args: argparse.Namespace) -> int:
    from app.core.logging import setup_logging

    setup_logging()
    await _print_coverage(args.interval)
    return 0


async def cmd_health(_args: argparse.Namespace) -> int:
    from app.core.logging import setup_logging
    from app.services.health_service import HealthService

    setup_logging()
    board = await HealthService().dashboard()
    s = board["summary"]
    print(f"  数据源总数 {s['total_providers']}  健康 {s['healthy']}  "
          f"未配置 {s['not_configured']}  已停用 {s['disabled']}  "
          f"今日失败 {s['failures_today']}  健康率 {s['health_ratio']:.0%}")
    print(f"  {'Provider':<26}{'状态':<16}{'延迟ms':>9}{'评分':>7}  说明")
    for p in board["providers"]:
        note = p.get("last_error") or p.get("last_failure_type") or ""
        print(f"  {p['provider']:<26}{p['status']:<16}{p['latency_ms']:>9.0f}{p['score']:>7.0f}  {note[:40]}")
    return 0


async def cmd_probe(_args: argparse.Namespace) -> int:
    from app.core.logging import setup_logging
    from app.providers.router import get_router

    setup_logging()
    results = await get_router().probe_all()
    for item in results:
        status = "OK  " if item.get("ok") else "FAIL"
        print(f"  {status} {item.get('provider', ''):<26}{item.get('latency_ms', 0):>8.0f}ms  "
              f"{item.get('message', '')[:60]}")
    return 0


async def cmd_replay(args: argparse.Namespace) -> int:
    from app.core.logging import setup_logging
    from app.services.replay_service import ReplayService

    setup_logging()
    result = await ReplayService().replay(args.date, perspective=args.perspective)
    if not result.get("available"):
        _fail(f"回放失败：{result.get('reason')}")
        return 2
    _ok(f"{args.date} 回放（视角：{args.perspective}）")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str)[:4000])
    return 0


async def cmd_backtest(args: argparse.Namespace) -> int:
    from app.core.logging import setup_logging
    from app.services.backtest_service import BacktestConfig, BacktestEngine

    setup_logging()
    cfg = BacktestConfig(
        strategy_code=args.strategy,
        start_date=args.start,
        end_date=args.end or "",
        contribution_frequency=args.freq,
        monthly_contribution=args.amount if args.freq == "monthly" else 0.0,
        weekly_contribution=args.amount if args.freq == "weekly" else 0.0,
        initial_capital=args.capital,
        validation_mode="oos" if args.oos else "insample",
        name=args.name,
    )
    result = await BacktestEngine().run(cfg)
    if not result.get("ok"):
        _fail(f"回测失败：{result.get('message')}")
        return 2
    print(f"  策略 {args.strategy}  频率 {args.freq}  每期 {args.amount}")
    if result.get("start_date"):
        print(f"  区间 {result.get('start_date')} ~ {result.get('end_date')}")
    print(f"  {'投入本金':<14}{result['total_invested']:>16,.2f}")
    print(f"  {'最终资产':<14}{result['final_value']:>16,.2f}")
    print(f"  {'净收益':<14}{result['net_profit']:>16,.2f}")
    print(f"  {'收益率 ROI':<14}{result['roi']:>15,.2f}%")
    print(f"  {'年化 CAGR':<14}{result['cagr']:>15,.2f}%")
    print(f"  {'最大回撤':<14}{result['max_drawdown']:>15,.2f}%")
    print(f"  {'回撤恢复天数':<14}{result['recovery_days']:>16,}")
    if result.get("recovery_note"):
        print(f"  [..]   {result['recovery_note']}")
    print(f"  {'Sharpe':<14}{result['sharpe']:>16}")
    print(f"  {'Sortino':<14}{result['sortino']:>16}")
    print(f"  {'波动率':<14}{result['volatility']:>15,.2f}%")
    print(f"  {'平均成本':<14}{result['avg_cost']:>16,.2f}")
    print(f"  {'持有 BTC':<14}{result['btc_amount']:>16,.8f}")
    print(f"  {'手续费合计':<14}{result['total_fees']:>16,.2f}")
    for warn in result.get("warnings", []):
        _warn(warn)
    if result.get("validation"):
        v = result["validation"]
        _info(f"样本外验证：训练 {v['train_period']} / 测试 {v['test_period']}")
    _ok(f"回测记录 run_id={result.get('run_id')}，模型版本 {result.get('model_version')}")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    from scripts.backup import create_backup

    path = create_backup(label=args.label)
    _ok(f"备份完成：{path}")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    from scripts.backup import restore_backup

    restore_backup(args.file, confirm=not args.yes)
    _ok("恢复完成")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    os.environ.setdefault("PYTHONPATH", str(BACKEND))
    os.chdir(BACKEND)
    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=bool(args.reload),
        log_config=None,
        access_log=not args.reload,
    )
    return 0


async def cmd_db_test(args: argparse.Namespace) -> int:
    """自检当前（或指定）数据库连接串 —— 与 Web 安装引导用的是同一套检查。"""
    from app.core.setup import probe_database

    url = args.url or os.environ.get("DATABASE_URL", "")
    if not url:
        _fail("没有给出连接串：加 --url，或先设置 DATABASE_URL")
        return 2
    r = await probe_database(url, timeout=args.timeout)
    _info(f"连接串（已隐藏口令）：{r.get('masked_url') or url}")
    for c in r["checks"]:
        (print if c["ok"] else _warn)(f"  [{'通过' if c['ok'] else '失败'}] {c['name']}：{c['detail']}")
    if r["ok"]:
        _ok(r["summary"])
        return 0
    _fail(r["summary"])
    return 1


def cmd_setup_reset(_args: argparse.Namespace) -> int:
    """把系统改回「未安装」状态，下次打开 Web 会重新进入安装引导。

    典型场景：想把数据从本机 SQLite 迁到一台共享的 PostgreSQL 上。
    这里只改标记，不动任何数据 —— 旧库文件原样留着，随时可以切回去。
    """
    from app.core.setup import ENV_PATH, upsert_env_vars

    upsert_env_vars({"SETUP_COMPLETED": "false"})
    _ok("已重置为未安装状态，下次打开网页会重新进入安装引导。")
    _warn(f"配置文件位置：{ENV_PATH}（数据不会因为这个操作丢失）")
    return 0


# --------------------------------------------------------------------- 入口


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="btcctl", description="BTC 研究平台运维命令行")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name: str, **kwargs: str) -> argparse.ArgumentParser:
        s = sub.add_parser(name, **kwargs)  # type: ignore[arg-type]
        return s

    add("init-db", help="初始化数据库并写入种子数据")
    s = add("backfill", help="回填历史 K 线（断点续传）")
    s.add_argument("--start", default=None)
    s.add_argument("--end", default=None, help="截止日期 YYYY-MM-DD，留空表示回填到今天")
    s.add_argument("--interval", default="1d")
    s.add_argument("--reset", action="store_true")
    s = add("incremental", help="增量同步")
    s.add_argument("--interval", default="1d")
    add("tick", help="采集一次最新价格")
    s = add("gap-repair", help="扫描并补齐本地数据缺口")
    s.add_argument("--interval", default="1d")
    s = add("quality", help="本地数据完整性扫描")
    s.add_argument("--interval", default="1d")
    add("collect-all", help="跑一遍链上/衍生品/情绪/宏观/ETF 采集")
    add("stats", help="数据表行数统计")
    s = add("coverage", help="查看本地历史覆盖范围")
    s.add_argument("--interval", default="1d")
    add("health", help="数据源健康面板")
    add("probe", help="一键测试所有数据源连通性")
    s = add("db-test", help="数据库连接自检（同 Web 安装引导）")
    s.add_argument("--url", default="", help="连接串；留空则用当前 DATABASE_URL")
    s.add_argument("--timeout", type=float, default=8.0)
    add("setup-reset", help="恢复为未安装状态，下次打开 Web 重进安装引导")
    s = add("replay", help="历史回放")
    s.add_argument("--date", required=True)
    s.add_argument("--perspective", default="then", choices=["then", "aftermath"])
    s = add("backtest", help="策略回测")
    s.add_argument("--strategy", default="dca_fixed")
    s.add_argument("--name", default="")
    s.add_argument("--start", default="2018-01-01")
    s.add_argument("--end", default="")
    s.add_argument("--freq", default="monthly", choices=["monthly", "weekly"])
    s.add_argument("--amount", type=float, default=1000.0)
    s.add_argument("--capital", type=float, default=0.0)
    s.add_argument("--oos", action="store_true")
    s = add("backup", help="备份数据库与配置")
    s.add_argument("--label", default="manual")
    s = add("restore", help="从备份恢复")
    s.add_argument("--file", required=True)
    s.add_argument("--yes", action="store_true")
    s = add("serve", help="直接启动后端服务")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8787)
    s.add_argument("--reload", action="store_true")
    return p


ASYNC_COMMANDS = {
    "init-db": cmd_init_db,
    "backfill": cmd_backfill,
    "incremental": cmd_incremental,
    "tick": cmd_tick,
    "gap-repair": cmd_gap_repair,
    "quality": cmd_quality,
    "collect-all": cmd_collect_all,
    "stats": cmd_stats,
    "coverage": cmd_coverage,
    "health": cmd_health,
    "probe": cmd_probe,
    "db-test": cmd_db_test,
    "replay": cmd_replay,
    "backtest": cmd_backtest,
}


def main() -> int:
    args = build_parser().parse_args()
    handler = ASYNC_COMMANDS.get(args.command)
    try:
        if handler:
            return asyncio.run(handler(args))
        if args.command == "backup":
            return cmd_backup(args)
        if args.command == "restore":
            return cmd_restore(args)
        if args.command == "serve":
            return cmd_serve(args)
        if args.command == "setup-reset":
            return cmd_setup_reset(args)
    except KeyboardInterrupt:
        print("\n  已中断。断点已保存，重新执行同一命令可继续。")
        return 130
    print(f"未知命令：{args.command}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
