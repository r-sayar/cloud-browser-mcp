#!/usr/bin/env python3
"""smart_browseros_mcp — adds caching/affordances + a Docker file-transfer bridge
to the BrowserOS MCP.

No proxying — wire this alongside the native `browseros-N` connector in Claude
Desktop. The agent uses native BrowserOS for primitives (snapshot, click,
navigate, …) and these tools for:

  • discovering high-level cached recipes when on a known site
  • uploading file *bytes* from Claude Desktop into a Dockerised BrowserOS
  • downloading file *bytes* back from BrowserOS to Claude Desktop

Tools:
  - site_describe(page)          → matches URL → cached recipe + next_actions
  - site_intents()               → full registry of recognised sites
  - site_open(site_id)           → open a known site by id (e.g. "gmail")
  - gmail_compose_draft(...)     → pre-fill Gmail compose URL
  - upload_file_inline(...)      → write bytes to the bind-mount, call BrowserOS upload_file
  - download_file(page, element) → click a download link, return file bytes as base64
  - list_downloads()             → list files waiting in the downloads folder
  - read_download(filename)      → read an already-downloaded file as base64

Architecture:

    Claude Desktop ──stdio──► smart_browseros_mcp (this server)
                                    │
                                    └─ HTTP MCP ──► localhost:920N/mcp (BrowserOS)
                                                        │
                                                        └─ ./data/N/ host bind ↔ /data/ container

Upload path:  Claude Desktop → base64 → host ./data/N/inline_uploads/ ↔ /data/inline_uploads/
Download path: BrowserOS → /data/downloads/ ↔ host ./data/N/downloads/ → base64 → Claude Desktop

Configuration (via env):
  BROWSEROS_URL              — http://localhost:9201/mcp   (slot 1 default)
  BROWSEROS_HOST_DATA_DIR    — /Users/rls/cloud_agents/data/1   (auto-derived from port)
  BROWSEROS_CONTAINER_DATA   — /data                        (rarely changed)
"""
from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import re
import secrets
import shutil
import sys
import time
from datetime import datetime, timezone
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

BROWSEROS_URL = os.environ.get("BROWSEROS_URL", "http://cloud-browser:9201/mcp")
CONTAINER_DATA = os.environ.get("BROWSEROS_CONTAINER_DATA", "/data")


def _default_host_data_dir() -> str:
    """Derive `./data/N/` from the BROWSEROS_URL port (9201→1, 9202→2, …).

    cloud_agents docker-compose maps host `./data/N/` → container `/data/`.
    """
    port = urlparse(BROWSEROS_URL).port or 9201
    slot = (port - 9200) if 9201 <= port <= 9299 else 1
    return str(Path(__file__).resolve().parent.parent / "data" / str(slot))


HOST_DATA_DIR = os.environ.get("BROWSEROS_HOST_DATA_DIR", _default_host_data_dir())

# Staging dirs older than this are cleaned up before each new upload.
_UPLOAD_STAGING_MAX_AGE = int(os.environ.get("BROWSEROS_UPLOAD_MAX_AGE", "600"))

# Max size returned inline as base64; larger files get a host_path hint instead.
_INLINE_MAX_BYTES = int(os.environ.get("BROWSEROS_INLINE_MAX_BYTES", str(20 * 1024 * 1024)))


def _downloads_dir() -> Path:
    """Host path for browser downloads: ./data/N/downloads/"""
    return Path(HOST_DATA_DIR) / "downloads"


def _container_downloads() -> str:
    return f"{CONTAINER_DATA}/downloads"


def _cleanup_old_upload_staging() -> None:
    """Remove upload staging dirs older than _UPLOAD_STAGING_MAX_AGE seconds."""
    staging = Path(HOST_DATA_DIR) / "inline_uploads"
    if not staging.is_dir():
        return
    cutoff = time.time() - _UPLOAD_STAGING_MAX_AGE
    for child in staging.iterdir():
        if child.is_dir() and child.stat().st_mtime < cutoff:
            shutil.rmtree(child, ignore_errors=True)


mcp = FastMCP("smart-browseros")

# Per-call token logging (sibling repo's mcp_lib). Best-effort: keep optional
# so this server stays runnable standalone (copy-paste-friendly).
try:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from mcp_lib.usage_log import install_logger  # type: ignore
    install_logger(mcp, "smart_browseros_mcp")
except Exception:
    pass


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
        "method": recipe.method,  # mcp | api | script — how this site-MCP works under the hood
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


_SITE_INTENTS_CACHE: str | None = None

@mcp.tool()
async def site_intents() -> str:
    """Return the full registry of recognised sites and their cached intents.

    Use this when you don't know what sites have cached recipes available, or
    when planning a multi-site task (e.g. "search the web, then send a Gmail
    summary").
    """
    global _SITE_INTENTS_CACHE
    if _SITE_INTENTS_CACHE is not None:
        return _SITE_INTENTS_CACHE
    out = []
    for r in RECIPES:
        out.append({
            "id": r.id,
            "site_mcp": r.site_mcp,
            "method": r.method,
            "url_match": r.url_match,
            "open_url": r.open_url,
            "intents": [{"tool": i.tool, "args": i.args, "summary": i.summary} for i in r.intents],
        })
    _SITE_INTENTS_CACHE = json.dumps({
        "sites": out,
        "count": len(out),
        "next_actions": [
            "site_open(site_id='gmail') — navigate to a known site",
            "site_describe(page) — once on a site, get contextual hints",
        ],
    }, indent=2, ensure_ascii=False)
    return _SITE_INTENTS_CACHE


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
        "method": recipe.method,  # mcp | api | script
        "page_id": new_pid,
        "url": recipe.open_url,
        "intents": [{"tool": i.tool, "args": i.args, "summary": i.summary} for i in recipe.intents],
        "next_actions": [
            f"site_describe(page={new_pid}) — verify and get contextual intents",
            *(f"{i.tool}{i.args} — {i.summary}" for i in recipe.intents[:3]),
        ],
    }, indent=2, ensure_ascii=False)


@mcp.tool()
async def gmail_compose_draft(
    to: str,
    subject: str = "",
    body: str = "",
    cc: str = "",
    bcc: str = "",
    page: int | None = None,
) -> str:
    """Open a Gmail compose window pre-filled with to/subject/body/cc/bcc.

    Uses Gmail's compose URL parameters — faster than clicking through the UI.
    Requires the user to already be signed in to Gmail. Never auto-sends.
    Returns the page_id of the compose tab.
    """
    from urllib.parse import urlencode
    params: dict[str, str] = {"view": "cm", "fs": "1"}
    if to:      params["to"]  = to
    if subject: params["su"]  = subject
    if body:    params["body"] = body
    if cc:      params["cc"]  = cc
    if bcc:     params["bcc"] = bcc
    url = "https://mail.google.com/mail/?" + urlencode(params)

    if page is not None:
        await _client().call("navigate_page", {"page": page, "url": url})
        return json.dumps({"ok": True, "page_id": page, "url": url})

    resp = await _client().call("new_page", {"url": url})
    text = resp if isinstance(resp, str) else json.dumps(resp)
    m = _PAGE_ID_RE.search(text)
    new_pid = int(m.group(1)) if m else -1
    return json.dumps({
        "ok": True,
        "page_id": new_pid,
        "url": url,
        "next_actions": ["Fill the compose form if needed, then click Send"],
    })


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

    # Opportunistic cleanup of old staging dirs (best-effort, non-fatal)
    try:
        _cleanup_old_upload_staging()
    except Exception:
        pass

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
async def download_file(
    page: int,
    element: int,
) -> str:
    """Click a download link or button and return the file bytes as base64.

    Bridges the Docker isolation gap for downloads — the mirror image of
    upload_file_inline. Flow:

        1. Tell BrowserOS to click `element` on `page` and intercept the
           browser download, saving it to /data/downloads/ inside the container.
        2. The file appears on the host at {HOST_DATA_DIR}/downloads/ via the
           bind mount.
        3. Read the bytes and return them as base64 so Claude Desktop can save
           or process the file without needing direct filesystem access.

    Returns {ok, filename, size_bytes, mime_type, file_b64} on success.
    For files larger than ~20 MB only host_path is returned (no inline bytes).

    Args:
        page:    tab id (from list_pages)
        element: element id of the download link / button (from take_snapshot)
    """
    dl_dir = _downloads_dir()
    dl_dir.mkdir(parents=True, exist_ok=True)

    # Snapshot existing files so we can detect the newly created one.
    before: set[Path] = set(dl_dir.iterdir())

    try:
        result = await _client().call("download_file", {
            "page": page,
            "element": element,
            "path": _container_downloads(),
        })
    except Exception as e:
        return json.dumps({
            "ok": False,
            "error": f"BrowserOS download_file failed: {e}",
            "hint": "Ensure BrowserOS version supports download_file with a path argument.",
        }, indent=2)

    # Resolve the saved file from BrowserOS result or by diff-ing the directory.
    host_file: Path | None = None

    if isinstance(result, dict):
        raw_path = result.get("path") or result.get("destination") or result.get("filePath")
        if raw_path:
            rel = str(raw_path).replace(CONTAINER_DATA, "").lstrip("/")
            candidate = Path(HOST_DATA_DIR) / rel
            if candidate.is_file():
                host_file = candidate

    if host_file is None:
        after: set[Path] = set(dl_dir.iterdir())
        new_files = after - before
        if new_files:
            host_file = max(new_files, key=lambda p: p.stat().st_mtime)

    if host_file is None or not host_file.is_file():
        return json.dumps({
            "ok": False,
            "error": "no new file found in downloads directory after triggering the download",
            "bos_result": result,
            "downloads_dir": str(dl_dir),
        }, indent=2)

    size = host_file.stat().st_size
    mime_type = mimetypes.guess_type(host_file.name)[0] or "application/octet-stream"

    if size > _INLINE_MAX_BYTES:
        return json.dumps({
            "ok": True,
            "filename": host_file.name,
            "size_bytes": size,
            "mime_type": mime_type,
            "file_b64": None,
            "host_path": str(host_file),
            "note": f"File exceeds {_INLINE_MAX_BYTES // 1024 // 1024} MB inline limit — read from host_path directly.",
            "next_actions": [
                f"read_download('{host_file.name}') — retry; or access host_path on the host filesystem",
            ],
        }, indent=2)

    file_b64 = base64.b64encode(host_file.read_bytes()).decode()
    return json.dumps({
        "ok": True,
        "filename": host_file.name,
        "size_bytes": size,
        "mime_type": mime_type,
        "file_b64": file_b64,
        "host_path": str(host_file),
        "next_actions": [
            "Decode file_b64 (base64) to get the raw bytes",
            "list_downloads() — see all files in the downloads folder",
        ],
    }, indent=2)


@mcp.tool()
async def list_downloads() -> str:
    """List files in the BrowserOS downloads folder.

    Shows everything in {HOST_DATA_DIR}/downloads/ — files saved there by
    download_file() or by the browser's own download manager. Use this to
    discover files available to read_download(), or to confirm that a download
    landed successfully.
    """
    dl_dir = _downloads_dir()
    if not dl_dir.is_dir():
        return json.dumps({
            "files": [],
            "count": 0,
            "downloads_dir": str(dl_dir),
            "note": "downloads directory does not exist yet",
        }, indent=2)

    entries = []
    for p in sorted(dl_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if p.is_file():
            st = p.stat()
            entries.append({
                "name": p.name,
                "size_bytes": st.st_size,
                "modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                "mime_type": mimetypes.guess_type(p.name)[0] or "application/octet-stream",
            })

    return json.dumps({
        "files": entries,
        "count": len(entries),
        "downloads_dir": str(dl_dir),
        "next_actions": (
            [f"read_download('{entries[0]['name']}') — read the newest file"] if entries else
            ["download_file(page, element) — trigger a browser download first"]
        ),
    }, indent=2)


@mcp.tool()
async def read_download(filename: str) -> str:
    """Read a file from the downloads folder and return its bytes as base64.

    Use this to retrieve a file that was already downloaded (by download_file()
    or the browser's own download manager). Returns base64-encoded bytes plus
    MIME type so Claude Desktop can process or save the file.

    Args:
        filename: file name inside the downloads folder (from list_downloads)
    """
    dl_dir = _downloads_dir()
    host_file = dl_dir / filename

    # Basic path traversal guard
    try:
        host_file = host_file.resolve()
        dl_dir.resolve()
        host_file.relative_to(dl_dir.resolve())
    except (ValueError, RuntimeError):
        return json.dumps({"ok": False, "error": "invalid filename (path traversal rejected)"})

    if not host_file.is_file():
        available = [p.name for p in dl_dir.iterdir() if p.is_file()] if dl_dir.is_dir() else []
        return json.dumps({
            "ok": False,
            "error": f"file not found: {filename}",
            "available": available[:20],
            "next_actions": ["list_downloads() — see what files are available"],
        }, indent=2)

    size = host_file.stat().st_size
    mime_type = mimetypes.guess_type(host_file.name)[0] or "application/octet-stream"

    if size > _INLINE_MAX_BYTES:
        return json.dumps({
            "ok": True,
            "filename": host_file.name,
            "size_bytes": size,
            "mime_type": mime_type,
            "file_b64": None,
            "host_path": str(host_file),
            "note": f"File exceeds {_INLINE_MAX_BYTES // 1024 // 1024} MB inline limit.",
        }, indent=2)

    file_b64 = base64.b64encode(host_file.read_bytes()).decode()
    return json.dumps({
        "ok": True,
        "filename": host_file.name,
        "size_bytes": size,
        "mime_type": mime_type,
        "file_b64": file_b64,
        "host_path": str(host_file),
    }, indent=2)


@mcp.tool()
async def smart_browseros_info() -> str:
    """Diagnostic: BROWSEROS_URL, host data dir, container paths, recipe count."""
    dl_dir = _downloads_dir()
    staging_dir = Path(HOST_DATA_DIR) / "inline_uploads"
    return json.dumps({
        "browseros_url": BROWSEROS_URL,
        "host_data_dir": HOST_DATA_DIR,
        "host_data_dir_exists": Path(HOST_DATA_DIR).is_dir(),
        "container_data": CONTAINER_DATA,
        "downloads_dir": str(dl_dir),
        "downloads_dir_exists": dl_dir.is_dir(),
        "downloads_count": sum(1 for p in dl_dir.iterdir() if p.is_file()) if dl_dir.is_dir() else 0,
        "upload_staging_dir": str(staging_dir),
        "upload_staging_max_age_seconds": _UPLOAD_STAGING_MAX_AGE,
        "inline_max_bytes": _INLINE_MAX_BYTES,
        "recipe_count": len(RECIPES),
        "recipes": [r.id for r in RECIPES],
    }, indent=2)


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
