#!/usr/bin/env python3
"""fu_berlin_mcp — FU Berlin ZEDAT-Webmail (SquirrelMail) as a high-level MCP.

ZEDAT runs SquirrelMail behind their SAML SSO at webmail.zedat.fu-berlin.de.
SquirrelMail's URLs are stable + scriptable: compose.php, right_main.php,
read_body.php, search.php, move_messages.php. We navigate the tab to those
PHP endpoints directly instead of poking inside the 4-frame layout.

Sign in once via http://localhost:6081/ (handle SSO + TAN matrix manually)
before these tools work.
"""
from __future__ import annotations
import asyncio, json, os, sys
from urllib.parse import quote
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mcp.server.fastmcp import FastMCP
from mcp_lib import client, ensure_tab, js, parse_new_page_id

BASE = "https://webmail.zedat.fu-berlin.de"
HOME = f"{BASE}/src/webmail.php"
mcp = FastMCP("fu-berlin")


async def _tab():
    return await ensure_tab("webmail.zedat.fu-berlin.de", HOME)


async def _logged_in(pid):
    info = await js(pid, """
      JSON.stringify({
        url: location.href,
        loggedIn: location.host === 'webmail.zedat.fu-berlin.de' &&
                  !location.href.includes('login.php'),
      })
    """)
    if isinstance(info, str): info = json.loads(info)
    return info.get("loggedIn", False), info.get("url", "")


def _login_error(url):
    return json.dumps({
        "ok": False, "error": "not_logged_in", "url": url,
        "next_actions": [
            "Open http://localhost:6081/, sign into webmail.zedat.fu-berlin.de "
            "via FU Berlin SAML SSO + TAN matrix, retry.",
        ],
    }, indent=2)


@mcp.tool()
async def fu_berlin_open_inbox() -> str:
    """Open the SquirrelMail INBOX (full page, not frameset)."""
    pid = await _tab()
    await client().call("navigate_page", {
        "page": pid, "url": f"{BASE}/src/right_main.php?mailbox=INBOX&startMessage=1",
    })
    await asyncio.sleep(1.5)
    ok, url = await _logged_in(pid)
    if not ok: return _login_error(url)
    return json.dumps({"ok": True, "page_id": pid, "next_actions": [
        "fu_berlin_list_recent(count=10)",
        "fu_berlin_search(query='subject:abc')",
        "fu_berlin_compose(to=, subject=, body=, send=False)",
    ]}, indent=2)


@mcp.tool()
async def fu_berlin_list_recent(count: int = 15) -> str:
    """List recent inbox messages (subject, sender, date, passed_id)."""
    pid = await _tab()
    # right_main.php returns a plain HTML table of messages.
    await client().call("navigate_page", {
        "page": pid, "url": f"{BASE}/src/right_main.php?mailbox=INBOX&startMessage=1",
    })
    await asyncio.sleep(1.0)
    ok, url = await _logged_in(pid)
    if not ok: return _login_error(url)
    rows = await js(pid, f"""
      (() => {{
        // SquirrelMail message table: each row has a checkbox named 'msg[]' with the passed_id,
        // and 4-5 td columns (flag, sender, date, subject).
        const rows = document.querySelectorAll('tr.even, tr.odd, table tr');
        const out = [];
        for (const r of rows) {{
          const checkbox = r.querySelector('input[type="checkbox"][name="msg[]"]');
          if (!checkbox) continue;
          const passed_id = checkbox.value;
          const tds = r.querySelectorAll('td');
          if (tds.length < 4) continue;
          const link = r.querySelector('a[href*="read_body.php"]');
          const subjAndUrl = link ? {{subject: link.innerText.trim(), href: link.href}} : {{subject: '', href: ''}};
          out.push({{
            index: out.length,
            passed_id,
            sender:  tds[2] ? tds[2].innerText.trim() : '',
            date:    tds[3] ? tds[3].innerText.trim() : '',
            subject: subjAndUrl.subject.slice(0, 200),
            href:    subjAndUrl.href,
            unread:  /\\bbold\\b/.test(r.innerHTML) || r.classList.contains('unread'),
          }});
          if (out.length >= {count}) break;
        }}
        return JSON.stringify(out);
      }})()
    """)
    if isinstance(rows, str): rows = json.loads(rows)
    return json.dumps({
        "messages": rows, "count": len(rows),
        "next_actions": [
            f"fu_berlin_open_email(passed_id='123') for any of the {len(rows)} above",
            "fu_berlin_search(query='from:professor')",
        ],
    }, indent=2, ensure_ascii=False)


@mcp.tool()
async def fu_berlin_search(query: str, count: int = 10) -> str:
    """Search the INBOX by `query`. Searches the Subject field by default;
    prefix `from:` or `to:` to scope.
    """
    pid = await _tab()
    where = "SUBJECT"
    what = query
    low = query.lower()
    if low.startswith("from:"): where, what = "FROM", query[5:].strip()
    elif low.startswith("to:"): where, what = "TO", query[3:].strip()
    elif low.startswith("subject:"): where, what = "SUBJECT", query[8:].strip()
    elif low.startswith("body:"): where, what = "BODY", query[5:].strip()
    url = (f"{BASE}/src/search.php?mailbox=INBOX&submit=Search"
           f"&what={quote(what)}&where={where}&search_button_in_button=Search")
    await client().call("navigate_page", {"page": pid, "url": url})
    await asyncio.sleep(1.5)
    return await fu_berlin_list_recent(count)


@mcp.tool()
async def fu_berlin_open_email(passed_id: str, mailbox: str = "INBOX") -> str:
    """Open a specific message by `passed_id` (from fu_berlin_list_recent)."""
    pid = await _tab()
    await client().call("navigate_page", {
        "page": pid,
        "url": f"{BASE}/src/read_body.php?mailbox={quote(mailbox)}&passed_id={passed_id}&startMessage=1",
    })
    await asyncio.sleep(1.0)
    ok, url = await _logged_in(pid)
    if not ok: return _login_error(url)
    body = await js(pid, """
      (() => {
        const pickRow = label => {
          const ths = document.querySelectorAll('th');
          for (const th of ths) {
            if (new RegExp(label, 'i').test(th.innerText)) return th.nextElementSibling?.innerText?.trim() || '';
          }
          return '';
        };
        const subj = pickRow('Subject');
        const from = pickRow('From');
        const date = pickRow('Date');
        const bodyEl = document.querySelector('.entitybody, .header_body, table[bgcolor] tr td');
        const body = bodyEl ? bodyEl.innerText.slice(0, 4000) : (document.body.innerText.slice(0, 4000));
        return JSON.stringify({subject: subj, from, date, body});
      })()
    """)
    if isinstance(body, str): body = json.loads(body)
    body["next_actions"] = [
        f"fu_berlin_compose(to='{body.get('from','')[:60]}', subject='Re: {body.get('subject','')[:60]}', body='…') — reply",
        "fu_berlin_open_inbox() — back",
    ]
    return json.dumps(body, indent=2, ensure_ascii=False)


@mcp.tool()
async def fu_berlin_compose(to: str, subject: str, body: str, send: bool = False) -> str:
    """Open SquirrelMail's compose.php pre-filled. If send=True, click Send."""
    url = (f"{BASE}/src/compose.php?compose_new=yes"
           f"&send_to={quote(to)}&subject={quote(subject)}&body={quote(body)}")
    resp = await client().call("new_page", {"url": url})
    text = resp if isinstance(resp, str) else json.dumps(resp)
    new_pid = parse_new_page_id(text)
    if new_pid is None:
        return json.dumps({"ok": False, "error": "could_not_parse_page_id", "raw": text[:200]})
    if not send:
        return json.dumps({
            "ok": True, "mode": "draft_open", "page_id": new_pid, "url": url,
            "next_actions": ["fu_berlin_compose(..., send=True) — submit", "close the draft tab manually if abandoning"],
        }, indent=2)
    await asyncio.sleep(1.0)
    # Click the "Send" submit button on compose.php
    await js(new_pid, """
      (() => {
        const btn = document.querySelector('input[type="submit"][name="send"], input[value="Send" i]');
        if (btn) btn.click();
        return 'clicked';
      })()
    """)
    return json.dumps({
        "ok": True, "mode": "sent", "page_id": new_pid,
        "next_actions": [
            "fu_berlin_open_inbox()",
            f"fu_berlin_search(query='to:{to}') — confirm delivery",
        ],
    }, indent=2)


@mcp.tool()
async def fu_berlin_screenshot() -> str:
    """PNG screenshot of the current ZEDAT webmail tab."""
    pid = await _tab()
    await client().call("take_screenshot", {"page": pid, "format": "png"})
    return json.dumps({"ok": True, "mime": "image/png"})


def main(): mcp.run(transport="stdio")
if __name__ == "__main__": main()
