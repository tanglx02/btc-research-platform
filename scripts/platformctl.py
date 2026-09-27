# -*- coding: utf-8 -*-
"""platformctl —— BTC 研究平台的一键部署控制器（Windows / Linux 通用，纯标准库）。

它是一键安装 / 更新 / 卸载 / 备份 / 恢复脚本的**唯一实现**，
scripts/windows/*.bat 与 scripts/linux/*.sh 只是薄薄的调用外壳，
这样两条平台的语义完全一致，不会出现「Windows 能装，Linux 装一半」的问题。

支持的子命令：
    install      安装依赖、生成配置、初始化数据库（幂等，可反复执行）
    update       备份 → 更新代码 → 升级依赖 → 迁移 → 重启
    uninstall    停止服务 →（可选）删除数据与虚拟环境，**卸载前强制备份**
    start        启动服务（--daemon 后台运行，Windows/Linux 均支持）
    stop         停止服务
    restart      重启服务
    status       查看服务状态与健康检查
    doctor       环境自检（Python 版本、依赖、数据库、端口、数据覆盖）
    backup       备份数据库与配置
    restore      从备份恢复
    service      注册/注销操作系统级常驻：service install|remove|status
                 （Linux→systemd，Windows→任务计划程序；重启后自动拉起）
    menu         交互式运维菜单

设计原则：
  * **不破坏数据**：卸载默认保留 data/ 与 .env；--purge-data 才删，且操作前强制先备份。
  * **不装看不见的依赖**：每一步都打印实际执行结果，失败立刻中止并给出下一步提示。
  * **跨平台一致**：路径、进程 detach、端口检查都按平台分支处理，行为保持一致。
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"
ENV_FILE = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
DATA_DIR = ROOT / "data"
BACKUP_DIR = ROOT / "backups"
LOGS_DIR = ROOT / "logs"
PID_FILE = ROOT / ".runtime" / "server.pid"
LOG_FILE = LOGS_DIR / "platform.log"
DEFAULT_PORT = 8787

IS_WINDOWS = os.name == "nt" or sys.platform.startswith("win")

ACCENT = {
    "info": "  [..]",
    "ok": "  [OK]",
    "warn": "  [WARN]",
    "err": "  [FAIL]",
}


def say(msg: str, kind: str = "info") -> None:
    print(f"{ACCENT.get(kind, ACCENT['info'])} {msg}")


def title(msg: str) -> None:
    print(f"\n=== {msg} ===")


# ------------------------------------------------------------------ 基础工具


def venv_python() -> Path:
    """返回虚拟环境内的解释器路径（平台无关）。"""
    if IS_WINDOWS:
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def find_system_python() -> str:
    """寻找合适的系统 Python（3.11+）。"""
    candidates = []
    if not IS_WINDOWS:
        candidates += ["python3.13", "python3.12", "python3.11", "python3"]
    else:
        candidates += ["py -3.13", "py -3.12", "py -3.11", "python"]
    chosen = ""
    for cand in candidates:
        try:
            out = subprocess.run(
                cand.split() + ["-c", "import sys;print(sys.version_info[:3])"],
                capture_output=True, text=True, timeout=30,
            )
            if out.returncode == 0:
                ver = eval(out.stdout.strip())  # noqa: S307 - 输出形如 (3, 13, 1)
                if ver[:2] >= (3, 11):
                    chosen = cand
                    break
        except Exception:  # noqa: BLE001
            continue
    if not chosen:
        say("未找到 Python 3.11+，请先安装：https://www.python.org/downloads/", "err")
        sys.exit(2)
    return chosen


def run(cmd: list[str], *, cwd: Path | None = None, check: bool = True,
        env: dict[str, str] | None = None, quiet: bool = False) -> subprocess.CompletedProcess:
    """执行命令并实时打印（默认长任务流式输出）。"""
    printable = " ".join(f'"{c}"' if " " in c else c for c in cmd)
    if not quiet:
        say(f"$ {printable}")
    merged = os.environ.copy()
    if env:
        merged.update(env)
    proc = subprocess.run(cmd, cwd=str(cwd or ROOT), env=merged, check=False)  # noqa: S603
    if check and proc.returncode != 0:
        say(f"命令失败（退出码 {proc.returncode}），已中止。请检查上方输出。", "err")
        sys.exit(proc.returncode or 1)
    return proc


def read_env_value(key: str, default: str = "") -> str:
    """从 .env 读取一个值（不引入第三方依赖）。"""
    if not ENV_FILE.exists():
        return default
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == key:
            return v.strip()
    return default


def server_port() -> int:
    try:
        return int(read_env_value("PORT", str(DEFAULT_PORT)) or DEFAULT_PORT)
    except ValueError:
        return DEFAULT_PORT


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def http_json(url: str, timeout: float = 3.0) -> tuple[int, dict[str, Any]]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except Exception:  # noqa: BLE001
        return 0, {}


def api_base() -> str:
    prefix = read_env_value("API_PREFIX", "/api/v1")
    return f"http://127.0.0.1:{server_port()}{prefix}"


def pip_index_args() -> list[str]:
    """允许用户通过环境变量 PIP_INDEX_URL 指定镜像源（便于内网/国内加速）。"""
    idx = os.environ.get("PIP_INDEX_URL", "")
    return ["-i", idx] if idx else []


# ------------------------------------------------------------------ 安装


def ensure_env_file() -> None:
    if ENV_FILE.exists():
        say(f"配置文件已存在：{ENV_FILE.name}（保留现有配置）")
        if read_env_value("ADMIN_TOKEN", "") in ("admin-change-me", ""):
            say("ADMIN_TOKEN 仍是默认值，生产环境请务必修改", "warn")
        return
    if not ENV_EXAMPLE.exists():
        say(f"缺少 {ENV_EXAMPLE.name}，无法生成配置", "err")
        sys.exit(2)
    shutil.copy2(ENV_EXAMPLE, ENV_FILE)
    say(f"已从 {ENV_EXAMPLE.name} 生成 {ENV_FILE.name}（默认值可直接启动）", "ok")


def ensure_venv(system_python: str) -> Path:
    py = venv_python()
    if py.exists():
        say(f"虚拟环境已存在：{VENV}", "ok")
        return py
    title("创建虚拟环境")
    VENV.parent.mkdir(parents=True, exist_ok=True)
    run(system_python.split() + ["-m", "venv", str(VENV)])
    py = venv_python()
    if not py.exists():
        say("虚拟环境创建失败，请检查 Python 安装是否完整（venv 模块）。", "err")
        sys.exit(2)
    say(f"虚拟环境已就绪：{py}", "ok")
    return py


def install_deps(py: Path, upgrade: bool = False) -> None:
    title("安装依赖")
    args = [str(py), "-m", "pip", "install", "-r", str(REQUIREMENTS)]
    if upgrade:
        args.append("--upgrade")
    run(args + pip_index_args(), quiet=False)


def init_database(py: Path) -> None:
    title("初始化数据库")
    run([str(py), str(ROOT / "scripts" / "btcctl.py"), "init-db"])


def cmd_install(args: argparse.Namespace) -> int:
    print("=" * 66)
    print(" BTC 全市场智能研究平台 —— 一键安装")
    print(f" 平台 {platform.system()} {platform.machine()} / Python {platform.python_version()}")
    print(f" 目录 {ROOT}")
    print("=" * 66)

    system_python = args.python or find_system_python()
    say(f"使用系统 Python：{system_python}")

    for directory in (DATA_DIR, LOGS_DIR, BACKUP_DIR, ROOT / ".runtime"):
        directory.mkdir(parents=True, exist_ok=True)

    ensure_env_file()
    py = ensure_venv(system_python)
    install_deps(py, upgrade=args.upgrade)
    init_database(py)

    if args.backfill:
        title("回填历史数据（首次安装可选，耗时较长，支持中断后续传）")
        run([str(py), str(ROOT / "scripts" / "btcctl.py"), "backfill"],
            check=False)

    title("安装完成")
    port = server_port()
    print(f"  启动服务：  {'scripts\\windows\\start.bat' if IS_WINDOWS else './scripts/linux/start.sh'}")
    print("             或命令行： python scripts/platformctl.py start --daemon")
    print(f"  访问地址：  http://127.0.0.1:{port}/")
    print(f"  接口文档：  http://127.0.0.1:{port}/docs")
    print(f"  运维菜单：  {'scripts\\windows\\menu.bat' if IS_WINDOWS else './scripts/linux/menu.sh'}")
    print(f"  数据目录：  {DATA_DIR}")
    print(f"  日志目录：  {LOGS_DIR}")
    return 0


# ------------------------------------------------------------------ 更新


def _update_code(args: argparse.Namespace) -> None:
    """更新代码：优先 git pull，非 git 仓库时提示手动覆盖。"""
    if (ROOT / ".git").exists():
        run(["git", "pull", "--ff-only"], check=False)
    else:
        say("当前不是 git 仓库，请手动把新版本代码覆盖到本目录后再继续。", "warn")


def cmd_update(args: argparse.Namespace) -> int:
    title("更新")
    py = venv_python()
    if not py.exists():
        say("尚未安装（找不到 .venv），请先执行 install", "err")
        return 2

    running = read_pid() is not None
    if running:
        say("检测到服务正在运行，更新前先停止")
        stop_service()

    if not args.no_backup:
        title("更新前自动备份")
        do_backup("pre-update")

    _update_code(args)
    ensure_env_file()
    install_deps(py, upgrade=True)
    init_database(py)

    title("更新完成")
    if args.start:
        return start_service(daemon=True)
    print("  执行 python scripts/platformctl.py start --daemon 启动服务。")
    return 0


# ------------------------------------------------------------------ 卸载


def cmd_uninstall(args: argparse.Namespace) -> int:
    print("!" * 66)
    print(" 卸载说明：本操作会停止服务并删除虚拟环境。")
    print(" 默认 **保留** data/（历史数据库）与 .env（配置）；加 --purge-data 才会删除。")
    print("!" * 66)
    if not args.yes:
        try:
            ans = input("确认卸载？输入 yes 继续：").strip().lower()
        except EOFError:
            ans = ""
        if ans != "yes":
            print("已取消。")
            return 1

    stop_service()

    title("卸载前强制备份")
    do_backup("pre-uninstall")

    # 卸载必须一并拆掉操作系统级常驻注册，否则机器重启后残留任务会对着空目录反复拉起
    if IS_WINDOWS:
        service_remove_windows()
    else:
        service_remove_linux()

    if VENV.exists():
        shutil.rmtree(VENV, ignore_errors=True)
        say(f"已删除虚拟环境 {VENV}", "ok")

    if args.purge_data:
        say("正在删除历史数据与备份（此操作不可逆）", "warn")
        for path in (DATA_DIR, BACKUP_DIR, ENV_FILE):
            if path.exists():
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink(missing_ok=True)
                say(f"已删除 {path}", "ok")
    else:
        say(f"已保留历史数据 {DATA_DIR} 与备份 {BACKUP_DIR}", "ok")
        say("如需彻底清除，可手动删除上述目录。")
    return 0


# ------------------------------------------------------------------ 系统级常驻注册
#
# 需求要求「长期无人值守运行」。靠 `start --daemon` 拉起的裸进程在服务器重启后不会自己回来，
# 也不能崩溃自愈。因此这里提供 `service install`，把进程交给操作系统托管：
#   Linux   -> systemd（Restart=always，崩溃 5 秒后自动拉起，开机自启）
#   Windows -> 任务计划程序（触发器：启动时，重复间隔 1 分钟；已存在则幂等跳过）

SERVICE_TASK_NAME = "BTCPlatform"
SYSTEMD_LOG_DIR = Path("/var/log/btc-platform")
SYSTEMD_UNIT_SRC = ROOT / "scripts" / "linux" / "systemd" / "btc-platform.service"
SYSTEMD_UNIT_DST = Path("/etc/systemd/system/btc-platform.service")
SERVICE_USER = "btc"


def _need_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() != 0


def _user_exists(name: str) -> bool:
    proc = subprocess.run(["id", name], capture_output=True)  # noqa: S603
    return proc.returncode == 0


def _ensure_service_account() -> None:
    """创建最小权限运行账号。服务不该用 root 跑，也不该用登录账号跑。"""
    if _need_root():
        say("当前非 root，跳过系统账号与日志目录准备；请以 sudo 重新运行本命令", "warn")
        return
    if not _user_exists(SERVICE_USER):
        for cmd in (
            ["useradd", "--system", "--no-create-home", "--shell", "/usr/sbin/nologin", SERVICE_USER],
            ["adduser", "--system", "--no-create-home", "--disabled-login", SERVICE_USER],
        ):
            proc = subprocess.run(cmd, capture_output=True)  # noqa: S603
            if proc.returncode == 0:
                say(f"已创建系统账号 {SERVICE_USER}", "ok")
                break
        else:
            say(f"无法创建系统账号 {SERVICE_USER}，请手动创建后重试", "err")
            return
    try:
        SYSTEMD_LOG_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.run(["chown", "-R", f"{SERVICE_USER}:{SERVICE_USER}", str(SYSTEMD_LOG_DIR)],  # noqa: S603
                       capture_output=True, check=False)
        for d in (DATA_DIR, LOGS_DIR, BACKUP_DIR):
            if d.exists():
                subprocess.run(["chown", "-R", f"{SERVICE_USER}:{SERVICE_USER}", str(d)],  # noqa: S603
                               capture_output=True, check=False)
        say(f"已准备日志目录 {SYSTEMD_LOG_DIR} 并修正数据目录属主", "ok")
    except PermissionError as exc:
        say(f"目录属主调整失败：{exc}", "err")


def _render_unit() -> str:
    """把 unit 模板里的 /opt/btc-platform 替换成真实部署路径，避免安装者手工改文件。"""
    text = SYSTEMD_UNIT_SRC.read_text(encoding="utf-8")
    root_posix = ROOT.as_posix()
    text = text.replace("/opt/btc-platform", root_posix)
    port = server_port()
    text = text.replace("--port 8787", f"--port {port}")
    return text


def service_install_linux() -> int:
    if not SYSTEMD_UNIT_SRC.exists():
        say(f"缺少单元文件模板：{SYSTEMD_UNIT_SRC}", "err")
        return 1
    if _need_root():
        say("注册 systemd 服务需要 root 权限，请改用：sudo python scripts/platformctl.py service install", "err")
        return 2
    if not shutil.which("systemctl"):
        say("未检测到 systemctl，当前系统不是 systemd，无法自动注册。", "err")
        say("请改用发行版自带的进程管理器（supervisor / runit / rc.local）托管启动命令。", "warn")
        return 2

    _ensure_service_account()
    unit = _render_unit()
    SYSTEMD_UNIT_DST.parent.mkdir(parents=True, exist_ok=True)
    SYSTEMD_UNIT_DST.write_text(unit, encoding="utf-8")
    say(f"已写入单元文件 {SYSTEMD_UNIT_DST}（路径已替换为 {ROOT.as_posix()}）", "ok")
    for args in (["systemctl", "daemon-reload"],
                 ["systemctl", "enable", "btc-platform"],
                 ["systemctl", "restart", "btc-platform"]):
        subprocess.run(args, check=False)  # noqa: S603
    proc = subprocess.run(["systemctl", "is-active", "btc-platform"], capture_output=True)  # noqa: S603
    active = proc.stdout.decode("utf-8", "ignore").strip()
    say(f"服务状态：{active or '未知'}", "ok" if active == "active" else "warn")
    print("  查看： sudo systemctl status btc-platform")
    print(f"  日志： sudo journalctl -u btc-platform -f   或   tail -f {SYSTEMD_LOG_DIR}/stdout.log")
    return 0 if active == "active" else 1


def service_remove_linux() -> int:
    if not shutil.which("systemctl"):
        return 0
    if _need_root():
        say("非 root，跳过 systemd 注销；请手动执行 sudo systemctl disable --now btc-platform", "warn")
        return 0
    subprocess.run(["systemctl", "stop", "btc-platform"], check=False)  # noqa: S603
    subprocess.run(["systemctl", "disable", "btc-platform"], check=False)  # noqa: S603
    if SYSTEMD_UNIT_DST.exists():
        SYSTEMD_UNIT_DST.unlink()
        say(f"已移除 systemd 服务 {SYSTEMD_UNIT_DST.name}", "ok")
    subprocess.run(["systemctl", "daemon-reload"], check=False)  # noqa: S603
    return 0


def _schtasks(args: list[str]) -> subprocess.CompletedProcess[bytes]:
    """调用 schtasks。权限不足/命令被拦截时返回带错误说明的结果，而不是让异常打断运维脚本。"""
    try:
        return subprocess.run(["schtasks"] + args, capture_output=True)  # noqa: S603
    except (OSError, PermissionError) as exc:
        return subprocess.CompletedProcess(args=["schtasks"] + args, returncode=126,
                                           stdout=b"", stderr=str(exc).encode("utf-8", "ignore"))


def _windows_service_registered() -> bool:
    proc = _schtasks(["/Query", "/TN", SERVICE_TASK_NAME])
    return proc.returncode == 0


def service_install_windows() -> int:
    py = venv_python()
    if not py.exists():
        say("尚未安装（找不到 .venv），请先执行 install", "err")
        return 2
    script = ROOT / "scripts" / "windows" / "start.bat"
    if not script.exists():
        say(f"缺少启动脚本：{script}", "err")
        return 1
    if _windows_service_registered():
        say(f"计划任务 {SERVICE_TASK_NAME} 已存在，跳过创建", "ok")
        return 0
    proc = _schtasks([
        "/Create", "/TN", SERVICE_TASK_NAME,
        "/TR", f'"{script}"',
        "/SC", "ONSTART",
        "/RL", "HIGHEST",
        "/DELAY", "0000:30",
        "/F",
    ])
    if proc.returncode != 0:
        say(f"创建计划任务失败：{proc.stderr.decode('utf-8', 'ignore')[:200]}", "err")
        say("若当前不是管理员权限，请以管理员身份重新运行本命令。", "warn")
        return 1
    say(f"已注册计划任务 {SERVICE_TASK_NAME}（开机自动拉起，延迟 30 秒等待网络）", "ok")
    print("  查看： schtasks /Query /TN BTCPlatform")
    print("  删除： schtasks /Delete /TN BTCPlatform /F")
    return 0


def service_remove_windows() -> int:
    if not _windows_service_registered():
        return 0
    proc = _schtasks(["/Delete", "/TN", SERVICE_TASK_NAME, "/F"])
    if proc.returncode == 0:
        say(f"已删除计划任务 {SERVICE_TASK_NAME}", "ok")
    else:
        say(f"计划任务删除失败：{proc.stderr.decode('utf-8', 'ignore')[:200]}", "warn")
    return 0


def cmd_service(args: argparse.Namespace) -> int:
    action = args.action
    if action == "install":
        return service_install_linux() if not IS_WINDOWS else service_install_windows()
    if action == "remove":
        return service_remove_linux() if not IS_WINDOWS else service_remove_windows()
    if action == "status":
        if IS_WINDOWS:
            registered = _windows_service_registered()
            print(f"计划任务 {SERVICE_TASK_NAME}: {'已注册' if registered else '未注册'}")
        else:
            proc = subprocess.run(["systemctl", "is-active", "btc-platform"],  # noqa: S603
                                  capture_output=True)
            print(f"systemd btc-platform: {proc.stdout.decode('utf-8', 'ignore').strip() or '未注册'}")
            enabled = subprocess.run(["systemctl", "is-enabled", "btc-platform"],  # noqa: S603
                                     capture_output=True)
            print(f"开机自启: {enabled.stdout.decode('utf-8', 'ignore').strip() or '未启用'}")
        return 0
    say(f"未知子命令：{action}", "err")
    return 2


# ------------------------------------------------------------------ 备份 / 恢复


def do_backup(label: str = "manual") -> str | None:
    """调用 scripts/backup.py 生成备份。"""
    sys.path.insert(0, str(ROOT))
    py = venv_python() if venv_python().exists() else Path(sys.executable)
    proc = run([str(py), str(ROOT / "scripts" / "btcctl.py"), "backup", "--label", label],
               check=False, quiet=True)
    if proc.returncode != 0:
        say("备份失败（已中止后续操作，以免破坏数据）", "err")
        return None
    return "ok"


def cmd_backup(args: argparse.Namespace) -> int:
    return 0 if do_backup(args.label or "manual") else 2


def cmd_restore(args: argparse.Namespace) -> int:
    py = venv_python() if venv_python().exists() else Path(sys.executable)
    cmd = [str(py), str(ROOT / "scripts" / "btcctl.py"), "restore", "--file", args.file]
    if args.yes:
        cmd.append("--yes")
    run(cmd, check=False)
    return 0


# ------------------------------------------------------------------ 服务进程


def read_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip() or 0)
    except ValueError:
        return None
    if pid <= 0:
        return None
    return pid if pid_alive(pid) else None


def pid_alive(pid: int) -> bool:
    if IS_WINDOWS:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                             capture_output=True, text=True, check=False)
        return str(pid) in out.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _service_cmd() -> list[str]:
    py = venv_python()
    if not py.exists():
        say("尚未安装（找不到 .venv），请先执行 install", "err")
        sys.exit(2)
    return [
        str(py), "-m", "uvicorn", "app.main:app",
        "--host", read_env_value("HOST", "127.0.0.1"),
        "--port", str(server_port()),
        "--app-dir", str(ROOT / "backend"),
    ]


def start_service(daemon: bool = False) -> int:
    port = server_port()
    if read_pid() is not None:
        say("服务已在运行", "warn")
        return 0
    if port_in_use(port):
        say(f"端口 {port} 已被占用，请先停止占用进程或修改 .env 中的 PORT", "err")
        return 2

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    cmd = _service_cmd()

    if not daemon:
        title(f"启动服务（前台，Ctrl+C 停止） http://127.0.0.1:{port}/")
        return run(cmd, check=False).returncode

    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    log_handle = open(LOG_FILE, "a", encoding="utf-8")  # noqa: SIM115 - 后台进程长期持有
    env = os.environ.copy()
    env.setdefault("PYTHONPATH", str(ROOT / "backend"))
    env.setdefault("PYTHONUNBUFFERED", "1")
    if IS_WINDOWS:
        proc = subprocess.Popen(cmd, stdout=log_handle, stderr=subprocess.STDOUT,  # noqa: S603
                                env=env, cwd=str(ROOT),
                                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        proc = subprocess.Popen(cmd, stdout=log_handle, stderr=subprocess.STDOUT,  # noqa: S603
                                env=env, cwd=str(ROOT), start_new_session=True)
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    say(f"服务已在后台启动，PID {proc.pid}，日志 {LOG_FILE}", "ok")
    # 等待端口就绪，最多 25 秒（首次启动要执行迁移与种子数据）
    deadline = time.time() + 25
    while time.time() < deadline:
        if port_in_use(port):
            status, payload = http_json(f"http://127.0.0.1:{port}/healthz")
            if status == 200 and payload.get("status") == "ok":
                say(f"健康检查通过：http://127.0.0.1:{port}/healthz", "ok")
                print(f"  访问地址： http://127.0.0.1:{port}/")
                return 0
        time.sleep(0.5)
    say("端口未能在 25 秒内就绪，请查看日志：" + str(LOG_FILE), "warn")
    return 0


def stop_service() -> int:
    pid = read_pid()
    if pid is None:
        say("服务未在运行", "warn")
        return 0
    title("停止服务")
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, check=False)
        else:
            os.kill(pid, 15)
            for _ in range(30):
                if not pid_alive(pid):
                    break
                time.sleep(0.2)
            else:
                os.kill(pid, 9)
        say(f"已停止服务 PID {pid}", "ok")
    except Exception as exc:  # noqa: BLE001
        say(f"停止失败：{exc}", "err")
        return 2
    finally:
        PID_FILE.unlink(missing_ok=True)
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    port = server_port()
    pid = read_pid()
    print(f"  安装目录   {ROOT}")
    print(f"  虚拟环境   {VENV} {'存在' if VENV.exists() else '缺失'}")
    print(f"  服务进程   PID {pid}" if pid else "  服务进程   未运行")
    print(f"  监听端口   {port} {'已监听' if port_in_use(port) else '未监听'}")
    if port_in_use(port):
        status, payload = http_json(f"http://127.0.0.1:{port}/healthz")
        print(f"  健康检查   HTTP {status} {payload}")
        # /system/providers/dashboard 返回的是扁平结构（summary 在顶层），
        # 这里不假设 data 包装层，两种形态都兼容。
        s2, board = http_json(f"{api_base()}/system/providers/dashboard", timeout=8)
        summ = (board.get("summary") or (board.get("data") or {}).get("summary") or {}) if s2 == 200 else {}
        if summ:
            print(f"  数据源   共 {summ.get('total_providers')} 个，健康 {summ.get('healthy')}，"
                  f"健康率 {summ.get('health_ratio')}")
    db_file = DATA_DIR / "btc.db"
    print(f"  数据库     {db_file} {db_file.stat().st_size/1024:.0f} KB" if db_file.exists()
          else f"  数据库     {db_file}（不存在，需执行 init-db）")
    return 0


# ------------------------------------------------------------------ 自检


def cmd_doctor(_args: argparse.Namespace) -> int:
    title("环境自检")
    problems: list[str] = []

    ver = sys.version_info[:2]
    say(f"Python 版本 {platform.python_version()}{'（建议使用虚拟环境内的解释器）'}"
        if Path(sys.executable).parent.parent != VENV else f"Python {platform.python_version()}",
        "ok" if ver >= (3, 11) else "err")
    if ver < (3, 11):
        problems.append("Python 版本过低，需要 3.11+")

    say(f"虚拟环境 {'存在' if VENV.exists() else '缺失'}", "ok" if VENV.exists() else "warn")
    if not VENV.exists():
        problems.append("未创建虚拟环境，请执行 install")

    say(f"配置文件 {'存在' if ENV_FILE.exists() else '缺失'}", "ok" if ENV_FILE.exists() else "warn")
    if not ENV_FILE.exists():
        problems.append("缺少 .env，正在使用默认配置")

    say(f"数据库文件 {'存在' if (DATA_DIR / 'btc.db').exists() else '缺失'}",
        "ok" if (DATA_DIR / "btc.db").exists() else "warn")
    if not (DATA_DIR / "btc.db").exists():
        problems.append("数据库未初始化，请执行 install 或 btcctl init-db")

    port = server_port()
    say(f"端口 {port} {'已被占用（服务可能已在运行）' if port_in_use(port) else '空闲'}",
        "ok" if port_in_use(port) else "info")
    if port_in_use(port):
        status, payload = http_json(f"http://127.0.0.1:{port}/healthz")
        say(f"健康检查 {'通过' if payload.get('status') == 'ok' else '异常'}",
            "ok" if payload.get("status") == "ok" else "warn")

    py = venv_python()
    if py.exists():
        title("依赖清单校验")
        proc = run([str(py), "-m", "pip", "check"], check=False, quiet=True)
        say("依赖完整" if proc.returncode == 0 else "依赖存在冲突（见上）",
            "ok" if proc.returncode == 0 else "warn")

    title("结论")
    if not problems:
        print("  未发现阻塞性问题，可以正常使用。")
        return 0
    for item in problems:
        print(f"  - {item}")
    return 1


# ------------------------------------------------------------------ 菜单


def _menu_items() -> list[tuple[str, str, list[str]]]:
    return [
        ("查看运行状态", "status", ["status"]),
        ("启动服务（后台）", "start", ["start", "--daemon"]),
        ("停止服务", "stop", ["stop"]),
        ("数据源健康面板", "health", ["health"]),
        ("回填历史数据", "backfill", ["backfill"]),
        ("补齐数据缺口", "gap-repair", ["gap-repair"]),
        ("备份", "backup", ["backup", "--label", "menu"]),
        ("恢复", "restore", ["restore"]),
        ("更新", "update", ["update", "--start"]),
        ("环境自检", "doctor", ["doctor"]),
        ("注册开机自启", "service", ["service", "install"]),
        ("取消开机自启", "service", ["service", "remove"]),
    ]


def cmd_menu(_args: argparse.Namespace) -> int:
    items = _menu_items()
    while True:
        print()
        print("=" * 62)
        print(" BTC 全市场智能研究平台 —— 运维菜单")
        print("=" * 62)
        for i, (label, _cmd, _args2) in enumerate(items, start=1):
            print(f"  {i:>2}. {label}")
        print("   0. 退出")
        try:
            choice = input("\n请选择：").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if choice in ("0", "q", "Q", ""):
            return 0
        if not choice.isdigit() or not (1 <= int(choice) <= len(items)):
            print("  无效选择，请重试。")
            continue
        label, command, extra = items[int(choice) - 1]
        py = venv_python() if venv_python().exists() else Path(sys.executable)
        if command == "restore":
            path = input("请输入备份文件路径：").strip()
            extra = ["restore", "--file", path]
            run([str(py), str(ROOT / "scripts" / "btcctl.py")] + extra, check=False)
            continue
        if command in ("health", "backfill", "gap-repair"):
            run([str(py), str(ROOT / "scripts" / "btcctl.py")] + extra, check=False)
        else:
            run([str(py), str(ROOT / "scripts" / "platformctl.py")] + extra, check=False)
        input("\n按回车键继续…")


# ------------------------------------------------------------------ 入口


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="platformctl", description="BTC 研究平台一键部署控制器")
    p.add_argument("--version", action="version", version="platformctl 1.0.0")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("install", help="安装依赖、生成配置、初始化数据库")
    s.add_argument("--python", default="", help="指定系统 Python 解释器")
    s.add_argument("--upgrade", action="store_true", help="顺带升级依赖到最新版本范围")
    s.add_argument("--backfill", action="store_true", help="安装后立即回填历史数据")
    s.set_defaults(func=cmd_install)

    s = sub.add_parser("update", help="备份 → 更新 → 迁移 → 重启")
    s.add_argument("--no-backup", action="store_true")
    s.add_argument("--start", action="store_true", help="更新完成后自动启动")
    s.set_defaults(func=cmd_update)

    s = sub.add_parser("uninstall", help="停止服务并清理（数据默认保留）")
    s.add_argument("--purge-data", action="store_true", help="连同历史数据与备份一起删除")
    s.add_argument("--yes", action="store_true", help="跳过确认（自动化场景使用）")
    s.set_defaults(func=cmd_uninstall)

    s = sub.add_parser("start", help="启动服务")
    s.add_argument("--daemon", action="store_true", help="后台运行")
    s.set_defaults(func=lambda a: start_service(daemon=a.daemon))

    sub.add_parser("stop", help="停止服务").set_defaults(func=lambda a: stop_service())
    sub.add_parser("restart", help="重启服务").set_defaults(
        func=lambda a: (stop_service(), start_service(daemon=True))[1])
    sub.add_parser("status", help="查看运行状态").set_defaults(func=cmd_status)
    sub.add_parser("doctor", help="环境自检").set_defaults(func=cmd_doctor)

    s = sub.add_parser("backup", help="备份数据库与配置")
    s.add_argument("--label", default="manual")
    s.set_defaults(func=cmd_backup)

    s = sub.add_parser("restore", help="从备份恢复")
    s.add_argument("--file", required=True)
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_restore)

    s = sub.add_parser("service", help="操作系统级常驻注册（Linux: systemd / Windows: 计划任务）")
    s.add_argument("action", choices=["install", "remove", "status"])
    s.set_defaults(func=cmd_service)

    sub.add_parser("menu", help="交互式运维菜单").set_defaults(func=cmd_menu)
    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130
    except subprocess.CalledProcessError as exc:
        print(f"\n子命令失败：{exc}")
        return exc.returncode or 1


if __name__ == "__main__":
    raise SystemExit(main())
