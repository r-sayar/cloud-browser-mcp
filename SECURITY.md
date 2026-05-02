# Security model

Read this **before** running cloud_agents on anything other than your laptop.

## What you're hosting

Each BrowserOS container is a **fully-authenticated Chromium driving real
HTTP traffic on your behalf.** Anyone who can reach its MCP port (default
`9201`/`9202`/`9203`) or its noVNC port (`6081`/`6082`/`6083`) can:

- Read every cookie, localStorage entry, and saved password in the profile
- Send authenticated requests as you on every site you've signed into
- Take screenshots of whatever's on screen
- Download arbitrary files
- Execute arbitrary JavaScript in the browser context

The MCP and noVNC services in this repo **ship with no authentication**. That
is deliberate for a single-user laptop setup where the only listener is
`localhost`. It is **not safe** the moment any port is reachable from outside
your machine.

## Threat models, ranked

| Deployment                                   | Safe by default? | What you must do                                                |
|----------------------------------------------|------------------|------------------------------------------------------------------|
| `localhost` only on your laptop              | ✅ yes           | Nothing. Don't change `docker compose ps`'s `0.0.0.0:` mapping. |
| LAN-reachable (e.g., on home Wi-Fi)          | ❌ no            | Bind to `127.0.0.1` only, or put behind Tailscale.              |
| Public IP, any cloud VM with port forwarding | ❌ very no       | Caddy/nginx in front + bearer auth or mTLS. **Required.**       |
| Tailscale / WireGuard private network        | ✅ yes           | Default config is fine; only your tailnet can reach it.         |

## How to lock it down

### Option A — keep it on `localhost` only (zero work)

Don't change anything. The default `docker-compose.yml` binds host ports
`0.0.0.0`, but if you're on a Mac with the firewall on incoming connections
are blocked anyway. **Verify** with `nmap` from another machine on your LAN —
if the ports show `closed`/`filtered`, you're fine.

To be extra safe, edit the port mappings to bind explicit loopback:

```yaml
ports:
  - "127.0.0.1:9201:9200"
  - "127.0.0.1:6081:6080"
  - "127.0.0.1:9111:9011"
```

Now nothing outside your machine can reach the services even if your firewall
is off.

### Option B — Tailscale (recommended for remote access)

Install Tailscale on the host and on the machines you want to drive it from.
The host gets a `100.x.y.z` IP only members of your tailnet can reach. Update
your Claude Desktop config:

```json
"browseros-1": { "command": "npx", "args": ["-y", "mcp-remote", "http://100.x.y.z:9201/mcp"] }
```

No public exposure, no TLS to manage, no auth to wire up.

### Option C — public HTTPS + bearer-token auth (Caddy)

If you genuinely need a public URL (collaborators on a different network, no
shared tailnet), put Caddy in front:

```caddyfile
mcp.yourdomain.com {
  @authorized header Authorization "Bearer <generated-token-here>"

  handle /b/1/* {
    @authorized {
      uri strip_prefix /b/1
      reverse_proxy localhost:9201 {
        transport http {
          response_header_timeout 0   # MCP/SSE: no timeout
          read_buffer 64KB
        }
      }
    }
    respond "Unauthorized" 401
  }
  # ...repeat for /b/2 → :9202, /b/3 → :9203
}
```

Generate a bearer token with `openssl rand -hex 32`. Drop it into the
`Authorization` header that `mcp-remote` sends:

```json
"browseros-1": {
  "command": "npx",
  "args": ["-y", "mcp-remote", "https://mcp.yourdomain.com/b/1/mcp",
           "--header", "Authorization:Bearer <generated-token-here>"]
}
```

If you can put **Cloudflare Access** or **Tailscale Funnel with ACLs** in front
instead, prefer that — bearer-token-in-headers is fine but easy to leak (logs,
shoulder-surfing the JSON config).

### Option D — none of the above

If you skip A–C and put this on a public IP, you have given the internet a
fully-authenticated browser running as you. Don't do that.

## Reporting security issues

Open an issue or email the maintainer if you find something. There's no
specific embargo policy for this project; this repo is a personal-scale
hobby project, not a security-critical service.
