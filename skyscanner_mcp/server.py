#!/usr/bin/env python3
"""skyscanner_mcp — Skyscanner (search flights, list saved). NO booking."""
from __future__ import annotations
import asyncio, json, os, sys
from urllib.parse import quote
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mcp.server.fastmcp import FastMCP
from mcp_lib import client, ensure_tab, js

mcp = FastMCP("skyscanner")
async def _tab(): return await ensure_tab("skyscanner", "https://www.skyscanner.com/")

@mcp.tool()
async def skyscanner_search_flights(origin: str, destination: str, depart: str, return_date: str = "", adults: int = 1) -> str:
    """Search flights via Skyscanner's URL-driven search.

    `origin`/`destination` are 3-letter IATA codes (SFO, LHR, …). Dates are
    YYMMDD (e.g. 260815 for 2026-08-15). `return_date=""` means one-way.
    """
    pid = await _tab()
    way = f"{origin.upper()}/{destination.upper()}/{depart}"
    if return_date: way += f"/{return_date}"
    url = f"https://www.skyscanner.com/transport/flights/{way}/?adults={adults}&cabinclass=economy"
    await client().call("navigate_page", {"page": pid, "url": url})
    await asyncio.sleep(4.0)  # Skyscanner's results take a moment
    items = await js(pid, """
      (() => {
        const cards = document.querySelectorAll('article[data-test-id="result-card"], div.FlightsTicket_container, li.FlightsTicket');
        return JSON.stringify(Array.from(cards).slice(0,10).map((c,i)=>({
          index: i,
          price: c.querySelector('.BpkText_bpk-text__price, [data-test-id="price"], .price')?.innerText?.trim() || '',
          carrier: c.querySelector('.carrier-name, .LegInfo_operator, [data-test-id="carrier"]')?.innerText?.trim() || '',
          duration: c.querySelector('.LegInfo_duration, [data-test-id="duration"]')?.innerText?.trim() || '',
          stops: c.querySelector('.stops-info, [data-test-id="stops"]')?.innerText?.trim() || '',
          times: c.querySelector('.LegInfo_routeStartTime, .start-time')?.innerText?.trim() + ' → ' + (c.querySelector('.LegInfo_routeEndTime, .end-time')?.innerText?.trim() || ''),
        })));
      })()
    """)
    if isinstance(items,str): items=json.loads(items)
    return json.dumps({"flights":items,"count":len(items),"origin":origin,"destination":destination,"depart":depart,"return":return_date,
                       "next_actions":["Open the Skyscanner tab in noVNC to actually book — this MCP intentionally does NOT book flights."]},
                      indent=2,ensure_ascii=False)

@mcp.tool()
async def skyscanner_list_saved() -> str:
    """List your saved searches / price alerts."""
    pid = await _tab()
    await client().call("navigate_page", {"page": pid, "url": "https://www.skyscanner.com/account/saved/lists"})
    await asyncio.sleep(2.0)
    items = await js(pid, """
      (() => {
        const cards = document.querySelectorAll('article, li.saved-item, div.saved-card');
        return JSON.stringify(Array.from(cards).slice(0,30).map((c,i)=>({
          index: i,
          title: c.querySelector('h3, h2, .title')?.innerText?.trim() || '',
          price: c.querySelector('.price')?.innerText?.trim() || '',
          link: c.querySelector('a')?.href || '',
        })));
      })()
    """)
    if isinstance(items,str): items=json.loads(items)
    return json.dumps({"saved":items,"count":len(items)},indent=2,ensure_ascii=False)

def main(): mcp.run(transport="stdio")
if __name__ == "__main__": main()
