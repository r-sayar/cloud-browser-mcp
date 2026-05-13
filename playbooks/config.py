"""Playbook backend configuration.

A backend defines where BrowserOS is reachable (MCP HTTP endpoint + noVNC URL).
Set PLAYBOOK_BACKEND env var to override the default, or pass backend= to run().

Available backends:
  hetzner   — remote BrowserOS on Hetzner (tunneled via autossh)
  local_1   — local Docker container 1
  local_2   — local Docker container 2
  local_3   — local Docker container 3
"""
import os

BACKENDS: dict[str, dict[str, str]] = {
    "hetzner": {
        "bos_url":   "http://localhost:9204/mcp",
        "novnc_url": "http://localhost:6084/",
    },
    "local_1": {
        "bos_url":   "http://localhost:9201/mcp",
        "novnc_url": "http://localhost:6081/",
    },
    "local_2": {
        "bos_url":   "http://localhost:9202/mcp",
        "novnc_url": "http://localhost:6082/",
    },
    "local_3": {
        "bos_url":   "http://localhost:9203/mcp",
        "novnc_url": "http://localhost:6083/",
    },
}

DEFAULT_BACKEND = os.environ.get("PLAYBOOK_BACKEND", "hetzner")


def get_backend(name: str | None = None) -> dict[str, str]:
    """Return the config dict for the named backend (or the default)."""
    key = name or DEFAULT_BACKEND
    if key not in BACKENDS:
        raise ValueError(f"Unknown backend '{key}'. Choose from: {list(BACKENDS)}")
    return BACKENDS[key]
