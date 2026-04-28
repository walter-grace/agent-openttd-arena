#!/bin/bash
# Launch OpenTTD as a DEDICATED SERVER with a stdin FIFO so commands
# (pause, etc.) can be injected from Python via:
#     echo "pause" > /tmp/ottd_cmd
#
# `-D` is the only mode where stdout works on macOS .app bundles
# (Cocoa GUI hijacks stdout in normal mode). The dedicated server has
# admin port + game port; you connect a regular OpenTTD GUI client to
# 127.0.0.1 to view/play.

LOG=/tmp/ottd_stdout.log
CMD_FIFO=/tmp/ottd_cmd
HEIGHTMAP="$HOME/Documents/OpenTTD/scenario/heightmap/LA_heightmap.png"

if [ ! -f "$HEIGHTMAP" ]; then
    echo "ERROR: heightmap not found: $HEIGHTMAP"
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
echo "  heightmap : $HEIGHTMAP"
echo "  log       : $LOG"
echo "  cmd fifo  : $CMD_FIFO  (echo 'pause' > $CMD_FIFO to unpause)"
echo "  game port : 3979 (join 127.0.0.1 in GUI client)"
echo "  admin port: 3977"
echo ""

/Applications/OpenTTD.app/Contents/MacOS/openttd \
    -D \
    -d script=2,net=1 \
    -g "$HEIGHTMAP" \
    >> "$LOG" 2>&1 < "$CMD_FIFO"
