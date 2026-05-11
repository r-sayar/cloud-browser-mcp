#!/usr/bin/env python3
"""smart_browseros_mcp — adds caching/affordances + a Docker file-upload bridge
to the BrowserOS MCP.

Three tools, no proxying. Wire this in alongside the native `browseros-N`
connector in Claude Desktop. The agent uses native BrowserOS for primitives
(snapshot, click, navigate, …) and these tools for:

  • discovering high-level cached recipes when on a known site
  • uploading file *bytes* from Claude Desktop into a Dockerised BrowserOS
    (where filesystem paths visible to the agent don't exist)

Tools:
  - site_describe(page)       → matches URL → cached recipe + next_actions
  - site_intents()            → full registry of recognised sites
  - site_open(site_id)        → open a known site by id (e.g. "gmail")
  - upload_file_inline(...)   → write bytes to the bind-mount and call BrowserOS upload_file

Architecture:

    Claude Desktop ──stdio──► smart_browseros_mcp (this server)
                                    │
                                    └─ HTTP MCP ──► localhost:920N/mcp (BrowserOS)
                                                        │
                                                        └─ ./data/N/ host bind ↔ /data/ container

Configuration (via env):
  BROWSEROS_URL              — http://localhost:9201/mcp   (slot 1 default)
  BROWSEROS_HOST_DATA_DIR    — /Users/rls/cloud_agents/data/1   (auto-derived from port)
  BROWSEROS_CONTAINER_DATA   — /data                        (rarely changed)
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import FastMCP

from recipes import (
    RECIPES,
    Recipe,
    context_intents_for,
    find_recipe,
)

BROWSEROS_URL = os.environ.get("BROWSEROS_URL", "http://localhost:9201/mcp")
CONTAINER_DATA = os.environ.get("BROWSEROS_CONTAINER_DATA", "/data")


def _default_host_data_dir() -> str:
    """Derive `./data/N/` from the BROWSEROS_URL port (9201→1, 9202→2, …).

    cloud_agents docker-compose maps host `./data/N/` → container `/data/`.
    """
    port = urlparse(BROWSEROS_URL).port or 9201
    slot = (port - 9200) if 9201 <= port <= 9299 else 1
    return str(Path(__file__).resolve().parent.parent / "data" / str(slot))


HOST_DATA_DIR = os.environ.get("BROWSEROS_HOST_DATA_DIR", _default_host_data_dir())

mcp = FastMCP("smart-browseros")


# ─── BrowserOS HTTP MCP client (copy of mcp_lib.BOSClient) ─────────────────────
class _BOSClient:
    """Minimal Streamable-HTTP MCP client. We don't import mcp_lib because we
    want this server to be a copy-paste-friendly single dir for upstreaming."""

    def __init__(self, url: str):
        self.url = url
        self.client = httpx.AsyncClient(timeout=60.0)
        self._req_id = 0
        self._initialized = False
        self._session_id: str | None = None

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        r = await self.client.post(self.url, headers=headers, json=body)
        r.raise_for_status()
        sid = r.headers.get("mcp-session-id") or r.headers.get("Mcp-Session-Id")
        if sid and not self._session_id:
            self._session_id = sid
        if not r.content:
            return {}
        return r.json()

    async def _ensure_initialized(self):
        if self._initialized:
            return
        self._req_id += 1
        await self._post({
            "jsonrpc": "2.0", "id": self._req_id, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "smart_browseros_mcp", "version": "0.1"},
            },
        })
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._initialized = True

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        await self._ensure_initialized()
        self._req_id += 1
        resp = await self._post({
            "jsonrpc": "2.0", "id": self._req_id, "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        })
        if "error" in resp:
            raise RuntimeError(f"BrowserOS error calling {tool}: {resp['error']}")
        contents = resp.get("result", {}).get("content", [])
        if not contents:
            return resp.get("result", {})
        text = contents[0].get("text", "")
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text


_bos: _BOSClient | None = None


def _client() -> _BOSClient:
    global _bos
    if _bos is None:
        _bos = _BOSClient(BROWSEROS_URL)
    return _bos


_PAGE_RE = re.compile(r"^(\d+)\.\s+(.+?)\n\s+(\S+)$", re.MULTILINE)
_PAGE_ID_RE = re.compile(r"Page ID:\s*(\d+)")


async def _get_page_url(page_id: int) -> tuple[str, str]:
    """Return (url, title) for `page_id` via a single evaluate_script call."""
    raw = await _client().call("evaluate_script", {
        "page": page_id,
        "expression": "JSON.stringify({url: location.href, title: document.title})",
    })
    if isinstance(raw, dict) and "value" in raw:
        raw = raw["value"]
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return "", ""
    else:
        data = raw if isinstance(raw, dict) else {}
    return data.get("url", ""), data.get("title", "")


# ─── tools ─────────────────────────────────────────────────────────────────────
@mcp.tool()
async def site_describe(page: int) -> str:
    """Identify the site on `page` and return cached recipe metadata.

    Returns JSON with the matched site's available high-level intents (tools
    exposed by the corresponding `<site>_mcp` server) and contextual
    next_actions based on the current URL. Use this BEFORE take_snapshot to
    discover whether a cached recipe path exists — it's faster and more
    reliable than re-deriving click sequences.

    For unknown sites returns `{recognized: false, ...}` so the caller can
    fall back to take_snapshot/click. Always succeeds (never errors on
    unknown URLs).
    """
    url, title = await _get_page_url(page)
    if not url:
        return json.dumps({
            "recognized": False,
            "url": "",
            "error": "could_not_read_url",
            "next_actions": ["take_snapshot(page) — fall back to raw DOM"],
        }, indent=2)

    recipe = find_recipe(url)
    if not recipe:
        return json.dumps({
            "recognized": False,
            "url": url,
            "title": title,
            "next_actions": [
                "take_snapshot(page) — no cached recipe; use raw DOM",
                "site_intents() — see what sites have cached recipes",
            ],
        }, indent=2, ensure_ascii=False)

    contextual = context_intents_for(recipe, url)
    return json.dumps({
        "recognized": True,
        "url": url,
        "title": title,
        "site": recipe.id,
        "site_mcp": recipe.site_mcp,
        "intents": [
            {"tool": i.tool, "args": i.args, "summary": i.summary}
            for i in recipe.intents
        ],
        "contextual_intents": contextual,
        "flow_hints": recipe.flow_hints,
        "notes": recipe.notes,
        "next_actions": [
            f"Prefer {recipe.site_mcp}_* tools over take_snapshot+click here.",
            *(f"On this URL, try: {t}" for t in contextual),
            *(f"Common flow: {h}" for h in recipe.flow_hints),
            "take_snapshot(page) — fallback if no intent fits",
        ],
    }, indent=2, ensure_ascii=False)


@mcp.tool()
async def site_intents() -> str:
    """Return the full registry of recognised sites and their cached intents.

    Use this when you don't know what sites have cached recipes available, or
    when planning a multi-site task (e.g. "search the web, then send a Gmail
    summary").
    """
    out = []
    for r in RECIPES:
        out.append({
            "id": r.id,
            "site_mcp": r.site_mcp,
            "url_match": r.url_match,
            "open_url": r.open_url,
            "intents": [{"tool": i.tool, "args": i.args, "summary": i.summary} for i in r.intents],
        })
    return json.dumps({
        "sites": out,
        "count": len(out),
        "next_actions": [
            "site_open(site_id='gmail') — navigate to a known site",
            "site_describe(page) — once on a site, get contextual hints",
        ],
    }, indent=2, ensure_ascii=False)


@mcp.tool()
async def site_open(site_id: str, page: int | None = None) -> str:
    """Open a known site by id (e.g. 'gmail', 'outlook', 'canvas').

    If `page` is given, navigates that tab; otherwise opens a new tab. Returns
    the page id and the recipe's intents so the agent can immediately call a
    high-level tool.
    """
    recipe = next((r for r in RECIPES if r.id == site_id or r.site_mcp == site_id), None)
    if recipe is None:
        return json.dumps({
            "ok": False,
            "error": f"unknown site_id '{site_id}'",
            "next_actions": ["site_intents() — list available site ids"],
        }, indent=2)

    if page is not None:
        await _client().call("navigate_page", {"page": page, "url": recipe.open_url})
        await asyncio.sleep(1.5)
        new_pid = page
    else:
        resp = await _client().call("new_page", {"url": recipe.open_url})
        text = resp if isinstance(resp, str) else json.dumps(resp)
        m = _PAGE_ID_RE.search(text)
        new_pid = int(m.group(1)) if m else -1
        await asyncio.sleep(1.5)

    return json.dumps({
        "ok": True,
        "site": recipe.id,
        "site_mcp": recipe.site_mcp,
        "page_id": new_pid,
        "url": recipe.open_url,
        "intents": [{"tool": i.tool, "args": i.args, "summary": i.summary} for i in recipe.intents],
        "next_actions": [
            f"site_describe(page={new_pid}) — verify and get contextual intents",
            *(f"{i.tool}{i.args} — {i.summary}" for i in recipe.intents[:3]),
        ],
    }, indent=2, ensure_ascii=False)


@mcp.tool()
async def upload_file_inline(
    page: int,
    element: int,
    file_name: str,
    file_b64: str,
) -> str:
    """Upload bytes to a file input — works inside Dockerised BrowserOS.

    BrowserOS's native `upload_file` requires absolute paths visible to the
    BrowserOS process. When BrowserOS runs in Docker and the agent runs on
    the host (or in Claude Desktop), the agent has no such path. This tool
    bridges that:

        1. base64-decode `file_b64` into the host bind-mount
           ({BROWSEROS_HOST_DATA_DIR}/inline_uploads/<random>/<file_name>)
        2. call BrowserOS `upload_file` with the equivalent container path
           ({BROWSEROS_CONTAINER_DATA}/inline_uploads/<random>/<file_name>)
        3. (the file is left on disk for ~10 min in case of debugging)

    Args:
        page: tab id (from list_pages)
        element: element id of the <input type="file"> from take_snapshot
        file_name: filename to give the upload (with extension)
        file_b64: base64-encoded file bytes
    """
    try:
        data = base64.b64decode(file_b64, validate=True)
    except Exception as e:
        return json.dumps({"ok": False, "error": f"invalid base64: {e}"})

    # Paths
    token = secrets.token_hex(6)
    host_dir = Path(HOST_DATA_DIR) / "inline_uploads" / token
    host_path = host_dir / file_name
    container_path = f"{CONTAINER_DATA}/inline_uploads/{token}/{file_name}"

    # Write to host bind mount; container sees it instantly via /data
    try:
        host_dir.mkdir(parents=True, exist_ok=True)
        host_path.write_bytes(data)
    except OSError as e:
        return json.dumps({
            "ok": False,
            "error": f"could not write to host data dir: {e}",
            "host_data_dir": HOST_DATA_DIR,
            "next_actions": [
                f"Set BROWSEROS_HOST_DATA_DIR to a writable path (default: {_default_host_data_dir()}).",
            ],
        }, indent=2)

    # Hand off to BrowserOS using the container path
    try:
        await _client().call("upload_file", {
            "page": page,
            "element": element,
            "files": [container_path],
        })
    except Exception as e:
        return json.dumps({
            "ok": False,
            "error": f"BrowserOS upload_file failed: {e}",
            "host_path": str(host_path),
            "container_path": container_path,
        }, indent=2)

    return json.dumps({
        "ok": True,
        "page": page,
        "element": element,
        "file_name": file_name,
        "size_bytes": len(data),
        "host_path": str(host_path),
        "container_path": container_path,
        "next_actions": [
            f"take_snapshot(page={page}) — verify the file appears attached",
            "Submit the form (the upload itself doesn't submit anything)",
        ],
    }, indent=2)


@mcp.tool()
async def smart_browseros_info() -> str:
    """Diagnostic: BROWSEROS_URL, host data dir, container path, recipe count."""
    return json.dumps({
        "browseros_url": BROWSEROS_URL,
        "host_data_dir": HOST_DATA_DIR,
        "host_data_dir_exists": Path(HOST_DATA_DIR).is_dir(),
        "container_data": CONTAINER_DATA,
        "recipe_count": len(RECIPES),
        "recipes": [r.id for r in RECIPES],
    }, indent=2)


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
