#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$candidate"
    break
  fi
done
if [ -z "$PY" ]; then
  echo
  echo "Python 3.10 or newer was not found."
  echo "Install it from https://www.python.org/downloads/ (or your package manager) and run this again."
  echo
  exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
  echo "Creating virtual environment..."
  "$PY" -m venv .venv
fi
VPY=".venv/bin/python"

if [ ! -f ".venv/requirements.installed" ] || ! cmp -s requirements.txt .venv/requirements.installed; then
  echo "Installing dependencies (first run only, this can take a minute)..."
  "$VPY" -m pip install --upgrade pip >/dev/null 2>&1 || true
  "$VPY" -m pip install -r requirements.txt
  cp requirements.txt .venv/requirements.installed
fi

exec "$VPY" server.py
