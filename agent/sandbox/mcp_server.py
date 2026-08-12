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
    read_squirrel_ai   - read current AI source (.nut) — always safe
    update_squirrel_ai - replace AI source + live-reload (DANGEROUS, env-gated)

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
import os
import re
import shutil
import sys
import threading
import time
from datetime import datetime
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
# Self-modifying AI: paths, guardrails, env flag.
# ---------------------------------------------------------------------------
# Path to the live AI source file. Override for tests via the
# NUTZ_AI_MAIN_NUT env var (test injects a tmpdir copy so no real game state
# is touched).
DEFAULT_AI_MAIN_NUT = Path.home() / "Documents" / "OpenTTD" / "ai" / "nutz_executor" / "main.nut"
AI_NAME = "Nutz Executor"

# Hard cap on .nut payload — defensive against runaway agent rewrites.
MAX_NUT_BYTES = 200 * 1024  # 200 KB

# Whitelist of allowed `import("...")` first-args. Squirrel `import` can pull
# arbitrary native libraries — only the two AI libs the executor actually
# needs are permitted. Pattern: import("name", "Class", version).
ALLOWED_IMPORTS = {"pathfinder.road", "graph.aystar"}

# Substrings that must NOT appear in the proposed source. These are hostile
# patterns: shell-out attempts, file I/O, eval-style indirection. The Squirrel
# VM the AI runs in shouldn't have most of these, but the agent may try
# anyway — fail closed.
FORBIDDEN_SUBSTRINGS = (
    "system(",
    "exec(",
    "Process(",
    "system.exec",
    "popen(",
    "spawn(",
    "os.execute",
    "io.open",
    "io.popen",
    "loadfile(",
    "dofile(",
    "require(",
)

# How long to wait after rcon reload before deciding the new AI is alive.
RELOAD_LIVENESS_DELAY_S = 5.0

# Regex to find import lines so we can validate them.
_IMPORT_RE = re.compile(r'import\s*\(\s*"([^"]*)"')


def _ai_edit_enabled() -> bool:
    """Self-modifying mode is gated behind an explicit env var. Default OFF."""
    return os.environ.get("MCP_ALLOW_AI_EDIT", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _ai_main_nut_path() -> Path:
    """Resolve target main.nut. Env override lets tests redirect at a tmp file
    without touching a real OpenTTD install."""
    override = os.environ.get("NUTZ_AI_MAIN_NUT")
    return Path(override) if override else DEFAULT_AI_MAIN_NUT


def _validate_squirrel_source(source: str) -> tuple[bool, str]:
    """Cheap pre-flight checks before we let the bytes near the file system.
    Returns (ok, reason). Reason is empty when ok=True."""
    if not isinstance(source, str) or not source:
        return False, "source must be a non-empty string"
    nbytes = len(source.encode("utf-8"))
    if nbytes > MAX_NUT_BYTES:
        return False, f"source is {nbytes} bytes; cap is {MAX_NUT_BYTES}"
    # The NoAI runtime requires a class extending AIController. Without one
    # the AI won't even load — refuse rather than nuke the install.
    if "AIController" not in source:
        return False, "source must reference AIController (class extends AIController)"
    # Whitelist imports. Any `import("foo"...)` whose name isn't in
    # ALLOWED_IMPORTS is rejected.
    for m in _IMPORT_RE.finditer(source):
        name = m.group(1)
        if name not in ALLOWED_IMPORTS:
            return False, f"import {name!r} not in whitelist {sorted(ALLOWED_IMPORTS)}"
    # Forbidden substrings.
    for needle in FORBIDDEN_SUBSTRINGS:
        if needle in source:
            return False, f"forbidden substring {needle!r} in source"
    # Crude balanced-brace sanity check — Squirrel is C-like, mismatched
    # braces guarantee a parse error in-game which would crash the AI.
    if source.count("{") != source.count("}"):
        return False, "unbalanced braces"
    if source.count("(") != source.count(")"):
        return False, "unbalanced parens"
    return True, ""


def _backup_main_nut(target: Path) -> Path | None:
    """Copy current main.nut to main.nut.backup-<ISO timestamp>. Returns the
    backup path, or None if the source file doesn't exist yet (first write)."""
    if not target.exists():
        return None
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%S")
    backup = target.with_suffix(target.suffix + f".backup-{ts}")
    shutil.copy2(target, backup)
    return backup


def _reload_ai_via_admin(client: OpenTTDAdminClient, company_id: int | None) -> dict:
    """Bounce the Nutz Executor AI in-game via rcon. We don't get a clean
    return code from rcon over admin — the response is async — so we just
    fire the commands and let the caller verify liveness afterwards.

    `company_id` is the existing AI's company id (0-indexed). If provided we
    `stop_ai N` before `rescan_ai` + `start_ai`. If None we skip the stop
    (no AI was running).
    """
    sent: list[str] = []
    if company_id is not None and company_id >= 0:
        # OpenTTD console: stop_ai takes the company NUMBER (1-indexed in UI,
        # but the rcon `stop_ai` command takes the human-visible company id —
        # i.e. the same one shown in the in-game company list).
        cmd = f"stop_ai {company_id + 1}"
        client.rcon(cmd)
        sent.append(cmd)
    client.rcon("rescan_ai")
    sent.append("rescan_ai")
    # Quoted name — must match info.nut GetName().
    cmd = f'start_ai "{AI_NAME}"'
    client.rcon(cmd)
    sent.append(cmd)
    return {"commands": sent}


def _check_ai_alive(client: OpenTTDAdminClient, company_name_substr: str = "Nutz") -> tuple[bool, int | None]:
    """Look at the cached company table for a company whose name or manager
    references the AI. If we find an `is_ai` company tagged by Nutz, treat
    it as alive. Returns (alive, company_id)."""
    for cid, c in (client.companies or {}).items():
        if not c.get("is_ai"):
            continue
        name = (c.get("name") or "") + " " + (c.get("manager") or "")
        if company_name_substr.lower() in name.lower():
            return True, cid
    return False, None


def _find_existing_ai_company(client: OpenTTDAdminClient) -> int | None:
    alive, cid = _check_ai_alive(client)
    return cid if alive else None


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
    {
        "name": "read_squirrel_ai",
        "description": "Return the current contents of the in-game Nutz "
                       "Executor AI source file. Use this to understand the "
                       "AI's current strategy before proposing modifications.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "update_squirrel_ai",
        "description": "Replace the in-game Nutz Executor AI source file "
                       "with new Squirrel code, then reload the AI in-game. "
                       "Returns the new company_id and reload outcome. "
                       "DANGEROUS — only enabled when MCP_ALLOW_AI_EDIT=true "
                       "env var is set. Always auto-backs up; auto-restores "
                       "on AI crash within 5s.",
        "inputSchema": {
            "type": "object",
            "required": ["source"],
            "properties": {
                "source": {
                    "type": "string",
                    "description": "Full new content of main.nut. Must "
                                   "include the AIController class. ~200KB max.",
                },
                "reason": {
                    "type": "string",
                    "description": "Why you're changing it (logged for traceability)",
                },
            },
        },
    },
    {
        "name": "propose_squirrel_diff",
        "description": "Apply a unified-diff patch to the in-game Nutz "
                       "Executor AI source, then reload via the same pipeline "
                       "as update_squirrel_ai. Easier than sending the whole "
                       "file when you only want to tweak a few lines. "
                       "DANGEROUS — same env gate (MCP_ALLOW_AI_EDIT=true) "
                       "and validation rules apply post-patch.",
        "inputSchema": {
            "type": "object",
            "required": ["patch"],
            "properties": {
                "patch": {
                    "type": "string",
                    "description": "Unified-diff patch text (output of `diff -u` "
                                   "/ `git diff`). Applied with the system "
                                   "`patch` command in dry-run-then-apply mode.",
                },
                "reason": {"type": "string"},
            },
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
    if name == "read_squirrel_ai":
        path = _ai_main_nut_path()
        if not path.exists():
            return _text({"ok": False, "reason": f"main.nut not found at {path}"})
        try:
            src = path.read_text(encoding="utf-8")
        except Exception as e:
            return _text({"ok": False, "reason": f"read failed: {e}"})
        return _text({
            "ok": True,
            "path": str(path),
            "bytes": len(src.encode("utf-8")),
            "source": src,
        })
    if name == "update_squirrel_ai":
        return _do_update_squirrel_ai(c, args)
    if name == "propose_squirrel_diff":
        return _do_propose_squirrel_diff(c, args)
    return _text({"error": f"unknown tool {name}"})


def _do_update_squirrel_ai(c: OpenTTDAdminClient, args: dict) -> dict:
    """Handler for update_squirrel_ai. Validates -> backs up -> writes ->
    reloads AI -> waits RELOAD_LIVENESS_DELAY_S -> rolls back if dead."""
    if not _ai_edit_enabled():
        return _text({
            "ok": False,
            "status": 403,
            "error": "Self-modifying mode is disabled.",
            "hint": "Set MCP_ALLOW_AI_EDIT=true on the MCP server's env to enable.",
        })
    source = args.get("source")
    reason = args.get("reason") or ""
    ok, why = _validate_squirrel_source(source)
    if not ok:
        return _text({"ok": False, "status": 400, "error": "validation failed", "reason": why})

    target = _ai_main_nut_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = _backup_main_nut(target)

    # Identify any currently-running Nutz AI so we can stop it cleanly.
    prior_cid = _find_existing_ai_company(c)

    try:
        target.write_text(source, encoding="utf-8")
    except Exception as e:
        return _text({
            "ok": False, "status": 500,
            "error": f"write failed: {e}",
            "backup": str(backup) if backup else None,
        })

    sys.stderr.write(
        f"[update_squirrel_ai] wrote {len(source.encode('utf-8'))}B to "
        f"{target} (backup={backup}, reason={reason!r})\n"
    )
    sys.stderr.flush()

    reload_info = _reload_ai_via_admin(c, prior_cid)

    # Wait briefly for the AI to either come up or crash.
    time.sleep(RELOAD_LIVENESS_DELAY_S)
    alive, new_cid = _check_ai_alive(c)

    if not alive and backup is not None:
        # Auto-rollback. Restore previous source and try once more.
        try:
            shutil.copy2(backup, target)
            sys.stderr.write(
                f"[update_squirrel_ai] AI dead after reload — restored backup "
                f"{backup}\n"
            )
            sys.stderr.flush()
            _reload_ai_via_admin(c, None)
        except Exception as e:
            return _text({
                "ok": False, "status": 500,
                "error": "AI failed to start AND backup restore failed",
                "restore_error": str(e),
                "backup": str(backup),
                "reload": reload_info,
            })
        return _text({
            "ok": False, "status": 500,
            "error": "AI failed to start within 5s — rolled back to previous source",
            "backup_restored": str(backup),
            "reload": reload_info,
        })

    return _text({
        "ok": True,
        "company_id": new_cid,
        "path": str(target),
        "backup": str(backup) if backup else None,
        "reload": reload_info,
        "reason": reason,
    })


def _do_propose_squirrel_diff(c: OpenTTDAdminClient, args: dict) -> dict:
    """Handler for propose_squirrel_diff. Applies unified diff via the system
    `patch` command, then re-uses the update pipeline."""
    if not _ai_edit_enabled():
        return _text({
            "ok": False, "status": 403,
            "error": "Self-modifying mode is disabled.",
            "hint": "Set MCP_ALLOW_AI_EDIT=true on the MCP server's env to enable.",
        })
    patch_text = args.get("patch")
    if not isinstance(patch_text, str) or not patch_text.strip():
        return _text({"ok": False, "status": 400, "error": "patch is required"})

    target = _ai_main_nut_path()
    if not target.exists():
        return _text({"ok": False, "status": 400,
                      "error": f"main.nut not found at {target}; cannot diff"})

    import subprocess
    import tempfile
    # Apply against a copy so we can validate before swapping in.
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        scratch = td_path / "main.nut"
        shutil.copy2(target, scratch)
        patch_file = td_path / "p.diff"
        patch_file.write_text(patch_text, encoding="utf-8")
        # `patch` is a hard dep — present on macOS + every Linux dev box.
        try:
            proc = subprocess.run(
                ["patch", "-u", "-p0", str(scratch.name), str(patch_file)],
                cwd=td_path, capture_output=True, text=True, timeout=10,
            )
        except FileNotFoundError:
            return _text({"ok": False, "status": 500,
                          "error": "system `patch` command not found"})
        except subprocess.TimeoutExpired:
            return _text({"ok": False, "status": 500, "error": "patch timed out"})
        if proc.returncode != 0:
            return _text({
                "ok": False, "status": 400,
                "error": "patch failed to apply",
                "stdout": proc.stdout, "stderr": proc.stderr,
            })
        new_source = scratch.read_text(encoding="utf-8")

    # Re-use the full update pipeline: validates, backs up, writes, reloads,
    # rolls back on AI crash.
    return _do_update_squirrel_ai(c, {"source": new_source,
                                      "reason": args.get("reason") or "diff-patch"})


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
            cur = x402_gate._currency()
            chain = x402_gate._chain()
            t["description"] = (
                f"{t['description']} [x402: ~${price} in {cur} on {chain} per "
                f"call. Pass an `_payment_token` arg with a bearer token from "
                f"the operator's payment gateway.]"
            )
            schema = t["inputSchema"]
            schema.setdefault("properties", {})
            schema["properties"]["_payment_token"] = {
                "type": "string",
                "description": (
                    f"x402 bearer token (mcp_...) — required, ~${price} in "
                    f"{cur} on {chain}."
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
