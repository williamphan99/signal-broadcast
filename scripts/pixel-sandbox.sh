#!/usr/bin/env bash
# Test the Android/Pixel port WITHOUT a phone. Spins up an interactive aarch64 Debian
# container that mirrors the proot-distro Debian guest a Pixel runs — same CPU arch
# (aarch64), same glibc, same Java 25 + signal-cli 0.14.x + scripts. You walk through the
# exact guest steps and watch them work.
#
# Requires: Docker (on Apple Silicon it runs arm64 Linux natively; on Intel it emulates,
# which is slower but works). Run from the repo root:  bash scripts/pixel-sandbox.sh
#
# What this CAN show you: setup installing everything, signal-cli launching, the real
# linking QR rendering, group sync, and dry-run/real sends.
# What it CANNOT show (needs a real Pixel): Android Doze/battery behaviour, Termux:Boot,
# and the on-phone Signal deep-link. Those are in PIXEL-SETUP.md's device checklist.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$PWD"
IMAGE="${IMAGE:-debian:trixie-slim}"

command -v docker >/dev/null || { echo "Docker isn't installed. Install Docker Desktop first." >&2; exit 1; }

# PERSIST=1 keeps link keys + config between runs (in .sandbox-data/), so you can link
# once and reuse it. Default is ephemeral (everything vanishes when you exit).
PERSIST="${PERSIST:-0}"
VOLARGS=()
if [ "$PERSIST" = "1" ]; then
  mkdir -p "$REPO/.sandbox-data/signal-cli-data" "$REPO/.sandbox-data/state"
  VOLARGS=(-v "$REPO/.sandbox-data/signal-cli-data:/root/signal-broadcast/signal-cli-data"
           -v "$REPO/.sandbox-data/state:/root/state")
  echo "PERSIST=1: link keys will be kept in .sandbox-data/ between runs."
fi

echo "Starting Pixel-like sandbox ($IMAGE, arm64). First run pulls the image + installs deps."
# Publish the web-UI port to the Mac's localhost so you can preview the app's GUI in your
# desktop browser (inside the container the server binds 0.0.0.0 so the map can reach it).
exec docker run -it --rm --platform linux/arm64 \
  -e DEBIAN_FRONTEND=noninteractive -e SB_WEBUI_HOST=0.0.0.0 \
  -p 127.0.0.1:8787:8787 \
  -v "$REPO:/src:ro" ${VOLARGS[@]+"${VOLARGS[@]}"} \
  "$IMAGE" bash -lc '
    set -e
    mkdir -p ~/signal-broadcast
    # Copy the working tree in, EXCLUDING your real config + link keys (kept off the host).
    tar -C /src \
      --exclude=./.git --exclude=./signal-cli-data --exclude=./logs --exclude=./vendor \
      --exclude=./config.toml --exclude=./groups.txt --exclude=./message.txt \
      --exclude=./attachments.txt --exclude="./Signal Broadcast.app" \
      --exclude=./__pycache__ --exclude=./.sandbox-data -cf - . \
      | tar -C ~/signal-broadcast -xf -
    cd ~/signal-broadcast
    cat <<BANNER

  ===================================================================
   Pixel-like sandbox — aarch64 $(. /etc/os-release; echo "$PRETTY_NAME")
   This mirrors the proot-distro Debian guest on a Pixel.

   Walk through it (same as PIXEL-SETUP.md, minus the Termux layer):

     bash scripts/setup-termux.sh          # Java 25 + signal-cli + native lib
     python3 -c "import engine; engine.save_account(input(\"number +…: \"))" \
                                           # or: nano config.toml  (apt install nano)
     bash scripts/link-termux.sh           # shows a REAL QR — scan with your phone
     bash scripts/pull-groups.sh           # after linking
     python3 broadcast.py --limit 2 --dry-run

   Want to PREVIEW THE APP GUI in your Mac browser?
     bash scripts/setup-termux.sh          # once, to install deps
     bash scripts/webui-termux.sh          # starts the web UI
     → then open  http://localhost:8787  in your Mac browser

   Just want to SEE it work? Run setup, then link-termux.sh and watch the QR
   render, then Ctrl-C. Nothing is sent.

   NOTE: linking here creates a REAL linked device on your Signal account.
   Remove it afterwards in Signal → Settings → Linked Devices. Do NOT run a
   full broadcast from here unless you mean it. Type exit to leave.
  ===================================================================

BANNER
    exec bash
  '
