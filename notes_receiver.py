"""Build and verify the receive-only overlay for the pinned JVM Signal release."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import zipfile

VERSION = "0.14.7"
SOURCE_SHA256 = "dee3ad01d99c2b30971a2cbfb4769dd76c969044705dc305209900fdd1c78284"
SOURCE_URL = (f"https://raw.githubusercontent.com/AsamK/signal-cli/v{VERSION}/"
              "lib/src/main/java/org/asamk/signal/manager/helper/IncomingMessageHandler.java")
PATCH = Path(__file__).resolve().parent / "scripts/signal-cli-notes/notes-only.patch"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def launcher_for(binary: str) -> str | None:
    """Use an overlay only when it matches this patch and the installed launcher."""
    root = Path(binary).resolve().parent.parent
    overlay = root / "notes-receiver"
    launcher = root / "bin/signal-cli-notes"
    try:
        if not (root / f"lib/libsignal-cli-{VERSION}.jar").is_file():
            return None
        expected = {
            "version": VERSION, "source": SOURCE_SHA256, "patch": digest(PATCH),
            "original_launcher": digest(Path(binary)), "launcher": digest(launcher),
            "library": digest(root / f"lib/libsignal-cli-{VERSION}.jar"),
            "jar": digest(overlay / "notes-receiver.jar"),
        }
        if json.loads((overlay / "manifest.json").read_text()) != expected:
            return None
        return str(launcher) if os.access(launcher, os.X_OK) else None
    except (OSError, ValueError):
        return None


def install(distribution: Path, java_home: Path, source: Path | None = None) -> Path:
    """Compile alongside the stock release; publish the verification manifest last."""
    distribution = distribution.resolve()
    original = distribution / "bin/signal-cli"
    if not (distribution / f"lib/libsignal-cli-{VERSION}.jar").is_file():
        raise RuntimeError(f"Selective receive requires JVM signal-cli {VERSION}.")
    if launcher_for(str(original)):
        return distribution / "bin/signal-cli-notes"
    overlay = distribution / "notes-receiver"
    overlay.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="build-", dir=overlay) as directory:
        build = Path(directory)
        upstream = build / "IncomingMessageHandler.java"
        if source is None:
            subprocess.run(["curl", "-fsSL", "--retry", "3", SOURCE_URL, "-o", str(upstream)],
                           check=True, timeout=120)
        else:
            upstream.write_bytes(source.read_bytes())
        if digest(upstream) != SOURCE_SHA256:
            raise RuntimeError("Signal receive source checksum mismatch; stock receiver retained.")
        upstream_bytes = upstream.read_bytes()
        subprocess.run(["patch", "--batch", "--fuzz=0", str(upstream), str(PATCH)],
                       check=True, timeout=10, capture_output=True)
        classes = build / "classes"
        subprocess.run([str(java_home / "bin/javac"), "--release", "25", "-cp", str(distribution / "lib/*"),
                        "-d", str(classes), str(upstream)], check=True, timeout=120)
        jar = build / "notes-receiver.jar"
        with zipfile.ZipFile(jar, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(classes.rglob("*.class")):
                archive.write(path, path.relative_to(classes).as_posix())
        original_text = original.read_text()
        if original_text.count("\nCLASSPATH=") != 1:
            raise RuntimeError("Unexpected Signal launcher; stock receiver retained.")
        launcher = build / "signal-cli-notes"
        launcher.write_text(original_text.replace(
            "\nCLASSPATH=", "\nCLASSPATH=$APP_HOME/notes-receiver/notes-receiver.jar:", 1))
        launcher.chmod(0o755)
        manifest = {
            "version": VERSION, "source": SOURCE_SHA256, "patch": digest(PATCH),
            "original_launcher": digest(original), "launcher": digest(launcher), "jar": digest(jar),
            "library": digest(distribution / f"lib/libsignal-cli-{VERSION}.jar"),
        }
        # Retain both sources with the locally built GPL overlay for inspection/rebuilds.
        (overlay / "IncomingMessageHandler.upstream.java").write_bytes(upstream_bytes)
        (overlay / "IncomingMessageHandler.java").write_bytes(upstream.read_bytes())
        (overlay / "COPYING").write_bytes(PATCH.with_name("COPYING").read_bytes())
        os.replace(jar, overlay / "notes-receiver.jar")
        os.replace(launcher, distribution / "bin/signal-cli-notes")
        manifest_file = build / "manifest.json"
        manifest_file.write_text(json.dumps(manifest, indent=2) + "\n")
        os.replace(manifest_file, overlay / "manifest.json")
    return distribution / "bin/signal-cli-notes"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("distribution", type=Path)
    parser.add_argument("--source", type=Path, help="Use a checksum-verified source already on disk")
    args = parser.parse_args()
    import engine
    home = engine._java_home()
    if not home:
        parser.error("Java 25 JDK is required; run Setup first.")
    try:
        launcher = install(args.distribution, Path(home), args.source)
        print(f"Selective Note-to-Self receiver ready: {launcher}")
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Could not build selective receiver: {exc}\n")
