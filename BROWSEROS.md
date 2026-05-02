# BrowserOS in the cloud — architecture (no BrowserOS code changes)

## Goal

Replace the Steel + Chromium pair in this repo with **BrowserOS** (the open-source
agent-browser at `browseros-ai/BrowserOS`), running per-user in the cloud, and
exposing each instance's built-in MCP server (`/mcp` on port 9239) to a user's
**local Claude Desktop** — so the agent runs in the cloud but is driven from
the user's laptop.

## Verified facts (from probing a running BrowserOS 0.44.0.1)

- The MCP server is at **`http://127.0.0.1:9200/mcp`**, not 9239. `9200` is
  the `server-port`; the other ports BrowserOS opens are CDP (`9011`) and
  extension-bridge (`9300`).
- Transport is **Streamable HTTP** — `GET /mcp` returns plain JSON
  `{"status":"ok","message":"MCP server is running. Use POST to interact."}`.
  No SSE-specific config needed; standard reverse-proxy with buffering off
  is enough.
- Loopback-only is enforced by a flag in `server_config.json`:
  `flags.allow_remote_in_mcp: false`. **This is a JSON config toggle, not
  source code.** Flipping it to `true` lets the server accept non-loopback
  connections.
- BrowserOS is Chromium 146.0.7821.31 under the hood, so the standard
  `--user-data-dir=` flag works for the profile path.

## Constraints

1. **MCP loopback** — sidesteppable via `allow_remote_in_mcp: true` (config
   change, no code) *or* via a `socat` sidecar (zero changes at all).
2. **Single browser per MCP.** Each BrowserOS process owns port 9200 and
   exposes exactly one browser session through it. Solved by one container
   per browser instance.
3. **Constraint on us:** no patches to BrowserOS source.

## Solution sketch

Three layers, all of which sit *outside* BrowserOS:

```
laptop                                        cloud host
┌─────────────────────┐    HTTPS              ┌────────────────────────────┐
│ Claude Desktop      │  + bearer token       │ Caddy (reverse proxy + TLS)│
│   mcpServers:       │ ────────────────────► │  /u/alice/mcp → alice:9240 │
│     browseros-cloud │                       │  /u/bob/mcp   → bob:9240   │
│       url: https... │                       └────────────┬───────────────┘
└─────────────────────┘                                    │
                                       ┌───────────────────┴──────────────┐
                                       ▼                                  ▼
                            ┌──────────────────────┐         ┌──────────────────────┐
                            │ container "alice"    │         │ container "bob"      │
                            │  Xvfb :10            │         │  Xvfb :10            │
                            │  BrowserOS           │         │  BrowserOS           │
                            │   ↳ 127.0.0.1:9239   │         │   ↳ 127.0.0.1:9239   │
                            │  socat               │         │  socat               │
                            │   0.0.0.0:9240 →     │         │   0.0.0.0:9240 →     │
                            │     127.0.0.1:9239   │         │     127.0.0.1:9239   │
                            │  noVNC (live-view)   │         │  noVNC               │
                            │  /data ← volume      │         │  /data ← volume      │
                            └──────────────────────┘         └──────────────────────┘
```

### Two ways to break out of loopback (pick one)

**Option A — flip the config flag (recommended):** mount our own
`server_config.json` into the container with `flags.allow_remote_in_mcp: true`
and `ports.server: 9200`. BrowserOS itself binds non-loopback. No sidecar.

**Option B — `socat` sidecar (strictest "no changes" reading):** keep the
stock config and run inside the container:
```bash
socat TCP-LISTEN:9240,bind=0.0.0.0,reuseaddr,fork TCP:127.0.0.1:9200
```
BrowserOS still binds loopback (untouched). `socat` lives in the same network
namespace and forwards. Same end result, one extra process.

Either way, the single-browser-per-MCP limit is sidestepped by **one
container per browser**: each container gets its own loopback, its own port
9200, and its own externally-exposed port.

### Per-container Dockerfile (rough)

We have `~/Downloads/BrowserOS.AppImage` (286 MB) — drop it into the image,
make it executable, and launch it under Xvfb:

```dockerfile
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y \
    xvfb x11vnc novnc websockify socat fuse libfuse2 \
    libnss3 libgtk-3-0 libxss1 libasound2 libgbm1 \
    fonts-liberation ca-certificates curl
# Drop in the AppImage
COPY BrowserOS.AppImage /opt/browseros.AppImage
RUN chmod +x /opt/browseros.AppImage && \
    /opt/browseros.AppImage --appimage-extract && \
    mv squashfs-root /opt/browseros && \
    rm /opt/browseros.AppImage
COPY entrypoint.sh /entrypoint.sh
COPY server_config.json /opt/server_config.json   # has allow_remote_in_mcp=true
EXPOSE 9200 6080            # MCP + noVNC
VOLUME /data                # BrowserOS profile
ENTRYPOINT ["/entrypoint.sh"]
```

`entrypoint.sh`:
1. `Xvfb :10 -screen 0 1920x1080x24 &`
2. `x11vnc -display :10 -forever -nopw &` and `websockify --web=/usr/share/novnc 6080 localhost:5900 &`
3. Copy our `server_config.json` into `/data/profile/.browseros/server_config.json` if it isn't there
4. `DISPLAY=:10 /opt/browseros/AppRun --user-data-dir=/data/profile`

(If you go with Option B instead, drop step 3 and add
`socat TCP-LISTEN:9240,bind=0.0.0.0,reuseaddr,fork TCP:127.0.0.1:9200 &`
between 2 and 4 — and expose 9240 instead of 9200.)

### Caddy on the host

```caddyfile
browseros.example.com {
    @alice path /u/alice/*
    handle @alice {
        uri strip_prefix /u/alice
        reverse_proxy alice-container:9240 {
            transport http {
                response_header_timeout 0     # MCP/SSE: no timeout
                read_buffer 64KB
            }
            header_up Authorization {http.request.header.Authorization}
        }
    }

    @bob path /u/bob/*
    handle @bob { ... }   # same shape

    # bearer-token auth: rely on Anthropic's MCP transport headers
    # OR put Cloudflare Access / Tailscale Funnel in front of the whole thing.
}
```

### Local Claude Desktop config

`~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "browseros-cloud": {
      "url": "https://browseros.example.com/u/alice/mcp",
      "headers": { "Authorization": "Bearer <per-user-token>" }
    }
  }
}
```

Multiple browsers per user? Just add more entries:

```json
{
  "mcpServers": {
    "browser-personal": { "url": "https://browseros.example.com/u/alice-personal/mcp", "headers": {"Authorization": "Bearer ..."} },
    "browser-work":     { "url": "https://browseros.example.com/u/alice-work/mcp",     "headers": {"Authorization": "Bearer ..."} }
  }
}
```

Each entry → its own container → its own BrowserOS → its own profile.

## Open items remaining (small)

1. **AppImage in Docker requires FUSE.** `libfuse2` is in the Dockerfile, and
   we extract the AppImage rather than running it directly to avoid FUSE
   inside containers. Tested pattern.
2. **`allow_remote_in_mcp` semantics.** Setting it to `true` is documented
   here as inferred from the flag name. Worth confirming the flag actually
   makes the server bind 0.0.0.0 (vs. just allowing remote *origins* on a
   loopback bind). If only the latter, fall back to Option B (socat).

## Why not just SSH-tunnel from the laptop?

Could work for one user, breaks for many. Path-based reverse proxy gives:
- one stable HTTPS URL per user
- TLS termination + auth in one place
- no per-user laptop tunnel setup
- works through corporate firewalls (just HTTPS, no SSH outbound)

## What I'm NOT proposing

- An MCP "aggregator" that fronts N BrowserOS instances behind a single
  endpoint. You could write one (intercept the MCP handshake, route per-tool
  to a specific backend), but it's complex and the path-based reverse-proxy
  approach gives you the same multi-browser story with zero protocol code.
- Modifying BrowserOS to bind 0.0.0.0. The user explicitly ruled this out, and
  socat is a strictly safer way to expose loopback services anyway (you keep
  control of who can reach it via the proxy in front).
