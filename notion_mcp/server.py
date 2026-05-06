#!/usr/bin/env python3
"""notion_mcp — Notion as a high-level MCP."""
from __future__ import annotations
import asyncio, json, os, sys
from urllib.parse import quote
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mcp.server.fastmcp import FastMCP
from mcp_lib import client, ensure_tab, js

HOME = "https://www.notion.so/"
mcp = FastMCP("notion")

async def _tab(): return await ensure_tab("notion.so", HOME)

async def _logged_in(pid):
    info = await js(pid, """JSON.stringify({url:location.href,loggedIn:!location.href.includes('login')&&!/log in|sign up/i.test(document.title)})""")
    if isinstance(info,str): info=json.loads(info)
    return info.get("loggedIn",False), info.get("url","")

def _login(url): return json.dumps({"ok":False,"error":"not_logged_in","url":url,"next_actions":["http://localhost:6081/, sign in"]},indent=2)

@mcp.tool()
async def notion_search(query: str, count: int = 10) -> str:
    """Search across your Notion workspace."""
    pid = await _tab()
    # Notion's search dialog is triggered with Cmd+P; URL search works via /search/<query>
    await client().call("navigate_page", {"page": pid, "url": f"https://www.notion.so/search/{quote(query)}"})
    await asyncio.sleep(2.0)
    ok,url = await _logged_in(pid)
    if not ok: return _login(url)
    items = await js(pid, f"""
      (() => {{
        const rs = document.querySelectorAll('div[role="option"], a[href*="/notion.so/"]');
        const out = [];
        for (const r of rs) {{
          const t = r.innerText.trim().slice(0, 200);
          const link = r.matches('a') ? r : r.querySelector('a');
          if (!t || (!link && !r.dataset?.itemId)) continue;
          out.push({{ index: out.length, title: t, url: link?.href || '' }});
          if (out.length >= {count}) break;
        }}
        return JSON.stringify(out);
      }})()
    """)
    if isinstance(items,str): items=json.loads(items)
    return json.dumps({"results":items,"count":len(items),"next_actions":[
        "notion_open_page(url='https://www.notion.so/...')",
        "notion_create_page(parent_url='...', title='...', body='...')",
    ]},indent=2,ensure_ascii=False)

@mcp.tool()
async def notion_open_page(url: str) -> str:
    """Open a Notion page; return its title and visible text."""
    pid = await _tab()
    await client().call("navigate_page", {"page": pid, "url": url})
    await asyncio.sleep(2.0)
    body = await js(pid, """
      (() => {
        const title = document.querySelector('h1.notion-page-block, [placeholder="Untitled"]')?.innerText?.trim() || document.title;
        const main = document.querySelector('.notion-page-content, main, .notion-frame');
        return JSON.stringify({
          title,
          text: main ? main.innerText.slice(0, 6000) : document.body.innerText.slice(0, 4000),
        });
      })()
    """)
    if isinstance(body,str): body=json.loads(body)
    body["next_actions"] = ["notion_append_to_page(url, text='…')","notion_search('…')"]
    return json.dumps(body,indent=2,ensure_ascii=False)

@mcp.tool()
async def notion_list_recent(count: int = 15) -> str:
    """List your recently visited pages from the sidebar."""
    pid = await _tab()
    await client().call("navigate_page", {"page": pid, "url": HOME})
    await asyncio.sleep(2.0)
    ok,url = await _logged_in(pid)
    if not ok: return _login(url)
    items = await js(pid, f"""
      (() => {{
        // Sidebar "Private" / recent list — links with hrefs to /<workspace>/<page-id>
        const links = document.querySelectorAll('a[href^="/"]:not([href="/"])');
        const out = [];
        const seen = new Set();
        for (const a of links) {{
          const u = a.href;
          if (seen.has(u)) continue;
          if (!/^https:\\/\\/(www\\.)?notion\\.so\\/[^/]+\\/[a-f0-9]/.test(u) && !/-[a-f0-9]{{32}}$/.test(u)) continue;
          seen.add(u);
          out.push({{ index: out.length, title: a.innerText.trim().slice(0,160), url: u }});
          if (out.length >= {count}) break;
        }}
        return JSON.stringify(out);
      }})()
    """)
    if isinstance(items,str): items=json.loads(items)
    return json.dumps({"recent":items,"count":len(items)},indent=2,ensure_ascii=False)

@mcp.tool()
async def notion_append_to_page(url: str, text: str) -> str:
    """Append a paragraph of `text` to the end of the page at `url`."""
    pid = await _tab()
    await client().call("navigate_page", {"page": pid, "url": url})
    await asyncio.sleep(2.0)
    safe = json.dumps(text)
    out = await js(pid, f"""
      (async () => {{
        const wait = ms => new Promise(r => setTimeout(r, ms));
        // Click at the end of the page to focus the editor
        const editor = document.querySelector('.notion-page-content, [contenteditable="true"]');
        if (!editor) return JSON.stringify({{error:'editor not found'}});
        editor.focus();
        // Move cursor to end
        const range = document.createRange();
        range.selectNodeContents(editor);
        range.collapse(false);
        const sel = window.getSelection();
        sel.removeAllRanges(); sel.addRange(range);
        await wait(120);
        // Type a newline then the content via execCommand to ensure Notion's
        // contenteditable mutation observers pick it up.
        document.execCommand('insertText', false, '\\n' + {safe});
        return JSON.stringify({{ok:true,appended:{safe}.length+' chars'}});
      }})()
    """)
    if isinstance(out,str): out=json.loads(out)
    return json.dumps(out,indent=2)

@mcp.tool()
async def notion_create_page(title: str, body: str = "") -> str:
    """Create a new top-level page in the user's workspace.

    Notion exposes /new — opens a fresh untitled page. We type the title,
    Enter, then the body.
    """
    pid = await _tab()
    await client().call("navigate_page", {"page": pid, "url": "https://www.notion.so/new"})
    await asyncio.sleep(2.5)
    ok,url = await _logged_in(pid)
    if not ok: return _login(url)
    safe_t = json.dumps(title); safe_b = json.dumps(body)
    out = await js(pid, f"""
      (async () => {{
        const wait = ms => new Promise(r => setTimeout(r, ms));
        const titleEl = document.querySelector('[placeholder="Untitled" i], h1[contenteditable="true"]');
        if (!titleEl) return JSON.stringify({{error:'title field not found'}});
        titleEl.focus();
        document.execCommand('insertText', false, {safe_t});
        await wait(150);
        // Enter to leave the title and create a body block
        titleEl.dispatchEvent(new KeyboardEvent('keydown', {{key:'Enter',bubbles:true}}));
        await wait(250);
        if ({safe_b}) document.execCommand('insertText', false, {safe_b});
        return JSON.stringify({{ok:true,url:location.href}});
      }})()
    """)
    if isinstance(out,str): out=json.loads(out)
    return json.dumps(out,indent=2)

def main(): mcp.run(transport="stdio")
if __name__ == "__main__": main()
