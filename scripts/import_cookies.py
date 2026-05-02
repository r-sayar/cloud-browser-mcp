#!/usr/bin/env python3
"""
import_cookies.py — copy cookies from the local Chrome on this laptop into the
cloud Chromium running inside the Steel container, over CDP.

Use case: the "fast path" for sites where you'd rather not re-log-in inside the
cloud browser. Mirrors browser-use Cloud's `profile.sh` helper.

Caveats:
  * macOS Keychain will prompt for your password the first time (browser_cookie3
    decrypts cookies using the Chrome Safe Storage key).
  * Some sites bind cookies to device fingerprint / IP / TLS-JA3 — pasted
    cookies get rejected or trigger account-security alerts. Google, banks,
    most airlines fall in this bucket. The script BLOCKS these by default;
    pass --force to override.
  * The cloud container must be running (`docker compose up -d`) and have at
    least one session live (`./scripts/start-session.sh`) so CDP is reachable.

Usage:
    pip install browser-cookie3 websockets requests
    python3 scripts/import_cookies.py twitter.com github.com linkedin.com
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Iterable

# --- domain blacklist ---------------------------------------------------------
# Sites where pasted cookies are likely to fail or trigger security alerts.
# Override with --force.
RISKY_SUFFIXES = (
    "google.com",
    "googleapis.com",
    "youtube.com",
    "paypal.com",
    "stripe.com",
    "apple.com",
    "icloud.com",
    "microsoft.com",
    "live.com",
    "bankofamerica.com",
    "chase.com",
    "wellsfargo.com",
    "amazon.com",  # has aggressive device-binding for shopping/payments
)


def is_risky(domain: str) -> bool:
    d = domain.lstrip(".").lower()
    return any(d == s or d.endswith("." + s) for s in RISKY_SUFFIXES)


# --- CDP plumbing -------------------------------------------------------------
def get_browser_ws(cdp_http: str) -> str:
    import requests
    from urllib.parse import urlparse, urlunparse

    info = requests.get(f"{cdp_http}/json/version", timeout=5).json()
    ws = info["webSocketDebuggerUrl"]
    # Steel's /json/version returns a host-stripped URL like
    # ws://localhost/devtools/browser/<id>. Rewrite the host to match the CDP
    # HTTP endpoint so the port is correct.
    cdp = urlparse(cdp_http)
    p = urlparse(ws)
    return urlunparse(("ws", cdp.netloc, p.path, p.params, p.query, p.fragment))


def to_cdp_cookie(c) -> dict:
    """Convert a browser_cookie3 cookie (http.cookiejar.Cookie) to CDP CookieParam."""
    out = {
        "name": c.name,
        "value": c.value,
        "domain": c.domain,
        "path": c.path or "/",
        "secure": bool(c.secure),
        "httpOnly": bool(c._rest.get("HttpOnly", False)) if hasattr(c, "_rest") else False,
    }
    if c.expires:
        out["expires"] = c.expires
    # SameSite: Chrome stores it; cookiejar exposes via _rest sometimes.
    same = (getattr(c, "_rest", {}) or {}).get("SameSite") or (
        getattr(c, "_rest", {}) or {}
    ).get("samesite")
    if same:
        s = str(same).capitalize()
        if s in ("Strict", "Lax", "None"):
            out["sameSite"] = s
    return out


async def set_cookies(ws_url: str, cookies: list[dict]) -> None:
    import websockets

    async with websockets.connect(ws_url, max_size=10 * 1024 * 1024) as ws:
        await ws.send(
            json.dumps({"id": 1, "method": "Storage.setCookies", "params": {"cookies": cookies}})
        )
        resp = json.loads(await ws.recv())
        if "error" in resp:
            raise SystemExit(f"CDP error: {resp['error']}")


# --- CLI ----------------------------------------------------------------------
def collect_cookies(domains: Iterable[str], force: bool) -> list[dict]:
    import browser_cookie3

    out: list[dict] = []
    skipped: list[str] = []
    jar = browser_cookie3.chrome()  # prompts Keychain on first run
    for c in jar:
        host = c.domain.lstrip(".").lower()
        if not any(host == d or host.endswith("." + d) for d in domains):
            continue
        if is_risky(c.domain) and not force:
            skipped.append(c.domain)
            continue
        out.append(to_cdp_cookie(c))
    if skipped:
        print(
            f"skipped {len(skipped)} cookies on risky domains "
            f"({', '.join(sorted(set(skipped)))[:120]}…) — pass --force to include",
            file=sys.stderr,
        )
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    p.add_argument("domains", nargs="+", help="domain suffixes to import (e.g. twitter.com)")
    p.add_argument(
        "--cdp",
        default="http://localhost:9011",
        help="CDP HTTP endpoint (default: http://localhost:9011, BrowserOS Linux). "
        "Note: CDP-from-host currently fails Chromium's Host-header check; run this "
        "from inside the container until that's fixed: "
        "`docker compose exec browseros python3 /scripts/import_cookies.py …`.",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="include cookies for blacklisted risky domains (Google, banks, etc.)",
    )
    p.add_argument("--dry-run", action="store_true", help="print what would be imported, don't send")
    args = p.parse_args()

    cookies = collect_cookies([d.lower() for d in args.domains], args.force)
    if not cookies:
        print("no matching cookies found in local Chrome", file=sys.stderr)
        sys.exit(1)

    print(f"found {len(cookies)} cookies across "
          f"{len({c['domain'] for c in cookies})} domains")
    if args.dry_run:
        for c in cookies:
            print(f"  {c['domain']:<30} {c['name']}")
        return

    ws_url = get_browser_ws(args.cdp)
    asyncio.run(set_cookies(ws_url, cookies))
    print(f"injected {len(cookies)} cookies into the cloud browser")
    print("→ navigate to one of the imported sites in the live-view to verify;")
    print("  then run ./scripts/stop-session.sh <id> to flush to ./data/profile/")


if __name__ == "__main__":
    main()
