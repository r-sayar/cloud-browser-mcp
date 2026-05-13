"""Playbook registry and runner.

Each playbook module lives in this directory and exports:
  DESCRIPTION: str          — one-line description
  TAGS:        list[str]    — e.g. ['fu-berlin', 'auth']
  async def run(**kwargs)   — main entry point

Run from the repo root:
  python -m playbooks                        # list all
  python -m playbooks fu_berlin_sso          # run by name
  python -m playbooks fu_berlin_sso --dry    # dry-run / describe
"""
from __future__ import annotations

import asyncio
import importlib
import os
import sys

_DIR = os.path.dirname(__file__)


def list_playbooks() -> list[dict]:
    """Return metadata for every playbook module in this directory."""
    results = []
    _skip = {"recorder"}
    for fname in sorted(os.listdir(_DIR)):
        if fname.startswith("_") or not fname.endswith(".py"):
            continue
        name = fname[:-3]
        if name in _skip:
            continue
        try:
            mod = importlib.import_module(f"playbooks.{name}")
            results.append({
                "name": name,
                "description": getattr(mod, "DESCRIPTION", "(no description)"),
                "tags": getattr(mod, "TAGS", []),
            })
        except Exception as e:
            results.append({"name": name, "description": f"(import error: {e})", "tags": []})
    return results


async def run_playbook(name: str, **kwargs) -> dict:
    """Import and run a named playbook. Returns its result dict."""
    try:
        mod = importlib.import_module(f"playbooks.{name}")
    except ModuleNotFoundError:
        raise ValueError(f"No playbook named '{name}'. Run `python -m playbooks` to list available ones.")
    return await mod.run(**kwargs)


def _main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print("Available playbooks:\n")
        for p in list_playbooks():
            tags = f"  [{', '.join(p['tags'])}]" if p["tags"] else ""
            print(f"  {p['name']:<30} {p['description']}{tags}")
        print("\nUsage: python -m playbooks <name> [--dry]")
        return

    name = args[0]
    dry = "--dry" in args
    if dry:
        try:
            mod = importlib.import_module(f"playbooks.{name}")
            print(f"Playbook: {name}")
            print(f"Description: {getattr(mod, 'DESCRIPTION', '(none)')}")
            print(f"Tags: {getattr(mod, 'TAGS', [])}")
        except ModuleNotFoundError:
            print(f"No playbook named '{name}'.")
        return

    result = asyncio.run(run_playbook(name))
    import json
    print(json.dumps(result, indent=2, ensure_ascii=False))
