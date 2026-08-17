#!/bin/bash
# Arena container entrypoint: write config, launch the headless server, start
# the Nutz Executor AI, and (optionally) the MCP bridge + dashboard. Foreground
# so the container's lifetime == the arena's.
set -euo pipefail

OTTD_DIR="${OTTD_DIR:-/arena}"
ADMIN_PW="${ADMIN_PW:-nutzarena}"
ADMIN_PORT=3977
GAME_PORT=3979
CFG="$OTTD_DIR/openttd.cfg"
LOG=/tmp/ottd.log

say() { printf "\033[1;32m▸ %s\033[0m\n" "$*"; }

# 1. Config — admin port + insecure login + QUOTED AI/GS slots + public knobs.
python3 - "$CFG" "$ADMIN_PW" "$ADMIN_PORT" "$SERVER_NAME" "$PUBLIC" "$MAX_COMPANIES" "$MAX_CLIENTS" <<'PY'
import sys, re, os
cfg, pw, port, name, public, max_co, max_cl = sys.argv[1:8]
s = open(cfg).read() if os.path.exists(cfg) else ""

def set_kv(s, section, key, val):
    pat = re.compile(r"(\[" + re.escape(section) + r"\]\n)(.*?)(?=\n\[|\Z)", re.S)
    m = pat.search(s)
    if not m:
        return s.rstrip() + f"\n\n[{section}]\n{key} = {val}\n"
    blk = m.group(2)
    if re.search(rf"^{re.escape(key)}\s*=", blk, re.M):
        blk = re.sub(rf"^{re.escape(key)}\s*=.*$", f"{key} = {val}", blk, flags=re.M)
    else:
        blk = blk + f"{key} = {val}\n"
    return s[:m.start(2)] + blk + s[m.end(2):]

def set_section(s, name, body):
    pat = re.compile(r"\[" + re.escape(name) + r"\]\n(?:.*?\n)*?(?=\n\[|\Z)")
    block = f"[{name}]\n{body}\n"
    return pat.sub(block, s, count=1) if ("[" + name + "]") in s else s.rstrip() + f"\n\n{block}"

for sec, k, v in [
    ("network", "admin_password", pw),
    ("network", "server_admin_port", port),
    ("network", "allow_insecure_admin_login", "true"),
    ("network", "server_name", name),
    ("network", "server_game_type", public),
    ("network", "max_companies", max_co),
    ("network", "max_clients", max_cl),
    ("network", "autoclean_companies", "false"),
    ("ai", "ai_in_multiplayer", "true"),
    ("difficulty", "max_no_competitors", max_co),
]:
    s = set_kv(s, sec, k, v)
# QUOTED names — OpenTTD truncates unquoted slot names at the first space.
s = set_section(s, "ai_players", '"Nutz Executor" = \nnone = \nnone = \nnone = ')
s = set_section(s, "game_scripts", '"Nutz Bridge: arena" = ')
open(cfg, "w").write(s)
print("config written")
PY

# 2. Launch the dedicated server (admin port binds inside the container only).
say "Starting OpenTTD dedicated server ($SERVER_NAME)…"
openttd -D -g -c "$CFG" > "$LOG" 2>&1 &
OTTD_PID=$!

# wait for the admin port
ADMIN_UP=0
for i in $(seq 1 30); do
  if python3 -c "import socket;socket.create_connection(('127.0.0.1',$ADMIN_PORT),2).close()" 2>/dev/null; then ADMIN_UP=1; break; fi
  sleep 1
done

# If the server never came up, die here. Carrying on leaves a container that
# `docker ps` reports as healthy while nothing is listening - which is how a
# missing shared library stayed invisible: the entrypoint printed "Starting
# OpenTTD..." and openttd had already exited.
if [ "$ADMIN_UP" != "1" ]; then
  echo "FATAL: OpenTTD never opened admin port $ADMIN_PORT. Server log:" >&2
  tail -40 "$LOG" >&2 || true
  exit 1
fi

# 3. Start the Nutz Executor AI over the admin port (slot alone won't spawn it).
say "Starting the Nutz Executor AI…"
python3 - "$ADMIN_PORT" "$ADMIN_PW" <<'PY' || echo "! AI auto-start failed"
import sys, time
sys.path.insert(0, "/app/agent")
from admin_client import OpenTTDAdminClient as C
c = C(password=sys.argv[2], port=int(sys.argv[1])); c.connect()
c.rcon('start_ai "Nutz Executor"'); time.sleep(3); c._poll(2); time.sleep(1)
print("  companies:", {k: v.get("name") for k, v in getattr(c, "companies", {}).items()})
c.close()
PY

# 4. Optional MCP bridge + dashboard.
if [ "${START_MCP:-1}" = "1" ]; then
  say "Starting MCP bridge on :8990…"
  ( cd /app && python3 -m agent.sandbox.mcp_http --port 8990 > /tmp/mcp.log 2>&1 & ) || true
fi
if [ "${START_DASHBOARD:-1}" = "1" ]; then
  say "Starting dashboard on :8080…"
  ( cd /app && ADMIN_PW="$ADMIN_PW" python3 -m agent.sandbox.dashboard --port 8080 > /tmp/dash.log 2>&1 & ) || true
fi

say "Arena is live. game :$GAME_PORT · dashboard :8080 · mcp :8990"
[ "$PUBLIC" != "0" ] && grep -i "invite code" "$LOG" || true

# Keep the container alive tied to the server; stream its log.
wait "$OTTD_PID"
