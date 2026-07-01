#!/usr/bin/env bash
# Link this guest as a SECONDARY Signal device (like Signal Desktop). Your number stays
# on the phone as primary and is never re-registered.
#
# Run inside the proot-distro guest:   bash scripts/link-termux.sh
#
# Same-phone linking (Signal app and signal-cli on the ONE Pixel): you can't scan your
# own screen, so use one of these — see PIXEL-SETUP.md:
#   A) Copy the printed sgnl://linkdevice… URI, render it as a QR on a second screen,
#      and scan THAT with the Pixel's Signal camera (Settings → Linked Devices → +).
#   B) From the Termux HOST (not this guest) run:  termux-open-url 'sgnl://linkdevice…'
#      which may hand off straight to Signal to complete the link (test this first).
set -uo pipefail
cd "$(dirname "$0")/.."

DEVICE_NAME="${1:-pixel-broadcast}"

# Reuse engine.py's resolver so we get the vendored JVM signal-cli + the right JAVA_HOME
# and JAVA_OPTS (-Xss32m, IPv4), identical to how a real send launches it.
python3 - "$DEVICE_NAME" <<'PY'
import subprocess, sys
import engine

name = sys.argv[1]
argv, env = engine.signal_cli_command("--config", str(engine.DATA_DIR), "link", "-n", name)
qr = engine.qrencode_bin()

print(f"Linking as '{name}'. A QR + raw link will appear below; keep this open until it confirms.\n")
proc = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, text=True, bufsize=1)
assert proc.stdout is not None
for line in proc.stdout:
    line = line.rstrip("\n")
    if line.startswith("sgnl://linkdevice") or line.startswith("tsdevice:"):
        subprocess.run([qr, "-t", "ANSIUTF8", line])
        print("\nIf you can't scan this screen (same-phone), use the raw link:")
        print(f"  {line}")
        print("  • render it as a QR on another screen and scan with Signal's camera, OR")
        print("  • from the Termux HOST run:  termux-open-url '<the link above>'\n")
    else:
        print(line)
rc = proc.wait()
sys.exit(rc)
PY

echo
echo "If linking succeeded, pull your groups next:  bash scripts/pull-groups.sh"
