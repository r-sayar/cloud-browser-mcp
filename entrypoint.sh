#!/bin/sh
# Bring up Xvfb → x11vnc → noVNC websockify → BrowserOS, then start two
# socat forwarders to expose BrowserOS's loopback-bound MCP and CDP on
# 0.0.0.0 of the container so Docker's port-forward can reach them.
set -eu

PROFILE_DIR="${PROFILE_DIR:-/data/profile}"
CONFIG_FILE="$PROFILE_DIR/.browseros/server_config.json"
mkdir -p "$PROFILE_DIR" /var/run/dbus /tmp/.X11-unix
chmod 1777 /tmp/.X11-unix || true

# 1. DBus session (BrowserOS expects one to exist)
if [ ! -e /var/run/dbus/pid ]; then
    dbus-daemon --system --fork >/dev/null 2>&1 || true
fi
eval "$(dbus-launch --sh-syntax)"
export DBUS_SESSION_BUS_ADDRESS DBUS_SESSION_BUS_PID

# 2. Xvfb on :10
Xvfb :10 -screen 0 1920x1080x24 -ac +extension RANDR +render -noreset \
    >/var/log/xvfb.log 2>&1 &
for _ in 1 2 3 4 5 6 7 8 9 10; do
    [ -e /tmp/.X11-unix/X10 ] && break
    sleep 0.3
done

# 3. x11vnc + noVNC for the live-view (http://localhost:6080/)
x11vnc -display :10 -forever -shared -rfbport 5900 -nopw -quiet \
    >/var/log/x11vnc.log 2>&1 &
websockify --web=/usr/share/novnc 6080 localhost:5900 \
    >/var/log/novnc.log 2>&1 &

# 4. Clear stale Chromium profile lock if a previous run was hard-killed
rm -f "$PROFILE_DIR"/Singleton* 2>/dev/null || true

# 4a. Pre-seed BrowserOS server config with pinned ports + remote-MCP flag.
#     If BrowserOS reads this on startup we skip the port-discovery dance
#     entirely. If it overwrites it, the socat fallback below still rescues us.
mkdir -p "$PROFILE_DIR/.browseros"
if [ ! -f "$CONFIG_FILE" ]; then
    cat > "$CONFIG_FILE" <<'JSON'
{"flags":{"allow_remote_in_mcp":true},"ports":{"cdp":9011,"extension":9300,"server":9200}}
JSON
fi

# 5. Background loop that watches for server_config.json (BrowserOS writes it
#    on startup), reads the actual MCP + CDP ports it picked, and starts socat
#    to expose them on 0.0.0.0 of the container.
(
    echo "[port-forwarder] waiting for $CONFIG_FILE"
    for _ in $(seq 1 60); do
        [ -f "$CONFIG_FILE" ] && break
        sleep 1
    done
    if [ ! -f "$CONFIG_FILE" ]; then
        echo "[port-forwarder] config file never appeared; giving up" >&2
        exit 1
    fi
    MCP_PORT=$(jq -r '.ports.server' "$CONFIG_FILE")
    CDP_PORT=$(jq -r '.ports.cdp'    "$CONFIG_FILE")
    echo "[port-forwarder] BrowserOS picked MCP=$MCP_PORT CDP=$CDP_PORT — forwarding 9200 and 9011"

    # Wait for the actual sockets to be live before starting socat
    for _ in $(seq 1 60); do
        if (echo > /dev/tcp/127.0.0.1/"$MCP_PORT") 2>/dev/null; then break; fi
        sleep 1
    done

    # Retry-loop both forwarders. BrowserOS sometimes briefly grabs the same
    # port we want during its boot dance; we wait it out.
    ( while true; do
        socat TCP-LISTEN:9200,bind=0.0.0.0,reuseaddr,fork TCP:127.0.0.1:"$MCP_PORT" \
            >>/var/log/socat-mcp.log 2>&1
        sleep 2
      done ) &
    ( while true; do
        socat TCP-LISTEN:9011,bind=0.0.0.0,reuseaddr,fork TCP:127.0.0.1:"$CDP_PORT" \
            >>/var/log/socat-cdp.log 2>&1
        sleep 2
      done ) &
    wait
) &

# 6. BrowserOS as PID-1's primary child
echo "[entrypoint] launching BrowserOS with profile $PROFILE_DIR"
exec /opt/browseros/AppRun \
    --no-sandbox \
    --disable-dev-shm-usage \
    --user-data-dir="$PROFILE_DIR" \
    --no-first-run \
    --no-default-browser-check \
    --start-maximized
