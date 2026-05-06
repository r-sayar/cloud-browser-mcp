#!/usr/bin/env python3
"""youtube_mcp — YouTube as a high-level MCP."""
from __future__ import annotations
import asyncio, json, os, sys
from urllib.parse import quote
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mcp.server.fastmcp import FastMCP
from mcp_lib import client, ensure_tab, js

HOME = "https://www.youtube.com/"
mcp = FastMCP("youtube")

async def _tab(): return await ensure_tab("youtube.com", HOME)

@mcp.tool()
async def youtube_search(query: str, count: int = 10) -> str:
    """Search YouTube for videos. Returns title, channel, duration, url."""
    pid = await _tab()
    await client().call("navigate_page", {"page": pid, "url": f"https://www.youtube.com/results?search_query={quote(query)}"})
    await asyncio.sleep(2.0)
    items = await js(pid, f"""
      (() => {{
        const rs = document.querySelectorAll('ytd-video-renderer, ytd-rich-item-renderer');
        const out = [];
        for (const r of rs) {{
          const link = r.querySelector('a#video-title, a.yt-lockup-metadata-view-model__title');
          if (!link) continue;
          const title = link.getAttribute('title') || link.innerText.trim();
          const channel = r.querySelector('ytd-channel-name a, .yt-content-metadata-view-model__metadata-text')?.innerText?.trim() || '';
          const duration = r.querySelector('span.ytd-thumbnail-overlay-time-status-renderer, badge-shape')?.innerText?.trim() || '';
          const views = r.querySelectorAll('.inline-metadata-item, span.style-scope.ytd-video-meta-block')?.[0]?.innerText?.trim() || '';
          out.push({{ index: out.length, title: title.slice(0,200), channel, duration, views, url: link.href }});
          if (out.length >= {count}) break;
        }}
        return JSON.stringify(out);
      }})()
    """)
    if isinstance(items,str): items=json.loads(items)
    return json.dumps({"videos":items,"count":len(items),"next_actions":[
        "youtube_open_video(url='https://...')","youtube_get_transcript(url='https://...')",
    ]},indent=2,ensure_ascii=False)

@mcp.tool()
async def youtube_open_video(url: str) -> str:
    """Open a video URL and return title, channel, description."""
    pid = await _tab()
    await client().call("navigate_page", {"page": pid, "url": url})
    await asyncio.sleep(2.5)
    info = await js(pid, """
      (() => {
        return JSON.stringify({
          title: document.querySelector('h1.ytd-watch-metadata yt-formatted-string, h1.title')?.innerText?.trim() || '',
          channel: document.querySelector('ytd-channel-name a, #owner #upload-info a')?.innerText?.trim() || '',
          views: document.querySelector('#info span, ytd-watch-info-text span')?.innerText?.trim() || '',
          description: document.querySelector('#description-inline-expander, #description ytd-text-inline-expander')?.innerText?.slice(0,1500) || '',
        });
      })()
    """)
    if isinstance(info,str): info=json.loads(info)
    info["next_actions"] = ["youtube_get_transcript(url='...')","youtube_search(query='...')"]
    return json.dumps(info,indent=2,ensure_ascii=False)

@mcp.tool()
async def youtube_get_transcript(url: str) -> str:
    """Open transcript pane for a video and return the text."""
    pid = await _tab()
    await client().call("navigate_page", {"page": pid, "url": url})
    await asyncio.sleep(2.5)
    transcript = await js(pid, """
      (async () => {
        const wait = ms => new Promise(r => setTimeout(r, ms));
        // Click the "..." → Show transcript button
        const moreBtn = document.querySelector('ytd-watch-metadata #button-shape button, button[aria-label*="more" i]');
        if (moreBtn) { moreBtn.click(); await wait(300); }
        const items = document.querySelectorAll('ytd-transcript-segment-renderer, .segment');
        if (items.length === 0) {
          // Try the dedicated transcript button on the description
          const tbtn = Array.from(document.querySelectorAll('button')).find(b => /transcript|transkript/i.test(b.innerText));
          if (tbtn) { tbtn.click(); await wait(800); }
        }
        const segs = document.querySelectorAll('ytd-transcript-segment-renderer .segment-text, .segment-text');
        return JSON.stringify({
          segmentCount: segs.length,
          text: Array.from(segs).map(s => s.innerText.trim()).join(' ').slice(0, 8000),
        });
      })()
    """)
    if isinstance(transcript,str): transcript=json.loads(transcript)
    transcript["next_actions"] = ["youtube_open_video(url='...')"]
    return json.dumps(transcript,indent=2,ensure_ascii=False)

@mcp.tool()
async def youtube_list_subscriptions() -> str:
    """List the latest videos from your subscriptions feed."""
    pid = await _tab()
    await client().call("navigate_page", {"page": pid, "url": "https://www.youtube.com/feed/subscriptions"})
    await asyncio.sleep(2.0)
    items = await js(pid, """
      (() => {
        const rs = document.querySelectorAll('ytd-rich-item-renderer');
        return JSON.stringify(Array.from(rs).slice(0,20).map((r,i)=>({
          index: i,
          title: r.querySelector('a#video-title-link, a.yt-lockup-metadata-view-model__title')?.getAttribute('title') || '',
          channel: r.querySelector('.yt-content-metadata-view-model__metadata-text')?.innerText?.trim() || '',
          url: r.querySelector('a#thumbnail, a.yt-lockup-view-model__thumbnail-link')?.href || '',
        })));
      })()
    """)
    if isinstance(items,str): items=json.loads(items)
    return json.dumps({"videos":items,"count":len(items)},indent=2,ensure_ascii=False)

@mcp.tool()
async def youtube_list_watchlater() -> str:
    """List your Watch Later playlist."""
    pid = await _tab()
    await client().call("navigate_page", {"page": pid, "url": "https://www.youtube.com/playlist?list=WL"})
    await asyncio.sleep(2.0)
    items = await js(pid, """
      (() => {
        const rs = document.querySelectorAll('ytd-playlist-video-renderer');
        return JSON.stringify(Array.from(rs).slice(0,30).map((r,i)=>({
          index: i,
          title: r.querySelector('#video-title')?.innerText?.trim() || '',
          channel: r.querySelector('#channel-name a, .ytd-channel-name')?.innerText?.trim() || '',
          duration: r.querySelector('span.ytd-thumbnail-overlay-time-status-renderer')?.innerText?.trim() || '',
          url: r.querySelector('a#video-title')?.href || '',
        })));
      })()
    """)
    if isinstance(items,str): items=json.loads(items)
    return json.dumps({"watchlater":items,"count":len(items)},indent=2,ensure_ascii=False)

def main(): mcp.run(transport="stdio")
if __name__ == "__main__": main()
