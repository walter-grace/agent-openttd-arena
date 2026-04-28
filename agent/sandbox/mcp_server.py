"""Nutz OpenTTD MCP Server — exposes the live OpenTTD admin port to any
AI agent that speaks Model Context Protocol.

Speaks MCP over stdio (the standard transport for Claude Desktop, Cursor,
Zed, etc.). Dependencies: nothing beyond stdlib + `admin_client.py`.

Tools:
    list_companies     - all companies + value, cargo, perf, name
    list_towns         - top towns by population
    list_stations      - all stations (id, name, tile)
    list_vehicles      - all vehicles with profit_ty / profit_ly
    game_state         - one-shot snapshot of everything
    dispatch_route     - plan + dispatch an intra-town or pair route
    fund_town          - PerformTownAction TOWN_ACTION_FUND_BUILDINGS
    send_chat          - broadcast message in game chat
    pause / unpause    - game control via rcon

Configure for Claude Desktop in `claude_desktop_config.json`:

    {
      "mcpServers": {
        "openttd": {
          "command": "python3",
          "args": ["/path/to/agent/sandbox/mcp_server.py"]
        }
      }
    }

Then ask Claude: "list towns" / "build a route in Chino" / etc.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))
try:
    from admin_client import OpenTTDAdminClient
except ModuleNotFoundError:
    from .admin_client import OpenTTDAdminClient  # type: ignore

try:
    from .planner import plan_route
except ImportError:
    from planner import plan_route  # type: ignore

try:
    from . import x402_gate
except ImportError:
    import x402_gate  # type: ignore


PROTOCOL_VERSION = "2025-03-26"
SERVER_NAME = "openttd"
SERVER_VERSION = "0.1.0"


# ---------------------------------------------------------------------------
# Singleton admin client (shared across tool calls so we don't reconnect every
# request). Auto-reconnects if the server restarts.
# ---------------------------------------------------------------------------

_client: OpenTTDAdminClient | None = None
_client_lock = threading.Lock()


def get_client() -> OpenTTDAdminClient:
    global _client
    with _client_lock:
        if _client is None or _client._sock is None:
            c = OpenTTDAdminClient(name="MCP")
            c.connect()
            time.sleep(2)  # let GS push first state
            _client = c
        return _client


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "game_state",
        "description": "Live snapshot of the OpenTTD game: date, companies, "
                       "stations, vehicles, towns. Call this first to see "
                       "what's going on.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_companies",
        "description": "List all companies with value, quarterly cargo "
                       "delivered, performance rating, and current name "
                       "(name often encodes phase/status).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_towns",
        "description": "Top towns by population, with x/y tile coordinates.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max towns to return", "default": 10},
            },
        },
    },
    {
        "name": "list_vehicles",
        "description": "All vehicles with profit_ty (this year), profit_ly "
                       "(last year), and lifetime age.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_stations",
        "description": "All stations on the map (id, name, tile).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "dispatch_route",
        "description": "Plan and dispatch a bus route as a blueprint. "
                       "Same town for from + to triggers intra-town mode "
                       "(2 stations within the same town, mirrors a working "
                       "manual pattern). Different towns dispatch a pair "
                       "route. The Nutz Executor AI consumes the blueprint "
                       "and builds the infrastructure.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "from_town": {"type": "string", "description": "Town name"},
                "to_town": {"type": "string", "description": "Town name (same as from for intra-town)"},
                "job_id": {"type": "integer", "description": "Unique job id (pick > 1000)", "default": 1001},
            },
            "required": ["from_town", "to_town"],
        },
    },
    {
        "name": "fund_town",
        "description": "Spend money to spawn new buildings in a town "
                       "(costs ~$5k, adds 3-5 houses). Call this multiple "
                       "times to grow a town quickly. Note: this requires "
                       "the calling AI to BE in a company; if no AI is "
                       "running the call has no effect.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "town_name": {"type": "string"},
            },
            "required": ["town_name"],
        },
    },
    {
        "name": "send_chat",
        "description": "Broadcast a message in OpenTTD chat (visible to all players).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
            },
            "required": ["message"],
        },
    },
    {
        "name": "pause",
        "description": "Pause the game.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "unpause",
        "description": "Unpause the game.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "rcon",
        "description": "Run an arbitrary OpenTTD console command (admin-only). "
                       "Use sparingly; many commands are restricted in network mode.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
            },
            "required": ["command"],
        },
    },
]


def call_tool(name: str, args: dict) -> dict:
    """Run a tool by name. Returns the MCP result content shape."""
    # x402 payment gate — runs BEFORE any state mutation. Strips the bearer
    # from args so the rest of the dispatcher never sees it. Free tools and
    # disabled-mode short-circuit through `check_payment` returning ok=True.
    args = dict(args or {})  # copy — we mutate
    payment_token = args.pop("_payment_token", None)
    gate_args = dict(args)
    if payment_token is not None:
        gate_args["_payment_token"] = payment_token
    ok, err = x402_gate.check_payment(name, gate_args)
    if not ok:
        return _text(err)

    c = get_client()
    if name == "game_state":
        gs = c.get_gs_state() or {}
        return _text({
            "date": f"{gs.get('year')}.{gs.get('month'):02d}" if gs.get("year") else None,
            "companies": gs.get("companies") or [],
            "vehicles": gs.get("vehicles") or [],
            "stations": gs.get("stations") or [],
            "towns_top_5": (gs.get("towns_top") or [])[:5],
            "industries_count": gs.get("industries_count"),
        })
    if name == "list_companies":
        gs = c.get_gs_state() or {}
        return _text(gs.get("companies") or [])
    if name == "list_towns":
        gs = c.get_gs_state() or {}
        towns = gs.get("towns_top") or []
        limit = int(args.get("limit", 10))
        return _text(towns[:limit])
    if name == "list_vehicles":
        gs = c.get_gs_state() or {}
        return _text(gs.get("vehicles") or [])
    if name == "list_stations":
        gs = c.get_gs_state() or {}
        return _text(gs.get("stations") or [])
    if name == "dispatch_route":
        gs = c.get_gs_state() or {}
        bp, notes = plan_route(gs, args["from_town"], args["to_town"],
                               job_id=int(args.get("job_id", 1001)))
        if bp is None:
            return _text({"ok": False, "reason": notes})
        c.send_gs(bp.to_admin_cmd())
        return _text({"ok": True, "notes": notes,
                      "blueprint": {"job_id": bp.job_id,
                                    "stations": [list(bp.station_a), list(bp.station_b)],
                                    "path_len": len(bp.path)}})
    if name == "fund_town":
        # Funding is an in-game AI action; without an AI in a company we can't
        # call PerformTownAction directly. Recommend via chat instead.
        msg = (f"Manual: open '{args['town_name']}' town window in OpenTTD "
               f"and click Fund New Buildings (~$5k each).")
        c.rcon(f'say "[MCP] {msg[:180]}"')
        return _text({"ok": True, "advice": msg})
    if name == "send_chat":
        c.rcon(f'say "{args["message"][:180].replace(chr(34), chr(39))}"')
        return _text({"ok": True})
    if name == "pause":
        c.rcon("pause")
        return _text({"ok": True})
    if name == "unpause":
        c.rcon("unpause")
        return _text({"ok": True})
    if name == "rcon":
        c.rcon(args["command"])
        return _text({"ok": True, "sent": args["command"]})
    return _text({"error": f"unknown tool {name}"})


def _text(obj: Any) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(obj, indent=2, default=str)}]}


def _annotated_tools() -> list[dict]:
    """Return TOOLS with x402 price hints injected into descriptions and
    schemas, so MCP clients can discover what each tool costs and where to
    pass the payment token. No-op when x402 is disabled."""
    enabled = x402_gate._mode() != "disabled"
    out: list[dict] = []
    for tool in TOOLS:
        t = {**tool, "inputSchema": json.loads(json.dumps(tool["inputSchema"]))}
        name = t["name"]
        if enabled and x402_gate.is_paid_tool(name):
            price = x402_gate.price_usdc(name)
            t["description"] = (
                f"{t['description']} [x402: {price} USDC per call. Pass an "
                f"`_payment_token` arg with a bearer token from the operator's "
                f"create-mcpay gateway.]"
            )
            schema = t["inputSchema"]
            schema.setdefault("properties", {})
            schema["properties"]["_payment_token"] = {
                "type": "string",
                "description": (
                    f"x402 bearer token (mcp_...) — required, costs "
                    f"{price} USDC."
                ),
            }
        out.append(t)
    return out


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 dispatch
# ---------------------------------------------------------------------------

def handle(req: dict) -> dict | None:
    method = req.get("method")
    rid = req.get("id")
    params = req.get("params") or {}
    if method == "initialize":
        return _ok(rid, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}, "logging": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if method == "notifications/initialized":
        return None  # notification, no response
    if method == "tools/list":
        return _ok(rid, {"tools": _annotated_tools()})
    if method == "tools/call":
        try:
            result = call_tool(params["name"], params.get("arguments") or {})
            return _ok(rid, result)
        except Exception as e:
            return _err(rid, -32000, str(e))
    if method == "ping":
        return _ok(rid, {})
    return _err(rid, -32601, f"method not found: {method}")


def _ok(rid: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


# ---------------------------------------------------------------------------
# stdio transport
# ---------------------------------------------------------------------------

def main() -> int:
    sys.stderr.write(f"Nutz OpenTTD MCP server starting (protocol {PROTOCOL_VERSION})\n")
    sys.stderr.write(f"  {x402_gate.status_summary()}\n")
    sys.stderr.flush()
    while True:
        try:
            line = sys.stdin.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            req = json.loads(line)
        except Exception as e:
            sys.stderr.write(f"parse error: {e}\n")
            sys.stderr.flush()
            continue
        try:
            resp = handle(req)
            if resp is not None:
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()
        except Exception as e:
            sys.stderr.write(f"handle error: {e}\n")
            sys.stderr.flush()
            err = _err(req.get("id"), -32603, str(e))
            sys.stdout.write(json.dumps(err) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
