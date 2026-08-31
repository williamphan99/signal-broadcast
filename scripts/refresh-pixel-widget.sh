#!/usr/bin/env bash
# Refresh the Termux:Widget launcher from inside the proot guest. Android exposes the
# Termux home directory at this stable path; if that path is unavailable, setup remains
# usable and the command-line launcher still works.
set -uo pipefail

DISTRO="${1:-debian}"
SHORTCUT_DIR="/data/data/com.termux/files/home/.shortcuts"
SHORTCUT="$SHORTCUT_DIR/Signal Broadcast"

if [ ! -d "$SHORTCUT_DIR" ] || [ ! -w "$SHORTCUT_DIR" ]; then
  exit 0
fi

cat > "$SHORTCUT" <<SH
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
if curl -fsS -o /dev/null http://127.0.0.1:8787 2>/dev/null; then
  termux-open-url http://127.0.0.1:8787
else
  proot-distro login $DISTRO -- sh -lc 'cd \$HOME/signal-broadcast && python3 webui.py' &
  server_pid=\$!
  tries=0
  until curl -fsS -o /dev/null http://127.0.0.1:8787 2>/dev/null; do
    if ! kill -0 "\$server_pid" 2>/dev/null; then
      wait "\$server_pid"
      exit \$?
    fi
    tries=\$((tries + 1))
    if [ "\$tries" -ge 120 ]; then
      echo "Signal Broadcast did not become ready. Open Termux to see the error." >&2
      wait "\$server_pid"
      exit \$?
    fi
    sleep 1
  done
  termux-open-url http://127.0.0.1:8787
  wait "\$server_pid"
fi
SH
chmod +x "$SHORTCUT"
