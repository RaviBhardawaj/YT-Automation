#!/bin/zsh
# One-step installer for macOS (Apple Silicon or Intel).
#   install/install-mac.sh             install everything, then start the studio
#   install/install-mac.sh --service   also start it automatically at login (keeps running in the background)
# Safe to run again: it only installs what is missing.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"

say() { print -P "%F{cyan}==>%f $*"; }

# 1. Homebrew (package manager) ------------------------------------------------------------
if ! command -v brew >/dev/null; then
  for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [[ -x $b ]] && eval "$($b shellenv)"; done
fi
if ! command -v brew >/dev/null; then
  say "Installing Homebrew (asks for your Mac password)…"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [[ -x $b ]] && eval "$($b shellenv)"; done
fi

# 2. System tools: Python 3.12, FFmpeg (video), libraqm (correct Hindi/Devanagari text) -----
for pkg in python@3.12 ffmpeg libraqm; do
  if brew list --versions "$pkg" >/dev/null; then say "$pkg already installed"
  else say "Installing $pkg…"; brew install "$pkg"; fi
done
PY="$(brew --prefix python@3.12)/bin/python3.12"

# 3. Python environment ------------------------------------------------------------------------
if [[ ! -x .venv/bin/python ]]; then
  say "Creating the Python environment…"
  "$PY" -m venv .venv
fi
say "Installing Python packages (first time takes a few minutes)…"
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt

# 4. Keys copied from another computer (optional) -----------------------------------------------
if [[ -f secrets/api_keys.env ]]; then
  say "Found secrets/api_keys.env — loading keys into the macOS Keychain…"
  .venv/bin/python scripts/import_keys.py
fi

# 5. Check -----------------------------------------------------------------------------------------
.venv/bin/python - <<'EOF'
from studio import bootstrap, config
bootstrap.init()
print(f"Studio ready. Data folder: {config.DATA_DIR}")
EOF

if [[ "${1:-}" == "--service" ]]; then
  say "Starting the studio at login (background service)…"
  scripts/install_launch_agent.sh
  sleep 3
  open "http://127.0.0.1:${STUDIO_PORT:-8765}"
else
  say "Done. Start it any time with:  ./run.sh"
  say "Keep it running in the background at login:  install/install-mac.sh --service"
fi
