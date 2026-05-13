#!/usr/bin/env bash
# Triggered by ~/Library/LaunchAgents/com.cloud-agents.browseros.plist at login.
# Starts Colima (headless Docker VM), waits for the daemon, then runs
# `docker compose up -d` for the 3 BrowserOS containers.
set -euo pipefail

# Configurable via the plist's EnvironmentVariables block; defaults below.
PROJECT_DIR="${CLOUD_AGENTS_DIR:-$HOME/cloud_agents}"
DOCKER_BIN="${DOCKER_BIN:-/usr/local/bin/docker}"
COLIMA_BIN="${COLIMA_BIN:-/usr/local/bin/colima}"
[ -x "$DOCKER_BIN" ] || DOCKER_BIN=$(/usr/bin/which docker || echo "")
[ -n "$DOCKER_BIN" ] || { echo "[launchd] docker not found in PATH" >&2; exit 1; }

echo "[launchd] $(date -u +%FT%TZ) — starting Colima"
# colima start is idempotent: exits 0 if already running
"$COLIMA_BIN" start 2>&1 || true

cd "$PROJECT_DIR"
echo "[launchd] $(date -u +%FT%TZ) — waiting for Docker daemon"

# Wait up to 3 minutes for Docker to be ready
for _ in $(seq 1 36); do
    if "$DOCKER_BIN" info >/dev/null 2>&1; then
        echo "[launchd] Docker ready, running compose up"
        exec "$DOCKER_BIN" compose up -d
    fi
    sleep 5
done

echo "[launchd] Docker never became ready after 3 min" >&2
exit 1
