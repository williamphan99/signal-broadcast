#!/usr/bin/env bash
# One-time: link this laptop to your phone as a SECONDARY Signal device.
# This does NOT register or move your number — your phone stays the primary,
# exactly like linking Signal Desktop.
set -euo pipefail
cd "$(dirname "$0")/.."

command -v signal-cli >/dev/null || { echo "Install signal-cli first: brew install signal-cli" >&2; exit 1; }
command -v qrencode   >/dev/null || { echo "Install qrencode first: brew install qrencode" >&2; exit 1; }

DATA_DIR="$PWD/signal-cli-data"
mkdir -p "$DATA_DIR"

echo "A QR code will appear below."
echo "On your phone: Signal > Settings > Linked Devices > '+' > scan it."
echo "Keep this window open until it confirms the link."
echo

# 'link' prints an sgnl://linkdevice URI, then blocks until the phone scans it.
# Stream the output: render the URI as a QR, pass everything else through.
signal-cli --config "$DATA_DIR" link -n "broadcast-laptop" | while IFS= read -r line; do
  case "$line" in
    sgnl://linkdevice*|tsdevice:*)
      qrencode -t ANSIUTF8 "$line"
      echo "(If the QR won't scan, here is the raw link to paste:)"
      echo "$line"
      echo
      ;;
    *) echo "$line" ;;
  esac
done

echo
echo "Linking finished. If it succeeded, your phone now lists 'broadcast-laptop'."
echo "Next: sync + pull your groups (see README step 3)."
