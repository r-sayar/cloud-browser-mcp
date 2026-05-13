"""Playbook: FU Berlin SSO login (ZEDAT + TAN matrix).

Hybrid flow — no passwords stored anywhere:
  1. Navigate to the SSO login page
  2. Print instructions: "fill username/password in the browser (autofill works), click Anmelden"
  3. Wait and watch until the TAN matrix challenge page appears
  4. Read the cell challenge, look up values in the local matrix file, fill and submit automatically
  5. Verify login success
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mcp_lib import BOSClient, parse_new_page_id, parse_pages_text

DESCRIPTION = "FU Berlin SSO login — you fill credentials, Claude handles TAN matrix"
TAGS = ["fu-berlin", "auth", "sso"]

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MATRIX_PATH = os.path.join(_REPO, "data", "1", "fu_berlin_code_matrix.txt")
_PORTAL_LOGIN = "https://mycampus.imp.fu-berlin.de/portal/login"
_SSO_HOST = "identity.fu-berlin.de"


# ─── TAN matrix ───────────────────────────────────────────────────────────────

def _parse_matrix(text: str) -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("FU"):
            continue
        parts = line.split()
        if parts and len(parts[0]) == 1 and parts[0].upper() in "ABCDEFGHI":
            rows[parts[0].upper()] = parts[1:]
    return rows


def _lookup_cells(matrix: dict[str, list[str]], cells: list[str]) -> list[str]:
    values = []
    for cell in cells:
        cell = cell.strip().upper()
        row, col = cell[0], int(cell[1]) - 1
        row_vals = matrix.get(row, [])
        if col >= len(row_vals):
            raise ValueError(f"Cell {cell} not found in matrix")
        values.append(row_vals[col])
    return values


# ─── helpers ──────────────────────────────────────────────────────────────────

async def _js(c: BOSClient, pid: int, script: str):
    raw = await c.call("evaluate_script", {"page": pid, "expression": script})
    if isinstance(raw, dict) and "value" in raw:
        v = raw["value"]
        try:
            return json.loads(v)
        except (json.JSONDecodeError, TypeError):
            return v
    return raw


async def _current_url(c: BOSClient, pid: int) -> str:
    raw = await _js(c, pid, "location.href")
    return raw if isinstance(raw, str) else str(raw)


def _is_logged_in(url: str) -> bool:
    return bool(url) and _SSO_HOST not in url and (
        "fu-berlin.de" in url or "mycampus" in url
    )


# ─── wait for TAN page or login, checking both URL and DOM ────────────────────

async def _page_state(c: BOSClient, pid: int) -> dict:
    """Return {url, has_tan_inputs} for the page."""
    raw = await c.call("evaluate_script", {"page": pid, "expression": """
      JSON.stringify({
        url: location.href,
        hasTan: document.querySelectorAll('input[name="fudis_otp_input"]').length > 0
      })
    """})
    if isinstance(raw, dict) and "value" in raw:
        raw = raw["value"]
    try:
        return json.loads(raw)
    except Exception:
        return {"url": "", "hasTan": False}


async def _wait_for_tan_or_login(c: BOSClient, pid: int, timeout: float = 300.0) -> dict:
    """Poll until TAN inputs appear in DOM OR login succeeds. Returns {url, has_tan}."""
    deadline = time.monotonic() + timeout
    print(f"[wait] polling page {pid} for TAN page or login (timeout={timeout}s)", flush=True)
    while time.monotonic() < deadline:
        state = await _page_state(c, pid)
        url = state.get("url", "")
        has_tan = state.get("hasTan", False)
        if has_tan or _is_logged_in(url):
            return state
        await asyncio.sleep(2.0)
    raise TimeoutError("Timed out waiting for TAN matrix or successful login")


# ─── TAN matrix handler ────────────────────────────────────────────────────────

async def _handle_tan_matrix(c: BOSClient, pid: int) -> dict:
    if not os.path.exists(_MATRIX_PATH):
        return {"ok": False, "error": f"Matrix file not found at {_MATRIX_PATH}"}

    with open(_MATRIX_PATH) as f:
        matrix = _parse_matrix(f.read())

    challenge_raw = await _js(c, pid, r"""
      (() => {
        const m = document.body.innerText.match(
          /\b([A-I][1-9])\s+([A-I][1-9])\s+([A-I][1-9])\s+([A-I][1-9])\s+([A-I][1-9])\s+([A-I][1-9])\b/
        );
        return m ? m.slice(1).join(' ') : '';
      })()
    """)

    if not challenge_raw or not challenge_raw.strip():
        return {"ok": False, "error": "Could not find TAN matrix challenge on page"}

    cells = challenge_raw.strip().split()
    print(f"[fu_berlin_sso] TAN challenge: {' '.join(cells)}")

    try:
        codes = _lookup_cells(matrix, cells)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    print(f"[fu_berlin_sso] Filling codes automatically...")

    codes_json = json.dumps(codes)
    await _js(c, pid, f"""
      (() => {{
        const codes = {codes_json};
        document.querySelectorAll('input[name="fudis_otp_input"]').forEach((inp, i) => {{
          Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')
            .set.call(inp, codes[i] || '');
          inp.dispatchEvent(new Event('input', {{bubbles: true}}));
          inp.dispatchEvent(new Event('change', {{bubbles: true}}));
        }});
      }})()
    """)

    await asyncio.sleep(0.3)
    await c.call("click", {"page": pid, "selector": 'button[type="submit"], input[value="Überprüfen"], input[type="submit"]'})
    await asyncio.sleep(3.0)

    body = await _js(c, pid, "document.body.innerText")
    if isinstance(body, str) and "Ungültig" in body:
        return {"ok": False, "error": "TAN matrix rejected — wrong codes. Stop and check matrix file."}

    return {"ok": True, "cells": cells}


# ─── main entry point ──────────────────────────────────────────────────────────

async def run(container: int = 1) -> dict:
    """Hybrid FU Berlin SSO login.

    Navigates to the SSO page, asks you to fill username/password (browser
    autofill works), then automatically handles the TAN matrix challenge.
    """
    novnc = 6080 + container
    bos_port = 9200 + container
    c = BOSClient(url=f"http://localhost:{bos_port}/mcp")

    steps = []

    try:
        # 1. Find or open the SSO login page
        raw = await c.call("list_pages", {})
        text = raw if isinstance(raw, str) else json.dumps(raw)
        pid = None
        for page_id, url in parse_pages_text(text):
            if _SSO_HOST in url or "mycampus" in url:
                pid = page_id
                break
        if pid is None:
            resp = await c.call("new_page", {"url": _PORTAL_LOGIN})
            resp_text = resp if isinstance(resp, str) else json.dumps(resp)
            pid = parse_new_page_id(resp_text)
            await asyncio.sleep(3.0)
            if pid is None:
                raise RuntimeError("Could not open new page")
        steps.append("opened_sso_tab")

        # 2. Navigate to SSO login if not already there
        current = await _current_url(c, pid)
        if _SSO_HOST not in current:
            await c.call("navigate_page", {"page": pid, "url": _PORTAL_LOGIN})
            await asyncio.sleep(3.0)
            current = await _current_url(c, pid)
        steps.append("on_sso_page")

        # 3. Already past credentials? Jump straight to TAN check
        state = await _page_state(c, pid)
        if not state.get("hasTan") and not _is_logged_in(state.get("url", "")):
            print(f"\n[fu_berlin_sso] SSO login page is open at http://localhost:{novnc}/")
            print("[fu_berlin_sso] Please fill in your username and password")
            print("               (browser autofill should work — just click Anmelden)\n")

            state = await _wait_for_tan_or_login(c, pid, timeout=300.0)
            steps.append("credentials_submitted")

        current = state.get("url", "")

        # 4. Handle TAN matrix if inputs are present in DOM
        if state.get("hasTan"):
            tan_result = await _handle_tan_matrix(c, pid)
            steps.append("tan_matrix_attempted")
            if not tan_result["ok"]:
                return {"ok": False, "steps": steps, "error": tan_result["error"]}
            steps.append("tan_accepted")
            await asyncio.sleep(2.0)
            current = await _current_url(c, pid)

        # 5. Verify
        final = await _page_state(c, pid)
        final_url = final.get("url", current)
        if _is_logged_in(final_url):
            print(f"[fu_berlin_sso] Logged in successfully.")
            return {"ok": True, "steps": steps, "url": final_url, "page_id": pid, "error": None}

        return {"ok": False, "steps": steps, "url": final_url, "error": "Still on SSO page after TAN step"}

    except TimeoutError as e:
        return {"ok": False, "steps": steps, "error": str(e)}
    except Exception as e:
        return {"ok": False, "steps": steps, "error": f"Unexpected error: {e}"}


if __name__ == "__main__":
    result = asyncio.run(run())
    print(json.dumps(result, indent=2))
