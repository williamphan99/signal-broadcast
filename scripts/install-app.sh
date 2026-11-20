#!/usr/bin/env bash
# Copy this checkout into Sudoku.app as the app's own git clone, so the app runs
# from ~/Applications instead of a downloaded project folder. Updates keep working:
# the clone pulls from the same remote. Usage: install-app.sh SOURCE BUNDLE
set -euo pipefail
SRC="$(cd "$1" && pwd -P)"
BUNDLE="$2"
APP="$BUNDLE/Contents/Resources/app"
BRANCH="$(git -C "$SRC" rev-parse --abbrev-ref HEAD)"
ORIGIN="$(git -C "$SRC" remote get-url origin 2>/dev/null || true)"

mkdir -p "$BUNDLE/Contents/Resources"
if [ -d "$APP/.git" ]; then
  git -C "$APP" fetch --quiet "$SRC" "$BRANCH"
  git -C "$APP" checkout --quiet -B "$BRANCH" FETCH_HEAD
else
  git clone --quiet --no-hardlinks --branch "$BRANCH" "$SRC" "$APP"
fi
if [ -n "$ORIGIN" ]; then
  # Updates come from the project's own remote, not the folder being replaced.
  git -C "$APP" remote set-url origin "$ORIGIN"
  git -C "$APP" config "branch.$BRANCH.remote" origin
  git -C "$APP" config "branch.$BRANCH.merge" "refs/heads/$BRANCH"
fi
# Reuse the large signal-cli download instead of fetching it again.
for dir in "$SRC"/vendor/signal-cli-*; do
  [ -d "$dir" ] || continue
  if [ ! -e "$APP/vendor/$(basename "$dir")" ]; then
    mkdir -p "$APP/vendor"
    cp -R "$dir" "$APP/vendor/"
  fi
done
