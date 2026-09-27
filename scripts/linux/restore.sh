#!/usr/bin/env bash
# BTC Intelligence Platform - restore
# Thin wrapper around scripts/platformctl.py (single cross-platform implementation)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

export PYTHONIOENCODING=utf-8
PY="$ROOT/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  PY="$(command -v python3 || command -v python)"
fi

exec "$PY" scripts/platformctl.py restore "$@"
