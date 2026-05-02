#!/usr/bin/env bash
# Release a Steel session by ID. Releasing flushes profile state to /data/profile.

set -euo pipefail

API="${STEEL_API:-http://localhost:3000}"

if [ $# -lt 1 ]; then
  echo "usage: $0 <session-id>" >&2
  exit 1
fi

curl -fsS -X POST "$API/v1/sessions/$1/release" -H "Content-Type: application/json" -d '{}'
echo
