#!/usr/bin/env python3
"""Thumbnails for the Send tab's photo strip, made with macOS's built-in `sips`.

Tk's PhotoImage reads PNG and GIF only — not the JPEG/HEIC that come off a phone —
so the photo strip needs every picture converted before it can be shown. The usual
answer is Pillow, but this app deliberately installs nothing through pip (Setup.command
only brews Java, signal-cli, qrencode and python-tk), and a new pip step is a new way
for setup to fail on someone's Mac. `sips` ships with macOS, opens everything Preview
opens (HEIC included), and costs one subprocess (~100ms) per photo.

Thumbnails are copies of the user's own photos, so they live in a per-session temp
directory — never the project folder. The Security tab's wipe deletes the originals
from wherever they live; a folder of readable copies left behind would quietly undo
that, so clear() runs on wipe, on quit, and at interpreter exit.
"""
from __future__ import annotations

import atexit
import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path

SIPS = "/usr/bin/sips"
CONVERT_TIMEOUT_S = 20          # a huge RAW/HEIC still finishes well inside this

_cache_dir: Path | None = None


def _dir() -> Path:
    global _cache_dir
    if _cache_dir is None or not _cache_dir.exists():
        _cache_dir = Path(tempfile.mkdtemp(prefix="signal-broadcast-thumbs-"))
        atexit.register(clear)
    return _cache_dir


def clear() -> None:
    """Delete every generated thumbnail. Safe to call more than once."""
    global _cache_dir
    if _cache_dir and _cache_dir.exists():
        shutil.rmtree(_cache_dir, ignore_errors=True)
    _cache_dir = None


def make(src: str, size: int) -> Path | None:
    """A PNG of ``src`` fitted inside ``size``×``size`` (aspect kept), or None if the
    file can't be read as an image. Cached per (path, mtime, size), so re-rendering
    the strip — which happens on every drag — never re-runs sips."""
    try:
        mtime = Path(src).stat().st_mtime_ns
    except OSError:
        return None
    key = hashlib.sha1(f"{src}|{mtime}|{size}".encode()).hexdigest()[:16]
    dest = _dir() / f"{key}.png"
    if dest.exists():
        return dest
    try:
        subprocess.run(
            [SIPS, "-s", "format", "png", "-Z", str(size), src, "--out", str(dest)],
            capture_output=True, timeout=CONVERT_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    # sips exits 0 on some files it couldn't really convert, so trust the output file,
    # not the return code.
    return dest if dest.exists() and dest.stat().st_size else None
