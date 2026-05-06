#!/usr/bin/env python3
"""linkedin_mcp — LinkedIn (search, profiles, messages)."""
from __future__ import annotations
import asyncio, json, os, sys
from urllib.parse import quote
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mcp.server.fastmcp import FastMCP
from mcp_lib import client, ensure_tab, js

mcp = FastMCP("linkedin")
async def _tab(): return await ensure_tab("linkedin.com", "https://www.linkedin.com/feed/")

async def _logged_in(pid):
    info = await js(pid, """JSON.stringify({url:location.href,loggedIn:!location.href.includes('/login')&&!location.href.includes('/uas/login')})""")
    if isinstance(info,str): info=json.loads(info)
    return info.get("loggedIn",False), info.get("url","")

def _login(url): return json.dumps({"ok":False,"error":"not_logged_in","url":url,"next_actions":["http://localhost:6081/, sign in"]},indent=2)

@mcp.tool()
async def linkedin_search_people(query: str, count: int = 10) -> str:
    """Search LinkedIn for people."""
    pid = await _tab()
    await client().call("navigate_page", {"page": pid, "url": f"https://www.linkedin.com/search/results/people/?keywords={quote(query)}"})
    await asyncio.sleep(2.5)
    ok,url = await _logged_in(pid)
    if not ok: return _login(url)
    items = await js(pid, f"""
      (() => {{
        const cards = document.querySelectorAll('.reusable-search__result-container, li.reusable-search__result-container, .search-result__info');
        const out = [];
        for (const c of cards) {{
          const link = c.querySelector('a.app-aware-link[href*="/in/"]');
          if (!link) continue;
          const name = c.querySelector('.entity-result__title-text a span[aria-hidden]')?.innerText?.trim() || link.innerText.split('\\n')[0].trim();
          const headline = c.querySelector('.entity-result__primary-subtitle')?.innerText?.trim() || '';
          const location = c.querySelector('.entity-result__secondary-subtitle')?.innerText?.trim() || '';
          out.push({{ index: out.length, name, headline, location, profile_url: link.href }});
          if (out.length >= {count}) break;
        }}
        return JSON.stringify(out);
      }})()
    """)
    if isinstance(items,str): items=json.loads(items)
    return json.dumps({"people":items,"count":len(items),"next_actions":["linkedin_view_profile(url='https://...')","linkedin_send_message(profile_url='...', text='...')"]},indent=2,ensure_ascii=False)

@mcp.tool()
async def linkedin_view_profile(url: str) -> str:
    """Open a LinkedIn profile; return name, headline, current role, summary."""
    pid = await _tab()
    await client().call("navigate_page", {"page": pid, "url": url})
    await asyncio.sleep(2.5)
    info = await js(pid, """
      (() => ({
        ok: true,
        name: document.querySelector('h1')?.innerText?.trim() || '',
        headline: document.querySelector('.text-body-medium.break-words')?.innerText?.trim() || '',
        location: document.querySelector('.text-body-small.inline.t-black--light.break-words')?.innerText?.trim() || '',
        about: document.querySelector('section[data-section="summary"] .display-flex.full-width div span[aria-hidden]')?.innerText?.trim()?.slice(0,2000) || '',
        currentRole: document.querySelector('.pv-text-details__right-panel-item-text, .pv-top-card--list-bullet li')?.innerText?.trim() || '',
      }))()
    """)
    if isinstance(info,str): info=json.loads(info)
    info_str = json.dumps(info)
    parsed = json.loads(info_str) if isinstance(info_str, str) else info
    parsed = parsed if isinstance(parsed, dict) else json.loads(parsed)
    parsed["next_actions"] = [f"linkedin_send_message(profile_url='{url}', text='…')", "linkedin_search_people('…')"]
    return json.dumps(parsed,indent=2,ensure_ascii=False)

@mcp.tool()
async def linkedin_list_messages(count: int = 15) -> str:
    """List recent messaging threads (preview only — open the page to see content)."""
    pid = await _tab()
    await client().call("navigate_page", {"page": pid, "url": "https://www.linkedin.com/messaging/"})
    await asyncio.sleep(2.5)
    items = await js(pid, f"""
      (() => {{
        const rs = document.querySelectorAll('li.msg-conversation-listitem, .msg-conversation-card');
        return JSON.stringify(Array.from(rs).slice(0,{count}).map((r,i)=>({{
          index: i,
          name: r.querySelector('.msg-conversation-listitem__participant-names span[aria-hidden]')?.innerText?.trim() || '',
          preview: r.querySelector('.msg-conversation-card__message-snippet')?.innerText?.trim()?.slice(0,200) || '',
          time: r.querySelector('.msg-conversation-listitem__time-stamp')?.innerText?.trim() || '',
        }})));
      }})()
    """)
    if isinstance(items,str): items=json.loads(items)
    return json.dumps({"threads":items,"count":len(items)},indent=2,ensure_ascii=False)

@mcp.tool()
async def linkedin_send_message(profile_url: str, text: str) -> str:
    """Send a message to the person at `profile_url`. Opens their profile, clicks Message, types, sends."""
    pid = await _tab()
    await client().call("navigate_page", {"page": pid, "url": profile_url})
    await asyncio.sleep(2.5)
    safe = json.dumps(text)
    out = await js(pid, f"""
      (async () => {{
        const wait = ms => new Promise(r=>setTimeout(r,ms));
        const msgBtn = Array.from(document.querySelectorAll('button')).find(b => /^message/i.test(b.innerText.trim()));
        if (!msgBtn) return JSON.stringify({{error:'message button not found'}});
        msgBtn.click();
        await wait(900);
        const editor = document.querySelector('div.msg-form__contenteditable[contenteditable="true"]');
        if (!editor) return JSON.stringify({{error:'msg editor not found'}});
        editor.focus();
        document.execCommand('insertText', false, {safe});
        await wait(180);
        const send = Array.from(document.querySelectorAll('button')).find(b => /^send/i.test(b.innerText.trim()) && !b.disabled);
        if (!send) return JSON.stringify({{ok:false,error:'send button disabled or not found',typed:true}});
        send.click();
        return JSON.stringify({{ok:true,sent:true}});
      }})()
    """)
    if isinstance(out,str): out=json.loads(out)
    return json.dumps(out,indent=2)

def main(): mcp.run(transport="stdio")
if __name__ == "__main__": main()
