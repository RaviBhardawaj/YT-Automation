#!/bin/zsh
# Start YT Automation Studio on http://127.0.0.1:8765 (creates the virtualenv on first run).
set -euo pipefail
cd "$(dirname "$0")"

PY=/opt/homebrew/bin/python3.12
[[ -x "$PY" ]] || PY=$(command -v python3.12 || true)
if [[ -z "$PY" ]]; then
  echo "Python 3.12 is required: brew install python@3.12"; exit 1
fi
command -v ffmpeg >/dev/null || { echo "FFmpeg is required: brew install ffmpeg"; exit 1; }
[[ -e /opt/homebrew/lib/libraqm.dylib ]] || echo "Note: for correct Hindi text rendering run: brew install libraqm"

if [[ ! -x .venv/bin/python ]]; then
  echo "Creating virtual environment…"
  "$PY" -m venv .venv
  .venv/bin/python -m pip install --quiet --upgrade pip
fi
# `python -m pip` (not .venv/bin/pip) keeps working if the project folder is moved
.venv/bin/python -m pip install --quiet -r requirements.txt

PORT="${STUDIO_PORT:-8765}"
( sleep 2; [[ "${STUDIO_NO_BROWSER:-}" == "1" ]] || open "http://127.0.0.1:${PORT}" ) &
exec .venv/bin/python -m studio
