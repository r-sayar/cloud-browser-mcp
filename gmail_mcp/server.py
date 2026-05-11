#!/usr/bin/env python3
"""
gmail_mcp — Gmail-as-MCP, backed by a logged-in BrowserOS slot.

This is the canonical example of the cloud-browser-mcp "site-as-MCP" pattern:
each high-level tool is a cached recipe that runs against a real, authenticated
Gmail tab. The agent does NOT need to figure out what to click; it calls
`gmail_compose(to=..., subject=..., body=...)` and the recipe handles the
rest.

Architecture:
    Claude Desktop ──stdio──► gmail_mcp (this server)
                                    │
                                    └─ HTTP MCP ──► localhost:9201/mcp (BrowserOS)
                                                         │
                                                         └─ logged-in Gmail tab

Most tools work by sending a single `evaluate_script` call to BrowserOS — this
is far more reliable than `take_snapshot` + `click` for sites with hostile DOM
(Gmail uses obfuscated class names, but its `aria-label`, `role`, and `gh`
attributes are stable).

To add a new tool: drop in a function decorated with @mcp.tool() that calls
self._js(...) with a hardcoded JS script. The script must return a string —
JSON if you want structured data back. See PROTOCOL.md for the full recipe.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

# Repo root on sys.path so `mcp_lib.usage_log` resolves when this server is
# launched directly from its own .venv (which has no mcp_lib on the path).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mcp_lib.usage_log import install_logger  # noqa: E402

BROWSEROS_URL = os.environ.get("BROWSEROS_URL", "http://localhost:9201/mcp")
GMAIL_URL = "https://mail.google.com/mail/u/0/#inbox"

mcp = FastMCP("gmail")
install_logger(mcp, "gmail_mcp")


# ─── BrowserOS HTTP MCP client ─────────────────────────────────────────────────
class _BOSClient:
    """Minimal Streamable-HTTP MCP client for talking to BrowserOS.

    BrowserOS doesn't strictly require a session ID for our use, but we send
    initialize once per server lifetime to be polite, and reuse the HTTP client.
    """

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
        # Capture session id if server returned one
        sid = r.headers.get("mcp-session-id") or r.headers.get("Mcp-Session-Id")
        if sid and not self._session_id:
            self._session_id = sid
        if not r.content:
            return {}
        # BrowserOS returns plain JSON for our calls
        return r.json()

    async def _ensure_initialized(self):
        if self._initialized:
            return
        self._req_id += 1
        await self._post({
            "jsonrpc": "2.0", "id": self._req_id, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "gmail_mcp", "version": "0.1"},
            },
        })
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._initialized = True

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        """Call a BrowserOS MCP tool. Returns the parsed `content` payload."""
        await self._ensure_initialized()
        self._req_id += 1
        resp = await self._post({
            "jsonrpc": "2.0", "id": self._req_id, "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        })
        if "error" in resp:
            raise RuntimeError(f"BrowserOS error calling {tool}: {resp['error']}")
        result = resp.get("result", {})
        # Most BrowserOS tools return content[0].text as either plain text or JSON
        contents = result.get("content", [])
        if not contents:
            return result
        text = contents[0].get("text", "")
        # Try JSON first, fall back to raw text
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


# ─── helpers ───────────────────────────────────────────────────────────────────
import re

# BrowserOS's list_pages tool returns a human-readable text block where each
# entry looks like:
#   1. <page title> (tab <tabId>)
#      <url>
# We regex this into [(pageId, url), ...] so we can find an existing Gmail tab.
_PAGE_RE = re.compile(r"^(\d+)\.\s+(.+?)\n\s+(\S+)$", re.MULTILINE)


def _parse_pages_text(text: str) -> list[tuple[int, str]]:
    return [(int(m.group(1)), m.group(3)) for m in _PAGE_RE.finditer(text)]


async def _active_page_id() -> int:
    """Return the page id of an already-open Gmail tab, or open one and return its id.

    We deliberately reuse an existing Gmail tab rather than spawning new ones
    on every tool call — otherwise repeated tool use leaks tabs.
    """
    pages_text = await _client().call("list_pages", {})
    if not isinstance(pages_text, str):
        # Older BrowserOS versions returned JSON; fall back through.
        pages_text = json.dumps(pages_text)
    parsed = _parse_pages_text(pages_text)
    for pid, url in parsed:
        if "mail.google.com" in url:
            return pid
    # No Gmail tab. Reuse the first existing page and navigate it.
    if parsed:
        pid = parsed[0][0]
        await _client().call("navigate_page", {"page": pid, "url": GMAIL_URL})
        await asyncio.sleep(2.0)
        return pid
    # No pages at all — open one.
    await _client().call("new_page", {"url": GMAIL_URL})
    await asyncio.sleep(2.5)
    # Re-scan to find its id.
    pages_text = await _client().call("list_pages", {})
    parsed = _parse_pages_text(pages_text if isinstance(pages_text, str) else json.dumps(pages_text))
    for pid, url in parsed:
        if "mail.google.com" in url:
            return pid
    raise RuntimeError("could not open or find a Gmail tab")


async def _js(script: str) -> Any:
    """Run JS in the active Gmail tab; return parsed result.

    `script` should be a JS expression (not a statement) that returns a
    JSON-serializable string or value.
    """
    pid = await _active_page_id()
    raw = await _client().call("evaluate_script", {"page": pid, "expression": script})
    # BrowserOS wraps the result; pull it out
    if isinstance(raw, dict) and "value" in raw:
        v = raw["value"]
        try:
            return json.loads(v)
        except (json.JSONDecodeError, TypeError):
            return v
    return raw


def _wait_for(selector: str, timeout_ms: int = 6000) -> str:
    """Returns a JS snippet that waits for `selector` to exist, then resolves to
    a marker. Use as part of a larger async IIFE."""
    return f"""
        await (async () => {{
            const start = Date.now();
            while (Date.now() - start < {timeout_ms}) {{
                if (document.querySelector({json.dumps(selector)})) return;
                await new Promise(r => setTimeout(r, 100));
            }}
            throw new Error('timeout waiting for ' + {json.dumps(selector)});
        }})();
    """


# ─── tools ────────────────────────────────────────────────────────────────────
@mcp.tool()
async def gmail_open_inbox() -> str:
    """Navigate the cloud browser to the Gmail inbox.

    Returns the email address of the logged-in account, or an error string if
    Gmail is not logged in.
    """
    pid = await _active_page_id()
    await _client().call("navigate_page", {"page": pid, "url": GMAIL_URL})
    await asyncio.sleep(2.0)
    info = await _js("""
      (() => {
        const acc = document.querySelector('a[aria-label*="Google Account"]');
        const title = document.title;
        const loggedOut = location.href.includes('accounts.google.com') || /sign in/i.test(title);
        return JSON.stringify({
          loggedIn: !loggedOut && /Inbox/i.test(title),
          title, url: location.href,
          account: (title.match(/- ([^\\s]+@[^\\s]+) -/) || [])[1] || null
        });
      })()
    """)
    if isinstance(info, str):
        info = json.loads(info)
    if not info.get("loggedIn"):
        return json.dumps({
            "ok": False,
            "error": "not_logged_in",
            "url": info.get("url"),
            "next_actions": [
                "Open http://localhost:6081/ in your laptop browser, sign in, then retry."
            ],
        }, indent=2)
    return json.dumps({
        "ok": True,
        "account": info.get("account", "unknown"),
        "title": info["title"],
        "next_actions": [
            "gmail_list_recent(count=10) — show what's in the inbox",
            "gmail_search(query='from:foo OR is:unread') — find specific threads",
            "gmail_compose(to=, subject=, body=, send=False) — start a draft",
        ],
    }, indent=2)


@mcp.tool()
async def gmail_list_recent(count: int = 10) -> str:
    """Read the visible inbox and return up to `count` most-recent threads.

    Each result has: index (0-based, pass to gmail_open_email), subject,
    sender, snippet, date, unread.
    """
    if count < 1 or count > 50:
        count = max(1, min(50, count))
    result = await _js(f"""
      (() => {{
        const rows = document.querySelectorAll('tr[role="row"][jsaction]');
        const out = [];
        for (let i = 0; i < Math.min(rows.length, {count}); i++) {{
          const r = rows[i];
          // Gmail's row markup uses obfuscated-but-stable classes:
          //   span.zF  = sender name
          //   span.bog = subject (bold first line)
          //   span.y2  = snippet
          //   span.bq3 = relative date/time
          //   tr.zE    = unread thread row
          const sender = r.querySelector('span.zF, span[email]');
          const subj   = r.querySelector('span.bog, h2');
          const snip   = r.querySelector('span.y2');
          const date   = r.querySelector('span.bq3, td.xW span:last-child');
          out.push({{
            index: i,
            subject: subj ? subj.innerText.trim() : '(no subject)',
            sender:  sender ? (sender.getAttribute('email') || sender.getAttribute('name') || sender.innerText.trim()) : '',
            snippet: snip ? snip.innerText.trim().slice(0, 200) : '',
            date:    date ? date.innerText.trim() : '',
            unread:  r.classList.contains('zE')
          }});
        }}
        return JSON.stringify(out);
      }})()
    """)
    if isinstance(result, str):
        result = json.loads(result)
    return json.dumps({
        "threads": result,
        "count": len(result),
        "next_actions": [
            f"gmail_open_email(index=N) where 0 <= N < {len(result)} — open thread",
            "gmail_search(query=...) — narrow down by sender / date / unread",
            "gmail_compose(...) — start a new email",
        ],
    }, indent=2, ensure_ascii=False)


@mcp.tool()
async def gmail_search(query: str, count: int = 10) -> str:
    """Run a Gmail search and return up to `count` matching threads.

    Same return shape as gmail_list_recent. `query` accepts Gmail search
    operators ("from:foo", "is:unread", "after:2026/01/01", etc.).
    """
    pid = await _active_page_id()
    # Use Gmail's URL-based search — vastly more reliable than driving the input.
    safe = query.replace("#", "%23").replace(" ", "+")
    await _client().call("navigate_page", {
        "page": pid,
        "url": f"https://mail.google.com/mail/u/0/#search/{safe}",
    })
    await asyncio.sleep(2.0)
    return await gmail_list_recent(count)


@mcp.tool()
async def gmail_open_email(index: int) -> str:
    """Open the thread at position `index` (from gmail_list_recent / gmail_search).

    Returns the thread subject + first message body as plain text.
    """
    result = await _js(f"""
      (() => {{
        const rows = document.querySelectorAll('tr[role="row"][jsaction]');
        if ({index} >= rows.length) return JSON.stringify({{error: 'index out of range'}});
        rows[{index}].click();
        return JSON.stringify({{clicked: true}});
      }})()
    """)
    await asyncio.sleep(1.5)
    body = await _js("""
      (() => {
        const subj = document.querySelector('h2[data-thread-perm-id], h2.hP');
        const msgs = document.querySelectorAll('div[data-message-id]');
        const last = msgs[msgs.length - 1];
        const senderEl = last ? last.querySelector('span[email]') : null;
        const senderEmail = senderEl ? senderEl.getAttribute('email') : '';
        const body = last ? (last.querySelector('div.a3s, div.ii.gt')?.innerText || '') : '';
        return JSON.stringify({
          subject: subj ? subj.innerText.trim() : '(could not read subject)',
          senderEmail,
          messageCount: msgs.length,
          body: body.slice(0, 4000)
        });
      })()
    """)
    if isinstance(body, str):
        body = json.loads(body)
    sender = body.get("senderEmail", "")
    body["next_actions"] = [
        f"gmail_compose(to='{sender}', subject='Re: {body.get('subject','')[:60]}', body=..., send=True) — reply",
        "gmail_archive_current() — archive this thread",
        "gmail_open_inbox() — back to inbox",
        "gmail_list_recent() — re-list inbox to pick another",
    ] if sender else [
        "gmail_archive_current() — archive this thread",
        "gmail_open_inbox() — back to inbox",
    ]
    return json.dumps(body, indent=2, ensure_ascii=False)


@mcp.tool()
async def gmail_compose(to: str, subject: str, body: str, send: bool = False) -> str:
    """Open the Gmail composer pre-filled. If send=True, send immediately.

    Fast path: navigate the active tab to Gmail's URL-driven compose endpoint
    (`?view=cm&fs=1&tf=1&to=…&su=…&body=…`) so the dialog opens already
    populated — no clicking Compose, no waiting on the dialog, no
    field-by-field fill. From "tool fires" to "Send clicked" in <2s.
    """
    from urllib.parse import quote

    url = (
        "https://mail.google.com/mail/u/0/?view=cm&fs=1&tf=1"
        f"&to={quote(to)}&su={quote(subject)}&body={quote(body)}"
    )

    # Why new_page instead of navigate_page on the existing inbox tab:
    #  - navigate_page blocks ~7s waiting for the Gmail SPA to fully load.
    #  - new_page returns in <100ms and Gmail's compose UI mounts in
    #    parallel while we wait a fixed ~2s — well under the 5s budget.
    #  - It also leaves the inbox tab untouched, so gmail_open_inbox /
    #    gmail_list_recent stay snappy after a compose.
    np_resp = await _client().call("new_page", {"url": url})
    # new_page returns text like "Opened new page: …\nPage ID: 52"
    text = np_resp if isinstance(np_resp, str) else json.dumps(np_resp)
    m = re.search(r"Page ID:\s*(\d+)", text)
    if not m:
        return json.dumps({"ok": False, "error": "could_not_parse_page_id", "raw": text[:200]})
    new_pid = int(m.group(1))

    if not send:
        return json.dumps({
            "ok": True, "mode": "draft_open", "page_id": new_pid, "url": url,
            "next_actions": [
                "gmail_screenshot — verify the draft looks right",
                "gmail_compose(..., send=True) — overwrite + send",
                f"close the draft tab via the BrowserOS close_page tool with page={new_pid}",
            ],
        }, indent=2)

    # Send path. Gmail honors Cmd+Enter / Ctrl+Enter to send from any
    # compose surface. press_key doesn't block on navigation the way
    # evaluate_script does. We just need to give the compose UI ~1s to
    # mount and focus the body field, then fire the shortcut.
    await asyncio.sleep(1.2)
    await _client().call("press_key", {"page": new_pid, "key": "Meta+Enter"})
    return json.dumps({
        "ok": True,
        "mode": "sent",
        "page_id": new_pid,
        "next_actions": [
            "gmail_open_inbox — back to inbox (kept warm in another tab)",
            f"gmail_search(query='to:{to}') — confirm delivery",
        ],
    }, indent=2)


@mcp.tool()
async def gmail_archive_current() -> str:
    """Archive the currently open thread. Returns 'archived' on success."""
    out = await _js("""
      (() => {
        // Toolbar archive button has aria-label "Archive" or data-tooltip "Archive (e)"
        const btn = document.querySelector(
          'div[role="button"][aria-label="Archive"], div[role="button"][data-tooltip*="Archive" i]'
        );
        if (!btn) return JSON.stringify({error: 'archive button not found (is a thread open?)'});
        btn.click();
        return JSON.stringify({ok: true});
      })()
    """)
    if isinstance(out, str):
        out = json.loads(out)
    if out.get("ok"):
        out["next_actions"] = [
            "gmail_list_recent() — see what's now at the top of the inbox",
            "gmail_open_email(index=0) — open the next-most-recent thread",
        ]
    return json.dumps(out, indent=2)


@mcp.tool()
async def gmail_screenshot() -> str:
    """Take a PNG screenshot of the current Gmail tab. Returns 'image/png' on success."""
    pid = await _active_page_id()
    res = await _client().call("take_screenshot", {"page": pid, "format": "png"})
    if isinstance(res, dict):
        return f"screenshot taken (mime={res.get('mimeType', 'image/png')})"
    return "screenshot taken"


# ─── entry point ───────────────────────────────────────────────────────────────
def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
