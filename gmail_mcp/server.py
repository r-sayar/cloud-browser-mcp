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

BROWSEROS_URL = os.environ.get("BROWSEROS_URL", "http://localhost:9201/mcp")
GMAIL_URL = "https://mail.google.com/mail/u/0/#inbox"

mcp = FastMCP("gmail")


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
async def _active_page_id() -> int:
    """Return the page id of the active tab, opening Gmail if needed."""
    pages = await _client().call("list_pages", {})
    page_list = pages.get("pages", []) if isinstance(pages, dict) else []
    for p in page_list:
        if "mail.google.com" in p.get("url", ""):
            return p["pageId"]
    # No Gmail tab open; navigate the first page to Gmail.
    if page_list:
        pid = page_list[0]["pageId"]
        await _client().call("navigate_page", {"page": pid, "url": GMAIL_URL})
        await asyncio.sleep(2.5)
        return pid
    # No pages at all — open one
    res = await _client().call("new_page", {"url": GMAIL_URL})
    await asyncio.sleep(2.5)
    return res.get("pageId", 1) if isinstance(res, dict) else 1


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
        return (f"Gmail is not logged in (url={info.get('url')}). "
                "Open http://localhost:6081/ to sign in inside the cloud browser, "
                "then retry.")
    return f"inbox open. signed in as {info.get('account', 'unknown')}; title={info['title']}"


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
    return json.dumps(result, indent=2, ensure_ascii=False)


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
        const body = last ? (last.querySelector('div.a3s, div.ii.gt')?.innerText || '') : '';
        return JSON.stringify({
          subject: subj ? subj.innerText.trim() : '(could not read subject)',
          messageCount: msgs.length,
          body: body.slice(0, 4000)
        });
      })()
    """)
    if isinstance(body, str):
        body = json.loads(body)
    return json.dumps(body, indent=2, ensure_ascii=False)


@mcp.tool()
async def gmail_compose(to: str, subject: str, body: str, send: bool = False) -> str:
    """Open the Gmail composer and fill it. If send=True, send immediately.

    The composer must already work — this tool clicks the Compose button
    (`div[gh="cm"]`), waits for the dialog, fills the To/Subject/Body inputs,
    and optionally hits the Send button.
    """
    pid = await _active_page_id()
    # Make sure we're at the inbox so Compose is visible
    cur_url = await _js("location.href")
    if "mail.google.com" not in (cur_url or ""):
        await _client().call("navigate_page", {"page": pid, "url": GMAIL_URL})
        await asyncio.sleep(1.5)

    # Cached recipe: click compose, wait for dialog, fill, optionally send.
    safe_to = json.dumps(to)
    safe_subj = json.dumps(subject)
    safe_body = json.dumps(body)
    send_flag = "true" if send else "false"

    js = f"""
      (async () => {{
        const log = [];
        const wait = ms => new Promise(r => setTimeout(r, ms));
        const waitFor = async (sel, t=8000) => {{
          const start = Date.now();
          while (Date.now() - start < t) {{
            const el = document.querySelector(sel);
            if (el) return el;
            await wait(100);
          }}
          throw new Error('timeout: ' + sel);
        }};
        // 1. Click compose
        const composeBtn = await waitFor('div[gh="cm"]');
        composeBtn.click();
        log.push('clicked compose');

        // 2. Wait for the composer dialog
        const dialog = await waitFor('div[role="dialog"][aria-label*="essage"], div.M9');
        await wait(300);

        // 3. Fill the "To" field
        const toField = dialog.querySelector('input[aria-label*="To recipients" i], textarea[name="to"]');
        if (!toField) throw new Error('To field not found');
        toField.focus();
        toField.value = '';
        toField.dispatchEvent(new Event('focus', {{bubbles: true}}));
        document.execCommand('insertText', false, {safe_to});
        log.push('filled to');

        // 4. Subject
        const subjField = dialog.querySelector('input[name="subjectbox"]');
        if (!subjField) throw new Error('Subject field not found');
        subjField.focus();
        subjField.value = '';
        document.execCommand('insertText', false, {safe_subj});
        log.push('filled subject');

        // 5. Body — Gmail uses a contenteditable div, not a textarea
        const bodyEl = dialog.querySelector('div[role="textbox"][aria-label*="Message Body"], div[g_editable="true"]');
        if (!bodyEl) throw new Error('Body field not found');
        bodyEl.focus();
        document.execCommand('insertText', false, {safe_body});
        log.push('filled body');

        // 6. Optionally send
        if ({send_flag}) {{
          const sendBtn = dialog.querySelector('div[role="button"][aria-label*="Send"], div[role="button"][data-tooltip*="Send"]');
          if (!sendBtn) throw new Error('Send button not found');
          sendBtn.click();
          log.push('clicked send');
        }} else {{
          log.push('left as draft');
        }}

        return JSON.stringify({{ok: true, steps: log}});
      }})()
    """
    out = await _js(js)
    if isinstance(out, str):
        try:
            out = json.loads(out)
        except Exception:
            return f"compose result: {out}"
    return json.dumps(out, indent=2)


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
    return json.dumps(out)


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
