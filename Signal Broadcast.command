#!/usr/bin/env bash
# Double-click to open the Signal Broadcast app.
set -uo pipefail
cd "$(dirname "$0")"

# Find a Python that has BOTH tkinter (the window) and tomllib (config.toml, 3.11+).
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
  echo "The app's requirements aren't installed yet."
  echo "Double-click 'Setup.command' first, then try again."
  osascript -e 'display alert "Setup needed" message "Double-click Setup.command first."' >/dev/null 2>&1 || true
  read -r -p "Press Return to close…" _
  exit 1
fi

exec "$PY" gui.py
