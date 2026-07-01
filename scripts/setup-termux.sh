#!/usr/bin/env bash
# Signal Broadcast — one-time setup INSIDE a proot-distro Debian/Ubuntu guest on Android.
#
# This is the Android counterpart of the macOS Setup.command. It installs the SAME runtime
# the Mac uses — signal-cli 0.14.x on Java 25. (An earlier version paired 0.13.x with
# Java 21 to avoid needing Java 25, but 0.13.x is too old for Signal's current linking
# protocol and fails with "Invalid ACI!" on scan — so we match the Mac exactly, fetching
# a portable Temurin JDK 25 since Debian/Termux don't package it.)
# See PIXEL-SETUP.md for the full Termux + proot-distro walkthrough.
#
# Run it from inside the guest:   bash scripts/setup-termux.sh
set -uo pipefail
cd "$(dirname "$0")/.."

# The JVM build (plain .tar.gz, NOT -Linux-native) so -Xss can enlarge the libsignal thread
# stack, exactly like the Mac build. Override with SIGNAL_CLI_VERSION=…
SIGNAL_CLI_VERSION="${SIGNAL_CLI_VERSION:-0.14.5}"

echo "=== Signal Broadcast — Termux/Debian setup ==="
echo

# 0. Sanity: we must be in an apt-based glibc guest (Debian/Ubuntu via proot-distro),
#    NOT bare Termux — signal-cli's native libsignal .so is glibc, and Termux is Bionic.
if ! command -v apt-get >/dev/null 2>&1; then
  echo "ERROR: apt-get not found." >&2
  echo "Run this INSIDE a proot-distro Debian/Ubuntu guest, not in bare Termux." >&2
  echo "See PIXEL-SETUP.md (bare Termux fails to load libsignal: UnsatisfiedLinkError)." >&2
  exit 1
fi
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"

# 1. Base tools. python3 runs the engine; qrencode renders the link QR; cron is optional
#    (only for scheduled sends). Java is handled separately (Temurin 25) in step 1b.
echo "Installing python3, qrencode, git, curl, cron…"
$SUDO apt-get update -y
$SUDO apt-get install -y python3 python3-flask qrencode git curl ca-certificates cron procps

# 1b. Java 25 via a portable Temurin build into ./vendor (Debian/Termux don't package it).
#     engine._java_home() discovers vendor/jdk*/bin/java automatically — no PATH changes.
JDK_DIR="$(ls -d vendor/jdk-* 2>/dev/null | head -1 || true)"
if [ -z "$JDK_DIR" ] || [ ! -x "$JDK_DIR/bin/java" ]; then
  case "$(uname -m)" in
    aarch64|arm64) JARCH=aarch64 ;; x86_64|amd64) JARCH=x64 ;;
    armv7l|armv7|arm) JARCH=arm ;; *) JARCH=aarch64 ;;
  esac
  echo "Downloading Temurin JDK 25 (${JARCH})…"
  mkdir -p vendor
  JURL="https://api.adoptium.net/v3/binary/latest/25/ga/linux/${JARCH}/jdk/hotspot/normal/eclipse"
  if curl -fsSL "$JURL" -o vendor/jdk.tar.gz; then
    tar -xzf vendor/jdk.tar.gz -C vendor && rm -f vendor/jdk.tar.gz
    JDK_DIR="$(ls -d vendor/jdk-* | head -1)"
  else
    rm -f vendor/jdk.tar.gz
    echo "ERROR: could not download Temurin JDK 25 for ${JARCH}." >&2
    exit 1
  fi
fi
export JAVA_HOME="$PWD/$JDK_DIR"          # used by the smoke test below
echo "Java: $("$JDK_DIR/bin/java" -version 2>&1 | head -1)"

# 2. Your settings live in config.toml (gitignored); seed it from the template once.
[ -f config.toml ] || cp config.example.toml config.toml

# 3. Download the JVM build of signal-cli into ./vendor. engine.py's _jvm_signal_cli()
#    globs vendor/signal-cli-*/bin/signal-cli and prefers it automatically — same as Mac.
JVM_CLI="vendor/signal-cli-${SIGNAL_CLI_VERSION}/bin/signal-cli"
if [ ! -x "$JVM_CLI" ]; then
  echo "Downloading signal-cli ${SIGNAL_CLI_VERSION} (JVM build)…"
  mkdir -p vendor
  URL="https://github.com/AsamK/signal-cli/releases/download/v${SIGNAL_CLI_VERSION}/signal-cli-${SIGNAL_CLI_VERSION}.tar.gz"
  if curl -fsSL "$URL" -o vendor/signal-cli.tar.gz; then
    tar -xzf vendor/signal-cli.tar.gz -C vendor && rm -f vendor/signal-cli.tar.gz
    echo "Installed JVM signal-cli to $JVM_CLI"
  else
    rm -f vendor/signal-cli.tar.gz
    echo "ERROR: could not download signal-cli ${SIGNAL_CLI_VERSION}." >&2
    exit 1
  fi
fi

# 3b. Provide the native libsignal library for ARM Linux. The signal-cli JVM tarball
#     bundles libsignal_jni for x86_64-Linux and macOS, but NOT aarch64/armv7 Linux —
#     so on a Pixel it fails with "Missing required native library dependency:
#     libsignal-client". We fetch the matching prebuilt .so from exquo/signal-libs-build
#     (the community builds made for exactly this: Termux / Raspberry Pi) and inject it
#     into the libsignal-client jar under the arch name the loader looks for.
LIBJAR="$(ls vendor/signal-cli-${SIGNAL_CLI_VERSION}/lib/libsignal-client-*.jar 2>/dev/null | head -1)"
ARCH="$(uname -m)"
case "$ARCH" in
  aarch64|arm64) TRIPLE="aarch64-unknown-linux-gnu"; JNINAME="libsignal_jni_aarch64.so" ;;
  armv7l|armv7|arm) TRIPLE="armv7-unknown-linux-gnueabihf"; JNINAME="libsignal_jni_arm.so" ;;
  x86_64|amd64) TRIPLE=""; JNINAME="" ;;   # x86_64 .so is already bundled in the jar
  *) TRIPLE=""; JNINAME="" ;;
esac
if [ -n "$LIBJAR" ] && [ -n "$TRIPLE" ]; then
  LSVER="$(basename "$LIBJAR" | sed -E 's/^libsignal-client-(.*)\.jar$/\1/')"
  if python3 -c "import sys,zipfile; sys.exit(0 if '$JNINAME' in zipfile.ZipFile('$LIBJAR').namelist() else 1)"; then
    echo "Native libsignal ($ARCH) already present in the jar — skipping."
  else
    echo "Fetching native libsignal ${LSVER} for ${ARCH} (exquo/signal-libs-build)…"
    SO_URL="https://github.com/exquo/signal-libs-build/releases/download/libsignal_v${LSVER}/libsignal_jni.so-v${LSVER}-${TRIPLE}.tar.gz"
    if curl -fsSL "$SO_URL" -o vendor/libsignal_jni.tar.gz; then
      tar -xzf vendor/libsignal_jni.tar.gz -C vendor && rm -f vendor/libsignal_jni.tar.gz
      # tar yields ./libsignal_jni.so — inject it into the jar under the arch-specific name.
      python3 - "$LIBJAR" "vendor/libsignal_jni.so" "$JNINAME" <<'PY'
import sys, zipfile, os
jar, so, name = sys.argv[1], sys.argv[2], sys.argv[3]
with zipfile.ZipFile(jar, "a") as z:
    z.write(so, name)
os.remove(so)
print(f"Injected {name} into {os.path.basename(jar)}")
PY
    else
      echo "ERROR: could not download native libsignal for ${ARCH} (libsignal ${LSVER})." >&2
      echo "Check https://github.com/exquo/signal-libs-build/releases for tag libsignal_v${LSVER}." >&2
      exit 1
    fi
  fi
fi

# 4. Smoke test: prove Java 25 can actually load signal-cli (a wrong Java version or a
#    missing native lib would fail loudly here — better than mid-broadcast).
echo
echo "Verifying signal-cli launches under Java 25…"
if JAVA_HOME="$PWD/$JDK_DIR" JAVA_OPTS="-Xss32m -Djava.net.preferIPv4Stack=true" "$JVM_CLI" --version; then
  echo "OK — signal-cli runs."
else
  echo "ERROR: signal-cli failed to launch. See the error above." >&2
  exit 1
fi

echo
echo "All set. Next:"
echo "  1) Link this device:  bash scripts/link-termux.sh   (see PIXEL-SETUP.md for the QR steps)"
echo "  2) Pull your groups:  bash scripts/pull-groups.sh"
echo "  3) Dry run:           python3 broadcast.py --limit 2 --dry-run"
echo "  4) Real run:          python3 broadcast.py"
