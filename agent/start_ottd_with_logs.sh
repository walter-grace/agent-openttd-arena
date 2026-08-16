#!/bin/bash
# Launch OpenTTD as a DEDICATED SERVER with a stdin FIFO so commands
# (pause, etc.) can be injected from Python via:
#     echo "pause" > /tmp/ottd_cmd
#
# `-D` is the only mode where stdout works on macOS .app bundles
# (Cocoa GUI hijacks stdout in normal mode). The dedicated server has
# admin port + game port; you connect a regular OpenTTD GUI client to
# 127.0.0.1 to view/play.
#
# Usage:
#     ./agent/start_ottd_with_logs.sh [scenario_name]
#
# `scenario_name` is whatever you passed to build_scenario.py — it looks
# for <name>_heightmap.png. Override the config/personal dir (and thus
# which openttd.cfg, ai/, and game/ dirs are used) with OTTD_DIR.

SCENARIO="${1:-${OTTD_SCENARIO:-la}}"
OTTD_DIR="${OTTD_DIR:-$HOME/Documents/OpenTTD}"
OTTD_BIN="${OTTD_BIN:-/Applications/OpenTTD.app/Contents/MacOS/openttd}"

LOG="${OTTD_LOG:-/tmp/ottd_stdout.log}"
CMD_FIFO="${OTTD_FIFO:-/tmp/ottd_cmd}"
HEIGHTMAP="$OTTD_DIR/scenario/heightmap/${SCENARIO}_heightmap.png"

if [ ! -f "$HEIGHTMAP" ]; then
    echo "ERROR: heightmap not found: $HEIGHTMAP"
    echo
    echo "Build one first, from any Google Maps URL:"
    echo "  python3 -m agent.sandbox.build_scenario \\"
    echo "      \"https://www.google.com/maps/@34.05,-118.24,9z\" $SCENARIO --size 2048"
    exit 1
fi

# Recreate the FIFO so previous holders are flushed.
rm -f "$CMD_FIFO"
mkfifo "$CMD_FIFO"

# Keep the writer side of the fifo open for the entire run, otherwise
# OpenTTD reads EOF the first time the fifo is empty and stops accepting
# commands. `tail -f /dev/null` is a quiet long-lived writer.
tail -f /dev/null > "$CMD_FIFO" &
FIFO_HOLDER=$!

trap "kill $FIFO_HOLDER 2>/dev/null; rm -f $CMD_FIFO" EXIT

echo "" > "$LOG"
echo "starting OpenTTD as DEDICATED SERVER"
echo "  scenario  : $SCENARIO"
echo "  heightmap : $HEIGHTMAP"
echo "  config dir: $OTTD_DIR"
echo "  log       : $LOG"
echo "  cmd fifo  : $CMD_FIFO  (echo 'pause' > $CMD_FIFO to unpause)"
echo "  game port : 3979 (join 127.0.0.1 in GUI client)"
echo "  admin port: 3977"
echo ""

"$OTTD_BIN" \
    -D \
    -c "$OTTD_DIR/openttd.cfg" \
    -d script=2,net=1 \
    -g "$HEIGHTMAP" \
    >> "$LOG" 2>&1 < "$CMD_FIFO"
