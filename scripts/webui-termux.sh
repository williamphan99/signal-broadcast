#!/usr/bin/env bash
# Start the mobile web UI. Run INSIDE the proot-distro guest:
#   bash scripts/webui-termux.sh
# Then open http://127.0.0.1:8787 in the phone's browser.
#
# For a one-tap home-screen launcher (Termux:Widget) that also holds a wake lock and
# opens the browser for you, see the "Daily use" section of PIXEL-SETUP.md.
set -uo pipefail
cd "$(dirname "$0")/.."
PORT="${SB_WEBUI_PORT:-8787}"
echo "Signal Broadcast web UI → http://127.0.0.1:${PORT}"
echo "(Leave this running. Ctrl-C to stop. Keep the phone awake during a send.)"
exec python3 webui.py
