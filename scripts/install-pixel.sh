#!/usr/bin/env bash
# ONE-COMMAND installer for Signal Broadcast on a Google Pixel (Android).
#
# Run it in the Termux app (the HOST — not inside a Linux guest):
#   curl -fsSL https://raw.githubusercontent.com/williamphan99/signal-broadcast/main/scripts/install-pixel.sh | bash
#
# It installs the Debian guest, clones the app, sets up Java 25 + signal-cli + the native
# lib, and creates two home-screen icons (Termux:Widget):
#   • "Signal Broadcast"         — launch the app (opens the web UI in your browser)
#   • "Update Signal Broadcast"  — pull the latest code (like re-running Setup on the Mac)
# Re-running this is safe: it updates instead of reinstalling.
set -uo pipefail

DISTRO="${DISTRO:-debian}"
REPO_URL="${REPO_URL:-https://github.com/williamphan99/signal-broadcast.git}"

echo "=== Signal Broadcast — Pixel installer ==="

# Must be Termux (host), not a proot guest and not a desktop.
if [ -z "${PREFIX:-}" ] || [ ! -d "/data/data/com.termux" ]; then
  echo "ERROR: run this in the Termux app on your phone (the host)." >&2
  echo "If you're inside a Linux guest, type 'exit' first." >&2
  exit 1
fi

echo "Installing proot-distro, termux-api, curl…"
pkg install -y proot-distro termux-api curl

if ! proot-distro list --installed 2>/dev/null | grep -qw "$DISTRO"; then
  echo "Installing the $DISTRO Linux guest…"
  proot-distro install "$DISTRO"
fi

echo "Setting up the app inside $DISTRO (Java 25, signal-cli, native lib — a few minutes)…"
proot-distro login "$DISTRO" -- sh -lc "
  set -e
  apt-get update && apt-get install -y git
  if [ -d \$HOME/signal-broadcast/.git ]; then
    cd \$HOME/signal-broadcast && git pull --ff-only
  else
    git clone $REPO_URL \$HOME/signal-broadcast
  fi
  cd \$HOME/signal-broadcast && bash scripts/setup-termux.sh
"

# Home-screen icons for Termux:Widget (reads ~/.shortcuts/).
mkdir -p ~/.shortcuts
cat > ~/.shortcuts/"Signal Broadcast" <<SH
#!/data/data/com.termux/files/usr/bin/sh
# Launch: hold a wake lock, open the app in the browser, run the server (foreground so the
# proot session stays alive). If it's already running, just reopen the browser.
termux-wake-lock
if curl -fsS -o /dev/null http://127.0.0.1:8787 2>/dev/null; then
  termux-open-url http://127.0.0.1:8787
else
  (sleep 4; termux-open-url http://127.0.0.1:8787) &
  proot-distro login $DISTRO -- sh -lc 'cd \$HOME/signal-broadcast && python3 webui.py'
fi
SH
chmod +x ~/.shortcuts/"Signal Broadcast"

cat > ~/.shortcuts/"Update Signal Broadcast" <<SH
#!/data/data/com.termux/files/usr/bin/sh
proot-distro login $DISTRO -- sh -lc 'cd \$HOME/signal-broadcast && bash scripts/update-termux.sh'
echo
echo "Update finished. Tap 'Signal Broadcast' to start."
SH
chmod +x ~/.shortcuts/"Update Signal Broadcast"

cat <<EOF

=== Done! ===
Add the buttons to your home screen:
  long-press the home screen → Widgets → Termux:Widget → place it, then pick
    • "Signal Broadcast"          (launch the app)
    • "Update Signal Broadcast"   (get the latest code)

First run: tap "Signal Broadcast", then in the browser tap Start linking and scan the QR
with Signal (Settings → Linked devices → +). After that it's just: tap → type → Send.
EOF
