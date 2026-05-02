#!/usr/bin/env bash
# Restore a profile tarball into ./data/profile. Stop the stack first (`docker compose down`).

set -euo pipefail

if [ $# -lt 1 ]; then
  echo "usage: $0 <profile-*.tgz>" >&2
  exit 1
fi

if [ -d ./data/profile ]; then
  echo "refusing to overwrite existing ./data/profile — move or delete it first." >&2
  exit 1
fi

mkdir -p ./data
tar xzf "$1" -C ./data
echo "restored ./data/profile from $1"
