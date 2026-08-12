#!/bin/bash
# setup_arena.sh — one command to a running OpenTTD arena server.
#
#   ./agent/setup_arena.sh
#
# Encodes every gotcha discovered getting a headless arena live on macOS
# (see TROUBLESHOOTING.md). Idempotent: safe to re-run. Starts a dedicated
# server with the admin port enabled, the Nutz Executor AI loaded, and the
# bridge GameScript active — the full stack an agent needs.
#
# Env overrides:
#   OTTD_DIR      personal/config dir (default: ~/Documents/OpenTTD)
#   ADMIN_PW      admin-port password (default: nutzarena — matches admin_client.py)
#   ADMIN_PORT    default 3977
#   GAME_PORT     default 3979
#   SERVER_NAME   default "arena"
#   HEIGHTMAP     optional heightmap PNG for a real-world map (else random)
set -euo pipefail

OTTD_DIR="${OTTD_DIR:-$HOME/Documents/OpenTTD}"
ADMIN_PW="${ADMIN_PW:-nutzarena}"
ADMIN_PORT="${ADMIN_PORT:-3977}"
GAME_PORT="${GAME_PORT:-3979}"
SERVER_NAME="${SERVER_NAME:-arena}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OTTD_BIN="/Applications/OpenTTD.app/Contents/MacOS/openttd"
OPENGFX_VER="7.1"

say() { printf "\033[1;32m▸ %s\033[0m\n" "$*"; }
warn() { printf "\033[1;33m! %s\033[0m\n" "$*"; }

# 1. OpenTTD ---------------------------------------------------------------
if [ ! -x "$OTTD_BIN" ]; then
  if command -v brew >/dev/null; then
    say "Installing OpenTTD via brew…"
    brew install --cask openttd
  else
    warn "OpenTTD not found and no brew. Install from https://www.openttd.org then re-run."
    exit 1
  fi
fi
say "OpenTTD: $("$OTTD_BIN" --help 2>&1 | head -1)"

# 2. Base graphics (brew cask ships NONE — the #1 gotcha) ------------------
mkdir -p "$OTTD_DIR/baseset"
if ! ls "$OTTD_DIR"/baseset/*/*.obg >/dev/null 2>&1 && ! ls "$OTTD_DIR"/baseset/opengfx* >/dev/null 2>&1; then
  say "No base graphics — downloading OpenGFX $OPENGFX_VER…"
  TMP="$(mktemp -d)"
  curl -sL -o "$TMP/opengfx.zip" "https://cdn.openttd.org/opengfx-releases/$OPENGFX_VER/opengfx-$OPENGFX_VER-all.zip"
  unzip -oq "$TMP/opengfx.zip" -d "$TMP"
  tar -xf "$TMP"/opengfx-*.tar -C "$OTTD_DIR/baseset/"
  rm -rf "$TMP"
  say "OpenGFX installed."
else
  say "Base graphics present."
fi

# 3. Content: Nutz Executor AI + bridge GameScript ------------------------
mkdir -p "$OTTD_DIR/ai" "$OTTD_DIR/game"
cp -R "$REPO_DIR/ottd_user/ai/nutz_executor" "$OTTD_DIR/ai/"
say "Nutz Executor AI installed."
# Generate the bridge GS (bare — no towns; use build_scenario.py for a
# real-world map + towns).
python3 - "$OTTD_DIR/game" "$REPO_DIR" <<'PY'
import sys
from pathlib import Path
out_dir, repo = sys.argv[1], sys.argv[2]
sys.path.insert(0, str(Path(repo) / "agent" / "sandbox"))
import bridge_gs
meta = bridge_gs.generate_bridge_with_towns([], "arena", out_dir=Path(out_dir))
print("bridge GS:", meta["path"])
PY

# 4. Config — admin port + insecure login + AI slot + GS slot -------------
# OpenTTD 15.x DISABLES password admin login by default; admin_client.py
# uses it, so allow_insecure_admin_login MUST be true. AIs/GS load from
# slot lines ("none" = empty; replace with the script's GetName()).
CFG="$OTTD_DIR/openttd.cfg"
say "Writing config with admin port + AI + GameScript…"
python3 - "$CFG" "$ADMIN_PW" "$ADMIN_PORT" "$SERVER_NAME" <<'PY'
import sys, re, os
cfg, pw, port, name = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
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

for sec, k, v in [
    ("network", "admin_password", pw),
    ("network", "server_admin_port", port),
    ("network", "allow_insecure_admin_login", "true"),
    ("network", "server_name", name),
    ("network", "server_game_type", "0"),      # 0=LAN/local, 1=public
    ("network", "autoclean_companies", "false"),
    ("ai", "ai_in_multiplayer", "true"),
    ("difficulty", "max_no_competitors", "4"),
]:
    s = set_kv(s, sec, k, v)

# AI + GS slots. CRITICAL: OpenTTD's config parser truncates the script
# name at the first SPACE ("Nutz Executor" -> "Nutz" -> "not found"), so the
# name MUST be quoted. Replace the whole section body to stay idempotent
# across OpenTTD's own config rewrites (it resets unknown slots to "none").
def set_section(s, name, body):
    pat = re.compile(r"\[" + re.escape(name) + r"\]\n(?:.*?\n)*?(?=\n\[|\Z)")
    block = f"[{name}]\n{body}\n"
    return pat.sub(block, s, count=1) if ("[" + name + "]") in s else s.rstrip() + f"\n\n{block}"

s = set_section(s, "ai_players", '"Nutz Executor" = \nnone = \nnone = \nnone = ')
s = set_section(s, "game_scripts", '"Nutz Bridge: arena" = ')

open(cfg, "w").write(s)
print("config written:", cfg)
PY

# 5. Launch dedicated server ----------------------------------------------
# Stop any dedicated server we started earlier so ports don't conflict.
pkill -f "OpenTTD.app.*openttd.*-D" 2>/dev/null && { warn "stopped a running dedicated server"; sleep 2; } || true
FIFO=/tmp/ottd_cmd; LOG=/tmp/ottd_stdout.log
# Keep the FIFO open for console input. Redirect the keeper's streams to
# /dev/null so it never holds this script's stdout/stderr open (else a caller
# that pipes our output hangs waiting for EOF).
rm -f "$FIFO"; mkfifo "$FIFO"; ( tail -f /dev/null > "$FIFO" 2>/dev/null </dev/null & )
GARG=(-g); [ -n "${HEIGHTMAP:-}" ] && GARG=(-g "$HEIGHTMAP")
say "Starting dedicated server…"
( "$OTTD_BIN" -D "${GARG[@]}" -c "$CFG" > "$LOG" 2>&1 < "$FIFO" & )
sleep 8

# Start the AI via the admin port. The [ai_players] slot alone doesn't spawn
# an AI on a dedicated -g game, so kick it explicitly (rcon, reliable).
say "Starting the Nutz Executor AI…"
python3 - "$ADMIN_PORT" "$ADMIN_PW" "$REPO_DIR/agent" <<'PY' || warn "could not auto-start AI — run: (admin) start_ai \"Nutz Executor\""
import sys, time
sys.path.insert(0, sys.argv[3])
from admin_client import OpenTTDAdminClient as C
port, pw = int(sys.argv[1]), sys.argv[2]
c = C(password=pw, port=port); c.connect()
c.rcon('start_ai "Nutz Executor"')
time.sleep(4); c._poll(2); time.sleep(2)
comps = getattr(c, "companies", {})
print("  AI company:", comps if comps else "(none yet — it may take a game-month)")
c.close()
PY

# 6. Web dashboard — view the game in any browser (no native client) --------
DASH_PORT="${DASH_PORT:-8080}"
if [ "${START_DASHBOARD:-1}" = "1" ]; then
  pkill -f "sandbox.dashboard" 2>/dev/null || true
  say "Starting web dashboard…"
  ( cd "$REPO_DIR" && ADMIN_PW="$ADMIN_PW" python3 -m agent.sandbox.dashboard \
      --admin-port "$ADMIN_PORT" --port "$DASH_PORT" > /tmp/arena_dashboard.log 2>&1 & )
  sleep 2
fi

cat <<EOF

$(say "Arena server is up.")
  dashboard   : http://localhost:$DASH_PORT      ← open this to watch the game
  server name : $SERVER_NAME   (find it in OpenTTD → Multiplayer → Search LAN)
  game port   : 127.0.0.1:$GAME_PORT   (join here to watch/play natively)
  admin port  : 127.0.0.1:$ADMIN_PORT  (password: $ADMIN_PW — the MCP layer uses this)
  console     : echo "<cmd>" > $FIFO    (e.g.  echo "companies" > $FIFO)
  logs        : tail -f $LOG   /tmp/arena_dashboard.log

Next:
  • Watch it:     open http://localhost:$DASH_PORT   (browser + Kitesurf; JSON at /state.json)
  • Play it:      open -a OpenTTD  → Multiplayer → join 127.0.0.1
  • Drive it:     python3 -m agent.sandbox.mcp_server        (stdio, local agent)
  • Serve remote: python3 -m agent.sandbox.mcp_http --port 8990   (HTTP, for distant agents)
  • Autoplay:     python3 -u -m agent.sandbox.conductor --interval 30 --intra-town
                  (dispatches blueprints the Nutz Executor builds — this is what
                   makes agents actually build)
EOF
