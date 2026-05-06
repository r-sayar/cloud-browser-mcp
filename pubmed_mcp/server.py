#!/usr/bin/env python3
"""pubmed_mcp — NCBI PubMed (search, get abstract). Public, no auth."""
from __future__ import annotations
import asyncio, json, os, sys
from urllib.parse import quote
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mcp.server.fastmcp import FastMCP
from mcp_lib import client, ensure_tab, js

mcp = FastMCP("pubmed")
async def _tab(): return await ensure_tab("pubmed.ncbi.nlm.nih.gov", "https://pubmed.ncbi.nlm.nih.gov/")

@mcp.tool()
async def pubmed_search(query: str, count: int = 10) -> str:
    """Search PubMed. Accepts standard PubMed query syntax (MeSH, [tw], etc.)."""
    pid = await _tab()
    await client().call("navigate_page", {"page": pid, "url": f"https://pubmed.ncbi.nlm.nih.gov/?term={quote(query)}"})
    await asyncio.sleep(2.0)
    items = await js(pid, f"""
      (() => {{
        const arts = document.querySelectorAll('article.full-docsum, .docsum-content');
        return JSON.stringify(Array.from(arts).slice(0,{count}).map((a,i)=>({{
          index: i,
          pmid: a.querySelector('span.docsum-pmid')?.innerText?.trim() || '',
          title: a.querySelector('a.docsum-title')?.innerText?.trim() || '',
          authors: a.querySelector('.docsum-authors')?.innerText?.trim()?.slice(0,200) || '',
          journalCitation: a.querySelector('.docsum-journal-citation, .docsum-citation')?.innerText?.trim() || '',
          url: a.querySelector('a.docsum-title')?.href || '',
        }})));
      }})()
    """)
    if isinstance(items,str): items=json.loads(items)
    return json.dumps({"papers":items,"count":len(items),"next_actions":["pubmed_get_abstract(pmid='12345678')"]},indent=2,ensure_ascii=False)

@mcp.tool()
async def pubmed_get_abstract(pmid: str) -> str:
    """Open a paper by PMID; return title, authors, abstract."""
    pid = await _tab()
    await client().call("navigate_page", {"page": pid, "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"})
    await asyncio.sleep(2.0)
    info = await js(pid, """
      (() => ({
        pmid: location.pathname.match(/\\/(\\d+)\\//)?.[1] || '',
        title: document.querySelector('h1.heading-title')?.innerText?.trim() || '',
        authors: Array.from(document.querySelectorAll('.authors-list-item .full-name')).map(a=>a.innerText.trim()),
        journal: document.querySelector('.journal-actions .id-link, button#full-view-journal-trigger')?.innerText?.trim() || '',
        abstract: document.querySelector('.abstract-content, #abstract .abstract-content')?.innerText?.trim()?.slice(0,5000) || '',
        doi: document.querySelector('.id-link[data-ga-action="DOI"]')?.innerText?.trim() || '',
      }))()
    """)
    if isinstance(info,str): info=json.loads(info)
    info_dict = info if isinstance(info, dict) else json.loads(info)
    info_dict["next_actions"] = ["pubmed_search('related query')"]
    return json.dumps(info_dict,indent=2,ensure_ascii=False)

def main(): mcp.run(transport="stdio")
if __name__ == "__main__": main()
