#!/bin/bash
# Quote Compare for Mac. Double-click this file, or run ./start.command in Terminal.
# The first start installs what the app needs (a few minutes); later starts take seconds.
cd "$(dirname "$0")" || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"  # Homebrew, also when started by a double-click

fail() {
  echo
  echo "$1"
  read -r -p "Press Return to close this window." _
  exit 1
}

# A .venv that no longer runs (the folder was moved, Python reinstalled) is made again.
if [ -e .venv ] && ! .venv/bin/python -c "import sys" >/dev/null 2>&1; then rm -rf .venv; fi
if [ ! -x .venv/bin/python ]; then
  command -v brew >/dev/null || fail "Homebrew is needed first. See README.md, step 2."
  echo "Setting up Quote Compare (first start only, a few minutes)..."
  brew install python@3.13 tesseract || fail "The setup did not finish. Check the internet connection and start again."
  "$(brew --prefix python@3.13)/bin/python3.13" -m venv .venv || fail "Python could not be set up. Start again."
fi
.venv/bin/python app/ensure_deps.py || fail "The setup did not finish. Check the internet connection and start again."
.venv/bin/python -m app.launch "$@" || fail "Quote Compare stopped with the message above."
