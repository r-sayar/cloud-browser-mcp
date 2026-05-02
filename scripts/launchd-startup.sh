#!/usr/bin/env bash
# Triggered by ~/Library/LaunchAgents/com.cloud-agents.browseros.plist at login.
# Waits for Docker Desktop's daemon to come up, then runs `docker compose up -d`
# in the project dir. Combined with `restart: unless-stopped` in compose, this
# ensures the 3 BrowserOS containers are alive after every laptop reboot, even
# if they were explicitly `down`'d before shutdown.
set -euo pipefail

# Configurable via the plist's EnvironmentVariables block; defaults below.
PROJECT_DIR="${CLOUD_AGENTS_DIR:-$HOME/cloud_agents}"
DOCKER_BIN="${DOCKER_BIN:-/usr/local/bin/docker}"
[ -x "$DOCKER_BIN" ] || DOCKER_BIN=$(/usr/bin/which docker || echo "")
[ -n "$DOCKER_BIN" ] || { echo "[launchd] docker not found in PATH" >&2; exit 1; }

cd "$PROJECT_DIR"
echo "[launchd] $(date -u +%FT%TZ) — waiting for Docker daemon"

# Wait up to 5 minutes for Docker to be ready (Docker Desktop is slow at login)
for _ in $(seq 1 60); do
    if "$DOCKER_BIN" info >/dev/null 2>&1; then
        echo "[launchd] Docker ready, running compose up"
        exec "$DOCKER_BIN" compose up -d
    fi
    sleep 5
done

echo "[launchd] Docker never became ready after 5 min" >&2
exit 1
