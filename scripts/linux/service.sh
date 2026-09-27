#!/usr/bin/env bash
# BTC Intelligence Platform - service (register/unregister OS-level autostart)
# Thin wrapper around scripts/platformctl.py (single cross-platform implementation)
#
#   ./scripts/linux/service.sh install   # 注册 systemd 常驻（需要 sudo）
#   ./scripts/linux/service.sh remove    # 注销
#   ./scripts/linux/service.sh status    # 查看
#
# Linux 下注册 systemd 必须以 root 运行，脚本检测到非 root 会自动加 sudo。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

export PYTHONIOENCODING=utf-8
PY="$ROOT/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  PY="$(command -v python3 || command -v python)"
fi

ACTION="${1:-status}"
if [[ "$(id -u)" -ne 0 ]] && command -v sudo >/dev/null 2>&1; then
  exec sudo "$PY" scripts/platformctl.py service "$ACTION"
fi
exec "$PY" scripts/platformctl.py service "$ACTION"
