#!/usr/bin/env bash
# Deploy cloud-browser-mcp to a freshly-provisioned Hetzner CPX31.
#
# Usage:   ./cloud/deploy.sh <tailnet-host-or-ip>
# Example: ./cloud/deploy.sh cbm-server
#          ./cloud/deploy.sh 100.64.0.5
#
# Prereqs: cloud-init has finished on the server (Tailscale up, Docker installed,
# user `cbm` exists with your SSH key). Verify with: `tailscale status`.

set -euo pipefail

if [ $# -lt 1 ]; then
  echo "Usage: $0 <tailnet-host-or-ip> [--with-data]"
  echo
  echo "  --with-data   also rsync ./data/{1,2,3} (logged-in profile state, ~MB-GB)"
  echo "                without this flag, the cloud server starts with fresh profiles."
  exit 2
fi

HOST="$1"
shift || true
WITH_DATA=0
for arg in "$@"; do
  case "$arg" in
    --with-data) WITH_DATA=1 ;;
    *) echo "unknown arg: $arg"; exit 2 ;;
  esac
done

REPO="$(cd "$(dirname "$0")/.." && pwd)"
USER_HOST="cbm@${HOST}"
DEST="/opt/cloud-browser-mcp"

echo "==> rsyncing code + AppImage to $USER_HOST:$DEST"
rsync -av --delete \
  --exclude='.git/' \
  --exclude='browseros/' \
  --exclude='legacy/' \
  --exclude='node_modules/' \
  --exclude='__pycache__/' \
  --exclude='data/' \
  --exclude='.claude/' \
  --exclude='cloud/' \
  "$REPO/" "$USER_HOST:$DEST/"

if [ "$WITH_DATA" -eq 1 ]; then
  echo "==> rsyncing ./data (profile state — this preserves logged-in sessions)"
  rsync -av \
    --exclude='Singleton*' \
    --exclude='*.lock' \
    --exclude='downloads/' \
    "$REPO/data/" "$USER_HOST:$DEST/data/"
fi

echo "==> building + starting containers on remote"
ssh "$USER_HOST" "cd $DEST && docker compose up -d --build"

echo "==> waiting for containers to be healthy"
ssh "$USER_HOST" "cd $DEST && docker compose ps"

cat <<EOF

Done. Reach the browsers via your tailnet:

  noVNC live-view:
    http://$HOST:6081/?scale=true   (browser 1)
    http://$HOST:6082/?scale=true   (browser 2)
    http://$HOST:6083/?scale=true   (browser 3)

  Update Claude Desktop MCP connectors to point to:
    http://$HOST:9201   (browser 1 MCP)
    http://$HOST:9202   (browser 2 MCP)
    http://$HOST:9203   (browser 3 MCP)

  Update dashboard.html: change 'localhost' to '$HOST' (or open it as
  ./dashboard.html?host=$HOST once the host param is wired in).

EOF
