#!/usr/bin/env python3
"""outlook_mcp — Outlook Web (Office 365 / FU Berlin / any OWA) as a
high-level MCP. Same shape as gmail_mcp but Outlook-specific recipes:
URL-driven compose via /mail/deeplink/compose, list/search via the inbox
DOM hooks (aria-label="Message list").

Sign in once via http://localhost:6081/ (route to https://outlook.office.com
or your tenant URL) before these tools work.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp.server.fastmcp import FastMCP
from mcp_lib import client, ensure_tab, js, parse_new_page_id

OUTLOOK_HOME = "https://outlook.office.com/mail/"
mcp = FastMCP("outlook")


async def _tab() -> int:
    return await ensure_tab("outlook.office.com", OUTLOOK_HOME)


async def _logged_in(pid: int) -> tuple[bool, str]:
    info = await js(pid, """
      JSON.stringify({
        url: location.href,
        loggedIn: location.href.startsWith('https://outlook.office.com/mail') &&
                  !location.href.includes('login.microsoftonline'),
      })
    """)
    if isinstance(info, str): info = json.loads(info)
    return info.get("loggedIn", False), info.get("url", "")


def _login_error(url: str) -> str:
    return json.dumps({
        "ok": False, "error": "not_logged_in", "url": url,
        "next_actions": [
            "Open http://localhost:6081/ on your laptop, sign into outlook.office.com, retry.",
        ],
    }, indent=2)


@mcp.tool()
async def outlook_open_inbox() -> str:
    """Navigate to the Outlook inbox."""
    pid = await _tab()
    await client().call("navigate_page", {"page": pid, "url": OUTLOOK_HOME})
    await asyncio.sleep(2.0)
    ok, url = await _logged_in(pid)
    if not ok: return _login_error(url)
    return json.dumps({"ok": True, "page_id": pid, "next_actions": [
        "outlook_list_recent(count=10)",
        "outlook_search(query='from:foo')",
        "outlook_compose(to=, subject=, body=, send=False)",
    ]}, indent=2)


@mcp.tool()
async def outlook_list_recent(count: int = 10) -> str:
    """Read the visible inbox; return up to `count` recent messages."""
    pid = await _tab()
    ok, url = await _logged_in(pid)
    if not ok: return _login_error(url)
    rows = await js(pid, f"""
      (() => {{
        const rows = document.querySelectorAll(
          '[role="option"][aria-label], div[data-convid], div[data-folder-id]'
        );
        const visible = Array.from(rows).filter(r => r.offsetParent !== null).slice(0, {count});
        return JSON.stringify(visible.map((r, i) => {{
          const aria = r.getAttribute('aria-label') || '';
          const lines = aria.split(/, ?/);
          const subjEl = r.querySelector('[id$="-subject"], div.lvHighlightAllClass');
          return {{
            index: i,
            ariaLabel: aria.slice(0, 300),
            subject: subjEl ? subjEl.innerText.trim() : (lines[2] || ''),
            sender:  lines[0] || '',
            snippet: lines.slice(3).join(', ').slice(0, 200),
            unread:  /unread/i.test(aria),
          }};
        }}));
      }})()
    """)
    if isinstance(rows, str): rows = json.loads(rows)
    return json.dumps({
        "messages": rows, "count": len(rows),
        "next_actions": [
            f"outlook_open_email(index=N) where 0 <= N < {len(rows)}",
            "outlook_compose(...) — start a new email",
        ],
    }, indent=2, ensure_ascii=False)


@mcp.tool()
async def outlook_search(query: str, count: int = 10) -> str:
    """Run an Outlook search via URL. `query` accepts standard OWA search."""
    pid = await _tab()
    await client().call("navigate_page", {
        "page": pid,
        "url": f"https://outlook.office.com/mail/inbox/?searchquery={quote(query)}",
    })
    await asyncio.sleep(2.0)
    return await outlook_list_recent(count)


@mcp.tool()
async def outlook_open_email(index: int) -> str:
    """Open the message at position `index` from outlook_list_recent."""
    out = await js(await _tab(), f"""
      (() => {{
        const rows = Array.from(document.querySelectorAll(
          '[role="option"][aria-label], div[data-convid]'
        )).filter(r => r.offsetParent !== null);
        if ({index} >= rows.length) return JSON.stringify({{error: 'index_out_of_range'}});
        rows[{index}].click();
        return JSON.stringify({{clicked: true}});
      }})()
    """)
    await asyncio.sleep(1.0)
    body = await js(await _tab(), """
      (() => {
        const subj = document.querySelector('[role="heading"][id*="subject" i], div.allowTextSelection h1');
        const bodyEl = document.querySelector('div[role="document"], div.rps_xxx, div[id*="UniqueMessageBody"]');
        return JSON.stringify({
          subject: subj ? subj.innerText.trim() : '(could not read subject)',
          body: bodyEl ? bodyEl.innerText.slice(0, 4000) : '',
        });
      })()
    """)
    if isinstance(body, str): body = json.loads(body)
    body["next_actions"] = [
        "outlook_compose(to=…, subject='Re: …', body=…, send=True) — reply",
        "outlook_archive_current() — archive",
        "outlook_open_inbox() — back",
    ]
    return json.dumps(body, indent=2, ensure_ascii=False)


@mcp.tool()
async def outlook_compose(to: str, subject: str, body: str, send: bool = False) -> str:
    """Open Outlook's URL-driven compose deeplink, optionally send via Ctrl+Enter.

    Outlook honors /mail/deeplink/compose?to=…&subject=…&body=… in a popup.
    """
    url = (
        "https://outlook.office.com/mail/deeplink/compose?"
        f"to={quote(to)}&subject={quote(subject)}&body={quote(body)}"
    )
    resp = await client().call("new_page", {"url": url})
    text = resp if isinstance(resp, str) else json.dumps(resp)
    new_pid = parse_new_page_id(text)
    if new_pid is None:
        return json.dumps({"ok": False, "error": "could_not_parse_page_id", "raw": text[:200]})
    if not send:
        return json.dumps({
            "ok": True, "mode": "draft_open", "page_id": new_pid, "url": url,
            "next_actions": ["outlook_compose(..., send=True) — overwrite + send"],
        }, indent=2)
    await asyncio.sleep(1.5)
    # Outlook send shortcut: Ctrl+Enter
    await client().call("press_key", {"page": new_pid, "key": "Control+Enter"})
    return json.dumps({
        "ok": True, "mode": "sent", "page_id": new_pid,
        "next_actions": [
            "outlook_open_inbox()",
            f"outlook_search('to:{to}') — confirm delivery",
        ],
    }, indent=2)


@mcp.tool()
async def outlook_archive_current() -> str:
    """Archive the currently open thread in Outlook."""
    out = await js(await _tab(), """
      (() => {
        const btn = document.querySelector(
          'button[aria-label*="Archive" i], button[name="Archive"]'
        );
        if (!btn) return JSON.stringify({error: 'archive button not found'});
        btn.click();
        return JSON.stringify({ok: true});
      })()
    """)
    if isinstance(out, str): out = json.loads(out)
    if out.get("ok"):
        out["next_actions"] = ["outlook_list_recent()"]
    return json.dumps(out, indent=2)


@mcp.tool()
async def outlook_screenshot() -> str:
    """PNG screenshot of the current Outlook tab."""
    pid = await _tab()
    await client().call("take_screenshot", {"page": pid, "format": "png"})
    return json.dumps({"ok": True, "mime": "image/png"})


def main(): mcp.run(transport="stdio")
if __name__ == "__main__": main()
