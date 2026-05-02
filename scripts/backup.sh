#!/usr/bin/env bash
# Snapshot the persistent Chrome profile to a tarball.
# Stop any running session first (./scripts/stop-session.sh <id>) so cookies are flushed to disk.

set -euo pipefail

OUT="profile-$(date -u +%Y%m%dT%H%M%SZ).tgz"
tar czf "$OUT" -C ./data profile
echo "wrote $OUT ($(du -h "$OUT" | cut -f1))"
