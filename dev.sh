#!/usr/bin/env bash
# One command to preview the Pixel app on your computer — the "pnpm dev" of this project.
#
#   bash dev.sh            # build once (a few min), then open the app in your browser
#   bash dev.sh rebuild    # force a rebuild after you change the code
#
# It runs the SAME environment a Pixel uses (aarch64 Debian + Java 25 + signal-cli 0.14.x), so
# what you see is what ships. Link keys persist in a Docker volume between runs, so you
# only link once. Requires Docker (Apple Silicon runs it natively; Intel emulates).
set -uo pipefail
cd "$(dirname "$0")"

command -v docker >/dev/null || { echo "Install Docker Desktop first: https://docker.com" >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "Docker isn't running — open Docker Desktop, then retry." >&2; exit 1; }

IMAGE="signal-broadcast-sandbox"
NAME="signal-broadcast-dev"
PORT="${PORT:-8787}"

if [ "${1:-}" = "rebuild" ] || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "▸ Building the preview image (one-time, ~3–5 min)…"
  docker build --platform linux/arm64 -f scripts/sandbox.Dockerfile -t "$IMAGE" . \
    || { echo "Build failed." >&2; exit 1; }
fi

docker rm -f "$NAME" >/dev/null 2>&1 || true
echo "▸ Starting the app… (your Signal link persists in the 'sbdata' volume)"
docker run -d --name "$NAME" --platform linux/arm64 \
  -p "127.0.0.1:${PORT}:8787" \
  -v sbdata:/root/signal-broadcast/signal-cli-data \
  "$IMAGE" >/dev/null || { echo "Failed to start container." >&2; exit 1; }

printf "▸ Waiting for the app"
for _ in $(seq 1 60); do
  if curl -fsS -o /dev/null "http://127.0.0.1:${PORT}/" 2>/dev/null; then break; fi
  printf "."; sleep 1
done
URL="http://127.0.0.1:${PORT}"
echo " → ${URL}"
(open "$URL" || xdg-open "$URL") >/dev/null 2>&1 || echo "Open ${URL} in your browser."

echo "▸ App is running. First screen is 'Link' — scan/tap to link your Signal, then use it."
echo "  (Press Ctrl-C to stop.)"
trap 'echo; echo "▸ Stopping…"; docker rm -f "$NAME" >/dev/null 2>&1; exit 0' INT TERM
docker logs -f "$NAME"
