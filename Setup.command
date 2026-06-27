#!/usr/bin/env bash
# Double-click ONCE to install everything the app needs, then open it.
# Safe to run again later — it skips anything already installed.
set -uo pipefail
cd "$(dirname "$0")"

echo "=== Signal Broadcast — setup ==="
echo

# If this folder arrived as a downloaded .zip it may be quarantined; clear it.
# (Folders from 'git clone' are not quarantined, so this is usually a no-op.)
xattr -dr com.apple.quarantine . 2>/dev/null || true

# Your settings live in config.toml (gitignored); seed it from the template once.
[ -f config.toml ] || cp config.example.toml config.toml

# 1. Homebrew (the macOS package installer)
if ! command -v brew >/dev/null 2>&1; then
  echo "Installing Homebrew — you may be asked for your Mac password…"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi
# Put brew on PATH for this session (Apple Silicon, then Intel).
if [ -x /opt/homebrew/bin/brew ]; then eval "$(/opt/homebrew/bin/brew shellenv)"
elif [ -x /usr/local/bin/brew ]; then eval "$(/usr/local/bin/brew shellenv)"; fi

# 2. The actual requirements: signal-cli (Signal), qrencode (the QR), python-tk (the window)
echo
echo "Installing signal-cli, qrencode, and python-tk (this can take a few minutes)…"
brew install signal-cli qrencode python-tk

# 3. Find a Python that can run the app (needs tkinter + tomllib).
find_python() {
  for cand in \
    "$(command -v python3 || true)" \
    /opt/homebrew/bin/python3 /usr/local/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/3.11/bin/python3; do
    [ -n "$cand" ] && [ -x "$cand" ] || continue
    if "$cand" -c "import tkinter, tomllib" >/dev/null 2>&1; then echo "$cand"; return 0; fi
  done
  return 1
}

PY="$(find_python || true)"
if [ -z "$PY" ]; then
  echo
  echo "Setup installed the tools but couldn't find a working Python with Tk."
  echo "Install Python from https://www.python.org/downloads/ and run Setup again."
  read -r -p "Press Return to close…" _
  exit 1
fi

echo
echo "Building the Dock app…"
bash scripts/make-dock-app.sh "$PY" \
  || echo "(Couldn't build the Dock app — you can still open 'Signal Broadcast.command'.)"

echo
echo "All set."
echo "• To keep it handy: open this folder in Finder and drag 'Signal Broadcast.app' onto your Dock."
echo "• Opening the app now — scan the QR code with your phone to link it."
exec "$PY" gui.py
