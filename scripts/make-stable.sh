#!/usr/bin/env bash
# One-shot installer that makes the cloud_agents stack survive Mac reboots:
#   1. Sets Docker Desktop's AutoStart=true so the daemon comes back at login.
#   2. Templates the LaunchAgent plist with this repo's path and loads it via
#      launchctl, so `docker compose up -d` runs at every login.
# Combined with `restart: unless-stopped` in docker-compose.yml, the three
# BrowserOS containers are alive immediately after every reboot.
#
# Idempotent — safe to re-run.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST_SRC="$REPO_DIR/launchd/com.cloud-agents.browseros.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.cloud-agents.browseros.plist"
DOCKER_SETTINGS="$HOME/Library/Group Containers/group.com.docker/settings-store.json"

echo "[make-stable] repo dir: $REPO_DIR"

# 1. Docker Desktop autostart
if [ -f "$DOCKER_SETTINGS" ]; then
    cp "$DOCKER_SETTINGS" "${DOCKER_SETTINGS}.backup-$(date +%s)"
    /usr/bin/python3 - <<PY
import json
p = "$DOCKER_SETTINGS"
d = json.load(open(p))
d["AutoStart"] = True
json.dump(d, open(p, "w"), indent=2)
print("[make-stable] Docker Desktop AutoStart =", json.load(open(p))["AutoStart"])
PY
else
    echo "[make-stable] WARNING: Docker Desktop settings file not found at $DOCKER_SETTINGS"
    echo "[make-stable]          Manually flip 'Start Docker Desktop when you log in' in Docker Desktop preferences."
fi

# 2. LaunchAgent
mkdir -p "$HOME/Library/LaunchAgents"
sed "s|__PROJECT_DIR__|$REPO_DIR|g" "$PLIST_SRC" > "$PLIST_DST"
plutil -lint "$PLIST_DST" >/dev/null
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load   "$PLIST_DST"
echo "[make-stable] LaunchAgent installed:"
launchctl list | grep cloud-agents || true

cat <<EOF

✓ done.
  - Docker Desktop will start at login.
  - LaunchAgent will run \`docker compose up -d\` once the daemon is ready.
  - Containers themselves have \`restart: unless-stopped\`.

Logs:  tail -f /tmp/cloud_agents.{out,err}.log
Undo:  launchctl unload $PLIST_DST && rm $PLIST_DST
EOF
