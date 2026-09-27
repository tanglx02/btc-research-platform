#!/usr/bin/env bash
# BTC Intelligence Platform - one-click installer (Linux)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
export PYTHONIOENCODING=utf-8
PY="$(command -v python3 || command -v python)"
if [[ -z "$PY" ]]; then
  echo "未找到 python3，请先安装 Python 3.11+ (apt install python3 python3-venv)"
  exit 2
fi
exec "$PY" scripts/platformctl.py install "$@"
