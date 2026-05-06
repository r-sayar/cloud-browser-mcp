#!/usr/bin/env python3
"""amazon_mcp — Amazon as a high-level MCP (search, view, orders, wishlist, cart).

Deliberately NO place_order tool: that's a financial action and is the kind
of irreversible side effect we don't want an agent to do on autopilot. To
actually place an order, take over the cloud browser via noVNC.
"""
from __future__ import annotations
import asyncio, json, os, sys
from urllib.parse import quote
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mcp.server.fastmcp import FastMCP
from mcp_lib import client, ensure_tab, js

HOME = "https://www.amazon.com/"
mcp = FastMCP("amazon")

async def _tab(): return await ensure_tab("amazon.com", HOME)

async def _logged_in(pid):
    info = await js(pid, """JSON.stringify({url:location.href,loggedIn:!/sign[ -]?in/i.test(document.title)&&!location.href.includes('/ap/signin')})""")
    if isinstance(info,str): info=json.loads(info)
    return info.get("loggedIn",False), info.get("url","")

def _login(url): return json.dumps({"ok":False,"error":"not_logged_in","url":url,"next_actions":["Open http://localhost:6081/, sign into amazon.com, retry."]},indent=2)

@mcp.tool()
async def amazon_search(query: str, count: int = 12) -> str:
    """Search Amazon for products. Returns title, price, ASIN, url."""
    pid = await _tab()
    await client().call("navigate_page", {"page": pid, "url": f"https://www.amazon.com/s?k={quote(query)}"})
    await asyncio.sleep(2.0)
    items = await js(pid, f"""
      (() => {{
        const cards = document.querySelectorAll('div[data-asin][data-component-type="s-search-result"]');
        const out = [];
        for (const c of cards) {{
          const asin = c.getAttribute('data-asin');
          if (!asin) continue;
          const titleEl = c.querySelector('h2 a span, h2 span');
          const linkEl = c.querySelector('h2 a');
          const priceEl = c.querySelector('.a-price > .a-offscreen');
          const ratingEl = c.querySelector('.a-icon-star-small .a-icon-alt, span.a-icon-alt');
          out.push({{
            index: out.length, asin,
            title: titleEl ? titleEl.innerText.trim().slice(0,200) : '',
            price: priceEl ? priceEl.innerText.trim() : '',
            rating: ratingEl ? ratingEl.innerText.trim() : '',
            url: linkEl ? linkEl.href : '',
          }});
          if (out.length >= {count}) break;
        }}
        return JSON.stringify(out);
      }})()
    """)
    if isinstance(items,str): items=json.loads(items)
    return json.dumps({"products":items,"count":len(items),"next_actions":[
        "amazon_view_product(asin='B0...') — product details",
        "amazon_add_to_cart(asin='B0...') — add to cart",
    ]},indent=2,ensure_ascii=False)

@mcp.tool()
async def amazon_view_product(asin: str) -> str:
    """Open product detail page; return title, price, rating, summary."""
    pid = await _tab()
    await client().call("navigate_page", {"page": pid, "url": f"https://www.amazon.com/dp/{asin}"})
    await asyncio.sleep(1.8)
    info = await js(pid, """
      (() => {
        const title = document.querySelector('#productTitle');
        const price = document.querySelector('.a-price > .a-offscreen, #priceblock_ourprice, span.a-price-whole');
        const rating = document.querySelector('#acrPopover, span.a-icon-alt');
        const bullets = Array.from(document.querySelectorAll('#feature-bullets li span')).map(s=>s.innerText.trim()).filter(Boolean).slice(0,8);
        const stock = document.querySelector('#availability span')?.innerText.trim() || '';
        return JSON.stringify({
          title: title?title.innerText.trim():'',
          price: price?price.innerText.trim():'',
          rating: rating?rating.innerText.trim():'',
          bullets, stock,
        });
      })()
    """)
    if isinstance(info,str): info=json.loads(info)
    info["next_actions"] = [f"amazon_add_to_cart(asin='{asin}')", "amazon_search(query='...')"]
    return json.dumps(info,indent=2,ensure_ascii=False)

@mcp.tool()
async def amazon_add_to_cart(asin: str, quantity: int = 1) -> str:
    """Add product to cart (does NOT check out)."""
    pid = await _tab()
    await client().call("navigate_page", {"page": pid, "url": f"https://www.amazon.com/dp/{asin}"})
    await asyncio.sleep(1.8)
    out = await js(pid, f"""
      (async () => {{
        const wait = ms => new Promise(r=>setTimeout(r,ms));
        const qty = document.querySelector('select#quantity, #quantity');
        if (qty) {{ qty.value = '{quantity}'; qty.dispatchEvent(new Event('change',{{bubbles:true}})); }}
        await wait(200);
        const btn = document.querySelector('#add-to-cart-button, input[name="submit.add-to-cart"]');
        if (!btn) return JSON.stringify({{error:'add-to-cart button not found'}});
        btn.click();
        return JSON.stringify({{ok:true,asin:'{asin}',quantity:{quantity}}});
      }})()
    """)
    if isinstance(out,str): out=json.loads(out)
    if out.get("ok"): out["next_actions"]=["amazon_view_cart()","amazon_search('...')"]
    return json.dumps(out,indent=2)

@mcp.tool()
async def amazon_view_cart() -> str:
    """Read the current cart contents."""
    pid = await _tab()
    await client().call("navigate_page", {"page": pid, "url": "https://www.amazon.com/gp/cart/view.html"})
    await asyncio.sleep(1.8)
    ok,url = await _logged_in(pid)
    if not ok: return _login(url)
    items = await js(pid, """
      (() => {
        const rows = document.querySelectorAll('div.sc-list-item, div[data-asin]');
        const out = [];
        rows.forEach(r => {
          const asin = r.getAttribute('data-asin');
          const title = r.querySelector('.sc-product-title, .a-truncate-cut, span.a-truncate')?.innerText?.trim() || '';
          const price = r.querySelector('.sc-product-price, span.a-price > span.a-offscreen')?.innerText?.trim() || '';
          if (title) out.push({asin, title: title.slice(0,160), price});
        });
        const subtotal = document.querySelector('span#sc-subtotal-amount-buybox, span.sc-price')?.innerText?.trim() || '';
        return JSON.stringify({items: out, subtotal});
      })()
    """)
    if isinstance(items,str): items=json.loads(items)
    return json.dumps(items,indent=2,ensure_ascii=False)

@mcp.tool()
async def amazon_list_orders(count: int = 10) -> str:
    """List recent orders. Requires login."""
    pid = await _tab()
    await client().call("navigate_page", {"page": pid, "url": "https://www.amazon.com/gp/your-account/order-history"})
    await asyncio.sleep(2.0)
    ok,url = await _logged_in(pid)
    if not ok: return _login(url)
    items = await js(pid, f"""
      (() => {{
        const orders = document.querySelectorAll('div.order, div.js-order-card, .a-box-group');
        const out = [];
        for (const o of orders) {{
          const placed = o.querySelector('.order-info .a-color-secondary, .a-row .order-date')?.innerText?.trim() || '';
          const total = o.querySelector('.order-info .a-column .value')?.innerText?.trim() || '';
          const ids = o.querySelectorAll('.order-info .actions .a-link-normal');
          const orderId = (o.innerText.match(/Order #?\\s*([0-9-]+)/)||[])[1] || '';
          const titles = Array.from(o.querySelectorAll('.a-row a.a-link-normal')).slice(0,2).map(a=>a.innerText.trim()).filter(Boolean);
          out.push({{ index: out.length, orderId, placed, total, items: titles }});
          if (out.length >= {count}) break;
        }}
        return JSON.stringify(out);
      }})()
    """)
    if isinstance(items,str): items=json.loads(items)
    return json.dumps({"orders":items,"count":len(items)},indent=2,ensure_ascii=False)

@mcp.tool()
async def amazon_list_wishlist() -> str:
    """List the default wishlist."""
    pid = await _tab()
    await client().call("navigate_page", {"page": pid, "url": "https://www.amazon.com/hz/wishlist/ls"})
    await asyncio.sleep(1.8)
    ok,url = await _logged_in(pid)
    if not ok: return _login(url)
    items = await js(pid, """
      (() => {
        const rows = document.querySelectorAll('li[data-id], div.g-item-sortable');
        return JSON.stringify(Array.from(rows).slice(0,30).map((r,i)=>({
          index: i,
          title: r.querySelector('a[href*="/dp/"]')?.innerText?.trim()?.slice(0,160) || '',
          price: r.querySelector('span.a-price > .a-offscreen')?.innerText?.trim() || '',
          url:   r.querySelector('a[href*="/dp/"]')?.href || '',
        })));
      })()
    """)
    if isinstance(items,str): items=json.loads(items)
    return json.dumps({"wishlist":items,"count":len(items)},indent=2,ensure_ascii=False)

def main(): mcp.run(transport="stdio")
if __name__ == "__main__": main()
