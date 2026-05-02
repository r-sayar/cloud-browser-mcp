#!/bin/sh
# Wrapper around Steel's entrypoint that starts Xvfb on :10 first, so Chromium can run headed.
set -e

if ! pgrep -x Xvfb >/dev/null 2>&1; then
  Xvfb :10 -screen 0 1920x1080x24 -ac +extension RANDR +render -noreset >/var/log/xvfb.log 2>&1 &
  for i in 1 2 3 4 5 6 7 8 9 10; do
    if [ -e /tmp/.X11-unix/X10 ]; then break; fi
    sleep 0.5
  done
fi

export DISPLAY=:10
exec /app/api/entrypoint.sh "$@"
