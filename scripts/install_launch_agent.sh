#!/bin/zsh
# Optional: run YT Automation Studio in the background at login (macOS LaunchAgent).
# Usage:  scripts/install_launch_agent.sh          install + start
#         scripts/install_launch_agent.sh remove   stop + uninstall
set -euo pipefail
cd "$(dirname "$0")/.."
LABEL="local.ytautomationstudio"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ "${1:-}" == "remove" ]]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Removed $LABEL"
  exit 0
fi

[[ -x .venv/bin/python ]] || { echo "Run ./run.sh once first to create the virtualenv."; exit 1; }
mkdir -p "$HOME/Library/LaunchAgents" data/logs
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$PWD/.venv/bin/python</string><string>-m</string><string>studio</string></array>
  <key>WorkingDirectory</key><string>$PWD</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$PWD/data/logs/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$PWD/data/logs/launchd.err.log</string>
</dict></plist>
EOF
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed. The studio now runs at login: http://127.0.0.1:8765"
