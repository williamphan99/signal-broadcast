#!/usr/bin/env bash
# Build "Signal Broadcast.app" — a Dock-pinnable launcher that opens the app with
# no Terminal window. Setup.command runs this automatically; you can also run it
# by hand to rebuild:  bash scripts/make-dock-app.sh [/path/to/python3]
set -uo pipefail
cd "$(dirname "$0")/.."          # project root, whatever the working directory was
PROJECT="$PWD"
PY="${1:-}"                      # preferred python (Setup passes the one it found)

APP="Signal Broadcast.app"
MACOS="$APP/Contents/MacOS"
rm -rf "$APP"
mkdir -p "$MACOS"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Signal Broadcast</string>
  <key>CFBundleDisplayName</key><string>Signal Broadcast</string>
  <key>CFBundleIdentifier</key><string>com.user.signal-broadcast.launcher</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>launcher</string>
</dict></plist>
PLIST

# The launcher cd's into the project and execs the GUI under a Python that has both
# tkinter and tomllib. The project path is baked in, so don't move the folder after
# setup — if you do, just re-run Setup. Tries the Setup-found python first, then the
# usual python.org / Homebrew locations, and shows a plain alert if none work.
cat > "$MACOS/launcher" <<LAUNCH
#!/bin/bash
cd "$PROJECT" || exit 1
for PY in "$PY" /usr/local/bin/python3 /opt/homebrew/bin/python3 \\
  /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \\
  /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \\
  /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \\
  /Library/Frameworks/Python.framework/Versions/3.11/bin/python3; do
  [ -n "\$PY" ] && [ -x "\$PY" ] || continue
  if "\$PY" -c 'import tkinter, tomllib' >/dev/null 2>&1; then
    exec "\$PY" "$PROJECT/gui.py"
  fi
done
osascript -e 'display alert "Signal Broadcast" message "Python is not set up yet. Open the project folder and double-click Setup.command, then try again."'
exit 1
LAUNCH
chmod +x "$MACOS/launcher"

echo "Built $APP"
