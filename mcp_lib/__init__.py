"""Shared helpers for site-as-MCP servers in cloud-browser-mcp.

A site MCP follows the protocol in docs/PROTOCOL.md:
  - thin Python stdio MCP server (uses `mcp.server.fastmcp.FastMCP`)
  - each tool is a cached recipe of BrowserOS calls
  - this library handles the BrowserOS HTTP MCP plumbing once, in one place,
    so individual site MCPs are small and focused on their site logic
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any

import httpx

DEFAULT_BROWSEROS_URL = "http://localhost:9201/mcp"

# BrowserOS list_pages returns text like:
#   1. <title> (tab <tabId>)
#      <url>
_PAGE_RE = re.compile(r"^(\d+)\.\s+(.+?)\n\s+(\S+)$", re.MULTILINE)
# new_page returns "Opened new page: …\nPage ID: 52"
_PAGE_ID_RE = re.compile(r"Page ID:\s*(\d+)")


def parse_pages_text(text: str) -> list[tuple[int, str]]:
    """Extract [(pageId, url), ...] from BrowserOS's list_pages text response."""
    return [(int(m.group(1)), m.group(3)) for m in _PAGE_RE.finditer(text)]


def parse_new_page_id(text: str) -> int | None:
    """Extract the new page id out of new_page's text response."""
    m = _PAGE_ID_RE.search(text)
    return int(m.group(1)) if m else None


class BOSClient:
    """Minimal Streamable-HTTP MCP client for BrowserOS.

    Reused by all site MCPs. Handles the initialize handshake once per
    process lifetime, then sends `tools/call` requests for each operation.
    """

    def __init__(self, url: str | None = None):
        self.url = url or os.environ.get("BROWSEROS_URL", DEFAULT_BROWSEROS_URL)
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
                "clientInfo": {"name": "cloud_browser_mcp_lib", "version": "0.1"},
            },
        })
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._initialized = True

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        """Call a BrowserOS MCP tool. Returns parsed text content (JSON-decoded
        if the response is JSON, else the raw string).
        """
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


_singleton: BOSClient | None = None


def client() -> BOSClient:
    """Return a process-wide BOSClient singleton (lazy-init)."""
    global _singleton
    if _singleton is None:
        _singleton = BOSClient()
    return _singleton


# ─── tab lifecycle helpers ─────────────────────────────────────────────────────
async def find_tab_matching(url_substring: str) -> int | None:
    """Return the page id of an open tab whose URL contains `url_substring`,
    or None."""
    pages_text = await client().call("list_pages", {})
    text = pages_text if isinstance(pages_text, str) else json.dumps(pages_text)
    for pid, url in parse_pages_text(text):
        if url_substring in url:
            return pid
    return None


async def ensure_tab(url_substring: str, fallback_url: str, settle_seconds: float = 2.0) -> int:
    """Find an existing tab whose URL contains `url_substring`, or open a new
    one navigating to `fallback_url`. Returns the page id."""
    pid = await find_tab_matching(url_substring)
    if pid is not None:
        return pid
    resp = await client().call("new_page", {"url": fallback_url})
    text = resp if isinstance(resp, str) else json.dumps(resp)
    new_pid = parse_new_page_id(text)
    await asyncio.sleep(settle_seconds)
    if new_pid is None:
        # Fall back: scan list_pages for the URL we just opened.
        pid = await find_tab_matching(url_substring)
        if pid is None:
            raise RuntimeError(f"could not open or find tab for {url_substring}")
        return pid
    return new_pid


async def js(page_id: int, script: str) -> Any:
    """Run a JS expression on `page_id`; return its parsed result."""
    raw = await client().call("evaluate_script", {"page": page_id, "expression": script})
    if isinstance(raw, dict) and "value" in raw:
        v = raw["value"]
        try:
            return json.loads(v)
        except (json.JSONDecodeError, TypeError):
            return v
    return raw
