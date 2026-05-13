"""Playbook recorder.

Two modes:

1. **Wrap mode** (for Claude-driven actions):
   Use `RecordingClient` instead of the bare `BOSClient`. Every `.call()` is
   logged. On `save()`, writes a JSON log AND a Python playbook.

     rec = RecordingClient(name="fu_berlin_sso", container=1)
     await rec.call("navigate_page", {"page": pid, "url": "https://..."})
     ...
     await rec.save()   # writes playbooks/_recordings/fu_berlin_sso.json
                        #        and playbooks/fu_berlin_sso.py

2. **Monitor mode** (for user-driven actions in noVNC):
   Run `python -m playbooks.recorder <name> [--container N]` before the user
   starts. It polls the browser every 2 s and records URL transitions and
   DOM snapshots until you press Ctrl-C. Generates a playbook skeleton from
   the observed sequence that Claude can flesh out.

     python -m playbooks.recorder fu_berlin_login --container 1
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import textwrap
import time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mcp_lib import BOSClient, parse_new_page_id, parse_pages_text

_RECORDINGS_DIR = os.path.join(os.path.dirname(__file__), "_recordings")
_PLAYBOOKS_DIR = os.path.dirname(__file__)


def _ensure_recordings_dir():
    os.makedirs(_RECORDINGS_DIR, exist_ok=True)


# ─── Wrap mode ─────────────────────────────────────────────────────────────────

class RecordingClient(BOSClient):
    """A BOSClient that records every tool call to a JSON log."""

    def __init__(self, name: str, container: int = 1, **kwargs):
        port = 9200 + container
        super().__init__(url=f"http://localhost:{port}/mcp", **kwargs)
        self.name = name
        self.container = container
        self._log: list[dict] = []
        self._start = time.time()

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        t0 = time.time()
        try:
            result = await super().call(tool, args)
            self._log.append({
                "t": round(time.time() - self._start, 2),
                "tool": tool,
                "args": args,
                "ok": True,
                "result_summary": _summarize(result),
            })
            return result
        except Exception as e:
            self._log.append({
                "t": round(time.time() - self._start, 2),
                "tool": tool,
                "args": args,
                "ok": False,
                "error": str(e),
            })
            raise

    def save(self, description: str = "") -> str:
        """Write the recording to JSON and generate a Python playbook. Returns the playbook path."""
        _ensure_recordings_dir()
        log_path = os.path.join(_RECORDINGS_DIR, f"{self.name}.json")
        with open(log_path, "w") as f:
            json.dump({
                "name": self.name,
                "container": self.container,
                "description": description,
                "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "steps": self._log,
            }, f, indent=2)
        playbook_path = _generate_playbook_from_log(log_path)
        return playbook_path


def _summarize(result: Any) -> str:
    s = json.dumps(result) if not isinstance(result, str) else result
    return s[:200]


# ─── Playbook generation from log ──────────────────────────────────────────────

def _generate_playbook_from_log(log_path: str) -> str:
    """Generate a Python playbook file from a recording JSON. Returns its path."""
    with open(log_path) as f:
        rec = json.load(f)

    name = rec["name"]
    desc = rec.get("description") or f"Recorded playbook: {name}"
    container = rec.get("container", 1)
    steps = rec.get("steps", [])

    lines = [
        f'"""Playbook: {name} (auto-generated from recording).',
        f'',
        f'{desc}',
        f'"""',
        f'from __future__ import annotations',
        f'import asyncio, json, os, sys',
        f'sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))',
        f'from mcp_lib import BOSClient',
        f'',
        f'DESCRIPTION = {json.dumps(desc)}',
        f'TAGS = ["recorded"]',
        f'',
        f'',
        f'async def run(container: int = {container}) -> dict:',
        f'    port = 9200 + container',
        f'    c = BOSClient(url=f"http://localhost:{{port}}/mcp")',
        f'    steps = []',
        f'',
    ]

    prev_t = 0.0
    for step in steps:
        if not step.get("ok"):
            lines.append(f'    # [FAILED] {step["tool"]}({json.dumps(step["args"])[:80]})')
            lines.append(f'    # error: {step.get("error", "?")}')
            continue

        delay = round(step["t"] - prev_t, 2)
        prev_t = step["t"]
        tool = step["tool"]
        args = step["args"]

        if delay > 0.5:
            lines.append(f'    await asyncio.sleep({min(delay, 5.0)})')

        args_repr = json.dumps(args)
        lines.append(f'    await c.call({json.dumps(tool)}, {args_repr})')
        lines.append(f'    steps.append({json.dumps(tool)})')

    lines += [
        f'',
        f'    return {{"ok": True, "steps": steps}}',
        f'',
        f'',
        f'if __name__ == "__main__":',
        f'    print(json.dumps(asyncio.run(run()), indent=2))',
    ]

    playbook_path = os.path.join(_PLAYBOOKS_DIR, f"{name}.py")
    with open(playbook_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return playbook_path


# ─── record_until_success — handoff + auto-stop ────────────────────────────────

async def record_until_success(
    name: str,
    container: int = 1,
    success_fn=None,
    prompt: str = "",
    poll_interval: float = 2.0,
    timeout: float = 600.0,
) -> dict:
    """
    Print `prompt` asking the user to do the task manually, then monitor the
    browser until `success_fn(url) -> bool` returns True (or timeout).

    Saves the URL sequence as a JSON log and returns:
      {"ok": True/False, "events": [...], "log_path": "..."}

    Example success_fn for FU Berlin login:
      lambda url: url and "identity.fu-berlin.de" not in url and "fu-berlin.de" in url
    """
    port = 9200 + container
    c = BOSClient(url=f"http://localhost:{port}/mcp")

    novnc_port = 6080 + container
    if prompt:
        print(prompt)
    print(f"[recorder] Browser is at http://localhost:{novnc_port}/")
    print(f"[recorder] Recording URL transitions. Will stop automatically on success, or press Ctrl-C.\n")

    events: list[dict] = []
    prev_url = ""
    start = time.time()
    succeeded = False

    prev_urls: dict[int, str] = {}

    async def _poll():
        nonlocal succeeded
        deadline = start + timeout
        while time.time() < deadline:
            try:
                pages_raw = await c.call("list_pages", {})
                text = pages_raw if isinstance(pages_raw, str) else json.dumps(pages_raw)
                pages = parse_pages_text(text)
                for pid, url in pages:
                    if url != prev_urls.get(pid):
                        t = round(time.time() - start, 2)
                        events.append({"t": t, "type": "navigate", "page": pid, "url": url})
                        print(f"  +{t:6.1f}s  [p{pid}] → {url[:80]}")
                        prev_urls[pid] = url
                    if success_fn and success_fn(url):
                        succeeded = True
                        print(f"\n[recorder] Success condition met on page {pid}. Stopping.")
                        return
            except Exception as e:
                print(f"  [poll error: {e}]")
            await asyncio.sleep(poll_interval)
        print(f"\n[recorder] Timeout after {timeout}s.")

    try:
        await _poll()
    except (KeyboardInterrupt, asyncio.CancelledError):
        print(f"\n[recorder] Stopped by user.")

    _ensure_recordings_dir()
    log_path = os.path.join(_RECORDINGS_DIR, f"{name}_monitor.json")
    with open(log_path, "w") as f:
        json.dump({
            "name": name,
            "container": container,
            "mode": "monitor",
            "succeeded": succeeded,
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "steps": [
                {"t": e["t"], "tool": "navigate_page",
                 "args": {"page": e["page"], "url": e["url"]}, "ok": True}
                for e in events
            ],
        }, f, indent=2)

    print(f"[recorder] Log saved to: {log_path}")
    return {"ok": succeeded, "events": events, "log_path": log_path}


# ─── Monitor mode (user-driven, polls browser state) ──────────────────────────

async def _monitor(name: str, container: int = 1, poll_interval: float = 2.0):
    """Poll BrowserOS every `poll_interval` seconds and record URL/title transitions."""
    port = 9200 + container
    c = BOSClient(url=f"http://localhost:{port}/mcp")

    print(f"[recorder] Monitoring container {container} for '{name}'.")
    print(f"[recorder] Perform the action in the browser at http://localhost:{6080 + container}/")
    print(f"[recorder] Press Ctrl-C when done.\n")

    events: list[dict] = []
    prev_url = ""
    start = time.time()

    try:
        while True:
            try:
                pages_raw = await c.call("list_pages", {})
                text = pages_raw if isinstance(pages_raw, str) else json.dumps(pages_raw)
                pages = parse_pages_text(text)

                # Track active page URL changes
                if pages:
                    pid, url = pages[0]
                    if url != prev_url:
                        t = round(time.time() - start, 2)
                        events.append({
                            "t": t,
                            "type": "navigate",
                            "page": pid,
                            "url": url,
                        })
                        print(f"  +{t:6.1f}s  → {url[:80]}")
                        prev_url = url
            except Exception as e:
                print(f"  [poll error: {e}]")

            await asyncio.sleep(poll_interval)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass

    print(f"\n[recorder] Recorded {len(events)} transitions. Generating playbook skeleton...")

    _ensure_recordings_dir()
    log_path = os.path.join(_RECORDINGS_DIR, f"{name}_monitor.json")
    with open(log_path, "w") as f:
        json.dump({
            "name": name,
            "container": container,
            "mode": "monitor",
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "steps": [
                {"t": e["t"], "tool": "navigate_page",
                 "args": {"page": e["page"], "url": e["url"]}, "ok": True}
                for e in events if e["type"] == "navigate"
            ],
        }, f, indent=2)

    playbook_path = _generate_playbook_from_log(log_path)
    print(f"[recorder] Playbook written to: {playbook_path}")
    print(f"[recorder] Raw log at: {log_path}")
    print(f"\nNOTE: The generated playbook replays URL navigation only.")
    print(f"      Edit it to add form-fill steps, DOM interactions, or TAN logic.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Record browser actions as a playbook")
    parser.add_argument("name", help="Playbook name (used as filename)")
    parser.add_argument("--container", type=int, default=1, help="BrowserOS slot (1/2/3)")
    parser.add_argument("--interval", type=float, default=2.0, help="Poll interval in seconds")
    args = parser.parse_args()
    asyncio.run(_monitor(args.name, args.container, args.interval))
