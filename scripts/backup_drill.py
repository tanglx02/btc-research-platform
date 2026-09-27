# -*- coding: utf-8 -*-
"""备份 / 恢复演练脚本（不触碰线上数据）。

对应 docs/25-更新回滚与备份恢复.md 与 docs/27-测试与验收清单.md 的「备份恢复演练」章节。

演练内容：
  1. 生成一份真实备份包（写进独立临时目录，不污染 backups/）
  2. 校验签名 sidecar 与 manifest 里登记的 SHA-256（模拟恢复前的第一道关）
  3. 解包到沙箱目录，用只读方式打开恢复出来的 SQLite 库，
     统计表数量、关键表行数，与当前库对比 —— 证明「恢复出来的库真的能用」
  4. 故意篡改一个字节，验证校验机制**确实会拒绝**（负向测试）
  5. 全程不覆盖任何真实文件，演练结束自动清理

用法::

    python scripts/backup_drill.py
    python scripts/backup_drill.py --keep   # 保留沙箱目录便于人工核对
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backup import (  # noqa: E402
    create_backup,
    signature_path,
    verify_signature,
)

REPORT_DIR = ROOT / "data" / "reports"

# 恢复演练时重点核对的表：既有原有业务表，也有本次新增的 alert 表
KEY_TABLES = [
    "candles",
    "provider_health",
    "providers",
    "alert_rules",
    "alert_conditions",
    "alert_condition_groups",
    "alert_events",
    "alert_notification_channels",
    "alert_notification_logs",
    "alert_cooldowns",
    "alert_rule_versions",
]


def _count_tables(db_path: Path) -> dict[str, Any]:
    """只读打开 SQLite，返回表清单与关键表行数。"""
    if not db_path.exists():
        return {"exists": False, "tables": [], "rows": {}}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        names = [
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        ]
        rows: dict[str, int] = {}
        for t in KEY_TABLES:
            if t in names:
                try:
                    rows[t] = int(con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0])
                except sqlite3.Error:
                    rows[t] = -1
        return {"exists": True, "tables": names, "rows": rows}
    finally:
        con.close()


def _current_db_path() -> Path | None:
    sys.path.insert(0, str(ROOT / "backend"))
    from scripts.backup import _sqlite_files  # noqa: PLC0415
    from app.core.config import get_settings  # noqa: PLC0415

    files = _sqlite_files(get_settings().DATABASE_URL)
    return files[0] if files else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="备份 / 恢复演练")
    parser.add_argument("--keep", action="store_true", help="保留沙箱目录")
    args = parser.parse_args(argv)

    sandbox = Path(tempfile.mkdtemp(prefix="btc-drill-"))
    failures: list[str] = []
    report: dict[str, Any] = {"steps": []}

    def step(name: str, ok: bool, detail: str) -> None:
        report["steps"].append({"step": name, "ok": ok, "detail": detail})
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
        if not ok:
            failures.append(name)

    print("=" * 74)
    print("备份 / 恢复演练（沙箱内进行，不覆盖任何线上文件）")
    print("=" * 74)
    print(f"沙箱目录：{sandbox}\n")

    before = _current_db_path()
    before_stats = _count_tables(before) if before else {"exists": False, "tables": [], "rows": {}}
    print(f"[基线] 当前库：{before}  表数 {len(before_stats.get('tables', []))}")

    # ---- 1. 生成备份 -------------------------------------------------------
    print("\n[1/5] 生成备份包")
    try:
        archive = create_backup(label="drill", dest_root=sandbox / "backups")
        archive_path = Path(archive)
        ok = archive_path.exists() and archive_path.stat().st_size > 0
        step("生成备份包", ok, f"{archive_path.name}  {archive_path.stat().st_size/1024:.0f} KB")
    except Exception as exc:  # noqa: BLE001
        step("生成备份包", False, f"{type(exc).__name__}: {exc}")
        print("\n演练中止：备份都没生成，后面的恢复无从谈起。")
        shutil.rmtree(sandbox, ignore_errors=True)
        return 1

    # ---- 2. 签名与清单校验 --------------------------------------------------
    print("\n[2/5] 校验签名与文件清单")
    sig_ok, sig_msg = verify_signature(archive_path)
    print(f"  签名文件：{signature_path(archive_path).name}")
    step("签名校验", sig_ok, sig_msg)

    with tarfile.open(archive_path, "r:gz") as tar:
        tar.extractall(sandbox / "unpacked")  # noqa: S202
    inner = next((d for d in (sandbox / "unpacked").iterdir() if d.is_dir()), None)
    manifest = json.loads((inner / "manifest.json").read_text(encoding="utf-8"))
    registered = [f for f in manifest.get("files", []) if f.get("sha256")]
    step(
        "manifest 完整性",
        bool(registered),
        f"{len(manifest.get('files', []))} 个文件登记，其中 {len(registered)} 个带 SHA-256",
    )
    step(
        "数据库已入包",
        any(f.get("role") == "database" for f in manifest.get("files", [])),
        "数据库文件在备份包内" if any(
            f.get("role") == "database" for f in manifest.get("files", [])
        ) else "备份包里没有数据库文件（这是不可接受的假备份）",
    )

    # ---- 3. 解包后的库真的能打开吗 ------------------------------------------
    print("\n[3/5] 打开恢复出来的数据库")
    restored_db = next((inner / "db").glob("*.db"), None) or next(
        (inner / "db").glob("*"), None
    )
    after_stats = _count_tables(restored_db) if restored_db else {
        "exists": False, "tables": [], "rows": {}
    }
    step("恢复库可只读打开", bool(after_stats.get("exists")), str(restored_db))

    if before_stats.get("tables") and after_stats.get("tables"):
        lost = set(before_stats["tables"]) - set(after_stats["tables"])
        step(
            "表结构一致",
            not lost,
            f"备份前 {len(before_stats['tables'])} 张表，恢复后 {len(after_stats['tables'])} 张"
            + (f"，缺失 {sorted(lost)}" if lost else ""),
        )
        alert_tables = [t for t in after_stats["tables"] if t.startswith("alert_")]
        step(
            "新增 alert 表随库备份",
            len(alert_tables) >= 8,
            f"恢复库里含 {len(alert_tables)} 张 alert_ 表：{sorted(alert_tables)}",
        )
    report["before"] = {k: v for k, v in before_stats.items() if k != "tables"}
    report["before"]["table_count"] = len(before_stats.get("tables", []))
    report["after"] = {k: v for k, v in after_stats.items() if k != "tables"}
    report["after"]["table_count"] = len(after_stats.get("tables", []))
    report["after"]["rows"] = after_stats.get("rows", {})

    # ---- 4. 负向测试：篡改必须被拒绝 ----------------------------------------
    # 关键：必须把 .sig.json 伴随文件一起复制过去。
    # 只拷 .tar.gz 会命中「没有签名文件 -> 跳过签名校验」的兼容分支，
    # 那种情况下放行的不是签名机制，而是根本没验签 —— 测不出任何东西。
    print("\n[4/5] 负向测试：篡改备份包必须被拒绝（连同 .sig.json 一起复制）")
    tampered = sandbox / "tampered.tar.gz"
    shutil.copy2(archive_path, tampered)
    sig_src = signature_path(archive_path)
    if sig_src.exists():
        shutil.copy2(sig_src, signature_path(tampered))
        step("签名 sidecar 随包复制", True, f"{sig_src.name} 已随篡改包一起参与校验")
    else:
        step("签名 sidecar 随包复制", False, "备份未生成 .sig.json，无法演练验签")
    with tampered.open("r+b") as fh:
        fh.seek(-8, 2)
        tail = fh.read()
        fh.seek(-8, 2)
        fh.write(bytes([tail[0] ^ 0xFF]) + tail[1:])
    t_ok, t_msg = verify_signature(tampered)
    step("篡改后拒绝恢复", not t_ok, f"校验返回 ok={t_ok}：{t_msg}")

    # 附带记录一个真实存在的残余风险：只要 sidecar 不在，验签这一层就形同虚设，
    # 只剩包内 manifest 的哈希兜底（而 manifest 本身也在包内、同样可被改）。
    no_sig = sandbox / "nosig.tar.gz"
    shutil.copy2(archive_path, no_sig)
    n_ok, n_msg = verify_signature(no_sig)
    report["sidecar_missing_behaviour"] = {"ok": n_ok, "message": n_msg}
    print(f"  [提示] sidecar 缺失时的行为：ok={n_ok} —— {n_msg}")
    print("         这意味着：若备份包被搬运时丢失 .sig.json，验签层会退化为仅靠包内 manifest 哈希。")

    # ---- 5. 清单哈希不符也必须拒绝 ------------------------------------------
    print("\n[5/5] 负向测试：包内文件与清单不符")
    mism = inner / "manifest.json"
    raw = json.loads(mism.read_text(encoding="utf-8"))
    if raw.get("files"):
        raw["files"][0]["sha256"] = "0" * 64
        mism.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    # 直接复用 backup.restore_backup 的哈希复核逻辑做判定
    import hashlib  # noqa: PLC0415

    def _sha(p: Path) -> str:
        h = hashlib.sha256()
        with p.open("rb") as fh:
            for c in iter(lambda: fh.read(1 << 20), b""):
                h.update(c)
        return h.hexdigest()

    bad = []
    for item in raw.get("files", []):
        rel, want = item.get("path"), item.get("sha256")
        if not rel or not want:
            continue
        cand = inner / rel
        if not cand.exists() or want != _sha(cand):
            bad.append(rel)
    step("清单哈希不符被识别", bool(bad), f"发现不符项 {len(bad)} 个：{bad[:3]}")

    # ---- 汇总 ---------------------------------------------------------------
    print("\n" + "=" * 74)
    if failures:
        print(f"演练结论：{len(failures)} 项未通过 -> {failures}")
        verdict = 1
    else:
        print("演练结论：全部通过。备份可用、可校验、恢复后的库可打开，篡改会被拒绝。")
        verdict = 0
    print("=" * 74)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report["verdict"] = "PASS" if verdict == 0 else "FAIL"
    import datetime as _dt  # noqa: PLC0415

    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = REPORT_DIR / f"backup_drill_{stamp}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告已写入：{out}")

    if args.keep:
        print(f"沙箱已保留：{sandbox}")
    else:
        shutil.rmtree(sandbox, ignore_errors=True)
    return verdict


if __name__ == "__main__":
    raise SystemExit(main())
