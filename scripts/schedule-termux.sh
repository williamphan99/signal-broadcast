#!/usr/bin/env bash
# Best-effort scheduled sending on Android. This is the Termux counterpart of the macOS
# launchd schedule. It installs a cron job (inside the guest) for each time in your
# config.toml `send_times`, then prints the Termux-HOST glue you must add so the job
# actually fires with the screen off.
#
# Run inside the proot-distro guest:   bash scripts/schedule-termux.sh
#
# IMPORTANT — Android reliability: Doze can throttle or delay background work. For the
# schedule to fire dependably you MUST also do the HOST-side steps this script prints
# (wake lock, battery-optimization exemption, Termux:Boot). Even then, treat scheduled
# sending as best-effort; a manual `python3 broadcast.py` is the reliable path.
set -uo pipefail
cd "$(dirname "$0")/.."
TAG="# signal-broadcast"

command -v crontab >/dev/null 2>&1 || { echo "cron not installed — run scripts/setup-termux.sh first." >&2; exit 1; }

# Build one cron line per send_time, reusing engine.parse_times() for HH:MM validation and
# engine.format_cron_line() for the line format (shared with the web UI, so they can't drift).
NEWLINES="$(python3 - <<'PY'
import engine
cfg = engine.load_config()
for e in engine.parse_times(cfg.send_times):
    print(engine.format_cron_line(e["Hour"], e["Minute"]))
PY
)"

if [ -z "$NEWLINES" ]; then
  echo "No send_times in config.toml — nothing to schedule. Edit send_times and re-run." >&2
  exit 1
fi

# Replace only our tagged block; leave any other cron jobs untouched.
EXISTING="$(crontab -l 2>/dev/null | grep -v -F "$TAG" || true)"
printf '%s\n%s\n' "$EXISTING" "$NEWLINES" | sed '/^$/d' | crontab -
echo "Installed cron entries:"
crontab -l | grep -F "$TAG"

# Start the cron daemon now (no systemd inside proot).
if ! pgrep -x cron >/dev/null 2>&1 && ! pgrep -x crond >/dev/null 2>&1; then
  (cron || /usr/sbin/cron || crond) 2>/dev/null && echo "cron daemon started."
fi

cat <<'EOF'

────────────────────────────────────────────────────────────────────────────
HOST-side steps (do these in Termux, OUTSIDE this guest) — required for it to fire:

1) Battery: Android Settings → Apps → Termux → Battery → "Unrestricted".
   (Without this, Doze will stop the schedule after the screen is off a while.)

2) Install Termux:Boot from F-Droid, open it once, then create a boot script so the
   wake lock is held and the guest's cron starts on every reboot:

   mkdir -p ~/.termux/boot
   cat > ~/.termux/boot/10-signal-broadcast.sh <<'BOOT'
   #!/data/data/com.termux/files/usr/bin/sh
   termux-wake-lock
   proot-distro login debian -- sh -lc 'cron || /usr/sbin/cron'
   BOOT
   chmod +x ~/.termux/boot/10-signal-broadcast.sh

   (Use your guest's name if not "debian". Install termux-api + `pkg install termux-api`
   so termux-wake-lock exists.)

3) To start it right now without rebooting, run in Termux (host):
   termux-wake-lock
   proot-distro login debian -- sh -lc 'cron || /usr/sbin/cron'

Reliability note: this is best-effort. Verify by watching logs/cron.log after a
scheduled time with the screen off for a while. For anything you truly can't miss,
run it manually or use an always-on Linux box instead of the phone.
────────────────────────────────────────────────────────────────────────────
EOF
