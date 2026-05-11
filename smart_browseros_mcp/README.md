# smart_browseros_mcp

Adds three things to a BrowserOS connector:

1. **`site_describe(page)`** — given a tab, identifies the site and returns the
   high-level intents available via the corresponding `<site>_mcp` server,
   plus contextual `next_actions` based on the URL state. Discovery aid:
   tells the agent "you're on Gmail — call `gmail_compose`, not
   `take_snapshot`+`click`+`fill`."
2. **`site_intents()`** / **`site_open(site_id)`** — full registry +
   navigate-to convenience for the 15 known sites.
3. **`upload_file_inline(page, element, file_name, file_b64)`** — accepts file
   bytes, writes them into the BrowserOS bind-mount (`./data/N/inline_uploads/…`),
   then calls native `upload_file` with the equivalent container path. Fixes
   the Docker file-upload gap (the agent has no host paths to pass).

## Wiring

`~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
"smart-browseros-1": {
  "command": "/Users/rls/cloud_agents/smart_browseros_mcp/.venv/bin/python",
  "args": ["/Users/rls/cloud_agents/smart_browseros_mcp/server.py"],
  "env": { "BROWSEROS_URL": "http://localhost:9201/mcp" }
}
```

Run alongside the native `browseros-N` connector — this server doesn't proxy,
just adds tools.

## Recipes

`recipes.py` has one entry per known site (gmail, outlook, canvas, claude_ai,
fu_berlin, amazon, linkedin, notion, youtube, luma, zoom, calendly, pubmed,
skyscanner, wikipedia). Each entry lists the corresponding `<site>_mcp` tools
and URL-pattern context hints. To add a site, append to `RECIPES`.

Every recipe declares a `method` — one of `"mcp"`, `"api"`, or `"script"` —
documenting how the site-MCP is implemented under the hood:

- `"mcp"` — built on top of an existing first/third-party MCP server.
- `"api"` — calls the site's official HTTP API (REST/GraphQL/etc).
- `"script"` — drives a live, authenticated browser tab via DOM/JS evaluation.

The classification surfaces in `site_describe` / `site_intents` / `site_open`
output so the agent (and the human reviewing the registry) can see at a
glance what cost / auth / reliability profile a given site has. See
[`docs/PROTOCOL.md`](../docs/PROTOCOL.md#classify-the-method-up-front) for
how to pick.

## Slot/host-data-dir mapping

The wrapper auto-derives the host bind-mount path from the BrowserOS port:
`9201` → `./data/1/`, `9202` → `./data/2/`, `9203` → `./data/3/`. Override
with `BROWSEROS_HOST_DATA_DIR` if your layout differs.
