#!/bin/zsh
# Put the studio online at https://<hostname> through a Cloudflare Tunnel running on this Mac.
#   scripts/go_public.sh studio.example.com     go online (run `cloudflared tunnel login` once first)
#   scripts/go_public.sh --off                  back to this-computer-only
# Nothing on your router changes: the tunnel is an outbound connection from this Mac to Cloudflare.
set -euo pipefail
cd "$(dirname "$0")/.."
TUNNEL="yt-studio"
PORT="${STUDIO_PORT:-8765}"
say() { print -P "%F{cyan}==>%f $*"; }

if [[ "${1:-}" == "--off" ]]; then
  launchctl bootout "gui/$(id -u)/com.cloudflare.cloudflared" 2>/dev/null || true
  rm -f deploy/public.env
  scripts/install_launch_agent.sh
  say "Offline: the studio is only on http://127.0.0.1:$PORT again (the tunnel is stopped, not deleted)."
  exit 0
fi

HOST="${1:-}"
[[ "$HOST" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]] || {
  echo "Usage: scripts/go_public.sh studio.yourdomain.com"; exit 1; }
command -v cloudflared >/dev/null || { echo "Install it first: brew install cloudflared"; exit 1; }
[[ -f "$HOME/.cloudflared/cert.pem" ]] || { echo "Sign in to Cloudflare first: cloudflared tunnel login"; exit 1; }

# 1. the tunnel (created once, reused afterwards)
if ! cloudflared tunnel list -o json | grep -q "\"name\":\"$TUNNEL\""; then
  say "Creating tunnel '$TUNNEL'…"
  cloudflared tunnel create "$TUNNEL"
fi
TID=$(cloudflared tunnel list -o json | python3 -c "import json,sys; print(next(t['id'] for t in json.load(sys.stdin) if t['name']=='$TUNNEL'))")

# 2. https://HOST → this tunnel (a DNS record in your Cloudflare zone)
say "Pointing https://$HOST at the tunnel…"
cloudflared tunnel route dns --overwrite-dns "$TUNNEL" "$HOST"

# 3. tunnel config: only the studio, nothing else on this Mac
mkdir -p "$HOME/.cloudflared"
cat > "$HOME/.cloudflared/config.yml" <<YML
tunnel: $TID
credentials-file: $HOME/.cloudflared/$TID.json
ingress:
  - hostname: $HOST
    service: http://127.0.0.1:$PORT
  - service: http_status:404
YML

# 4. the studio learns its public address (YouTube sign-in, secure cookies, real visitor IPs)
cat > deploy/public.env <<ENV
STUDIO_PUBLIC_URL=https://$HOST
STUDIO_TRUST_PROXY=1
ENV
scripts/install_launch_agent.sh

# 5. tunnel as a login service on this Mac (restarts by itself)
if [[ ! -f "$HOME/Library/LaunchAgents/com.cloudflare.cloudflared.plist" ]]; then
  cloudflared service install
else
  launchctl kickstart -k "gui/$(id -u)/com.cloudflare.cloudflared" 2>/dev/null || true
fi

say "Online: https://$HOST"
say "Last step (Google): add  https://$HOST/oauth/google/callback  as an Authorized redirect URI of your OAuth client."
