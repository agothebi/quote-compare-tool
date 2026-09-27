#!/usr/bin/env bash
# Quote Compare for Mac (and Linux). Double-click in Finder via run.command, or run: ./run.sh
# First start: creates .venv and installs what the app needs. Then: starts the app and opens the browser.
set -u
cd "$(dirname "$0")" || exit 1

pause_and_exit() {
  echo
  read -r -p "Press Return to close this window." _ || true
  exit "${1:-1}"
}

if [ ! -x ".venv/bin/python" ]; then
  PY=""
  for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && \
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      PY="$candidate"; break
    fi
  done
  if [ -z "$PY" ]; then
    echo "Python 3.11 or newer is needed. Install it from python.org (or: brew install python@3.12), then start again."
    pause_and_exit 1
  fi
  echo "Setting up Quote Compare with $("$PY" --version) (first start only)..."
  "$PY" -m venv .venv || { rm -rf .venv; echo "Could not create the Python environment."; pause_and_exit 1; }
fi

.venv/bin/python tools/ensure_deps.py || pause_and_exit 1
PYTHONUNBUFFERED=1 .venv/bin/python -m app.launch "$@" || pause_and_exit 1
