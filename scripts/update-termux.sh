#!/usr/bin/env bash
# Update Signal Broadcast to the latest code. Run INSIDE the proot-distro guest:
#   bash scripts/update-termux.sh
#
# It pulls the new code, installs any new dependencies (setup is idempotent — it skips
# whatever's already there), and stops the running web UI so the next launch loads the
# new code. Your Signal link + settings (signal-cli-data/, config.toml, groups.txt,
# message.txt) are gitignored, so they're preserved — you never re-link or re-configure.
#
# One-tap version: a Termux:Widget host script can run this via
#   proot-distro login debian -- sh -lc 'cd ~/signal-broadcast && bash scripts/update-termux.sh'
set -uo pipefail
cd "$(dirname "$0")/.."

echo "=== Updating Signal Broadcast ==="

# 1. Pull the latest code. Tracked files only — your gitignored link/config/groups are
#    untouched. --ff-only keeps it safe: it refuses rather than creating a merge mess.
echo "Pulling latest code…"
if ! git pull --ff-only; then
  echo "ERROR: git pull failed (local edits to tracked files, or diverged history)." >&2
  echo "Fix those, then re-run. Your link/config are safe (they're gitignored)." >&2
  exit 1
fi

# 2. Install any new dependencies. Idempotent: existing Java 25 / signal-cli / native lib
#    are skipped; only a changed version is fetched.
echo "Checking dependencies…"
bash scripts/setup-termux.sh

# 3. Stop a running web UI so the next launch picks up the new code.
if pgrep -f "python3 webui.py" >/dev/null 2>&1; then
  echo "Stopping the running web UI (restart it to load the new code)…"
  pkill -f "python3 webui.py" || true
fi

echo
echo "Updated. Start the app again with:  bash scripts/webui-termux.sh"
echo "(or just tap the Signal Broadcast home-screen icon)"
