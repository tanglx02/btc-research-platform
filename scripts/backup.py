# -*- coding: utf-8 -*-
"""系统备份与一键恢复。

备份内容（对应需求「一键备份 / 一键恢复」）：
  1. 数据库        —— SQLite 直接复制数据文件（含 WAL），PostgreSQL 走 pg_dump
  2. 配置          —— .env（含 Provider 的 Key、优先级、代理等，注意含敏感信息）
  3. Provider 配置 —— 部分配置存于数据库（providers / provider_config），随库备份
  4. 用户的资金计划/持仓/回测结果 —— 全部在数据库中，随库备份
  5. 清单文件      —— manifest.json 记录版本、时间、文件清单与校验值，便于核对

设计原则：
  * 备份是**自包含**的：拿到一个 .tar.gz 就能在另一台机器上恢复，不需要额外文件；
  * 恢复是**显式确认**的：覆盖现有数据前必须确认，避免误操作抹掉历史数据；
  * 失败必须清晰报错，不做「看起来成功」的部分恢复。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_settings() -> Any:
    sys.path.insert(0, str(ROOT / "backend"))
    from app.core.config import get_settings  # noqa: PLC0415

    return get_settings()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------ 备份签名
# SECRET_KEY 在这里有了真实用途：给备份包做 HMAC-SHA256 签名。
# 长期运行的系统里，备份会被拷到网盘/异地机上；没有签名就无法判断「恢复的这份是否被人改过」。
# 注意：不等于防篡改 —— 密钥与备份放在同一台机器上时，攻击者可同时拿到两者。
# 它真正防止的是「备份文件在搬运过程中损坏或被无意替换」，ISTS 语义下的完整性校验。
PLACEHOLDER_SECRET_PREFIX = "change-me"

SIGNATURE_ALGORITHM = "hmac-sha256"


def _secret_key() -> str:
    try:
        return str(_load_settings().SECRET_KEY or "")
    except Exception:  # noqa: BLE001 - 配置不可用时退化为不签名，而不是中断备份
        return ""


def _is_placeholder_secret(secret: str) -> bool:
    return (not secret) or secret.startswith(PLACEHOLDER_SECRET_PREFIX)


def signature_path(archive: Path) -> Path:
    return archive.with_suffix(archive.suffix + ".sig.json")


def write_signature(archive: Path, secret: str | None = None) -> dict[str, Any] | None:
    """为备份包生成签名 sidecar。密钥是占位值时明确标记 signature_strength=none，不假装有效防护。"""
    key = secret if secret is not None else _secret_key()
    digest = _sha256(archive)
    payload: dict[str, Any] = {
        "file": archive.name,
        "sha256": digest,
        "algorithm": SIGNATURE_ALGORITHM,
        "signed_at": datetime.now(timezone.utc).isoformat(),
        "signature_strength": "none（SECRET_KEY 为默认占位值，请在 .env 中设置强随机值后再备份）"
        if _is_placeholder_secret(key)
        else "hmac",
    }
    if not _is_placeholder_secret(key):
        payload["hmac_sha256"] = hmac.new(
            key.encode("utf-8"), f"{archive.name}:{digest}".encode("utf-8"), hashlib.sha256
        ).hexdigest()
    signature_path(archive).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return payload


def verify_signature(archive: Path) -> tuple[bool, str]:
    """校验备份包完整性。返回 (是否可安全继续, 说明)。

    校验失败一定返回 False —— 绝不因为「密钥变了」之类的理由就放行。
    """
    sig_file = signature_path(archive)
    if not sig_file.exists():
        return True, "备份包没有伴随签名文件（老版本备份或手工搬运时丢失），跳过签名校验，仅按清单校验文件哈希"
    try:
        sig = json.loads(sig_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return False, f"签名文件无法解析：{exc}"

    digest = _sha256(archive)
    if sig.get("sha256") and not hmac.compare_digest(str(sig["sha256"]), digest):
        return False, "备份包内容与其记录的 SHA-256 不一致，文件已损坏或被替换"

    stored = sig.get("hmac_sha256")
    if not stored:
        return True, "签名文件未包含 HMAC（生成时 SECRET_KEY 为占位值），只完成了 SHA-256 校验"
    key = _secret_key()
    if _is_placeholder_secret(key):
        return False, ("备份包带 HMAC 签名，但当前环境的 SECRET_KEY 是默认值，无法验证。"
                       "请先把 .env 里的 SECRET_KEY 改成与备份时一致的值")
    expect = hmac.new(key.encode("utf-8"), f"{archive.name}:{digest}".encode("utf-8"),
                      hashlib.sha256).hexdigest()
    if not hmac.compare_digest(stored, expect):
        return False, "HMAC 校验失败：备份包不是由持有当前 SECRET_KEY 的一方生成的，或文件被篡改"
    return True, "签名校验通过（SHA-256 + HMAC-SHA256）"


def _sqlite_files(db_url: str) -> list[Path]:
    """返回 SQLite 需要一起打包的文件（主库 + WAL + SHM）。

    只复制主库而不复制 WAL 会丢掉尚未 checkpoint 的写入，恢复后数据倒退 ——
    这是长期运行系统里最容易踩的备份坑之一。
    """
    if not db_url.startswith("sqlite"):
        return []
    raw = db_url.split("///", 1)[-1]
    p = Path(raw)
    if not p.is_absolute():
        p = ROOT / raw
    files = [p] if p.exists() else []
    for suffix in ("-wal", "-shm"):
        extra = p.with_name(p.name + suffix)
        if extra.exists():
            files.append(extra)
    return files


def _dump_postgres(db_url: str, target: Path) -> bool:
    """PostgreSQL → pg_dump 纯文本 SQL。没有 pg_dump 时明确失败（不做假备份）。"""
    pg_dump = shutil.which("pg_dump")
    if not pg_dump:
        print("  [WARN] 未找到 pg_dump，PostgreSQL 数据库未备份。请先安装 postgresql-client。")
        return False
    url = db_url.replace("+psycopg", "").replace("+asyncpg", "")
    try:
        result = subprocess.run([pg_dump, "--no-owner", "--no-acl", url],  # noqa: S603
                                capture_output=True, timeout=1800)
    except subprocess.TimeoutExpired:
        print("  [WARN] pg_dump 超时，数据库未备份")
        return False
    if result.returncode != 0:
        print(f"  [WARN] pg_dump 失败：{result.stderr.decode('utf-8', 'ignore')[:300]}")
        return False
    target.write_bytes(result.stdout)
    return True


def backup_dir() -> Path:
    s = _load_settings()
    d = Path(s.BACKUP_DIR)
    return d if d.is_absolute() else ROOT / s.BACKUP_DIR


def create_backup(label: str = "", dest_root: Path | None = None) -> str:
    """创建一份完整备份，返回备份文件路径。"""
    s = _load_settings()
    root = Path(dest_root) if dest_root else backup_dir()
    root.mkdir(parents=True, exist_ok=True)

    stamp = _utc_now()
    name = f"btc-backup-{stamp}" + (f"-{label}" if label else "")
    tmp_dir = Path(tempfile.mkdtemp(prefix="btcbk-"))
    stage = tmp_dir / name
    (stage / "db").mkdir(parents=True)
    (stage / "config").mkdir(parents=True)
    (stage / "logs").mkdir(parents=True)

    manifest: dict[str, Any] = {
        "name": name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "database_type": "postgresql" if s.DATABASE_URL.startswith(("postgres", "postgresql")) else "sqlite",
        "files": [],
        "warnings": [],
    }

    print("== 备份数据库 ==")
    if manifest["database_type"] == "sqlite":
        files = _sqlite_files(s.DATABASE_URL)
        if not files:
            manifest["warnings"].append("未找到 SQLite 数据文件，数据库未备份")
            print("  [WARN] 未找到 SQLite 数据文件")
        for f in files:
            target = stage / "db" / f.name
            shutil.copy2(f, target)
            manifest["files"].append({"role": "database", "path": f"db/{f.name}",
                                      "bytes": target.stat().st_size, "sha256": _sha256(target)})
            print(f"  [OK]   {f.name}  {target.stat().st_size/1024:.0f} KB")
    else:
        target = stage / "db" / "postgres.sql"
        if _dump_postgres(s.DATABASE_URL, target):
            manifest["files"].append({"role": "database", "path": "db/postgres.sql",
                                      "bytes": target.stat().st_size, "sha256": _sha256(target)})
            print(f"  [OK]   postgres.sql  {target.stat().st_size/1024:.0f} KB")
        else:
            manifest["warnings"].append("PostgreSQL 数据库未备份成功")

    print("== 备份配置 ==")
    for cfg in sorted(ROOT.glob(".env*")):
        if cfg.name == ".env.example":
            continue
        target = stage / "config" / cfg.name
        shutil.copy2(cfg, target)
        manifest["files"].append({"role": "config", "path": f"config/{cfg.name}",
                                  "bytes": target.stat().st_size})
        print(f"  [OK]   {cfg.name}（含密钥，请妥善保管）")
    if not list((stage / "config").iterdir()):
        print("  [提示] 未发现 .env（当前使用默认配置或纯环境变量）")

    print("== 备份最近日志（故障排查用，可选） ==")
    log_dir = Path(s.LOG_DIR)
    log_dir = log_dir if log_dir.is_absolute() else ROOT / s.LOG_DIR
    if log_dir.exists():
        keep = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:3]
        for f in keep:
            target = stage / "logs" / f.name
            shutil.copy2(f, target)
            manifest["files"].append({"role": "log", "path": f"logs/{f.name}", "bytes": target.stat().st_size})
            print(f"  [OK]   {f.name}")

    (stage / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    archive = root / f"{name}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(stage, arcname=name)
    shutil.rmtree(tmp_dir, ignore_errors=True)

    sig = write_signature(archive)
    size_kb = archive.stat().st_size / 1024
    print(f"\n备份完成：{archive}")
    print(f"  大小 {size_kb:.0f} KB   文件数 {len(manifest['files'])}")
    print(f"  签名文件：{signature_path(archive).name}（{sig.get('signature_strength')}）")
    if manifest["warnings"]:
        print("  [注意] " + "; ".join(manifest["warnings"]))
    return str(archive)


def list_backups() -> list[dict[str, Any]]:
    root = backup_dir()
    if not root.exists():
        return []
    rows = []
    for f in sorted(root.glob("*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True):
        rows.append({"file": str(f), "bytes": f.stat().st_size,
                     "modified": datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc).isoformat()})
    return rows


def restore_backup(archive: str, confirm: bool = True) -> int:
    """从备份恢复。覆盖现有数据前会要求显式确认。"""
    path = Path(archive)
    if not path.exists():
        print(f"[ERROR] 备份文件不存在：{archive}")
        return 1

    # 恢复之前的最后一道关：先验出身，再谈信任
    ok_sig, sig_msg = verify_signature(path)
    print(f"[校验] {sig_msg}")
    if not ok_sig:
        print("[ERROR] 校验未通过，已中止恢复。请确认备份来源；不要用「看起来能解压」代替校验。")
        return 1

    # 先解包到临时目录读取清单，确认内容再决定是否覆盖
    tmp_dir = Path(tempfile.mkdtemp(prefix="btcrs-"))
    with tarfile.open(path, "r:gz") as tar:
        tar.extractall(tmp_dir)  # noqa: S202 - 备份文件由本系统生成
    inner = next((d for d in tmp_dir.iterdir() if d.is_dir()), None)
    if inner is None:
        print("[ERROR] 备份包结构异常：找不到内容目录")
        return 1
    manifest_path = inner / "manifest.json"
    if not manifest_path.exists():
        print("[ERROR] 备份包缺少 manifest.json，无法安全恢复")
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    # 清单里登记了 sha256 的文件逐个复核；不一致说明包体损坏
    hash_errors: list[str] = []
    for item in manifest.get("files", []):
        rel = item.get("path")
        want = item.get("sha256")
        if not rel or not want:
            continue
        candidate = inner / rel
        if not candidate.exists():
            hash_errors.append(f"{rel}：包内缺失")
            continue
        if not hmac.compare_digest(str(want), _sha256(candidate)):
            hash_errors.append(f"{rel}：哈希与清单不符")
    if hash_errors:
        for msg in hash_errors:
            print(f"[ERROR] {msg}")
        print("[ERROR] 备份内容校验失败，未做任何修改。")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return 1

    print("=" * 62)
    print(f"备份包：{path.name}")
    print(f"创建时间：{manifest.get('created_at')}")
    print(f"数据库类型：{manifest.get('database_type')}")
    print("包含文件：")
    for f in manifest.get("files", []):
        print(f"  - [{f.get('role')}] {f.get('path')}  ({f.get('bytes', 0)/1024:.0f} KB)")
    print("=" * 62)
    print("恢复会**覆盖**当前的数据库与 .env 配置。当前数据如果尚未备份，将无法找回。")

    if confirm:
        try:
            answer = input("确认恢复？请输入 yes 继续：").strip().lower()
        except EOFError:
            answer = ""
        if answer != "yes":
            print("已取消，未做任何修改。")
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return 1

    s = _load_settings()
    db_dir = Path(s.DATA_DIR)
    db_dir = db_dir if db_dir.is_absolute() else ROOT / s.DATA_DIR
    db_dir.mkdir(parents=True, exist_ok=True)

    if manifest.get("database_type") == "sqlite":
        for item in (inner / "db").glob("*"):
            shutil.copy2(item, db_dir / item.name)
            print(f"  [OK]   恢复数据库文件 {item.name} -> {db_dir / item.name}")
    else:
        sql = inner / "db" / "postgres.sql"
        if sql.exists():
            psql = shutil.which("psql")
            if not psql:
                print("[ERROR] 未找到 psql，无法恢复 PostgreSQL 备份。请先安装 postgresql-client。")
                return 1
            url = s.DATABASE_URL.replace("+psycopg", "").replace("+asyncpg", "")
            r = subprocess.run([psql, url, "-f", str(sql)], capture_output=True)  # noqa: S603
            if r.returncode != 0:
                print(f"[ERROR] psql 恢复失败：{r.stderr.decode('utf-8', 'ignore')[:300]}")
                return 1
            print("  [OK]   PostgreSQL 数据已导入")

    cfg_src = inner / "config"
    if cfg_src.exists():
        for item in cfg_src.iterdir():
            target = ROOT / item.name
            if target.exists():
                backup_old = target.with_suffix(target.suffix + f".bak-{_utc_now()}")
                shutil.copy2(target, backup_old)
                print(f"  [OK]   原配置已另存为 {backup_old.name}")
            shutil.copy2(item, target)
            print(f"  [OK]   恢复配置 {item.name}")

    shutil.rmtree(tmp_dir, ignore_errors=True)
    print("\n恢复完成。请重启服务使配置生效：")
    print("  python scripts/btcctl.py serve")
    return 0
