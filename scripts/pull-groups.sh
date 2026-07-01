#!/usr/bin/env bash
# One-time (re-run only if your groups change): fetch the groups your linked
# number belongs to and write them to groups.txt. Uses the same engine the
# app does, so the result is identical to the GUI's "Refresh groups".
set -euo pipefail
cd "$(dirname "$0")/.."

# Don't hard-check `signal-cli` on PATH: on Android it's the vendored JVM build under
# vendor/ (found by engine.signal_cli_bin(), not on PATH). The engine raises a clear
# BroadcastError if it's genuinely missing.

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
