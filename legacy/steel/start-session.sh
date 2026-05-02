#!/usr/bin/env bash
# Launch a Steel session that uses the persistent profile and loads the Claude in Chrome extension.
# Prints the session ID and a live-view URL you can open in your laptop browser.

set -euo pipefail

API="${STEEL_API:-http://localhost:3000}"
UI="${STEEL_UI:-http://localhost:5173}"

EXTENSIONS_JSON='[]'
if [ -f "./extensions/claude-in-chrome/manifest.json" ]; then
  EXTENSIONS_JSON='["claude-in-chrome"]'
else
  echo "warn: ./extensions/claude-in-chrome/manifest.json not found — starting WITHOUT the Claude extension." >&2
  echo "      Drop the unpacked extension into ./extensions/claude-in-chrome/ and re-run." >&2
fi

RESP=$(curl -fsS -X POST "$API/v1/sessions" \
  -H "Content-Type: application/json" \
  -d "{
    \"userDataDir\": \"/data/profile\",
    \"persist\": true,
    \"headless\": false,
    \"extensions\": $EXTENSIONS_JSON,
    \"dimensions\": {\"width\": 1280, \"height\": 800}
  }")

SESSION_ID=$(printf '%s' "$RESP" | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')

echo "session id:  $SESSION_ID"
echo "live view:   $UI/sessions/$SESSION_ID"
echo "cdp:         ws://localhost:9223/devtools/browser"
