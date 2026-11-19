#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# OR-Tools (the solver) only ships for 64-bit Python 3.10 to 3.14, so any
# other Python fails at "pip install". Find one that fits.
CHECK='import sys, struct; sys.exit(0 if (3, 10) <= sys.version_info[:2] <= (3, 14) and struct.calcsize("P") == 8 else 1)'

PY=""
for candidate in python3.13 python3.12 python3.11 python3.14 python3.10 python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c "$CHECK" 2>/dev/null; then
    PY="$candidate"
    break
  fi
done
if [ -z "$PY" ]; then
  echo
  echo "No supported Python was found."
  echo
  echo "The solver needs 64-bit Python 3.10 to 3.14. OR-Tools, the solver library,"
  echo "has no build for Python 3.15 or newer yet."
  echo "Install Python 3.13 from https://www.python.org/downloads/ (or your package"
  echo "manager, e.g. 'brew install python@3.13') and run this again."
  echo
  exit 1
fi

VPY=".venv/bin/python"
# A virtual environment made earlier with an unsupported Python can never
# install the solver: start over with the one found above.
if [ -x "$VPY" ] && ! "$VPY" -c "$CHECK" 2>/dev/null; then
  echo "Rebuilding the virtual environment with a supported Python..."
  rm -rf ./.venv
fi

if [ ! -x "$VPY" ]; then
  echo "Creating virtual environment..."
  "$PY" -m venv .venv
fi

if [ ! -f ".venv/requirements.installed" ] || ! cmp -s requirements.txt .venv/requirements.installed; then
  echo "Installing dependencies (first run only, this can take a minute)..."
  "$VPY" -m pip install --upgrade pip >/dev/null 2>&1 || true
  "$VPY" -m pip install -r requirements.txt
  cp requirements.txt .venv/requirements.installed
fi

exec "$VPY" server.py
