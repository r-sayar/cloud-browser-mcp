# cloud-browser-mcp

Use a real browser **remotely**, from your local Claude Desktop, over MCP.
Self-hosted: **N independent
[BrowserOS](https://github.com/browseros-ai/BrowserOS) instances in Docker,
each exposing its built-in MCP server, all wired into Claude Desktop as
separate connectors.** You log into your accounts once via a browser-based
live-view, profile state persists, and Claude drives a real Chromium with your
sessions intact.

This repo replicates the spirit of [Browser Use Cloud](https://browser-use.com/cloud),
but the *agent* is Anthropic's MCP-driven model running through BrowserOS — no
separate per-token billing, no rented infra. You own the bytes.

## Architecture

```
┌─ your laptop ─────────┐                              ┌─ host running Docker ────────────────────┐
│  Claude Desktop       │                              │  cbm-browseros-1                │
│   mcpServers:         │ ─ stdio ─ npx mcp-remote ──► │   Xvfb + noVNC + Chromium-fork           │
│     browseros-1: ──┐  │           http://host:9201  │   /data/1/profile  ← cookies, history…   │
│     browseros-2: ──┼──┼───────────► :9202 ────────► │  cbm-browseros-2                │
│     browseros-3: ──┘  │             :9203 ────────► │  cbm-browseros-3                │
│                       │                              │   restart: unless-stopped                │
│  Web browser          │ ─ live-view (noVNC) ──────► │   noVNC ports :6081, :6082, :6083        │
└───────────────────────┘                              └──────────────────────────────────────────┘
```

| Slot | MCP port | noVNC                  | CDP   | Profile dir |
|------|----------|------------------------|-------|-------------|
| 1    | 9201     | http://localhost:6081/ | 9111  | `./data/1`  |
| 2    | 9202     | http://localhost:6082/ | 9112  | `./data/2`  |
| 3    | 9203     | http://localhost:6083/ | 9113  | `./data/3`  |

Each slot is fully isolated: separate profile, separate logins, separate tabs.
At the start of a Claude Desktop chat you say *"for this conversation use
browseros-2"* and that chat is bound to slot 2.

---

## Full setup (Mac, ~15 minutes)

### 1. Prerequisites

```bash
# Docker Desktop — https://docs.docker.com/desktop/install/mac-install/
# Node.js (for npx mcp-remote)
brew install node
# GitHub Desktop / git, plus Claude Desktop installed
```

### 2. Clone the repo

```bash
git clone https://github.com/r-sayar/cloud-browser-mcp.git ~/cloud-browser-mcp
cd ~/cloud-browser-mcp
```

### 3. Drop in the BrowserOS Linux AppImage

```bash
# Get it from https://github.com/browseros-ai/BrowserOS/releases (Linux .AppImage)
# ~280 MB; the repo's .gitignore excludes it so it stays out of git.
cp ~/Downloads/BrowserOS.AppImage .
```

### 4. Bring up the stack

```bash
docker compose up -d --build       # ~3 min on first run (apt + AppImage extract)
```

Verify all three slots are healthy:

```bash
for p in 9201 9202 9203; do echo -n "$p: "; curl -s http://localhost:$p/health; echo; done
# → 9201: {"status":"ok","cdpConnected":true}
# → 9202: {"status":"ok","cdpConnected":true}
# → 9203: {"status":"ok","cdpConnected":true}
```

### 5. Wire the connectors into Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` —
add the `mcpServers` block (top level, alongside any `preferences`):

```json
{
  "mcpServers": {
    "browseros-1": { "command": "npx", "args": ["-y", "mcp-remote", "http://localhost:9201/mcp"] },
    "browseros-2": { "command": "npx", "args": ["-y", "mcp-remote", "http://localhost:9202/mcp"] },
    "browseros-3": { "command": "npx", "args": ["-y", "mcp-remote", "http://localhost:9203/mcp"] }
  }
}
```

> **Why `mcp-remote`?** Claude Desktop's "Connectors" UI requires HTTPS, but
> `mcp-remote` runs as a stdio MCP subprocess that forwards to any HTTP URL —
> bypassing the HTTPS check while keeping the wire protocol identical.

⌘Q Claude Desktop fully (not just close window) and reopen. The three connectors
will show up in the tool picker.

### 6. Offline auth (per slot, one-time)

Open the noVNC URL for whichever slot you want to set up — for example
**http://localhost:6081/** for slot 1 — in your laptop browser. You'll see the
cloud Chromium desktop. Click into the page; keys + mouse work.

Inside that cloud browser:

- Sign into Google (or whichever sites you want the agent to act on).
- Sign into BrowserOS itself if it prompts (its own account for the Klavis 40+
  external-service integrations).

Profile state is written to `./data/<slot>/profile/` on your host. Survives
container restarts, repo backup, etc. Each slot has its own profile —
slot 1's logins are invisible to slot 2.

### 7. Use it

In any Claude Desktop chat, say:

> "For this conversation, use **browseros-2**. Open my Gmail and tell me how
> many unread messages I have."

Claude will pick the slot-2 toolset, navigate gmail.com, take a snapshot, count.
Open another chat in parallel; tell it *"use browseros-3"*. Fully isolated, no
interference.

---

## Auto-start on every reboot (Mac)

This is set up automatically by `make-stable.sh` (see below), but for the
record, the two pieces are:

1. **Docker Desktop opens at login.** We flipped `AutoStart: true` in
   `~/Library/Group Containers/group.com.docker/settings-store.json`.
2. **Containers come back, even after `docker compose down`.** A LaunchAgent at
   `~/Library/LaunchAgents/com.cloud-agents.browseros.plist` runs
   `scripts/launchd-startup.sh` at login, which waits for the Docker daemon and
   then runs `docker compose up -d`. Combined with `restart: unless-stopped` in
   the compose file, the stack is bullet-proof across reboots.

To install on a fresh machine:

```bash
./scripts/make-stable.sh
```

To inspect or tear down:

```bash
launchctl list | grep cloud-agents       # → 21687  0  com.cloud-agents.browseros
tail -f /tmp/cloud_agents.{out,err}.log
launchctl unload ~/Library/LaunchAgents/com.cloud-agents.browseros.plist
```

---

## Adding more browsers

Edit `docker-compose.yml`. Each slot is one service block; add a fourth:

```yaml
  browseros-4:
    <<: *browseros-defaults
    container_name: cbm-browseros-4
    depends_on: [browseros-1]
    ports: ["9204:9200", "9114:9011", "6084:6080"]
    volumes: ["./data/4:/data"]
```

Then add a fourth `mcpServers` entry in Claude Desktop config and ⌘Q + reopen.

Resource budget: ~600 MB RAM per idle browser, can spike to 2 GB. Three is
comfortable on 8 GB; four+ wants 12 GB+.

---

## Running on a remote host (optional)

The single-machine local setup above works for personal use. If you want it on
a server, the cheapest paths in order:

1. **Self-host on any spare hardware** (Pi 4+, NUC, Mac mini, old laptop) +
   [Tailscale](https://tailscale.com/) for remote access. Free; ~$1/mo electricity.
   Update Claude Desktop to point at `http://<tailscale-ip>:9201/mcp` instead of
   localhost. Done.
2. **Hetzner CX32** (€7.55/mo, 8 GB RAM) + Tailscale. Same idea, ~$8/mo.
3. **Public HTTPS via Caddy** in front of the stack, with bearer-token auth.
   ~30 min extra setup; only do it if you need to share access with collaborators.

A Caddy + auth deploy bundle for option 3 is sketched in [BROWSEROS.md](BROWSEROS.md).

---

## Cookie-import fast-path (optional)

`scripts/import_cookies.py` reads cookies from your laptop's Chrome and pushes
them into a slot's browser via CDP. Useful for sites that don't fingerprint-bind
cookies (banks/Google/Apple are blacklisted by default).

```bash
pip install -r scripts/requirements.txt
docker cp scripts/import_cookies.py cbm-browseros-1:/tmp/
docker compose exec browseros-1 pip install browser-cookie3 websockets requests --quiet
docker compose exec browseros-1 python3 /tmp/import_cookies.py twitter.com github.com
```

> CDP from the host hits a Chromium Host-header check that we'd need an
> HTTP-aware proxy to fix; running the script from inside the container
> sidesteps it. MCP path is unaffected.

---

## Profile backup / restore

```bash
docker compose down
./scripts/backup.sh                          # → profile-<timestamp>.tgz
./scripts/restore.sh profile-…tgz            # restores to ./data
docker compose up -d
```

---

## Contributing

Issues and PRs welcome. The wrapper code is small and intentionally
unopinionated — most improvements should land cleanly. If you're adding a
new feature, please update both the README and at least one of the existing
smoke tests so reviewers can confirm the shape of the change quickly.

## Layout

```
.
├── Dockerfile                  # Debian + Xvfb + noVNC + extracted AppImage
├── entrypoint.sh               # Xvfb → x11vnc → noVNC → socat → BrowserOS
├── docker-compose.yml          # 3 services with YAML anchor for shared defaults
├── BrowserOS.AppImage          # gitignored — drop in from BrowserOS releases
├── data/{1,2,3}/profile/       # Persistent BrowserOS profiles (cookies etc.)
├── launchd/
│   └── com.cloud-agents.browseros.plist   # macOS LaunchAgent template
├── scripts/
│   ├── launchd-startup.sh      # what the LaunchAgent runs
│   ├── make-stable.sh          # one-shot installer for autostart
│   ├── import_cookies.py       # cookie fast-path (run inside container)
│   ├── backup.sh / restore.sh  # tar/untar of ./data
│   └── requirements.txt
├── BROWSEROS.md                # multi-tenant cloud-deploy design
├── legacy/steel/               # earlier Steel + Chromium prototype, kept for reference
└── README.md
```

## ⚠ Security

The MCP and noVNC services in this repo **ship with no authentication.** That
is fine for `localhost` on your laptop — and dangerous the moment any port is
reachable from outside your machine.

**Read [SECURITY.md](SECURITY.md) before you put this on anything other than
your own laptop.** Short version: lock to `127.0.0.1` only, use Tailscale for
remote access, or put Caddy + bearer-token auth in front for a public URL.

## Known caveats

1. **BrowserOS Max plan.** Some BrowserOS features (the LLM-powered ones it
   ships with) need a BrowserOS account; the MCP works without one.
2. **CDP from host is broken** (Chromium Host-header check via socat).
   Workaround: `docker compose exec browseros-N`. Doesn't affect MCP.
3. **`--no-sandbox` in container.** Required because Chromium's namespace
   sandbox needs userns capabilities Docker rarely exposes. Standard practice for
   containerized Chromium; don't expose the container to untrusted input.
4. **Resource use.** Three idle browsers ~1.8 GB; spike to 5–6 GB under load.
   `shm_size: 2gb` per service is sized for that.

## License

The wrapper code in this repo is MIT. The bundled BrowserOS binary is governed
by [BrowserOS's own license](https://github.com/browseros-ai/BrowserOS/blob/main/LICENSE)
(AGPLv3 + Ungoogled Chromium BSD).
