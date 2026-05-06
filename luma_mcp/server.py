#!/usr/bin/env python3
"""luma_mcp — Luma (events) as a high-level MCP."""
from __future__ import annotations
import asyncio, json, os, sys
from urllib.parse import quote
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mcp.server.fastmcp import FastMCP
from mcp_lib import client, ensure_tab, js

mcp = FastMCP("luma")
async def _tab(): return await ensure_tab("luma.com", "https://lu.ma/home")

@mcp.tool()
async def luma_list_upcoming(count: int = 15) -> str:
    """List upcoming events the user is attending or invited to."""
    pid = await _tab()
    await client().call("navigate_page", {"page": pid, "url": "https://lu.ma/home"})
    await asyncio.sleep(2.0)
    items = await js(pid, f"""
      (() => {{
        const cards = document.querySelectorAll('a[href^="/"]:has(div), div.event-card, a.event-link');
        const out = [];
        const seen = new Set();
        for (const c of cards) {{
          const link = c.matches('a') ? c : c.querySelector('a');
          if (!link || !/^\\/(?!home|create|event)/.test(link.getAttribute('href') || '')) continue;
          if (seen.has(link.href)) continue;
          seen.add(link.href);
          const title = (link.querySelector('h3, .event-name, .title')?.innerText || link.innerText).trim().slice(0,200);
          const when = c.querySelector('.event-time, time, .date')?.innerText?.trim() || '';
          out.push({{ index: out.length, title, when, url: link.href }});
          if (out.length >= {count}) break;
        }}
        return JSON.stringify(out);
      }})()
    """)
    if isinstance(items,str): items=json.loads(items)
    return json.dumps({"events":items,"count":len(items),"next_actions":["luma_view_event(url='https://lu.ma/...')"]},indent=2,ensure_ascii=False)

@mcp.tool()
async def luma_view_event(url: str) -> str:
    """Open an event; return title, host, time, location, description."""
    pid = await _tab()
    await client().call("navigate_page", {"page": pid, "url": url})
    await asyncio.sleep(1.8)
    info = await js(pid, """
      (() => ({
        title: document.querySelector('h1')?.innerText?.trim() || '',
        host: document.querySelector('.host-row .name, .host-name, .organizer-name')?.innerText?.trim() || '',
        when: document.querySelector('time, .when-row, .event-time')?.innerText?.trim() || '',
        where: document.querySelector('.location-row, .where-row, .event-location')?.innerText?.trim() || '',
        description: document.querySelector('.event-description, .about-row')?.innerText?.trim()?.slice(0,2000) || '',
      }))()
    """)
    if isinstance(info,str): info=json.loads(info)
    info["next_actions"] = ["luma_rsvp(url='...') — register","luma_list_upcoming()"]
    return json.dumps(info,indent=2,ensure_ascii=False)

@mcp.tool()
async def luma_rsvp(url: str) -> str:
    """Click the Register / RSVP button on an event page."""
    pid = await _tab()
    await client().call("navigate_page", {"page": pid, "url": url})
    await asyncio.sleep(1.5)
    out = await js(pid, """
      (() => {
        const btn = Array.from(document.querySelectorAll('button')).find(b => /^(register|rsvp|attend|join|request)/i.test(b.innerText.trim()));
        if (!btn) return JSON.stringify({error:'register/RSVP button not found — possibly already registered or event is closed'});
        btn.click();
        return JSON.stringify({ok:true,clicked:btn.innerText.trim()});
      })()
    """)
    if isinstance(out,str): out=json.loads(out)
    return json.dumps(out,indent=2)

def main(): mcp.run(transport="stdio")
if __name__ == "__main__": main()
