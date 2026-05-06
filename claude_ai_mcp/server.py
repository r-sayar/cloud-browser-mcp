#!/usr/bin/env python3
"""claude_ai_mcp — claude.ai as a high-level MCP, backed by a logged-in
BrowserOS slot. Lets one Claude session manage another Claude.ai
conversation: list recents, open a thread, send a message, read the latest
response.

Sign in once via http://localhost:6081/ before these tools work.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

# allow importing the shared lib from one dir up
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp.server.fastmcp import FastMCP
from mcp_lib import client, ensure_tab, js

CLAUDE_HOME = "https://claude.ai/recents"
CLAUDE_NEW = "https://claude.ai/new"

mcp = FastMCP("claude-ai")


async def _tab() -> int:
    return await ensure_tab("claude.ai", CLAUDE_HOME)


async def _logged_in(pid: int) -> tuple[bool, str]:
    info = await js(pid, """
      JSON.stringify({
        url: location.href,
        title: document.title,
        loggedIn: !location.href.includes('/login') && !/sign in/i.test(document.title),
      })
    """)
    if isinstance(info, str):
        info = json.loads(info)
    return info.get("loggedIn", False), info.get("url", "")


def _login_error(url: str) -> str:
    return json.dumps({
        "ok": False, "error": "not_logged_in", "url": url,
        "next_actions": [
            "Open http://localhost:6081/ on your laptop, sign into claude.ai, retry.",
        ],
    }, indent=2)


@mcp.tool()
async def claude_ai_open_recents() -> str:
    """Navigate to the claude.ai 'Recents' (chat list) page."""
    pid = await _tab()
    await client().call("navigate_page", {"page": pid, "url": CLAUDE_HOME})
    await asyncio.sleep(1.5)
    ok, url = await _logged_in(pid)
    if not ok:
        return _login_error(url)
    return json.dumps({
        "ok": True, "page_id": pid,
        "next_actions": [
            "claude_ai_list_recent_chats(count=10)",
            "claude_ai_new_chat(message='…')",
        ],
    }, indent=2)


@mcp.tool()
async def claude_ai_list_recent_chats(count: int = 10) -> str:
    """List recent chats from /recents. Returns [{index, title, href, snippet}…]."""
    pid = await _tab()
    ok, url = await _logged_in(pid)
    if not ok:
        return _login_error(url)
    items = await js(pid, f"""
      (() => {{
        const links = Array.from(document.querySelectorAll('a[href*="/chat/"]')).slice(0, {count});
        const out = links.map((a, i) => ({{
          index: i,
          title: (a.querySelector('div, span')?.innerText || a.innerText || '').trim().slice(0, 120),
          href: a.href,
          snippet: a.innerText.trim().slice(0, 200),
        }}));
        return JSON.stringify(out);
      }})()
    """)
    if isinstance(items, str):
        items = json.loads(items)
    return json.dumps({
        "chats": items, "count": len(items),
        "next_actions": [
            f"claude_ai_open_chat(index=N) where 0 <= N < {len(items)}",
            "claude_ai_new_chat(message='…') — start fresh",
        ],
    }, indent=2)


@mcp.tool()
async def claude_ai_open_chat(index: int) -> str:
    """Open chat at position `index` from claude_ai_list_recent_chats."""
    pid = await _tab()
    href = await js(pid, f"""
      (() => {{
        const links = document.querySelectorAll('a[href*="/chat/"]');
        if ({index} >= links.length) return null;
        const a = links[{index}];
        a.click();
        return a.href;
      }})()
    """)
    await asyncio.sleep(1.0)
    if not href:
        return json.dumps({"ok": False, "error": "index_out_of_range"})
    return json.dumps({
        "ok": True, "url": href, "page_id": pid,
        "next_actions": [
            "claude_ai_get_last_response() — read what Claude last said",
            "claude_ai_send_message(message='…') — reply",
        ],
    }, indent=2)


@mcp.tool()
async def claude_ai_new_chat(message: str) -> str:
    """Start a fresh chat by navigating to /new and sending `message`."""
    pid = await _tab()
    await client().call("navigate_page", {"page": pid, "url": CLAUDE_NEW})
    await asyncio.sleep(1.5)
    ok, url = await _logged_in(pid)
    if not ok:
        return _login_error(url)
    return await claude_ai_send_message(message)


@mcp.tool()
async def claude_ai_send_message(message: str) -> str:
    """Type `message` into the active chat composer and send."""
    pid = await _tab()
    safe = json.dumps(message)
    out = await js(pid, f"""
      (async () => {{
        const wait = ms => new Promise(r => setTimeout(r, ms));
        const waitFor = async (sel, t=5000) => {{
          const start = Date.now();
          while (Date.now() - start < t) {{
            const el = document.querySelector(sel);
            if (el) return el;
            await wait(60);
          }}
          throw new Error('timeout: ' + sel);
        }};
        const composer = await waitFor(
          'div[contenteditable="true"][role="textbox"], textarea[name="prompt"]'
        );
        composer.focus();
        document.execCommand('insertText', false, {safe});
        await wait(120);
        // Send button has aria-label "Send Message" / "Send"
        const sendBtn = document.querySelector(
          'button[aria-label*="Send" i]:not([disabled]), button[type="submit"]:not([disabled])'
        );
        if (sendBtn) {{ sendBtn.click(); return JSON.stringify({{sent: 'click'}}); }}
        // Fallback: Enter
        composer.dispatchEvent(new KeyboardEvent('keydown', {{key: 'Enter', bubbles: true}}));
        return JSON.stringify({{sent: 'enter'}});
      }})()
    """)
    if isinstance(out, str):
        try: out = json.loads(out)
        except Exception: out = {"sent": "unknown", "raw": out}
    out["next_actions"] = [
        "claude_ai_get_last_response() — read the reply (give it a few seconds)",
    ]
    return json.dumps(out, indent=2)


@mcp.tool()
async def claude_ai_get_last_response() -> str:
    """Return the most recent assistant message in the current chat."""
    pid = await _tab()
    out = await js(pid, """
      (() => {
        const blocks = document.querySelectorAll(
          'div[data-test-render-count], div[data-testid*="assistant"], div.font-claude-message'
        );
        const last = blocks[blocks.length - 1];
        return JSON.stringify({
          text: last ? last.innerText.slice(0, 4000) : '',
          blockCount: blocks.length,
        });
      })()
    """)
    if isinstance(out, str): out = json.loads(out)
    out["next_actions"] = [
        "claude_ai_send_message('…') — follow up",
        "claude_ai_open_recents() — back to chat list",
    ]
    return json.dumps(out, indent=2)


@mcp.tool()
async def claude_ai_screenshot() -> str:
    """Take a PNG screenshot of the current claude.ai tab."""
    pid = await _tab()
    res = await client().call("take_screenshot", {"page": pid, "format": "png"})
    return json.dumps({"ok": True, "mime": "image/png"})


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
