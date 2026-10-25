#!/bin/zsh
# Run YT Automation Studio in the background at login on macOS (restarts if it stops, keeps the Mac awake
# while it runs so scheduled videos are made and uploaded).
# Usage:  scripts/install_launch_agent.sh          install + start
#         scripts/install_launch_agent.sh remove   stop + uninstall
set -euo pipefail
cd "$(dirname "$0")/.."
LABEL="com.ytautomation.studio"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ "${1:-}" == "remove" ]]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Removed $LABEL"
  exit 0
fi

[[ -x .venv/bin/python ]] || { echo "Run install/install-mac.sh first."; exit 1; }
mkdir -p "$HOME/Library/LaunchAgents" data/logs
# Online settings (written by scripts/go_public.sh) travel with the service
EXTRA_ENV=""
if [[ -f deploy/public.env ]]; then
  while IFS='=' read -r k v; do
    [[ "$k" =~ ^STUDIO_[A-Z_]+$ ]] && EXTRA_ENV+="<key>$k</key><string>$v</string>"
  done < deploy/public.env
fi
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/caffeinate</string><string>-i</string>
    <string>$PWD/.venv/bin/python</string><string>-m</string><string>studio</string></array>
  <key>WorkingDirectory</key><string>$PWD</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>$EXTRA_ENV</dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$PWD/data/logs/server.out</string>
  <key>StandardErrorPath</key><string>$PWD/data/logs/server.out</string>
</dict></plist>
EOF
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed. The studio runs at login: http://127.0.0.1:${STUDIO_PORT:-8765}"
