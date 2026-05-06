#!/usr/bin/env python3
"""zoom_mcp — Zoom web (list upcoming meetings, list recordings)."""
from __future__ import annotations
import asyncio, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mcp.server.fastmcp import FastMCP
from mcp_lib import client, ensure_tab, js

mcp = FastMCP("zoom")
async def _tab(): return await ensure_tab("zoom.us", "https://zoom.us/meeting")

@mcp.tool()
async def zoom_list_upcoming(count: int = 15) -> str:
    """List upcoming meetings on the user's Zoom account."""
    pid = await _tab()
    await client().call("navigate_page", {"page": pid, "url": "https://zoom.us/meeting"})
    await asyncio.sleep(2.0)
    items = await js(pid, f"""
      (() => {{
        const rows = document.querySelectorAll('tr.meeting-list-tr, tr.meeting-item, [data-test="meeting-row"]');
        return JSON.stringify(Array.from(rows).slice(0,{count}).map((r,i)=>({{
          index: i,
          topic: r.querySelector('td.meeting-info-tc, .meeting-title')?.innerText?.trim() || '',
          when: r.querySelector('td.meeting-time-tc, .meeting-time')?.innerText?.trim() || '',
          meetingId: r.querySelector('td.meeting-number-tc, .meeting-number')?.innerText?.trim() || '',
          joinUrl: r.querySelector('a[href*="/j/"], a.meeting-join-link')?.href || '',
        }})));
      }})()
    """)
    if isinstance(items,str): items=json.loads(items)
    return json.dumps({"meetings":items,"count":len(items),"next_actions":["zoom_list_recordings()"]},indent=2,ensure_ascii=False)

@mcp.tool()
async def zoom_list_recordings(count: int = 15) -> str:
    """List recent cloud recordings."""
    pid = await _tab()
    await client().call("navigate_page", {"page": pid, "url": "https://zoom.us/recording"})
    await asyncio.sleep(2.0)
    items = await js(pid, f"""
      (() => {{
        const rows = document.querySelectorAll('tr.recording-list-tr, tr.recording-item');
        return JSON.stringify(Array.from(rows).slice(0,{count}).map((r,i)=>({{
          index: i,
          topic: r.querySelector('.recording-topic, td.topic-tc')?.innerText?.trim() || '',
          startTime: r.querySelector('.recording-start-time')?.innerText?.trim() || '',
          duration: r.querySelector('.recording-duration')?.innerText?.trim() || '',
          link: r.querySelector('a[href*="/rec/share"]')?.href || '',
        }})));
      }})()
    """)
    if isinstance(items,str): items=json.loads(items)
    return json.dumps({"recordings":items,"count":len(items)},indent=2,ensure_ascii=False)

def main(): mcp.run(transport="stdio")
if __name__ == "__main__": main()
