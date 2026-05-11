"""Per-tool-call usage logger for site-MCP servers.

Drop one line into a FastMCP server and every `@mcp.tool()` call gets a JSONL
record of: timestamp, server, tool, status, input/output tokens, duration_ms.

Wiring:

    from mcp.server.fastmcp import FastMCP
    from mcp_lib.usage_log import install_logger

    mcp = FastMCP("gmail_mcp")
    install_logger(mcp, "gmail_mcp")     # ← one line, before any @mcp.tool()

    @mcp.tool()                          # already-existing decorators unchanged
    async def gmail_compose(...): ...

View with `python -m mcp_lib.usage_stats [--day|--week|--by tool|server|day]`.

Token counting is a rough `len(text) // 4` estimate — good enough to compare
call costs, not for billing. Override the log path with $MCP_USAGE_LOG.
"""
from __future__ import annotations

import functools
import inspect
import json
import os
import time
from pathlib import Path
from typing import Any

LOG_PATH = Path(os.environ.get(
    "MCP_USAGE_LOG",
    str(Path.home() / ".cloud_agents" / "mcp_usage.jsonl"),
))


def estimate_tokens(s: str) -> int:
    """~4 chars per token. Crude but stable; swap for tiktoken if needed."""
    if not s:
        return 0
    return max(1, len(s) // 4)


def _serialize(x: Any) -> str:
    if x is None:
        return ""
    if isinstance(x, str):
        return x
    try:
        return json.dumps(x, default=str, separators=(",", ":"))
    except Exception:
        return str(x)


def _write(record: dict) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    except Exception:
        # Never let logging break a tool call.
        pass


def _logged(fn, server: str):
    tool_name = fn.__name__
    is_async = inspect.iscoroutinefunction(fn)

    def _serialize_inputs(args, kwargs) -> str:
        return _serialize({"args": [_serialize(a) for a in args],
                           "kwargs": {k: _serialize(v) for k, v in kwargs.items()}})

    def _record(inp: str, out: str, status: str, dur_ms: int, error: str | None = None) -> None:
        rec = {
            "ts": time.time(),
            "server": server,
            "tool": tool_name,
            "status": status,
            "input_tokens": estimate_tokens(inp),
            "output_tokens": estimate_tokens(out),
            "duration_ms": dur_ms,
        }
        if error is not None:
            rec["error"] = error[:200]
        _write(rec)

    if is_async:
        @functools.wraps(fn)
        async def async_wrap(*args, **kwargs):
            t0 = time.perf_counter()
            inp = _serialize_inputs(args, kwargs)
            try:
                result = await fn(*args, **kwargs)
                _record(inp, _serialize(result), "ok", int((time.perf_counter() - t0) * 1000))
                return result
            except Exception as e:
                _record(inp, "", "error", int((time.perf_counter() - t0) * 1000), str(e))
                raise
        return async_wrap

    @functools.wraps(fn)
    def sync_wrap(*args, **kwargs):
        t0 = time.perf_counter()
        inp = _serialize_inputs(args, kwargs)
        try:
            result = fn(*args, **kwargs)
            _record(inp, _serialize(result), "ok", int((time.perf_counter() - t0) * 1000))
            return result
        except Exception as e:
            _record(inp, "", "error", int((time.perf_counter() - t0) * 1000), str(e))
            raise
    return sync_wrap


def install_logger(mcp_instance: Any, server_name: str) -> None:
    """Monkey-patch a FastMCP instance so every @mcp.tool() auto-logs.

    Idempotent: safe to call once per server. Wraps `mcp_instance.tool` so
    subsequent `@mcp.tool()` decorations transparently wrap the function in
    a logger before passing it to the original registration.
    """
    if getattr(mcp_instance, "_usage_logger_installed", False):
        return
    orig_tool = mcp_instance.tool

    def patched_tool(*args, **kwargs):
        def deco(fn):
            return orig_tool(*args, **kwargs)(_logged(fn, server_name))
        return deco

    mcp_instance.tool = patched_tool
    mcp_instance._usage_logger_installed = True
