#!/usr/bin/env bash
# Double-click ONCE to install everything the app needs, then open it.
# Safe to run again later — it skips anything already installed.
#
# The app is installed as ~/Applications/Sudoku.app. Run from a downloaded folder,
# Setup copies itself into the app and continues there; afterwards the downloaded
# folder is no longer needed. Everything stays on this Mac.
set -uo pipefail
cd "$(dirname "$0")"

BUNDLE="${SUDOKU_APP:-$HOME/Applications/Sudoku.app}"
APP_DIR="$BUNDLE/Contents/Resources/app"
MIGRATE_FROM=""
if [ "${1:-}" = "--migrate-from" ] && [ -n "${2:-}" ]; then MIGRATE_FROM="$2"; fi
if [ "$(pwd -P)" != "$(cd "$APP_DIR" 2>/dev/null && pwd -P)" ]; then
  echo "=== Installing Sudoku into $BUNDLE ==="
  if ! git diff --quiet HEAD 2>/dev/null; then
    echo "(Changes in this folder that are not committed are not copied.)"
  fi
  if ! bash scripts/install-app.sh "$PWD" "$BUNDLE"; then
    echo "Could not copy the app into $BUNDLE. Nothing else was changed."
    read -r -p "Press Return to close…" _
    exit 1
  fi
  exec bash "$APP_DIR/Setup.command" --migrate-from "$(pwd -P)"
fi

echo "=== Sudoku — setup ==="
echo

# If this folder arrived as a downloaded .zip it may be quarantined; clear it.
# (Folders from 'git clone' are not quarantined, so this is usually a no-op.)
xattr -dr com.apple.quarantine . 2>/dev/null || true

# Existing local settings are migrated only after password setup in the app.

# 1. Homebrew (the macOS package installer)
if ! command -v brew >/dev/null 2>&1; then
  echo "Installing Homebrew — you may be asked for your Mac password…"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi
# Put brew on PATH for this session (Apple Silicon, then Intel).
if [ -x /opt/homebrew/bin/brew ]; then eval "$(/opt/homebrew/bin/brew shellenv)"
elif [ -x /usr/local/bin/brew ]; then eval "$(/usr/local/bin/brew shellenv)"; fi

# 2. Requirements: Java 25 (runs signal-cli), qrencode (the QR), python-tk (the window).
#    We run the JVM build of signal-cli (downloaded in step 2b), NOT Homebrew's
#    native build — the native build crashes (StackOverflowError) when encrypting
#    for some groups. We still install the native one as a last-resort fallback.
echo
echo "Installing Java, signal-cli, qrencode, and python-tk (this can take a few minutes)…"
brew install openjdk@25 signal-cli qrencode python-tk

# 2b. Download the JVM build of signal-cli into ./vendor (version-pinned). The app
#     prefers this over the native build; see engine.py's signal_cli_bin().
SIGNAL_CLI_VERSION="0.14.7"
JVM_DIR="vendor/signal-cli-${SIGNAL_CLI_VERSION}"
JVM_CLI="${JVM_DIR}/bin/signal-cli"
# Check for libsignal-client, not just the launcher. It carries the native library
# signal-cli loads on startup for EVERY command, and it's the biggest jar in the
# tarball — so a truncated download or an interrupted extract loses it while still
# leaving bin/signal-cli in place. Guarding on the launcher alone made that state
# permanent: Setup saw an install and skipped, while every send died with
# NoClassDefFoundError. Re-download whenever the jar is missing.
if ! compgen -G "${JVM_DIR}/lib/libsignal-client*.jar" >/dev/null; then
  [ -e "$JVM_DIR" ] && echo "Existing signal-cli install is incomplete — reinstalling…"
  echo "Downloading signal-cli ${SIGNAL_CLI_VERSION} (JVM build, ~100 MB)…"
  mkdir -p vendor
  URL="https://github.com/AsamK/signal-cli/releases/download/v${SIGNAL_CLI_VERSION}/signal-cli-${SIGNAL_CLI_VERSION}.tar.gz"
  if curl -fSL --retry 3 --retry-delay 2 "$URL" -o vendor/signal-cli.tar.gz; then
    # Extract to a staging dir and only swap it in once the jar is confirmed present,
    # so a failure here leaves any previous working install untouched.
    rm -rf vendor/.staging && mkdir -p vendor/.staging
    if tar -xzf vendor/signal-cli.tar.gz -C vendor/.staging \
       && compgen -G "vendor/.staging/signal-cli-${SIGNAL_CLI_VERSION}/lib/libsignal-client*.jar" >/dev/null; then
      rm -rf "$JVM_DIR"
      mv "vendor/.staging/signal-cli-${SIGNAL_CLI_VERSION}" "$JVM_DIR"
      echo "Installed JVM signal-cli to $JVM_CLI"
    else
      echo "(Download was incomplete — the app will fall back to the native build.)"
    fi
    rm -rf vendor/.staging vendor/signal-cli.tar.gz
  else
    rm -f vendor/signal-cli.tar.gz
    echo "(Could not download the JVM build — the app will fall back to the native one.)"
  fi
fi

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

"$PY" notes_receiver.py "$JVM_DIR" || exit 1

echo
echo "Building the app…"
# Stop the background service (old and new names) so storage can be moved safely.
launchctl bootout "gui/$(id -u)/local.sudoku.helper" >/dev/null 2>&1 || true
launchctl bootout "gui/$(id -u)/com.user.signal-broadcast.service" >/dev/null 2>&1 || true
"$PY" -m venv .venv || exit 1
.venv/bin/python -m pip install --disable-pip-version-check -r requirements-macos.txt || exit 1
swiftc scripts/mac-security.swift -o vendor/mac-security || exit 1
swiftc scripts/mac-thumbnail.swift -o vendor/mac-thumbnail || exit 1

# Move an earlier installation to the neutral names. Safe to repeat; it stops
# without changing anything it is unsure about, and Setup stops with it.
MIGRATE_ARGS=()
[ -n "$MIGRATE_FROM" ] && MIGRATE_ARGS=(--from "$MIGRATE_FROM")
if ! .venv/bin/python mac_migrate.py ${MIGRATE_ARGS[@]+"${MIGRATE_ARGS[@]}"}; then
  echo "Fix the problem above, then run Setup again. Your data was not deleted."
  read -r -p "Press Return to close…" _
  exit 1
fi
.venv/bin/python scripts/install-mac-service.py || exit 1
PY="$PWD/.venv/bin/python"
bash scripts/make-dock-app.sh "$PY" || { echo "Could not build Sudoku.app."; exit 1; }

# FileVault encrypts the whole disk, including anything outside the app's library.
echo
if fdesetup isactive >/dev/null 2>&1; then
  echo "FileVault is on."
else
  echo "FileVault is OFF. Turn it on so this Mac's whole disk is encrypted, not just the app's library."
  echo "Opening System Settings → Privacy & Security → FileVault. Choose Turn On, and keep the"
  echo "recovery key somewhere safe and offline (not in iCloud) if you want nothing in the cloud."
  open "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?FileVault" 2>/dev/null \
    || open "/System/Library/PreferencePanes/Security.prefPane"
fi

echo
echo "All set. The app is installed as $BUNDLE."
echo "• Drag it from that folder to your Dock. If an old 'Signal Broadcast' icon is pinned, remove it."
echo "• When locked, the app looks like a Sudoku game. To unlock: Game → Enter Puzzle Code…"
echo "  (Command-K), type your password, then type it again on the password screen."
echo "  Three wrong codes or passwords in a row erase everything."
open -a "$BUNDLE" --args --setup

# The downloaded folder holds only source code now, and its name gives the app away.
if [ -n "$MIGRATE_FROM" ] && [ "$MIGRATE_FROM" != "$(pwd -P)" ] && [ -f "$MIGRATE_FROM/Setup.command" ] \
   && [ -d "$MIGRATE_FROM/.git" ]; then
  echo
  echo "The folder you installed from is no longer needed:"
  echo "  $MIGRATE_FROM"
  if [ -n "$(git -C "$MIGRATE_FROM" status --porcelain 2>/dev/null)" ]; then
    echo "It has files or changes that are not in the installed app, so it was kept. Delete it yourself when ready."
  else
    read -r -p "Delete it now? Your data is not in it. [y/N] " answer
    case "$answer" in
      y|Y|yes|YES) rm -rf "$MIGRATE_FROM" && echo "Deleted." ;;
      *) echo "Kept. You can delete it later." ;;
    esac
  fi
fi
