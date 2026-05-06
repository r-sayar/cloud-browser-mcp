#!/usr/bin/env python3
"""wikipedia_mcp — Wikipedia (no auth, public)."""
from __future__ import annotations
import asyncio, json, os, sys
from urllib.parse import quote
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mcp.server.fastmcp import FastMCP
from mcp_lib import client, ensure_tab, js

mcp = FastMCP("wikipedia")
async def _tab(): return await ensure_tab("wikipedia.org", "https://en.wikipedia.org/")

@mcp.tool()
async def wikipedia_search(query: str, count: int = 10) -> str:
    """Search Wikipedia. Returns top matches."""
    pid = await _tab()
    await client().call("navigate_page", {"page": pid, "url": f"https://en.wikipedia.org/w/index.php?search={quote(query)}"})
    await asyncio.sleep(1.5)
    items = await js(pid, f"""
      (() => {{
        // Either we got redirected straight to an article, or a results list
        if (document.querySelector('#firstHeading') && !document.querySelector('.mw-search-result')) {{
          return JSON.stringify({{ exact_match: location.href, title: document.querySelector('#firstHeading').innerText }});
        }}
        const rs = document.querySelectorAll('.mw-search-result');
        return JSON.stringify(Array.from(rs).slice(0,{count}).map((r,i)=>({{
          index: i,
          title: r.querySelector('.mw-search-result-heading a')?.innerText?.trim() || '',
          url: r.querySelector('.mw-search-result-heading a')?.href || '',
          snippet: r.querySelector('.searchresult')?.innerText?.trim()?.slice(0,250) || '',
        }})));
      }})()
    """)
    if isinstance(items,str): items=json.loads(items)
    return json.dumps({"results":items,"next_actions":["wikipedia_get_article(url='https://en.wikipedia.org/wiki/...')"]},indent=2,ensure_ascii=False)

@mcp.tool()
async def wikipedia_get_article(url: str, section: str = "") -> str:
    """Open an article; return title, lead paragraph(s), section list. If
    `section` is provided, return that section's text instead of the lead."""
    pid = await _tab()
    await client().call("navigate_page", {"page": pid, "url": url})
    await asyncio.sleep(1.5)
    safe = json.dumps(section)
    info = await js(pid, f"""
      (() => {{
        const title = document.querySelector('#firstHeading')?.innerText?.trim() || '';
        const sections = Array.from(document.querySelectorAll('h2 .mw-headline, h3 .mw-headline')).map(h => h.innerText.trim()).slice(0,40);
        let body = '';
        if ({safe}) {{
          const head = Array.from(document.querySelectorAll('h2 .mw-headline, h3 .mw-headline')).find(h => h.innerText.trim().toLowerCase() === {safe}.toLowerCase());
          if (head) {{
            let n = head.parentElement.nextElementSibling;
            const parts = [];
            while (n && !/^H[23]$/.test(n.tagName)) {{
              if (/^P$/.test(n.tagName) || /^UL$/.test(n.tagName)) parts.push(n.innerText.trim());
              n = n.nextElementSibling;
            }}
            body = parts.join('\\n\\n').slice(0,4000);
          }}
        }} else {{
          // First few paragraphs of the lead
          const lead = document.querySelector('#mw-content-text .mw-parser-output');
          const paras = lead ? Array.from(lead.children).filter(c => c.tagName === 'P').slice(0,4) : [];
          body = paras.map(p => p.innerText.trim()).join('\\n\\n').slice(0,4000);
        }}
        return JSON.stringify({{ title, sections, body }});
      }})()
    """)
    if isinstance(info,str): info=json.loads(info)
    info["next_actions"] = [
        f"wikipedia_get_article(url='{url}', section='<one of the sections above>')",
        "wikipedia_search(query='related topic')",
    ]
    return json.dumps(info,indent=2,ensure_ascii=False)

def main(): mcp.run(transport="stdio")
if __name__ == "__main__": main()
