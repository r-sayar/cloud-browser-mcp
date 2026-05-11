"""CLI for viewing MCP usage stats logged by mcp_lib.usage_log.

Examples:
    python -m mcp_lib.usage_stats                  # all-time, by tool
    python -m mcp_lib.usage_stats --day            # last 24h, by tool
    python -m mcp_lib.usage_stats --week --by day  # last 7 days, grouped by day
    python -m mcp_lib.usage_stats --by server      # all-time, by MCP server
"""
from __future__ import annotations

import argparse
import json
import os
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

LOG_PATH = Path(os.environ.get(
    "MCP_USAGE_LOG",
    str(Path.home() / ".cloud_agents" / "mcp_usage.jsonl"),
))


def load_records(since: float | None = None) -> list[dict]:
    if not LOG_PATH.exists():
        return []
    out = []
    with open(LOG_PATH) as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since and r.get("ts", 0) < since:
                continue
            out.append(r)
    return out


def _bucket_key(r: dict, by: str) -> str:
    if by == "day":
        return datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d")
    return r.get(by, "<unknown>")


def aggregate(records: list[dict], by: str) -> list[tuple[str, dict]]:
    bucket: dict[str, dict] = defaultdict(lambda: {"calls": 0, "errors": 0, "input": 0, "output": 0, "ms": 0})
    for r in records:
        k = _bucket_key(r, by)
        b = bucket[k]
        b["calls"] += 1
        if r.get("status") == "error":
            b["errors"] += 1
        b["input"] += r.get("input_tokens", 0)
        b["output"] += r.get("output_tokens", 0)
        b["ms"] += r.get("duration_ms", 0)
    rows = list(bucket.items())
    if by == "day":
        rows.sort(key=lambda kv: kv[0])  # chronological
    else:
        rows.sort(key=lambda kv: kv[1]["input"] + kv[1]["output"], reverse=True)  # cost desc
    return rows


def fmt_table(by: str, rows: list[tuple[str, dict]]) -> str:
    if not rows:
        return "(no calls)"
    headers = [by, "calls", "err", "in tok", "out tok", "total", "avg/call", "ms"]
    data = []
    for k, v in rows:
        total = v["input"] + v["output"]
        avg = total // v["calls"] if v["calls"] else 0
        data.append([str(k), v["calls"], v["errors"], v["input"], v["output"], total, avg, v["ms"]])
    widths = [max(len(str(x)) for x in [h] + [r[i] for r in data]) for i, h in enumerate(headers)]
    lines = []
    lines.append("  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    lines.append("  ".join("-" * w for w in widths))
    for r in data:
        lines.append("  ".join(str(c).ljust(w) for c, w in zip(r, widths)))
    return "\n".join(lines)


def main() -> None:
    global LOG_PATH  # noqa: PLW0603
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    grp = p.add_mutually_exclusive_group()
    grp.add_argument("--day", action="store_true", help="last 24h")
    grp.add_argument("--week", action="store_true", help="last 7 days")
    grp.add_argument("--month", action="store_true", help="last 30 days")
    p.add_argument("--by", choices=["tool", "server", "day"], default="tool")
    p.add_argument("--log", default=None, help=f"override log path (default: {LOG_PATH})")
    args = p.parse_args()

    if args.log:
        LOG_PATH = Path(args.log)

    now = time.time()
    if args.day:
        since, label = now - 86400, "Last 24h"
    elif args.week:
        since, label = now - 86400 * 7, "Last 7 days"
    elif args.month:
        since, label = now - 86400 * 30, "Last 30 days"
    else:
        since, label = None, "All time"

    records = load_records(since=since)
    print(f"=== MCP usage — {label} — {len(records)} calls — log: {LOG_PATH} ===")
    if not records:
        print("(no records — log file empty or filter excludes everything)")
        return

    total_in = sum(r.get("input_tokens", 0) for r in records)
    total_out = sum(r.get("output_tokens", 0) for r in records)
    errors = sum(1 for r in records if r.get("status") == "error")
    print(f"totals: input={total_in:,}  output={total_out:,}  total={total_in + total_out:,} tokens   ({errors} errors)")
    print()
    rows = aggregate(records, args.by)
    print(f"by {args.by}:")
    print(fmt_table(args.by, rows))


if __name__ == "__main__":
    main()
