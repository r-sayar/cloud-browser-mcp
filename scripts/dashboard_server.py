#!/usr/bin/env python3
"""Tiny HTTP server for dashboard.html — adds on/off control over the
docker-compose browseros-N slots.

Serves dashboard.html and three JSON endpoints, all on 127.0.0.1 only:

    GET  /api/slots                 → [{id, container, state}, …]
    POST /api/slots/{n}/start       → {id, container, state}
    POST /api/slots/{n}/stop        → {id, container, state}

`state` is one of:
    "running"  — container exists and is running
    "stopped"  — container exists but is stopped
    "missing"  — no container with this name (compose never created it)
    "error"    — docker call failed; see `error` field

Runs against the local docker daemon via the `docker` CLI. Bind is hard-coded
to 127.0.0.1 — never expose this server publicly.

Launch:  python3 scripts/dashboard_server.py [--port 7000]
"""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_HTML = REPO_ROOT / "dashboard.html"
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"

# Slot id → docker container name + compose service name. Keep in sync with
# docker-compose.yml's `browseros-N` services.
SLOTS = {
    1: ("cbm-browseros-1", "browseros-1"),
    2: ("cbm-browseros-2", "browseros-2"),
    3: ("cbm-browseros-3", "browseros-3"),
}


def _docker_state(container: str) -> str:
    """Return 'running' | 'stopped' | 'missing' | 'error: …'."""
    try:
        out = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Status}}", container],
            capture_output=True, text=True, timeout=5,
        )
    except FileNotFoundError:
        return "error: docker CLI not on PATH"
    except subprocess.TimeoutExpired:
        return "error: docker inspect timed out"
    if out.returncode != 0:
        # "No such object" → container doesn't exist
        if "No such object" in (out.stderr or ""):
            return "missing"
        return f"error: {out.stderr.strip()[:120]}"
    status = (out.stdout or "").strip()
    if status == "running":
        return "running"
    return "stopped"  # exited, created, paused, dead, restarting — all treated as off


def _compose(args: list[str]) -> tuple[bool, str]:
    """Run `docker compose <args>` from the repo root. Returns (ok, message)."""
    cmd = ["docker", "compose", "-f", str(COMPOSE_FILE), *args]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return False, "docker CLI not on PATH"
    except subprocess.TimeoutExpired:
        return False, "compose timed out"
    if out.returncode != 0:
        return False, (out.stderr or out.stdout).strip()[:200]
    return True, (out.stdout or "").strip()[:200]


def _slot_snapshot(slot_id: int) -> dict:
    container, _ = SLOTS[slot_id]
    return {"id": slot_id, "container": container, "state": _docker_state(container)}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: A003 — quieter logs
        sys.stderr.write(f"[dashboard] {self.address_string()} {fmt % args}\n")

    def _send_json(self, code: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):  # noqa: N802
        # Preview-server health checks hit HEAD /. Reply 200 so the page is
        # considered ready; no body to send.
        if self.path == "/" or self.path.startswith("/index"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def do_GET(self):  # noqa: N802
        if self.path == "/" or self.path.startswith("/index"):
            try:
                self._send_text(200, DASHBOARD_HTML.read_bytes(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send_text(500, b"dashboard.html missing", "text/plain")
            return
        if self.path == "/api/slots":
            self._send_json(200, [_slot_snapshot(i) for i in SLOTS])
            return
        self._send_text(404, b"not found", "text/plain")

    def do_POST(self):  # noqa: N802
        parts = self.path.strip("/").split("/")
        # /api/slots/<n>/start  |  /api/slots/<n>/stop
        if len(parts) == 4 and parts[:2] == ["api", "slots"] and parts[3] in ("start", "stop"):
            try:
                slot = int(parts[2])
            except ValueError:
                self._send_json(400, {"error": "slot must be an integer"})
                return
            if slot not in SLOTS:
                self._send_json(404, {"error": f"unknown slot {slot}"})
                return
            _, service = SLOTS[slot]
            action = parts[3]
            # Use `compose up -d --no-recreate` for start (handles "missing" case
            # by creating the container if compose never did); `compose stop` for stop.
            if action == "start":
                ok, msg = _compose(["up", "-d", "--no-recreate", service])
            else:
                ok, msg = _compose(["stop", service])
            snap = _slot_snapshot(slot)
            snap["docker_message"] = msg
            self._send_json(200 if ok else 500, snap)
            return
        self._send_text(404, b"not found", "text/plain")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=7000, help="port to listen on (default: 7000)")
    ap.add_argument("--bind", default="127.0.0.1", help="bind address (default: 127.0.0.1 — DO NOT change to 0.0.0.0)")
    args = ap.parse_args()

    if args.bind != "127.0.0.1":
        print(f"WARNING: binding to {args.bind} exposes container start/stop to the network.", file=sys.stderr)

    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"dashboard at http://{args.bind}:{args.port}/  (Ctrl+C to stop)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", flush=True)
        srv.server_close()


if __name__ == "__main__":
    main()
