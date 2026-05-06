#!/usr/bin/env python3
"""calendly_mcp — Calendly (event types + scheduled meetings)."""
from __future__ import annotations
import asyncio, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mcp.server.fastmcp import FastMCP
from mcp_lib import client, ensure_tab, js

mcp = FastMCP("calendly")
async def _tab(): return await ensure_tab("calendly.com", "https://calendly.com/event_types/user/me")

@mcp.tool()
async def calendly_list_event_types() -> str:
    """List your Calendly event types (the bookable links you've configured)."""
    pid = await _tab()
    await client().call("navigate_page", {"page": pid, "url": "https://calendly.com/event_types/user/me"})
    await asyncio.sleep(2.0)
    items = await js(pid, """
      (() => {
        const cards = document.querySelectorAll('div[data-component="EventType"], li.event-type, div.event-type-card');
        return JSON.stringify(Array.from(cards).slice(0,20).map((c,i)=>({
          index: i,
          title: c.querySelector('h3, .event-type-name')?.innerText?.trim() || '',
          duration: c.querySelector('.event-type-duration, [data-component="duration"]')?.innerText?.trim() || '',
          link: c.querySelector('a[href*="calendly.com/"]')?.href || '',
        })));
      })()
    """)
    if isinstance(items,str): items=json.loads(items)
    return json.dumps({"event_types":items,"count":len(items)},indent=2,ensure_ascii=False)

@mcp.tool()
async def calendly_list_scheduled(count: int = 15) -> str:
    """List your upcoming scheduled meetings on Calendly."""
    pid = await _tab()
    await client().call("navigate_page", {"page": pid, "url": "https://calendly.com/scheduled_events"})
    await asyncio.sleep(2.0)
    items = await js(pid, f"""
      (() => {{
        const rows = document.querySelectorAll('div[data-component="EventListItem"], tr.scheduled-event, li.event-row');
        return JSON.stringify(Array.from(rows).slice(0,{count}).map((r,i)=>({{
          index: i,
          when: r.querySelector('time, .start-time, .event-time')?.innerText?.trim() || '',
          invitee: r.querySelector('.invitee-name, [data-component="InviteeName"]')?.innerText?.trim() || '',
          eventType: r.querySelector('.event-type-name, [data-component="EventTypeName"]')?.innerText?.trim() || '',
          status: r.querySelector('.status-badge, .event-status')?.innerText?.trim() || '',
        }})));
      }})()
    """)
    if isinstance(items,str): items=json.loads(items)
    return json.dumps({"meetings":items,"count":len(items)},indent=2,ensure_ascii=False)

def main(): mcp.run(transport="stdio")
if __name__ == "__main__": main()
