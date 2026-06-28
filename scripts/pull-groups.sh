#!/usr/bin/env bash
# One-time (re-run only if your groups change): fetch the groups your linked
# number belongs to and write them to groups.txt. Uses the same engine the
# app does, so the result is identical to the GUI's "Refresh groups".
set -euo pipefail
cd "$(dirname "$0")/.."

command -v signal-cli >/dev/null || { echo "Install signal-cli first (run Setup)." >&2; exit 1; }

python3 - <<'PY'
import sys
import engine
try:
    account = engine.detect_account() or engine.load_config().account
    count = engine.sync_groups(account, on_log=lambda m: print(m, file=sys.stderr))
except engine.BroadcastError as exc:
    sys.exit(str(exc))
print(f"Wrote groups.txt with {count} groups.", file=sys.stderr)
PY
