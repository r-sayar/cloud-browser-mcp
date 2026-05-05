# Cloud migration: local Docker → Hetzner CPX31 + Tailscale

Total cost target: **~€16.49/mo** for the server, **€0/mo** for Tailscale Personal.

## What you do (the parts I cannot automate)

### 1. Tailscale account (~2 min, free)

1. Sign up at https://login.tailscale.com (use Google / GitHub SSO).
2. Install the macOS client: `brew install --cask tailscale` then launch it and sign in.
3. Generate an **auth key** for the server: https://login.tailscale.com/admin/settings/keys
   - Reusable: ✅ (so you can re-bootstrap without regenerating)
   - Ephemeral: ❌ (the server should persist)
   - Pre-approved: ✅
   - Copy the `tskey-auth-...` string.

### 2. Hetzner Cloud account + server (~5 min, this is where you pay)

1. Sign up at https://accounts.hetzner.com/signUp — note: requires identity verification (email + sometimes a card preauth).
2. Create a project (any name), then **Add Server**:
   - **Location**: Falkenstein (closest to Berlin) or Nuremberg
   - **Image**: Ubuntu 24.04
   - **Type**: **CPX31** (4 vCPU AMD, 8 GB RAM, 160 GB SSD, 20 TB traffic) — €16.49/mo
   - **Networking**: ✅ Public IPv4
   - **SSH keys**: add your laptop's public key (`cat ~/.ssh/id_ed25519.pub`) — or skip and rely on the one in cloud-init
   - **Cloud config**: paste the contents of [`cloud-init.yaml`](cloud-init.yaml) **after** filling in two placeholders:
     - replace `ssh-ed25519 AAAA__REPLACE_ME__...` with your laptop's public key
     - replace `tskey-auth-__REPLACE_ME__` with the Tailscale auth key from step 1
   - **Firewall**: create one allowing only port 22 from `0.0.0.0/0` and `::/0`. Apply it to the server.
   - Click **Create & Buy now** — this is the payment step.
3. Wait ~60–90 seconds for cloud-init to finish (Docker + Tailscale install).

### 3. Verify the server appeared on your tailnet

On your laptop:
```bash
tailscale status | grep cbm-server
```
You should see something like `100.64.0.5  cbm-server  ...`.

### 4. Run the deploy script (~5–10 min, includes Docker image build)

```bash
cd ~/cloud_agents
./cloud/deploy.sh cbm-server --with-data
```

`--with-data` migrates your logged-in profiles (FU Berlin SSO, Hunter.io, Google Passwords, etc.) so you don't have to log in again. Drop the flag to start with fresh profiles instead.

The script rsyncs the repo + the BrowserOS AppImage + (optionally) `./data`, then runs `docker compose up -d --build` on the remote host. It prints the new noVNC and MCP URLs at the end.

### 5. Update Claude Desktop

In Claude Desktop's MCP connector settings, change the `browseros-1/2/3` URLs from `http://localhost:9201/...` to `http://cbm-server:9201/...` (and 9202, 9203). Same for the noVNC URLs in `dashboard.html` (or just open it as `dashboard.html?host=cbm-server` once that param is wired up).

### 6. Tear down local (optional)

Once you've confirmed the cloud setup works:
```bash
cd ~/cloud_agents
docker compose down       # stops local containers
# keep ./data around as a backup until you trust the cloud copy
```

## Architecture recap

```
your laptop                                   Hetzner CPX31 (Falkenstein)
┌──────────────────┐                          ┌──────────────────────────────┐
│ Claude Desktop ──┼──── tailscale ──────────►│ tailscale0 (100.64.0.5)      │
│ Browser tabs ────┤                          │   ↓                          │
│ Tailscale client │                          │ docker0                      │
└──────────────────┘                          │   cbm-browseros-1/2/3        │
                                              │   /opt/cloud-browser-mcp/    │
                                              │     data/{1,2,3}/  (profiles)│
                                              │ ufw: only 22 public          │
                                              └──────────────────────────────┘
```

Public internet sees nothing but SSH on port 22. All noVNC / MCP / CDP traffic stays inside your tailnet.
