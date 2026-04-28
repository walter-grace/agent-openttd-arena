"""Nutz Chatbot — listens to in-game chat via admin port and replies.

Run alongside the conductor:
    python3 -u -m sandbox.chatbot

Supported commands (case-insensitive, just type in game chat):
    help              - list commands
    status            - profitable bus count, total income
    towns             - top 8 towns by population
    build <name>      - dispatch an intra-town blueprint for that town
    grow <name>       - schedule a town grow (currently advisory)
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
try:
    from admin_client import OpenTTDAdminClient
except ModuleNotFoundError:
    from .admin_client import OpenTTDAdminClient  # type: ignore

try:
    from .planner import plan_route
except ImportError:
    from planner import plan_route  # type: ignore


PROMPT = "Nutz"


def reply(c: OpenTTDAdminClient, msg: str) -> None:
    # Use rcon say so all clients see the message in chat.
    safe = msg.replace('"', "'")[:200]
    try:
        c.rcon(f'say "[{PROMPT}] {safe}"')
    except Exception:
        pass


def cmd_status(c: OpenTTDAdminClient) -> str:
    gs = c.get_gs_state() or {}
    vehs = gs.get("vehicles") or []
    profitable = [v for v in vehs if (v.get("profit_ty") or 0) > 0]
    total_ty = sum(v.get("profit_ty", 0) or 0 for v in vehs)
    cos = gs.get("companies") or []
    co_lines = ", ".join(
        f"{co.get('name','?')[:18]} val=${co.get('value','?')}"
        for co in cos[:3]
    )
    return (f"vehicles={len(vehs)} profitable={len(profitable)} "
            f"sum_ty=${total_ty} | {co_lines}")


def cmd_towns(c: OpenTTDAdminClient) -> str:
    gs = c.get_gs_state() or {}
    towns = gs.get("towns_top") or []
    top = sorted(towns, key=lambda t: -int(t.get("pop", 0)))[:5]
    return "top: " + ", ".join(f"{t.get('name')}({t.get('pop')})" for t in top)


def cmd_build(c: OpenTTDAdminClient, town_name: str, job_id: int) -> str:
    gs = c.get_gs_state() or {}
    bp, notes = plan_route(gs, town_name, town_name, job_id=job_id)
    if bp is None:
        return f"plan fail: {notes[:120]}"
    try:
        c.send_gs(bp.to_admin_cmd())
    except Exception as e:
        return f"send fail: {e}"
    return f"dispatched bp{job_id}: {notes[:120]}"


def main() -> int:
    c = OpenTTDAdminClient(name="NutzBot")
    c.connect()
    time.sleep(2)
    print("[chatbot] connected to admin port. Type in game chat: help")
    reply(c, "chatbot online. type 'help'")
    job_id = 1000  # high to avoid colliding with conductor's job ids
    last_seen_ts = time.time()
    while True:
        try:
            chats = c.drain_chat() or []
        except Exception:
            chats = []
        for chat in chats:
            text = (chat.get("text") or "").strip()
            ts = chat.get("ts", 0)
            if ts <= last_seen_ts:
                continue
            last_seen_ts = ts
            # Skip our own messages (server echoes).
            if text.startswith("[" + PROMPT + "]"):
                continue
            print(f"[chatbot] heard: {text!r}")
            low = text.lower()
            try:
                if low in ("help", "?", "h"):
                    reply(c, "cmds: status | towns | build <town> | help")
                elif low == "status":
                    reply(c, cmd_status(c))
                elif low == "towns":
                    reply(c, cmd_towns(c))
                elif low.startswith("build "):
                    name = text.split(None, 1)[1].strip()
                    job_id += 1
                    reply(c, cmd_build(c, name, job_id))
                else:
                    # ignore non-commands silently
                    pass
            except Exception as e:
                reply(c, f"err: {e}")
        time.sleep(2)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nbye")
