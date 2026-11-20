#!/usr/bin/env bash
# Finish Sudoku.app around this checkout, which Setup keeps at
# Sudoku.app/Contents/Resources/app: the native launcher, Info.plist and icon.
# Usage: make-dock-app.sh [PYTHON]   (Setup passes the app's virtual-environment Python)
set -uo pipefail
cd "$(dirname "$0")/.."
PROJECT="$PWD"
CONTENTS="$(cd "$PROJECT/../.." && pwd)"
case "$CONTENTS" in
  *.app/Contents) ;;
  *) echo "Run Setup.command: the app is built inside ~/Applications/Sudoku.app." >&2; exit 1 ;;
esac
BUNDLE="$(dirname "$CONTENTS")"
PY="${1:-$PROJECT/.venv/bin/python}"
mkdir -p "$CONTENTS/MacOS" "$CONTENTS/Resources"

# Icon: shipped as a PNG, packed into an .icns here. A missing icon is harmless.
ICON_KEY=""
ICONSET="$(mktemp -d)/Sudoku.iconset"
mkdir -p "$ICONSET"
for size in 16 32 128 256 512; do
  sips -z $size $size assets/sudoku-icon.png --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
  sips -z $((size * 2)) $((size * 2)) assets/sudoku-icon.png --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
if iconutil -c icns "$ICONSET" -o "$CONTENTS/Resources/Sudoku.icns"; then
  ICON_KEY="<key>CFBundleIconFile</key><string>Sudoku</string>"
fi
rm -rf "$(dirname "$ICONSET")"

cat > "$CONTENTS/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Sudoku</string>
  <key>CFBundleDisplayName</key><string>Sudoku</string>
  <key>CFBundleIdentifier</key><string>local.sudoku</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>Sudoku</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSApplicationCategoryType</key><string>public.app-category.puzzle-games</string>
  $ICON_KEY
</dict></plist>
PLIST

# The native launcher runs Python inside this app's own process, so the Dock, menu
# bar and Activity Monitor say "Sudoku". It links against the Python the virtual
# environment was made from. If it cannot be built, a script launcher still works.
BASE="$("$PY" -c 'import sys; print(sys._base_executable)' 2>/dev/null || true)"
CONFIG="${BASE}-config"
[ -x "$CONFIG" ] || CONFIG="$(dirname "$BASE")/python3-config"
if [ -x "$CONFIG" ] && clang scripts/sudoku-launcher.c -o "$CONTENTS/MacOS/Sudoku.new" \
     $("$CONFIG" --cflags --embed) $("$CONFIG" --ldflags --embed) 2>/dev/null; then
  mv -f "$CONTENTS/MacOS/Sudoku.new" "$CONTENTS/MacOS/Sudoku"
else
  rm -f "$CONTENTS/MacOS/Sudoku.new"
  echo "(Using the script launcher; the Dock may show Python while the app runs.)"
  cat > "$CONTENTS/MacOS/Sudoku" <<'LAUNCH'
#!/bin/bash
cd "$(dirname "$0")/../Resources/app" || exit 1
[ -x .venv/bin/python ] && exec .venv/bin/python mac_app.py "$@"
osascript -e 'display alert "Sudoku" message "Run Setup.command inside the app again to finish installing."'
exit 1
LAUNCH
  chmod +x "$CONTENTS/MacOS/Sudoku"
fi
# Tell Launch Services about the new name and icon.
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$BUNDLE" 2>/dev/null || true
echo "Built $BUNDLE"
