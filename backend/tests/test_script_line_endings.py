"""运维脚本的格式守卫。

这条测试存在的理由很实在：仓库里所有 ``.bat`` 曾经被 Git Bash 的 heredoc 写成 **LF**，
于是用户双击 ``一键启动.bat`` 时看到的是：

    'tle' 不是内部或外部命令，也不是可运行的程序
    '/d' 不是内部或外部命令，也不是可运行的程序
    [ERROR] Bundled Python runtime not found: runtime\\python.exe

``cmd.exe`` 会把纯 LF 的批处理当成一坨粘在一起的文本来解析，``title`` 被啃成 ``tle``、
``cd /d`` 啃成 ``/d`` —— 结果磁盘目录没切换到脚本所在目录，相对路径自然找不到。
反过来，Linux 的 ``.sh`` 如果变成 CRLF，shebang 行会多一个 ``\\r``，
``#!/usr/bin/env bash\\r`` 这样的解释器路径是不存在的，脚本照样跑不起来。

两边方向相反，同一个坑，所以一条测试同时看住：
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WINDOWS_SCRIPTS = REPO_ROOT / "scripts" / "windows"
LINUX_SCRIPTS = REPO_ROOT / "scripts" / "linux"


def _bat_files() -> list[Path]:
    if not WINDOWS_SCRIPTS.is_dir():  # 精简发行缺 scripts/ 时不该让测试崩
        return []
    return sorted(WINDOWS_SCRIPTS.glob("*.bat")) + sorted(WINDOWS_SCRIPTS.glob("*.cmd"))


def _sh_files() -> list[Path]:
    if not LINUX_SCRIPTS.is_dir():
        return []
    return sorted(LINUX_SCRIPTS.glob("*.sh"))


@pytest.mark.parametrize("path", _bat_files(), ids=lambda p: p.name)
def test_windows_bat_uses_crlf(path) -> None:
    """Windows 批处理必须是 CRLF —— LF 会让 cmd.exe 把行粘到一块，
    表现为一堆『xxx 不是内部或外部命令』的半截命令。"""
    raw = path.read_bytes()
    lines = raw.replace(b"\r\n", b"\n").count(b"\n")
    crlf = raw.count(b"\r\n")
    assert lines > 0, f"{path.name} 是空文件"
    assert crlf == lines, (
        f"{path.name} 的换行符不是纯 CRLF：共 {lines} 行，其中 {crlf} 行是 CRLF。"
        "用 LF 保存的 .bat 在 cmd.exe 里会被错误解析"
    )


@pytest.mark.parametrize("path", _bat_files(), ids=lambda p: p.name)
def test_windows_bat_is_ascii(path) -> None:
    """批处理保持纯 ASCII。

    中文一行都不要放进 .bat：UTF-8 中文 + ``chcp 65001`` 在不同版本 cmd 上的表现并不一致，
    最坏情况是多字节字符被截断后把后面的命令也带崩。中文说明一律放在 UTF-8 的 .txt / .md 里。
    """
    raw = path.read_bytes()
    bad = sorted({b for b in raw if b > 127})
    assert not bad, f"{path.name} 含非 ASCII 字节 {bad[:8]}，请把中文挪出批处理文件"


@pytest.mark.parametrize("path", _sh_files(), ids=lambda p: p.name)
def test_linux_sh_uses_lf(path) -> None:
    """Linux 脚本必须是 LF —— CRLF 会让 shebang 带上 \\r，解释器路径直接失效。"""
    raw = path.read_bytes()
    assert raw.count(b"\r") == 0, f"{path.name} 含 CR 字节，Linux 下 shebang 会失效"
    assert raw.startswith(b"#!"), f"{path.name} 缺少 shebang"


@pytest.mark.parametrize("path", _sh_files(), ids=lambda p: p.name)
def test_linux_sh_resolves_root_from_itself(path) -> None:
    """`.sh` 必须用 ``BASH_SOURCE`` 推出仓库根目录，与 .bat 的 ``%~dp0`` 同理。"""
    text = path.read_text(encoding="utf-8")
    assert "BASH_SOURCE" in text, f"{path.name} 没有用 BASH_SOURCE 定位脚本目录"
    assert "set -euo pipefail" in text, f"{path.name} 缺少 set -euo pipefail，出错会静默继续"


def test_windows_start_wrappers_exist() -> None:
    """start / stop 这两个最常见的入口不能消失 —— 文档和 README 都在引用它们。"""
    for name in ("start.bat", "stop.bat", "install.bat", "doctor.bat"):
        assert (WINDOWS_SCRIPTS / name).exists(), f"缺少运维入口 {name}"


def test_wrappers_resolve_paths_from_script_location() -> None:
    """`.bat` 必须用 ``%~dp0`` 定位自己的目录，不能依赖启动时的当前工作目录。

    双击运行时的当前目录不一定是脚本所在目录（快捷方式、资源管理器、计划任务都可能不同），
    一旦依赖 cwd，就会出现和本次事故同类型的『找不到 python.exe』。
    """
    for path in _bat_files():
        text = path.read_text(encoding="ascii", errors="replace")
        assert "%~dp0" in text, f"{path.name} 没有用 %~dp0 定位脚本目录"
