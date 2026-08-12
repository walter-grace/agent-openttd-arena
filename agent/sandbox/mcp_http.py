"""HTTP transport for the OpenTTD MCP server — lets DISTANT agents play.

The stdio server (`mcp_server.py`) requires the agent to spawn the Python
process locally, so only someone on the same machine can drive the game.
This wraps the exact same JSON-RPC dispatcher (`mcp_server.handle`) in an
HTTP endpoint, so any agent anywhere can connect over the network to a
hosted arena.

Transport: MCP messages are POSTed as JSON to ``/mcp`` and the JSON-RPC
response is returned in the body (the simple, non-streaming shape of MCP's
Streamable HTTP transport — enough for tool calls). ``GET /health`` is a
liveness probe.

Payment: over HTTP we finally have headers, so the agent passes its arena
key as ``Authorization: Bearer mcp_…``. For ``tools/call`` this bridge
injects that bearer into the tool arguments as ``_payment_token`` — exactly
what the existing `x402_gate` reads — so every paid tool is charged against
the gateway (set ``X402_MODE=gateway`` + ``X402_GATEWAY_URL`` to your
deployed arena-gateway Worker). Free/observation tools need no token.

Run on the game host (next to OpenTTD + admin port):

    X402_MODE=gateway \\
    X402_GATEWAY_URL=https://arena-gateway.<you>.workers.dev \\
    X402_CHAIN=robinhood-chain X402_CURRENCY=HERO \\
    python3 -m agent.sandbox.mcp_http --port 8990

Then expose it (a Cloudflare Tunnel keeps the box unexposed) and point
agents at ``https://arena.<you>.dev/mcp`` with their Bearer key.

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Import the shared dispatcher + gate from the stdio server.
try:
    from . import mcp_server, x402_gate
except ImportError:  # run as a script
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from agent.sandbox import mcp_server, x402_gate  # type: ignore

MAX_BODY = 1 << 20  # 1 MB cap on a single MCP message


def _inject_bearer(req: dict, bearer: str | None) -> dict:
    """For tools/call on a paid tool, put the HTTP bearer where the gate
    looks for it (``arguments._payment_token``). No-op otherwise, and never
    overrides a token the client already supplied in-band."""
    if not bearer or req.get("method") != "tools/call":
        return req
    params = req.setdefault("params", {})
    name = params.get("name")
    if not x402_gate.is_paid_tool(name):
        return req
    args = params.setdefault("arguments", {})
    args.setdefault("_payment_token", bearer)
    return req


class Handler(BaseHTTPRequestHandler):
    server_version = "arena-mcp-http/0.1"

    def _send(self, code: int, obj, *, cors=True):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "authorization,content-type")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # quieter than the default
        sys.stderr.write("[mcp_http] " + (a[0] % a[1:]) + "\n")

    def do_OPTIONS(self):
        self._send(204, {})

    def do_GET(self):
        if self.path.rstrip("/") in ("", "/health"):
            return self._send(200, {"ok": True, "service": "arena-mcp-http", "x402": x402_gate.status_summary()})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") not in ("/mcp", ""):
            return self._send(404, {"error": "not found — POST MCP messages to /mcp"})
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return self._send(400, {"error": "missing or oversized body"})
        try:
            req = json.loads(self.rfile.read(length))
        except Exception as e:
            return self._send(400, {"error": f"invalid JSON: {e}"})

        auth = self.headers.get("Authorization") or ""
        bearer = auth[7:].strip() if auth.startswith("Bearer ") else None

        # Batch or single. MCP is single-message per POST in the simple shape.
        if isinstance(req, list):
            out = [r for r in (self._dispatch(m, bearer) for m in req) if r is not None]
            return self._send(200, out)
        resp = self._dispatch(req, bearer)
        # Notifications (resp is None) → 202 Accepted, empty.
        if resp is None:
            return self._send(202, {})
        self._send(200, resp)

    def _dispatch(self, req: dict, bearer: str | None):
        try:
            req = _inject_bearer(req, bearer)
            return mcp_server.handle(req)
        except Exception as e:  # never leak a stack trace to the client
            return mcp_server._err(req.get("id"), -32603, str(e))


def main() -> int:
    ap = argparse.ArgumentParser(description="HTTP transport for the OpenTTD MCP server")
    ap.add_argument("--host", default=os.environ.get("MCP_HTTP_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("MCP_HTTP_PORT", "8990")))
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    sys.stderr.write(
        f"arena MCP-HTTP on http://{args.host}:{args.port}/mcp — {x402_gate.status_summary()}\n"
        f"  agents POST JSON-RPC here with `Authorization: Bearer <arena key>`\n"
    )
    sys.stderr.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
