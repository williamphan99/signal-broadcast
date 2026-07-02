#!/usr/bin/env python3
"""Core broadcast engine — shared by the GUI (gui.py) and the CLI (broadcast.py).

signal-cli sends a single message per invocation; this module owns the loop, the
pacing, the retry/backoff, and the success/failure ledger. It is UI-agnostic:
progress and log lines are delivered through callbacks, and it raises
``BroadcastError`` instead of exiting so a GUI can show a dialog rather than die.
"""

from __future__ import annotations

import collections
import fcntl
import hashlib
import json
import os
import plistlib
import queue
import random
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator

# Platform switch. Assigning to a bool (rather than testing sys.platform inline)
# deliberately defeats type-checker platform-narrowing, so the Linux/Termux branches
# aren't flagged "unreachable" when the checker runs on macOS. The macOS wrapper
# (Dock app, launchd schedule, station-mode watcher, pmset) only runs when this is True;
# elsewhere the portable CLI core (engine send loop + broadcast.py) is what's used.
IS_DARWIN = sys.platform == "darwin"

# Everything resolves relative to this file, so behaviour is identical whether a
# human, a launcher, or launchd starts it from an arbitrary working directory.
PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / "signal-cli-data"
LOGS_DIR = PROJECT_DIR / "logs"
LAST_RUN_FILE = LOGS_DIR / "last-run.txt"
LAST_SEND_FILE = LOGS_DIR / "last-send.json"  # counts-only summary for the UI
SEND_LOCK_FILE = LOGS_DIR / "sending.lock"    # exclusive: one broadcast at a time
RUN_PROGRESS_FILE = LOGS_DIR / "run-progress.json"  # in-flight run, for crash resume
CONFIG_FILE = PROJECT_DIR / "config.toml"          # per-user (holds the number); gitignored
CONFIG_EXAMPLE_FILE = PROJECT_DIR / "config.example.toml"  # tracked template
GROUPS_FILE = PROJECT_DIR / "groups.txt"
MESSAGE_FILE = PROJECT_DIR / "message.txt"
ATTACHMENTS_FILE = PROJECT_DIR / "attachments.txt"

# Bumped by hand on a meaningful change, so the UI can show which build is running
# (e.g. to confirm a machine actually pulled the latest code). app_version() appends
# the short git commit when available, so every push is distinguishable even if this
# number isn't bumped.
APP_VERSION = "1.15.3"


def git_pull() -> tuple[bool, str]:
    """Update the app in place: a fast-forward-only `git pull` in the project folder.
    Returns (changed, message) — changed is False when already up to date or on any
    error, so the caller only restarts when there's actually new code. Never raises."""
    try:
        proc = subprocess.run(["git", "-C", str(PROJECT_DIR), "pull", "--ff-only"],
                              capture_output=True, text=True, errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"Couldn't run git: {exc}"
    out = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0:
        return False, out or "git pull failed."
    if "Already up to date" in out:
        return False, "You're already on the latest version."
    return True, out or "Updated."


def app_version() -> str:
    """Human-readable build tag: the version plus the short git commit if we can
    read one. Falls back to just the version (e.g. on a copy with no .git)."""
    try:
        proc = subprocess.run(["git", "-C", str(PROJECT_DIR), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=3)
        commit = proc.stdout.strip()
        if proc.returncode == 0 and commit:
            return f"{APP_VERSION} ({commit})"
    except (OSError, subprocess.SubprocessError):
        pass
    return APP_VERSION

# launchd schedule (the daily auto-send job)
SCHEDULE_LABEL = "com.user.signal-broadcast"
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
INSTALLED_PLIST = LAUNCH_AGENTS_DIR / f"{SCHEDULE_LABEL}.plist"
LOCAL_PLIST = PROJECT_DIR / f"{SCHEDULE_LABEL}.plist"

# Station-mode watcher: wipes the app's data if the Mac is unplugged (see watcher.py).
WATCHER_LABEL = "com.user.signal-broadcast.watcher"
WATCHER_PLIST = LAUNCH_AGENTS_DIR / f"{WATCHER_LABEL}.plist"

# Throttle fingerprints in signal-cli stderr. These get long exponential backoff;
# anything else gets a couple of quick retries then is marked failed.
THROTTLE_PATTERN = re.compile(r"rate.?limit|throttl|\b429\b|\b413\b", re.IGNORECASE)
RETRY_AFTER_PATTERN = re.compile(r"retry[ _-]?after[^0-9]*(\d+)", re.IGNORECASE)

THROTTLE_BACKOFF_BASE_S = 30.0   # first throttle wait; doubles each retry
THROTTLE_BACKOFF_CAP_S = 300.0   # never wait longer than 5 min between retries
NON_THROTTLE_RETRIES = 2         # quick retries for transient (non-rate) errors
NON_THROTTLE_WAIT_S = 5.0
SEND_TIMEOUT_S = 900             # per-send ceiling (15 min). Very large groups can take
                                 # several minutes to fan out to every member; this is a
                                 # CEILING, not a target — a healthy send returns the
                                 # instant signal-cli replies, so a generous value never
                                 # slows a normal run, it only buys buffer against falsely
                                 # flagging a slow-but-fine giant group "uncertain". Still
                                 # bounded so a genuinely stuck send can't hang forever
                                 # (the live loader/heartbeat lets the operator Stop one).
CONFIRM_GRACE_S = 60             # after a send times out, how long to keep listening
                                 # for signal-cli's late reply before declaring the
                                 # daemon stuck. Confirms a slow-but-fine send instead
                                 # of leaving it "uncertain"; no new send starts during
                                 # this wait, so only one send is ever in flight.
# First-sync after linking. A big account's groups don't all arrive in one
# receive, so we drain in short bursts until the count stops growing (or the cap).
SYNC_BURST_S = 5                 # one receive burst while draining the phone's sync
SYNC_MAX_S = 60                  # overall cap — large accounts (100+ groups) take longer
SYNC_STABLE_ROUNDS = 2           # stop once the group count holds steady this many rounds
LISTGROUPS_TIMEOUT_S = 30        # listGroups is mostly local; guard against a network hang
MIN_DELAY_S = 10.0               # hard floor: never send faster than this, whatever the config
# Parallel sending: how many whole-group sends may be in flight at once on the single
# account. 1 = the safe default (strictly one at a time).
# >1 launches new sends every base_delay but lets their fan-out overlap — it can
# finish a run sooner ONLY if signal-cli actually overlaps sends (its account lock may
# serialise them anyway), and it raises the rate of NEW sends, so ban risk is higher.
# Opt-in via config.toml / Security tab; capped here so no config can over-parallelise.
MAX_CONCURRENT_SENDS = 5


class BroadcastError(Exception):
    """Recoverable, user-facing problem (bad config, missing file, no signal-cli)."""


# Callback aliases. Defaults are no-ops so callers can pass only what they need.
LogFn = Callable[[str], None]
# status is one of: "sent" | "failed" | "skipped" | "uncertain" (timed out — may have sent)
ProgressFn = Callable[[int, int, str, str, float], None]  # done, total, name, status, seconds
StopFn = Callable[[], bool]
# Fired when a group's send is dispatched (after pacing, before it completes), so a
# front-end can show what's in flight right now — essential with concurrent_sends > 1,
# where several groups send at once. Args: (position, group name).
StartFn = Callable[[int, str], None]


@dataclass
class Config:
    account: str
    base_delay_seconds: float
    jitter_seconds: float
    cooldown_hours: float
    max_retries: int
    send_times: list[str]
    debug: bool = False  # write raw signal-cli errors to logs/debug-*.txt
    wipe_on_close: bool = False  # erase all data when the app is quit (armed in Security)
    concurrent_sends: int = 1  # whole-group sends in flight at once (1 = safe default; up to 5)


@dataclass
class GroupSendResult:
    group_id: str
    name: str
    ok: bool
    skipped: bool = False   # not attempted on purpose (e.g. admin-only group)
    uncertain: bool = False  # send timed out — may have delivered; never auto-retried/resent
    reason: str = ""        # short why, for skips/failures (PII-safe category)


@dataclass
class GroupEntry:
    group_id: str
    name: str
    enabled: bool  # False = commented out in groups.txt = skipped


@dataclass
class RunSummary:
    at: str  # ISO timestamp of the last completed broadcast
    total: int
    sent: int
    failed: int
    skipped: int = 0  # admin-only groups not attempted
    uncertain: int = 0  # sends that timed out and may have delivered


@dataclass
class InterruptedRun:
    """A broadcast that didn't finish (the app was killed/crashed mid-run). Used to
    offer a resume that skips groups already sent, instead of re-sending everything."""
    fingerprint: str                     # of the message+attachments at the time
    total: int                           # groups in the original run
    done: int                            # already sent/uncertain/skipped — won't redo
    remaining: list[tuple[str, str]]     # still to send (unattempted + clean failures)
    # Groups that were mid-send when the run died ("attempting"), or timed out: the
    # message MAY already have gone out, so they are never auto-resent — only surfaced
    # so the operator can check Signal and decide.
    uncertain: list[tuple[str, str]] = field(default_factory=list)


_GROUPS_HEADER = (
    "# Generated by the app. Format: <base64-group-id><TAB><group name>.\n"
    "# Comment a line with # to skip that group on the next run.\n"
)


# --------------------------------------------------------------------------- #
# Config + input files
# --------------------------------------------------------------------------- #
def ensure_config() -> None:
    """Materialise config.toml from the tracked template on first run / after a
    wipe. config.toml holds the linked number, so it's gitignored and never
    committed — only this placeholder template is. No-op once it exists."""
    if CONFIG_FILE.exists() or not CONFIG_EXAMPLE_FILE.exists():
        return
    shutil.copyfile(CONFIG_EXAMPLE_FILE, CONFIG_FILE)


def _config_num(raw: dict, key: str, default, cast):
    """Coerce a config scalar, turning a bad value into a clear BroadcastError instead
    of a raw ValueError. A wrong-typed TOML value (base_delay_seconds = "fast") must
    not crash an unattended scheduled run with a traceback main() can't explain."""
    val = raw.get(key, default)
    try:
        return cast(val)
    except (ValueError, TypeError):
        raise BroadcastError(f"config.toml: {key} must be a number (got {val!r}).")


def load_config() -> Config:
    ensure_config()
    if not CONFIG_FILE.exists():
        raise BroadcastError(f"Missing {CONFIG_FILE.name}.")
    raw = tomllib.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    account = str(raw.get("account", ""))
    if not account or "X" in account:
        raise BroadcastError('Signal number not set yet (config.toml: account = "+61...").')
    return Config(
        account=account,
        base_delay_seconds=_config_num(raw, "base_delay_seconds", 10, float),
        jitter_seconds=_config_num(raw, "jitter_seconds", 3, float),
        cooldown_hours=_config_num(raw, "cooldown_hours", 0, float),
        max_retries=_config_num(raw, "max_retries", 4, int),
        send_times=[str(t) for t in raw.get("send_times", [])],
        debug=bool(raw.get("debug", False)),
        wipe_on_close=bool(raw.get("wipe_on_close", False)),
        # Clamp to [1, MAX]: a hand-edited config can't push parallelism past the cap,
        # and a bad/zero value falls back to the safe one-at-a-time default.
        concurrent_sends=max(1, min(MAX_CONCURRENT_SENDS,
                                    _config_num(raw, "concurrent_sends", 1, int))),
    )


def save_account(number: str) -> None:
    """Persist a detected/linked number into config.toml without disturbing the
    rest of the file (simple line rewrite — config.toml stays hand-editable)."""
    ensure_config()
    if not CONFIG_FILE.exists():
        return
    lines = CONFIG_FILE.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.lstrip().startswith("account"):
            indent = line[: len(line) - len(line.lstrip())]
            comment = line.split("#", 1)[1] if "#" in line else ""
            lines[i] = f'{indent}account            = "{number}"' + (f"  #{comment}" if comment else "")
            break
    CONFIG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def save_send_times(times: list[str]) -> None:
    """Persist schedule times into config.toml, preserving the trailing comment."""
    ensure_config()
    if not CONFIG_FILE.exists():
        return
    lines = CONFIG_FILE.read_text(encoding="utf-8").splitlines()
    arr = "[" + ", ".join(f'"{t}"' for t in times) + "]"
    for i, line in enumerate(lines):
        if line.lstrip().startswith("send_times"):
            indent = line[: len(line) - len(line.lstrip())]
            comment = "  #" + line.split("#", 1)[1] if "#" in line else ""
            lines[i] = f"{indent}send_times         = {arr}{comment}"
            break
    CONFIG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def set_config_value(key: str, value: bool | int | float | str) -> None:
    """Update a single scalar key in config.toml in place, preserving its trailing
    comment; append the key if it isn't there yet. Keeps config.toml hand-editable
    and is how the Security tab persists the speed / logging / wipe settings."""
    ensure_config()
    if not CONFIG_FILE.exists():
        return
    if isinstance(value, bool):
        rendered = "true" if value else "false"   # bool before int: bool IS an int
    elif isinstance(value, (int, float)):
        rendered = f"{value:g}"
    else:
        rendered = f'"{value}"'
    lines = CONFIG_FILE.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped.startswith(key):
            continue
        # Guard against prefix collisions (e.g. a future 'debug_x' vs 'debug').
        rest = stripped[len(key):]
        if rest[:1] not in ("", " ", "\t", "="):
            continue
        indent = line[: len(line) - len(stripped)]
        comment = "  #" + line.split("#", 1)[1] if "#" in line else ""
        lines[i] = f"{indent}{key} = {rendered}{comment}"
        CONFIG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return
    lines.append(f"{key} = {rendered}")
    CONFIG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_message(path: Path = MESSAGE_FILE) -> str:
    if not path.exists():
        raise BroadcastError(f"Missing message file: {path.name}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise BroadcastError("The message is empty — nothing to send.")
    return text


def read_attachments(path: Path = ATTACHMENTS_FILE) -> list[str]:
    """One image path per line; blanks and # comments ignored. Raises if a listed
    file is missing so we never blast every group with a broken image."""
    if not path.exists():
        return []
    resolved: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        p = Path(line)
        if not p.is_absolute():
            p = PROJECT_DIR / p
        if not p.exists():
            raise BroadcastError(f"Attachment not found: {line}")
        resolved.append(str(p))
    return resolved


def write_message(text: str, path: Path = MESSAGE_FILE) -> None:
    path.write_text(text.strip() + "\n", encoding="utf-8")


def write_attachments(paths: list[str], path: Path = ATTACHMENTS_FILE) -> None:
    header = (
        "# One image path per line. Lines starting with # are ignored.\n"
        "# Managed by the app, but safe to hand-edit.\n"
    )
    body = "".join(f"{p}\n" for p in paths)
    path.write_text(header + body, encoding="utf-8")


def read_groups(path: Path = GROUPS_FILE) -> list[tuple[str, str]]:
    """Lines of '<base64-id>\\t<name>'. Blanks and # comments ignored, so a group
    can be skipped by commenting it out. Name is cosmetic (shown in the report)."""
    if not path.exists():
        raise BroadcastError("No groups yet — link your phone and pull groups first.")
    groups: list[tuple[str, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        gid, _, name = line.partition("\t")
        gid = gid.strip()
        if gid:
            groups.append((gid, name.strip() or gid))
    if not groups:
        raise BroadcastError("No groups yet — link your phone and pull groups first.")
    return groups


def count_groups(path: Path = GROUPS_FILE) -> int:
    try:
        return len(read_groups(path))
    except BroadcastError:
        return 0


def read_group_entries(path: Path = GROUPS_FILE) -> list[GroupEntry]:
    """Every group in groups.txt with its enabled/excluded state — including the
    commented-out ones, so the UI can show all groups with tick boxes."""
    if not path.exists():
        return []
    entries: list[GroupEntry] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s:
            continue
        commented = s.startswith("#")
        body = s[1:].strip() if commented else s
        # A real group line has a tab and a space-free id; header comments don't.
        if "\t" not in body:
            continue
        gid, _, name = body.partition("\t")
        gid = gid.strip()
        if not gid or " " in gid:
            continue
        entries.append(GroupEntry(gid, name.strip() or gid, not commented))
    return entries


def write_group_selection(enabled_ids: set[str]) -> None:
    """Rewrite groups.txt, commenting out any group not in enabled_ids."""
    lines = []
    for e in read_group_entries():
        row = f"{e.group_id}\t{e.name}"
        lines.append(row if e.group_id in enabled_ids else f"# {row}")
    GROUPS_FILE.write_text(_GROUPS_HEADER + "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Cooldown gate
# --------------------------------------------------------------------------- #
def cooldown_blocks_run(cooldown_hours: float) -> str | None:
    """Return a human reason if the last run was too recent, else None."""
    if cooldown_hours <= 0 or not LAST_RUN_FILE.exists():
        return None
    try:
        last = datetime.fromisoformat(LAST_RUN_FILE.read_text(encoding="utf-8").strip())
    except ValueError:
        return None
    # Compare in UTC so the cooldown can't be skewed by a DST shift or manual clock
    # change (naive local times are non-monotonic — across a fall-back the wall clock
    # repeats an hour, so "now" can read earlier than "last"). A legacy naive stamp
    # (written before this fix) is interpreted as local time.
    if last.tzinfo is None:
        last = last.astimezone()
    next_ok = last + timedelta(hours=cooldown_hours)
    now = datetime.now(timezone.utc)
    if now < next_ok:
        mins = round((next_ok - now).total_seconds() / 60)
        return f"last run was {last.astimezone():%Y-%m-%d %H:%M}; cooldown clears in ~{mins} min"
    return None


def stamp_run() -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    LAST_RUN_FILE.write_text(datetime.now(timezone.utc).isoformat(timespec="seconds"),
                             encoding="utf-8")


@contextmanager
def send_lock() -> Iterator[None]:
    """Hold an exclusive lock for the duration of a broadcast so two senders can't
    run at once — a second app window, or the scheduler firing while the app is
    mid-send. Two concurrent senders would fight over signal-cli's account lock and
    both stall. Uses flock, which the OS releases automatically if the process dies,
    so a crashed run never leaves the lock stuck. Raises BroadcastError if a send is
    already running."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(SEND_LOCK_FILE, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()  # not holding the lock — don't leak the descriptor
        raise BroadcastError("A send is already in progress (this app or the "
                             "scheduler). Wait for it to finish, or Stop it first.")
    # We hold the lock now; the unlock/close belongs in a finally that only runs
    # on this path (so it never touches an already-closed fd from the branch above).
    try:
        try:
            fh.write(str(os.getpid()))
            fh.flush()
        except OSError:
            pass
        yield
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        except OSError:
            pass
        fh.close()


def _reap_orphan_signal_cli(on_log: LogFn = lambda *_: None) -> int:
    """Kill any signal-cli process still using OUR data dir. Only call this while
    holding send_lock(): then no legitimate sender is running, so such a process can
    only be an orphan from a crashed or force-quit run that's still holding the
    account lock — the usual cause of a daemon that times out on startup. Returns the
    number terminated. Best-effort: never raises."""
    try:
        found = subprocess.run(["pgrep", "-f", str(DATA_DIR)],
                               capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return 0
    killed: list[int] = []
    for tok in found.stdout.split():
        if not tok.isdigit():
            continue
        pid = int(tok)
        if pid == os.getpid():
            continue
        # Confirm it really is a signal-cli process before signalling it.
        try:
            cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                                 capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        if "signal-cli" not in cmd.stdout:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            killed.append(pid)
        except OSError:
            pass
    if killed:
        on_log(f"Cleared {len(killed)} leftover signal-cli process(es) holding the account lock.")
        # SIGTERM is async and a JVM signal-cli can take seconds to release the account
        # lock (or ignore SIGTERM). Wait until each really exits before we let the caller
        # retry the daemon — a fixed sleep risked retrying while the orphan still held the
        # lock. Escalate to SIGKILL for anything still alive past the grace deadline.
        _wait_for_pids_exit(killed, deadline_s=8.0)
    return len(killed)


def _pid_alive(pid: int) -> bool:
    """True if the process still exists. Signal 0 only probes; ESRCH means gone, and
    EPERM means it exists but isn't ours (so still 'alive' for our purposes)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _wait_for_pids_exit(pids: list[int], deadline_s: float) -> None:
    """Block until every pid has exited or the deadline passes; SIGKILL stragglers so
    the account lock is actually free before the caller retries the daemon."""
    deadline = time.monotonic() + deadline_s
    remaining = list(pids)
    while remaining and time.monotonic() < deadline:
        remaining = [p for p in remaining if _pid_alive(p)]
        if remaining:
            time.sleep(0.1)
    for pid in remaining:  # still alive past the grace period — force it
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    if remaining:
        # Give the OS a moment to reap the SIGKILLed process and drop the lock.
        time.sleep(0.5)


# --------------------------------------------------------------------------- #
# signal-cli plumbing
# --------------------------------------------------------------------------- #
# Why we bundle a JVM build of signal-cli in vendor/ (installed by Setup):
# Homebrew's signal-cli is a GraalVM *native image* that crashes with
# "java.lang.StackOverflowError" when encrypting for some groups — the overflow
# happens on a thread inside libsignal whose ~2 MB stack the native build's
# -XX:StackSize can't enlarge, so every send to that group fails. The JVM build
# runs that same encryption on ordinary Java threads whose stack size IS
# controllable via -Xss (see THREAD_STACK below), which clears the crash.
VENDOR_DIR = PROJECT_DIR / "vendor"
THREAD_STACK = "32m"  # per-thread stack for the JVM build; the native thread that
                      # overflowed had only ~2 MB, so this is generous headroom.

# Stack-size flag for the native build (fallback when no JVM build is bundled).
# It rarely helps the crashing group — kept only so a native-only machine isn't
# left with nothing — and must precede the subcommand.
SIGNAL_CLI_RUNTIME_OPTS = ["-XX:StackSize=16m"]


def _jvm_signal_cli() -> Path | None:
    """The bundled JVM build's launcher, if Setup installed one. Preferred over the
    Homebrew native build because the native build crashes on some group sends."""
    if not VENDOR_DIR.is_dir():
        return None
    found = sorted(VENDOR_DIR.glob("signal-cli-*/bin/signal-cli"))
    return found[-1] if found else None


def _termux_prefix() -> str:
    """Termux installs everything under $PREFIX (default when unset for a plain probe)."""
    return os.environ.get("PREFIX", "/data/data/com.termux/files/usr")


def _find_bin(name: str) -> str:
    """Locate a binary: PATH first, then the known per-platform bin dirs (Homebrew, /usr,
    Termux's $PREFIX). A minimal PATH — macOS launchd/Dock, or a cron job — can exclude the
    real bin dir, so a bare which() would wrongly report it missing even when installed."""
    found = shutil.which(name)
    if found:
        return found
    for base in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", f"{_termux_prefix()}/bin"):
        p = Path(base) / name
        if p.exists():
            return str(p)
    raise BroadcastError(f"{name} is not installed. Run Setup first.")


def signal_cli_bin() -> str:
    jvm = _jvm_signal_cli()
    if jvm:
        return str(jvm)
    return _find_bin("signal-cli")


def qrencode_bin() -> str:
    return _find_bin("qrencode")


def _is_jvm_build(binary: str) -> bool:
    jvm = _jvm_signal_cli()
    return jvm is not None and str(jvm) == binary


def _java_home() -> str | None:
    """The Java home for the JVM build.

    signal-cli 0.14.x is compiled for Java 25 (older JDKs can't load it), and 0.14.x is
    also the version Signal's *current* linking protocol requires — 0.13.x on Java 21 hits
    "Invalid ACI!" when finishing a device link. So both platforms target Java 25.

    macOS: only offer @25 and the unversioned (newer) Homebrew kegs, never @21.
    Linux/Termux: prefer a JDK we vendored (a portable Temurin 25, since Debian/Termux
    don't package Java 25), then $JAVA_HOME, then whatever's on PATH / in the system."""
    if IS_DARWIN:
        for base in ("/opt/homebrew/opt/openjdk@25", "/usr/local/opt/openjdk@25",
                     "/opt/homebrew/opt/openjdk", "/usr/local/opt/openjdk"):
            if (Path(base) / "bin" / "java").exists():
                return base
        return os.environ.get("JAVA_HOME")  # last resort: whatever the machine has set

    # Linux / Termux (non-Darwin).
    for jdk in sorted(VENDOR_DIR.glob("jdk*"), reverse=True):  # vendored Temurin 25
        if (jdk / "bin" / "java").exists():
            return str(jdk)
    env_home = os.environ.get("JAVA_HOME")
    if env_home and (Path(env_home) / "bin" / "java").exists():
        return env_home
    java = shutil.which("java")
    if java:
        # <home>/bin/java → <home>, following the alternatives/symlink chain.
        return str(Path(java).resolve().parent.parent)
    jvm_dir = Path("/usr/lib/jvm")
    if jvm_dir.is_dir():
        for base in sorted((str(p) for p in jvm_dir.glob("*")), reverse=True):
            if (Path(base) / "bin" / "java").exists():
                return base
    return None


def _signal_env(binary: str) -> dict | None:
    """Environment for a signal-cli call. For the JVM build, point it at a Java 25+
    home and enlarge every thread's stack via JAVA_OPTS — this is the actual fix for
    the StackOverflowError. Returns None for the native build (inherit the parent env).

    We also force IPv4 (-Djava.net.preferIPv4Stack=true): on machines whose network
    advertises IPv6 but can't actually route it (common behind some VPNs/routers),
    Java tries the IPv6 address first and stalls or fails with NoRouteToHostException —
    which shows up as failed image (CDN) uploads and a daemon that times out on start.
    Forcing IPv4 sidesteps that. Safe on any machine with working IPv4 (i.e. all but
    the rare IPv6-only host)."""
    if not _is_jvm_build(binary):
        return None
    env = dict(os.environ)
    home = _java_home()
    if home:
        env["JAVA_HOME"] = home
    extra = f"-Xss{THREAD_STACK} -Djava.net.preferIPv4Stack=true"
    env["JAVA_OPTS"] = f"{env.get('JAVA_OPTS', '')} {extra}".strip()
    return env


def _cli(binary: str, *args: str) -> list[str]:
    """Build a signal-cli argv. The native build takes the stack-size fix as a
    leading runtime flag; the JVM build gets its stack size from JAVA_OPTS instead
    (passing -XX:StackSize to it would be rejected as an unknown argument)."""
    if _is_jvm_build(binary):
        return [binary, *args]
    return [binary, *SIGNAL_CLI_RUNTIME_OPTS, *args]


def signal_cli_command(*args: str) -> tuple[list[str], dict | None]:
    """Resolve the (argv, env) for a one-off signal-cli call: picks the bundled JVM
    build over the native one, applies the stack-size fix, and supplies the Java env
    for the JVM build. Use this for any signal-cli launch outside the send loop (e.g.
    linking in the UI) so every call site is treated identically."""
    binary = signal_cli_bin()
    return _cli(binary, *args), _signal_env(binary)


def detect_account() -> str | None:
    """Return the linked number signal-cli knows about, or None."""
    try:
        binary = signal_cli_bin()
    except BroadcastError:
        return None
    try:
        proc = subprocess.run(_cli(binary, "--config", str(DATA_DIR), "-o", "json", "listAccounts"),
                              capture_output=True, text=True, errors="replace",
                              timeout=LISTGROUPS_TIMEOUT_S, env=_signal_env(binary))
    except subprocess.TimeoutExpired:
        return None  # a hung JVM must not block the GUI worker indefinitely
    if proc.returncode != 0:
        return None
    try:
        accounts = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return None
    for entry in accounts:
        number = entry.get("number") or entry.get("account")
        if number:
            return str(number)
    return None


def wait_for_account(timeout_s: float = 12.0) -> str | None:
    """Poll detect_account() briefly, for use right after `signal-cli link` exits.
    The just-exited link JVM can still hold the account DB lock for a moment — more
    pronounced on macOS, where JVM shutdown lags — so an immediate detect_account()
    can return None even though the link fully succeeded. Retrying for a few seconds
    rides out that window instead of falsely declaring the link failed (which showed
    up as a fresh, valid link reporting zero groups)."""
    deadline = time.monotonic() + timeout_s
    while True:
        acct = detect_account()
        if acct:
            return acct
        if time.monotonic() >= deadline:
            return None
        time.sleep(1.0)


def is_linked() -> bool:
    data = DATA_DIR / "data"
    return data.exists() and any(data.iterdir())


def link_is_broken() -> bool:
    """True only when link files exist on disk but signal-cli POSITIVELY reports no
    registered account — a link that died mid-provision, or this device was removed
    from the phone's Linked Devices. In that state every receive/listGroups/send
    fails with "User … is not registered" and only relinking helps. Any error or
    timeout returns False, so a transient JVM/network problem can never bounce a
    healthy install back to the link screen."""
    if not is_linked():
        return False
    try:
        binary = signal_cli_bin()
    except BroadcastError:
        return False
    try:
        proc = subprocess.run(_cli(binary, "--config", str(DATA_DIR), "-o", "json", "listAccounts"),
                              capture_output=True, text=True, errors="replace",
                              timeout=LISTGROUPS_TIMEOUT_S, env=_signal_env(binary))
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    try:
        accounts = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return False
    return not any(e.get("number") or e.get("account") for e in accounts)


def _request_sync(binary: str, account: str) -> None:
    """Best-effort nudge: ask the phone (primary) to (re)send contacts + groups.
    Ignored on failure — the phone usually pushes a sync on linking anyway."""
    try:
        subprocess.run(_cli(binary, "--config", str(DATA_DIR), "-a", account, "sendSyncRequest"),
                       capture_output=True, text=True, errors="replace",
                       timeout=LISTGROUPS_TIMEOUT_S, env=_signal_env(binary))
    except subprocess.TimeoutExpired:
        pass  # best-effort nudge; a network stall must not hang the sync


# signal-cli errors that will NEVER recover by retrying: the account isn't usable
# (link died mid-provision, or this device was removed from the phone). Looping the
# sync for the full SYNC_MAX_S against these is pointless — bail out and report.
ACCOUNT_UNUSABLE_PATTERN = re.compile(
    r"not registered|not a registered|unregistered|no account|account.*not found|"
    r"\b401\b|\b403\b|unauthor|authentication", re.IGNORECASE)


def sync_groups(account: str, on_log: LogFn = lambda *_: None) -> int:
    """Drain the phone's contacts/groups sync and (over)write groups.txt. A large
    account's groups arrive over several seconds, so nudge the phone then receive
    in short bursts until the count stops growing (or SYNC_MAX_S). Reports a running
    count so the wait is visibly progressing. Returns the final group count.

    Raises BroadcastError if we never once managed to read the group list — that is a
    real failure (signal-cli erroring on every call), and it must NOT be reported as
    "0 groups", which is indistinguishable from an account that simply has none. This
    surfaced the reported bug: a broken link / dead account churned for 60s and then
    silently showed zero groups with no reason. A successful listGroups that returns
    0 is a genuine empty account and returns 0 normally."""
    binary = signal_cli_bin()
    _request_sync(binary, account)
    on_log("Syncing your groups from your phone…")
    deadline = time.monotonic() + SYNC_MAX_S
    last, stable = -1, 0     # last == -1 means "no listGroups has EVER succeeded"
    last_error = ""
    while time.monotonic() < deadline and stable < SYNC_STABLE_ROUNDS:
        try:
            # --timeout is signal-cli's own burst cap; the outer subprocess timeout is
            # a hard kill-switch for a wedged JVM that ignores it (with a little slack).
            recv = subprocess.run(_cli(binary, "--config", str(DATA_DIR), "-a", account,
                                       "receive", "--timeout", str(SYNC_BURST_S)),
                                  capture_output=True, text=True, errors="replace",
                                  timeout=SYNC_BURST_S + 10, env=_signal_env(binary))
        except subprocess.TimeoutExpired:
            break  # a single hung burst must not block past SYNC_MAX_S
        if recv.returncode != 0:
            err = (recv.stderr or recv.stdout or "").strip()
            if err:
                last_error = err
            # A permanent account problem won't fix itself — stop churning and report.
            if ACCOUNT_UNUSABLE_PATTERN.search(err):
                break
        try:
            count = pull_groups(account)
        except BroadcastError as exc:
            last_error = str(exc)
            if ACCOUNT_UNUSABLE_PATTERN.search(last_error):
                break  # dead account / removed device — no point retrying for 60s
            continue   # transient fetch error — try another burst
        on_log(f"Syncing your groups from your phone… ({count} so far)")
        # Only settle on a non-zero count; while we still have nothing, keep
        # draining until the cap, since the phone's first sync can be slow.
        stable = stable + 1 if (count == last and count > 0) else 0
        last = count
    if last < 0:
        # Never read the list once — this is a failure, not an empty account. Report
        # the real reason so the UI can show it (and relink if the account is dead).
        raise BroadcastError(last_error.strip() or
                             "Couldn't reach signal-cli to sync your groups. Try again.")
    return last


def pull_groups(account: str) -> int:
    """Fetch the groups this number belongs to and (over)write groups.txt,
    preserving any groups you previously excluded. Returns the count written."""
    binary = signal_cli_bin()
    try:
        proc = subprocess.run(_cli(binary, "--config", str(DATA_DIR), "-o", "json", "-a", account, "listGroups"),
                              capture_output=True, text=True, errors="replace", timeout=LISTGROUPS_TIMEOUT_S,
                              env=_signal_env(binary))
    except subprocess.TimeoutExpired:
        raise BroadcastError("Timed out fetching groups. Check the connection and try again.")
    if proc.returncode != 0:
        raise BroadcastError("Could not fetch groups:\n" + (proc.stderr or proc.stdout))
    groups = json.loads(proc.stdout or "[]")
    was_disabled = {e.group_id for e in read_group_entries() if not e.enabled}
    lines = []
    for g in groups:
        if g.get("isMember", True) and not g.get("isBlocked", False):
            gid = g.get("id", "")
            name = (g.get("name") or "(no name)").replace("\t", " ").replace("\n", " ")
            if gid:
                row = f"{gid}\t{name}"
                lines.append(f"# {row}" if gid in was_disabled else row)
    GROUPS_FILE.write_text(_GROUPS_HEADER + "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return len(lines)


def unsendable_groups(account: str) -> set[str]:
    """Group ids the linked account CANNOT post to: announcement groups
    (permissionSendMessage == ONLY_ADMINS) where this account is a non-admin member.
    Used to skip those cleanly instead of letting the send fail. Best-effort — returns
    an empty set on any error, and only flags a group when we can positively confirm
    we're a non-admin member, so a quirk never wrongly skips a group you can post in."""
    try:
        binary = signal_cli_bin()
        proc = subprocess.run(
            _cli(binary, "--config", str(DATA_DIR), "-o", "json", "-a", account, "listGroups"),
            capture_output=True, text=True, errors="replace",
            timeout=LISTGROUPS_TIMEOUT_S, env=_signal_env(binary))
        if proc.returncode != 0:
            return set()
        groups = json.loads(proc.stdout or "[]")
    except (subprocess.SubprocessError, OSError, ValueError):
        return set()
    blocked: set[str] = set()
    for g in groups:
        gid = g.get("id")
        if not gid or g.get("permissionSendMessage") != "ONLY_ADMINS":
            continue
        me = next((m for m in (g.get("members") or []) if m.get("number") == account), None)
        if me is not None and not me.get("isAdmin"):
            blocked.add(gid)  # confirmed non-admin in an admin-only group
    return blocked


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #
def _send_one(binary: str, account: str, group_id: str,
              message: str, attachments: list[str]) -> tuple[bool, bool, str]:
    """Send to one group. Returns (ok, throttled, stderr)."""
    cmd = _cli(binary, "-a", account, "--config", str(DATA_DIR),
               "send", "-g", group_id, "-m", message)
    if attachments:
        cmd += ["-a", *attachments]
    try:
        # errors="replace": signal-cli can emit non-UTF-8 bytes (names, locale text)
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                              timeout=SEND_TIMEOUT_S, env=_signal_env(binary))
    except subprocess.TimeoutExpired:
        return False, False, f"timed out after {SEND_TIMEOUT_S}s"
    if proc.returncode == 0:
        return True, False, ""
    stderr = (proc.stderr or proc.stdout or "").strip()
    return False, bool(THROTTLE_PATTERN.search(stderr)), stderr


# Result tuple shared by the one-shot _send_one and the daemon's send: (ok, throttled, err).
SendFn = Callable[[str, str, list[str]], "tuple[bool, bool, str]"]


class SignalCliDaemon:
    """One long-lived `signal-cli jsonRpc` process for a whole broadcast. Avoids the
    ~1-2s JVM startup per group and keeps encryption sessions warm in memory, so
    sends (especially to big groups) speed up after the first. Speaks JSON-RPC 2.0
    over stdin/stdout, one JSON object per line; unsolicited notifications (incoming
    messages/receipts) are ignored. Start it AFTER any other signal-cli call for the
    account — it holds the account lock for its lifetime."""

    def __init__(self, account: str, start_timeout: float = 30.0) -> None:
        binary = signal_cli_bin()
        # --receive-mode manual: do NOT open the receive connection at startup. On a
        # slow or blocked network (e.g. behind a VPN) that startup connection stalls,
        # so the version probe below times out and we drop to the slower per-send
        # path. We only ever send, never receive, so we don't need it; signal-cli
        # still connects on demand for each send.
        cmd = _cli(binary, "-a", account, "--config", str(DATA_DIR),
                   "jsonRpc", "--receive-mode", "manual")
        # Capture stderr (was DEVNULL): when startup fails we need signal-cli's own
        # words — "account is already in use", an auth error, a connect failure —
        # instead of guessing. A reader thread drains it into a small ring buffer.
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, errors="replace", env=_signal_env(binary))
        self._lock = threading.Lock()
        # Serialises stdin writes only. Kept separate from _lock (which guards the
        # pending/timed-out/late maps) so a write can't block the reader thread's
        # _route. With parallel sending (concurrent_sends > 1) two send() calls can
        # _dispatch at once; without this their JSON lines could interleave on stdin
        # and corrupt the request stream.
        self._write_lock = threading.Lock()
        self._next_id = 0
        self._pending: dict[int, queue.Queue] = {}
        # A send that times out on our side may still complete: signal-cli answers
        # late. Instead of discarding that answer we keep listening — _timed_out holds
        # request ids we gave up waiting on, and _late captures their eventual reply so
        # send() can confirm the real outcome (see _wait_late).
        self._timed_out: set[int] = set()
        self._late: dict[int, dict] = {}
        self._stderr: collections.deque[str] = collections.deque(maxlen=50)
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._stderr_reader = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_reader.start()
        # Confirm the process is up and answering before we rely on it; if not, the
        # caller falls back to one-shot sends instead of hanging on every group. On
        # failure, surface what signal-cli printed so the real cause is diagnosable.
        try:
            self._request("version", {}, timeout=start_timeout)
        except BroadcastError as exc:
            detail = self.recent_stderr()
            self.close()
            msg = str(exc)
            if detail:
                msg += f" | signal-cli: {detail[-300:]}"
            raise BroadcastError(msg)

    def _drain_stderr(self) -> None:
        err = self._proc.stderr
        if err is None:
            return
        for line in err:
            line = line.strip()
            if line:
                self._stderr.append(line)

    def recent_stderr(self) -> str:
        return "\n".join(self._stderr)

    def _read_loop(self) -> None:
        out = self._proc.stdout
        if out is None:
            return
        for line in out:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            self._route(msg)

    def _route(self, msg: dict) -> None:
        """Deliver one parsed JSON-RPC message to its waiter. A reply whose id we'd
        stopped waiting on (a timed-out send) is kept in _late so send() can still
        confirm it; ids with no waiter (notifications) are ignored."""
        mid = msg.get("id")
        if mid is None:
            return  # notification (incoming message/receipt) — not our response
        with self._lock:
            if mid in self._timed_out:
                # A send we'd given up waiting on just completed — keep its answer so
                # send() can turn "uncertain" into a real sent/failed verdict.
                self._late[mid] = msg
                self._timed_out.discard(mid)
                self._pending.pop(mid, None)
                return
            box = self._pending.pop(mid, None)
        if box is not None:
            box.put(msg)

    def is_running(self) -> bool:
        return self._proc.poll() is None

    def _dispatch(self, method: str, params: dict) -> tuple[int, queue.Queue]:
        """Write a request and return (id, response-box) without waiting. Holding the
        box reference (rather than re-looking it up) avoids a race with _read_loop."""
        with self._lock:
            if self._proc.poll() is not None:
                raise BroadcastError("signal-cli daemon is not running")
            self._next_id += 1
            mid = self._next_id
            box: queue.Queue = queue.Queue(maxsize=1)
            self._pending[mid] = box
        line = json.dumps({"jsonrpc": "2.0", "method": method,
                           "params": params, "id": mid}) + "\n"
        try:
            assert self._proc.stdin is not None
            with self._write_lock:  # one whole request line at a time — no interleaving
                self._proc.stdin.write(line)
                self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            with self._lock:
                self._pending.pop(mid, None)
            # The write failed AFTER we started sending the request line — the bytes may
            # already have reached signal-cli before the pipe broke, so the send may have
            # dispatched. Mark it uncertain (don't retry, don't resend), unlike the
            # pre-write "not running" check above which proves nothing left us.
            raise BroadcastError(f"daemon send may have dispatched (write failed): {exc}")
        return mid, box

    def _await(self, mid: int, box: queue.Queue, timeout: float, keep_listening: bool) -> dict:
        """Wait for request ``mid``'s response. On timeout, if ``keep_listening`` we
        leave it registered so _read_loop captures a late reply (for await_late);
        otherwise we drop it (the startup probe, where a late answer is useless)."""
        try:
            return box.get(timeout=timeout)
        except queue.Empty:
            with self._lock:
                if keep_listening:
                    self._timed_out.add(mid)
                else:
                    self._pending.pop(mid, None)
            raise BroadcastError(f"daemon timed out after {timeout:.0f}s")

    def _request(self, method: str, params: dict, timeout: float) -> dict:
        """Dispatch + wait, giving up cleanly on timeout (no late capture)."""
        mid, box = self._dispatch(method, params)
        return self._await(mid, box, timeout, keep_listening=False)

    def send(self, group_id: str, message: str, attachments: list[str]) -> tuple[bool, bool, str]:
        """Send to one group. Same (ok, throttled, err) shape as the one-shot _send_one.
        If the send times out, signal-cli is probably still finishing it — wait a
        bounded grace (CONFIRM_GRACE_S) for its late reply and return the REAL verdict
        rather than guessing 'uncertain'. No new request is issued during that wait, so
        only one send is ever in flight on the account; only a send that never answers
        at all stays uncertain."""
        params: dict = {"groupId": group_id, "message": message}
        if attachments:
            params["attachment"] = list(attachments)
        try:
            mid, box = self._dispatch("send", params)
        except BroadcastError as exc:
            return False, False, str(exc)
        try:
            resp = self._await(mid, box, SEND_TIMEOUT_S, keep_listening=True)
        except BroadcastError as exc:
            if not CLIENT_TIMEOUT_PATTERN.search(str(exc)):
                return False, False, str(exc)  # e.g. a broken write — nothing to confirm
            late = self._wait_late(mid, CONFIRM_GRACE_S)
            if late is not None:
                return self._parse_send_response(late)
            return False, False, str(exc)      # never confirmed — genuinely uncertain
        return self._parse_send_response(resp)

    @staticmethod
    def _parse_send_response(resp: dict) -> tuple[bool, bool, str]:
        err = resp.get("error")
        if err:
            text = str(err.get("message", "")).strip() or "send failed"
            return False, bool(THROTTLE_PATTERN.search(text)), text
        return True, False, ""

    def _wait_late(self, mid: int, grace: float) -> "dict | None":
        """After request ``mid`` timed out, wait up to ``grace`` seconds for signal-cli's
        late reply (captured into _late by _route). Returns the reply, or None if it
        never arrives (the daemon is genuinely stuck)."""
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            with self._lock:
                resp = self._late.pop(mid, None)
            if resp is not None:
                return resp
            time.sleep(0.2)
        with self._lock:
            self._timed_out.discard(mid)   # stop listening; a later reply is ignored
        return None

    def close(self) -> None:
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
        except OSError:
            pass
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()


def _throttle_wait(attempt: int, stderr: str) -> float:
    """Exponential backoff, but honour an explicit retry-after if larger.
    ``attempt`` is 1-based (1 = first retry)."""
    backoff = min(THROTTLE_BACKOFF_CAP_S, THROTTLE_BACKOFF_BASE_S * (2 ** (attempt - 1)))
    hinted = RETRY_AFTER_PATTERN.search(stderr)
    return max(backoff, float(hinted.group(1))) if hinted else backoff


def _interruptible_sleep(seconds: float, should_stop: StopFn) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not should_stop():
        time.sleep(min(0.25, deadline - time.monotonic()))


# Posting to an announcement group as a non-admin: a permanent "you can't post here"
# condition, never worth retrying. We normally catch these up front (unsendable_groups),
# but this is the safety net for when that pre-check couldn't run.
ADMIN_ONLY_PATTERN = re.compile(
    r"only admins|only administrators|announcement group|not allowed to send|"
    r"sending is restricted|admins?[\s_-]*only", re.I)

# Map signal-cli's error text to a short, PII-safe reason. We never log the raw
# text (it can contain a group id or recipient number) — only the category here.
_ERROR_CATEGORIES = [
    (ADMIN_ONLY_PATTERN, "admin-only group (you can't post here)"),
    (re.compile(r"timed?\s*out|timeout|connection|unreachable|unknownhost|refused|"
                r"no route to host|noroutetohost|ssl|certificate|\bcdn\b|\bdns\b|"
                r"socket|io\s*exception|broken pipe|reset by peer|end of stream|"
                r"failed to get response", re.I),
     "network or connection problem"),
    (re.compile(r"attachment|upload|file too large|too large", re.I), "attachment or upload problem"),
    (re.compile(r"untrusted identity|unregistered|not registered|invalid number", re.I),
     "recipient or identity problem"),
    (re.compile(r"\b401\b|\b403\b|unauthor|forbidden", re.I), "authorisation problem"),
]


def classify_error(stderr: str) -> str:
    """A short, PII-safe reason for a send failure — never the raw text."""
    if THROTTLE_PATTERN.search(stderr):
        return "rate limited"
    for pattern, label in _ERROR_CATEGORIES:
        if pattern.search(stderr):
            return label
    return "unknown error"


# Our own client-side timeout strings — the daemon read-timeout ("daemon timed out
# after 120s") and the one-shot subprocess timeout ("timed out after 120s"). Hitting
# these means we DISPATCHED the send but never got signal-cli's verdict, so the
# message may well have gone out. We must not auto-retry (that risks a duplicate)
# and must not mark it a clean failure (that would feed it to "Resend failed").
CLIENT_TIMEOUT_PATTERN = re.compile(r"timed out after \d+\s*s", re.I)

# Daemon errors meaning the request MAY have been dispatched before we lost contact
# (a write that broke after the line went out). Same "don't retry, don't call it
# failed" treatment as a client-side timeout — re-sending could duplicate the message.
DAEMON_UNCERTAIN_PATTERN = re.compile(r"may have dispatched", re.I)

# signal-cli reported the send FAILED because the request went out but no response came
# back ("Failed to get response for request" / "no response received"). The server may
# already have accepted the message before the socket dropped — so, exactly like a
# timeout, it MAY have been delivered. We trust this over signal-cli's "failed" verdict
# because the failure was in getting the *reply*, not in sending. Don't retry, don't
# resend (either could duplicate); flag it for a manual check instead.
SENT_NO_REPLY_PATTERN = re.compile(r"failed to get response|no response (?:received|for)", re.I)


def _is_uncertain_send(err: str) -> bool:
    """True if a send error means the message may already have gone out — a client
    timeout, a post-write daemon failure, or a request that was sent but never got a
    response. Such a group must never be retried or resent (it could duplicate)."""
    return bool(CLIENT_TIMEOUT_PATTERN.search(err)
                or DAEMON_UNCERTAIN_PATTERN.search(err)
                or SENT_NO_REPLY_PATTERN.search(err))


def _deliver_to_group(send_one: SendFn, group_id: str,
                      message: str, attachments: list[str], max_retries: int,
                      on_log: LogFn, should_stop: StopFn, debug: bool = False) -> tuple[str, str]:
    """Try one group with retries via ``send_one`` (one-shot or daemon — same shape).
    Returns (status, reason): status is "sent", "failed", or "uncertain"; reason is a
    short, PII-safe category for non-sent outcomes ("" when sent) used for the run's
    failure breakdown. "uncertain" is a client-side timeout: we don't know whether it
    delivered, so we neither retry nor call it failed. Throttled sends back off
    exponentially; other clean errors get a couple of quick retries. Log lines carry no
    group name, id, or raw signal-cli output — only counts, retry timing, and a category."""
    throttle_attempt = 0
    quick_attempt = 0
    while not should_stop():
        ok, throttled, err = send_one(group_id, message, attachments)
        if ok:
            return "sent", ""
        if debug and err:
            append_debug(f"group {group_id} (throttled={throttled}): {err}")
        if _is_uncertain_send(err):
            # We lost contact before signal-cli confirmed — it may have delivered.
            on_log("Send unconfirmed — it may have gone through; not retrying, to avoid a duplicate.")
            return "uncertain", "timed out — may have sent"
        if throttled:
            throttle_attempt += 1
            if throttle_attempt > max_retries:
                on_log(f"Gave up after {max_retries} throttled retries")
                return "failed", "rate limited"
            wait = _throttle_wait(throttle_attempt, err)  # err parsed for retry-after, never logged
            on_log(f"Throttled — backing off {wait:.0f}s (retry {throttle_attempt}/{max_retries})")
            _interruptible_sleep(wait, should_stop)
        elif ADMIN_ONLY_PATTERN.search(err):
            # Non-admin in an announcement group — retrying can never succeed.
            on_log("Send failed — admin-only group (you can't post here).")
            return "failed", "admin-only group (you can't post here)"
        else:
            quick_attempt += 1
            reason = classify_error(err)
            if quick_attempt > NON_THROTTLE_RETRIES:
                on_log(f"Send failed — {reason}.")
                return "failed", reason
            on_log(f"Send error ({reason}) — retrying in {NON_THROTTLE_WAIT_S:.0f}s")
            _interruptible_sleep(NON_THROTTLE_WAIT_S, should_stop)
    return "failed", "stopped before sending"


def _pace_delay(base: float, jitter: float) -> float:
    # Randomised gap, but clamped to a hard floor so no config can ever burst.
    return max(MIN_DELAY_S, base + random.uniform(-jitter, jitter))


def _start_daemon(account: str, on_log: LogFn, debug: bool) -> "SignalCliDaemon | None":
    """Start the jsonRpc daemon. If it won't start, the usual cause is a leftover
    signal-cli from a crashed/force-quit run still holding the account lock; we hold
    the send lock, so any such process is an orphan — clear it and try once more.
    Returns None only if it still won't start (caller then falls back to per-send)."""
    try:
        return SignalCliDaemon(account)
    except (BroadcastError, OSError) as exc:
        detail = str(exc)
        if debug:
            append_debug(f"daemon start failed: {detail}")
        if _reap_orphan_signal_cli(on_log):
            try:
                return SignalCliDaemon(account)
            except (BroadcastError, OSError) as exc2:
                detail = str(exc2)
                if debug:
                    append_debug(f"daemon start failed after cleanup: {detail}")
        on_log(f"Running signal-cli per send (daemon unavailable: {detail}).")
        return None


def missing_attachments(attachments: list[str]) -> list[str]:
    """Attachment paths that no longer point at a real file. Checked before a run so
    a moved/deleted image fails fast once, instead of failing every single group."""
    return [a for a in attachments if not Path(a).is_file()]


# Signal hosts a preflight probe tries (any one reachable = OK). Hardcoded but stable.
_SIGNAL_HOSTS = (("chat.signal.org", 443), ("cdn.signal.org", 443), ("cdn2.signal.org", 443))


def check_signal_reachable(timeout: float = 3.0) -> str | None:
    """Best-effort: can this machine open a TCP connection to Signal? Returns None if
    reachable, else a short reason. Forces IPv4 (AF_INET) to mirror signal-cli's
    preferIPv4Stack, so a machine with broken IPv6 isn't wrongly flagged. Only a total
    inability to reach ANY host counts — a cert/TLS issue doesn't (we just open a
    socket; signal-cli trusts Signal's pinned cert)."""
    last = ""
    for host, port in _SIGNAL_HOSTS:
        try:
            infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
        except OSError:
            last = f"can't resolve {host}"
            continue
        for *_, sockaddr in infos:
            ip, prt = sockaddr[0], sockaddr[1]  # AF_INET -> (ip, port)
            try:
                socket.create_connection((ip, prt), timeout=timeout).close()
                return None  # reached at least one Signal host
            except OSError as exc:
                last = str(exc)
    return ("Couldn't reach Signal's servers — check the internet connection and turn "
            "off any VPN" + (f" ({last})" if last else "") + ".")


def broadcast(*, config: Config, groups: list[tuple[str, str]], message: str,
              attachments: list[str], base_delay: float | None = None,
              on_log: LogFn = lambda *_: None,
              on_progress: ProgressFn = lambda *_: None,
              on_group_start: StartFn = lambda *_: None,
              should_stop: StopFn = lambda: False) -> list[GroupSendResult]:
    """Send ``message`` (+ attachments) to every group, slowly. Returns a result
    per attempted group. Honours ``should_stop`` between and during sends."""
    binary = signal_cli_bin()
    delay = base_delay if base_delay is not None else config.base_delay_seconds
    results: list[GroupSendResult] = []

    # Guarantee at most ONE send per group, even if the list has duplicates — a group
    # listed twice (e.g. two enabled lines in groups.txt) would otherwise be delivered
    # to twice in a single run. This is the single enforcement point for every caller.
    seen: set[str] = set()
    deduped: list[tuple[str, str]] = []
    for gid, name in groups:
        if gid not in seen:
            seen.add(gid)
            deduped.append((gid, name))
    if len(deduped) != len(groups):
        on_log(f"Ignoring {len(groups) - len(deduped)} duplicate group(s) in the list.")
    groups = deduped
    total = len(groups)

    # Preflight: a moved/deleted image would otherwise fail every group, so fail fast
    # and clearly before sending anything (and before taking the lock).
    missing = missing_attachments(attachments)
    if missing:
        raise BroadcastError("These attachment files are missing — re-add or remove "
                             "them before sending:\n  " + "\n  ".join(missing))

    # One broadcast at a time: a second app window — or the scheduler firing while
    # the app is mid-send — would fight over signal-cli's account lock and both
    # stall. send_lock() raises BroadcastError if a send is already running.
    with send_lock():
        # Soft preflight: warn (don't block — the probe could be wrong) if we can't
        # reach Signal, so the user gets an early heads-up instead of N timeouts.
        unreachable = check_signal_reachable()
        if unreachable:
            on_log(f"⚠ {unreachable} Trying anyway — sends may time out; Stop if needed.")
        # Admin-only (announcement) groups you can't post in: skip them up front rather
        # than burning a doomed send + retries on each. Best-effort; empty on any error.
        # NOTE: run this BEFORE starting the daemon — both want the account lock.
        blocked = unsendable_groups(config.account)
        if blocked:
            n = sum(1 for gid, _ in groups if gid in blocked)
            if n:
                on_log(f"{n} selected group(s) are admin-only — you can't post there; skipping them.")

        # Record the run so a crash mid-broadcast doesn't lose track of what was
        # already sent (cleared in the finally on a normal return — see below).
        begin_run_progress(groups, message_fingerprint(message, attachments))

        # Keep one signal-cli process alive for the whole run: no per-group JVM
        # startup, warm encryption sessions. Clears a stale-lock orphan and retries
        # once; falls back to one-shot sends only if it still can't start.
        daemon = _start_daemon(config.account, on_log, config.debug)

        def send_one(gid: str, msg: str, atts: list[str]) -> tuple[bool, bool, str]:
            nonlocal daemon
            if daemon is None:
                return _send_one(binary, config.account, gid, msg, atts)
            res = daemon.send(gid, msg, atts)
            ok, err = res[0], res[2]
            if ok:
                return res
            uncertain = _is_uncertain_send(err)
            # Retire the daemon — and switch the rest of the run to one-shot — when:
            #  * the send is UNCERTAIN (timed out even after waiting for a late reply
            #    inside daemon.send(), or the write broke). The daemon may STILL be
            #    processing the request we gave up on, so reusing it would run two sends
            #    at once on one account — the contention these fixes exist to prevent.
            #    Retire it and do NOT re-send this group (the caller marks it uncertain).
            #  * the daemon has DIED. It can't send anything more; if the request
            #    provably never left us, re-sending this one group one-shot is safe.
            if uncertain or not daemon.is_running():
                try:
                    daemon.close()
                finally:
                    daemon = None
                if uncertain:
                    on_log("signal-cli send was abandoned mid-flight; per-send for the rest.")
                    return res
                on_log("signal-cli daemon stopped; falling back to per-send.")
                return _send_one(binary, config.account, gid, msg, atts)
            return res

        # Parallelism: one at a time (1) unless the user opted in AND the daemon is up.
        # The per-send fallback can't be parallelised — concurrent one-shot processes
        # would each grab signal-cli's account lock and stall — so without a daemon we
        # always run strictly sequentially.
        K = config.concurrent_sends if daemon is not None else 1

        def record(gid: str, name: str, status: str, reason: str = "") -> None:
            """Append one finished group's result. 'uncertain' (a timed-out send that
            may have delivered) is kept out of failures so it's never auto-resent. The
            PII-safe ``reason`` rides along on non-sent results for the run breakdown."""
            if status == "uncertain":
                results.append(GroupSendResult(gid, name, ok=False, uncertain=True,
                                               reason=reason or "may have sent (timed out)"))
            else:
                results.append(GroupSendResult(gid, name, ok=(status == "sent"),
                                               reason="" if status == "sent" else reason))

        def run_parallel() -> None:
            """Up to K whole-group sends in flight at once on the one account. New sends
            launch no faster than the pacing gap (so the RATE of new sends matches the
            sequential path), but their fan-out may overlap. Exactly-once holds — each
            group is pulled from the queue by exactly one worker. Always via the daemon
            (parallel one-shots would deadlock on the account lock). If the daemon dies,
            the remaining sends fail cleanly (the request never left us) — resendable,
            not duplicated."""
            assert daemon is not None                   # K >= 2 only when the daemon is up
            send_fn = daemon.send
            prog_lock = threading.Lock()                # guards results + the progress file
            launch_lock = threading.Lock()              # paces the launch of NEW sends
            next_launch = [0.0]

            def advance(pos: int, name: str, status: str, secs: float) -> None:
                # pos = the group's STABLE 1-based position in the run, so each log line
                # maps to a specific group by order even though sends finish out of order
                # under concurrency. (The progress BAR is driven by the front-end's own
                # completion counter, which stays monotonic.)
                on_progress(pos, total, name, status, secs)

            # Every group — sendable AND blocked — goes on the queue in list order,
            # tagged with its stable position, so admin-only groups surface at their
            # natural place in the timeline instead of all bunched at the start.
            work: queue.Queue = queue.Queue()
            for pos, (gid, name) in enumerate(groups, start=1):
                work.put((pos, gid, name))

            def worker() -> None:
                while not should_stop():
                    try:
                        pos, gid, name = work.get_nowait()
                    except queue.Empty:
                        return
                    if gid in blocked:
                        # Admin-only: no send, no pacing — record in place and move on.
                        with prog_lock:
                            results.append(GroupSendResult(gid, name, ok=False,
                                                           skipped=True, reason="admin-only"))
                        advance(pos, name, "skipped", 0.0)
                        continue
                    # Reserve a paced launch slot, then release the lock BEFORE sleeping
                    # so other workers aren't blocked while this one waits out its gap.
                    with launch_lock:
                        start_at = max(time.monotonic(), next_launch[0])
                        next_launch[0] = start_at + _pace_delay(delay, config.jitter_seconds)
                    wait = start_at - time.monotonic()
                    if wait > 0:
                        _interruptible_sleep(wait, should_stop)
                    if should_stop():
                        return
                    with prog_lock:
                        record_group_progress(gid, "attempting")
                    on_group_start(pos, name)  # now in flight — show it in the live view
                    t0 = time.monotonic()
                    status, reason = _deliver_to_group(send_fn, gid, message, attachments,
                                                       config.max_retries, on_log, should_stop,
                                                       config.debug)
                    secs = time.monotonic() - t0
                    with prog_lock:
                        record(gid, name, status, reason)
                        record_group_progress(gid, status)  # persisted now — crash-recoverable
                    advance(pos, name, status, secs)

            workers = [threading.Thread(target=worker, daemon=True) for _ in range(K)]
            for w in workers:
                w.start()
            for w in workers:
                w.join()

        try:
            on_log(f"Broadcasting to {total} groups | {len(attachments)} attachment(s) | "
                   f"~{delay:.0f}s minimum between sends"
                   + (f" | up to {K} at once" if K >= 2 else ""))
            if K >= 2:
                run_parallel()
            else:
                for i, (gid, name) in enumerate(groups, start=1):
                    if should_stop():
                        on_log("Stopped.")
                        break
                    if gid in blocked:
                        results.append(GroupSendResult(gid, name, ok=False, skipped=True, reason="admin-only"))
                        on_progress(i, total, name, "skipped", 0.0)  # not a failure — never attempted
                        continue  # no send, no pacing delay — nothing left the machine
                    # Mark the group "attempting" BEFORE the send leaves the machine. If the
                    # process is killed (station-mode unplug, force-quit) in the window between
                    # a successful send and recording it, the marker stays "attempting" and a
                    # resume treats it as uncertain — never auto-resending a message that may
                    # already have gone out. Overwritten with the real status below.
                    record_group_progress(gid, "attempting")
                    on_group_start(i, name)  # now in flight — show it in the live view
                    t0 = time.monotonic()
                    status, reason = _deliver_to_group(send_one, gid, message, attachments,
                                                       config.max_retries, on_log, should_stop, config.debug)
                    secs = time.monotonic() - t0  # wall time for this group (includes any retries)
                    record(gid, name, status, reason)
                    record_group_progress(gid, status)  # persisted now, so a crash here is recoverable
                    on_progress(i, total, name, status, secs)
                    if i < total and not should_stop():
                        # Adaptive pacing: the gap is a MINIMUM interval between sends, and the
                        # time the send already took counts toward it. A send that took longer
                        # than the target has already spaced itself out, so the next one goes
                        # immediately; a fast send waits out only the remainder.
                        wait = max(0.0, _pace_delay(delay, config.jitter_seconds) - secs)
                        if wait > 0:
                            _interruptible_sleep(wait, should_stop)
        finally:
            if daemon is not None:
                daemon.close()
        # Reached only on a normal return (completed or stopped) — NOT if the loop
        # raised, in which case the caller has no results and we keep the marker so a
        # resume is still possible. A surviving file therefore means the run was
        # killed or aborted by an error before finishing.
        clear_run_progress()
    return results


def failure_breakdown(results: list[GroupSendResult]) -> str:
    """A short, PII-safe summary of why sends failed this run, e.g.
    "2 network or connection problem, 1 rate limited" — counts by category only, no
    group names or ids. "" when nothing failed. Used in the activity log so a big run's
    failures are diagnosable without the old (useless, gibberish) failures-*.txt file."""
    counts: dict[str, int] = {}
    for r in results:
        if not r.ok and not r.skipped and not r.uncertain:
            counts[r.reason or "unknown error"] = counts.get(r.reason or "unknown error", 0) + 1
    return ", ".join(f"{n} {cat}" for cat, n in
                     sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def write_run_summary(results: list[GroupSendResult]) -> None:
    """Record a counts-only summary of the last broadcast for the UI — no group
    names, ids, or message text, just totals. Lives in logs/ so it's wiped with
    everything else on unlink (incl. a station-mode trip) and never committed."""
    if not results:
        return
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    skipped = sum(1 for r in results if r.skipped)
    uncertain = sum(1 for r in results if r.uncertain)
    failed = sum(1 for r in results if not r.ok and not r.skipped and not r.uncertain)
    summary = {"at": datetime.now().isoformat(timespec="seconds"),
               "total": len(results), "sent": sum(1 for r in results if r.ok),
               "failed": failed, "skipped": skipped, "uncertain": uncertain}
    LAST_SEND_FILE.write_text(json.dumps(summary), encoding="utf-8")


def read_run_summary() -> RunSummary | None:
    if not LAST_SEND_FILE.exists():
        return None
    try:
        d = json.loads(LAST_SEND_FILE.read_text(encoding="utf-8"))
        return RunSummary(at=str(d["at"]), total=int(d["total"]),
                          sent=int(d["sent"]), failed=int(d["failed"]),
                          skipped=int(d.get("skipped", 0)),
                          uncertain=int(d.get("uncertain", 0)))
    except (ValueError, KeyError):
        return None


# --------------------------------------------------------------------------- #
# Crash-safe run progress: persisted per group so a kill/crash mid-broadcast
# doesn't lose track of what was already sent (which would re-send on the next
# run). Cleared when broadcast() returns normally — so a surviving file means the
# process died mid-run. group ids only (needed to resume); never message text.
# --------------------------------------------------------------------------- #
def message_fingerprint(message: str, attachments: list[str]) -> str:
    """A short, content-derived id so a resume can tell it's the same payload. Not
    the text itself — just a hash, so it's safe to keep in logs/."""
    h = hashlib.sha256()
    h.update(message.encode("utf-8", "replace"))
    for a in attachments:
        h.update(b"\0")
        h.update(a.encode("utf-8", "replace"))
    return h.hexdigest()[:16]


def begin_run_progress(groups: list[tuple[str, str]], fingerprint: str) -> None:
    # PII-safe on disk: store opaque group ids only — NO group names and NO message
    # text (just the fingerprint hash). The ids are unavoidable: after a crash + app
    # restart they're the only record of which groups the run covered, so resume needs
    # them to finish the exact same groups (guessing by position could resend to the
    # wrong one). Cleared on a normal finish, so a surviving file means the run died.
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    data = {"at": datetime.now().isoformat(timespec="seconds"), "fp": fingerprint,
            "groups": [g for g, _ in groups], "done": {}}
    RUN_PROGRESS_FILE.write_text(json.dumps(data), encoding="utf-8")


def record_group_progress(group_id: str, status: str) -> None:
    """Mark one group's outcome ("sent"/"failed"/"uncertain"/"skipped"). Rewritten
    after every group so a crash leaves an accurate record. Best-effort."""
    try:
        data = json.loads(RUN_PROGRESS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    data.setdefault("done", {})[group_id] = status
    try:
        RUN_PROGRESS_FILE.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def clear_run_progress() -> None:
    RUN_PROGRESS_FILE.unlink(missing_ok=True)


def clear_run_progress_if_idle() -> bool:
    """Clear the resume marker, but ONLY while no send is running (we can take the
    send lock). The marker is a single account-global file; if another process (a
    second window, or the scheduler firing while the app is mid-send) holds the lock,
    the marker belongs to that LIVE run and clearing it would destroy its crash-resume
    record. Returns True if cleared (lock was free), False if a send is in progress."""
    try:
        with send_lock():
            clear_run_progress()
            return True
    except BroadcastError:
        return False


def read_interrupted_run() -> InterruptedRun | None:
    """If a previous broadcast was interrupted (process died mid-run), describe what's
    left to send. Only groups that were never attempted or were a CLEAN failure are
    resumable. A group recorded "attempting" (the run was killed AFTER the send was
    dispatched but BEFORE its outcome was recorded) or "uncertain"/"sent"/"skipped" is
    NEVER auto-resent — re-sending could duplicate a message that already went out.
    "attempting"/"uncertain" groups are reported separately so the operator can check
    Signal and decide. Returns None only when there's nothing left to resume OR flag."""
    if not RUN_PROGRESS_FILE.exists():
        return None
    try:
        data = json.loads(RUN_PROGRESS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    # "groups" is a flat list of opaque ids (no names on disk); names aren't needed to
    # resume — the resend is keyed by id, and the UI shows counts, not names.
    gids = [str(g) for g in data.get("groups", [])]
    done = data.get("done", {})
    remaining = [(g, "") for g in gids if done.get(g) in (None, "failed")]
    # "attempting" = killed mid-send; "uncertain" = timed out. Both may have delivered.
    uncertain = [(g, "") for g in gids if done.get(g) in ("attempting", "uncertain")]
    if not gids or (not remaining and not uncertain):
        return None
    return InterruptedRun(fingerprint=str(data.get("fp", "")), total=len(gids),
                          done=len(gids) - len(remaining), remaining=remaining,
                          uncertain=uncertain)


def append_activity(line: str) -> None:
    """Append a PII-safe activity line to today's plain-text log so a send can be
    reviewed after the window is closed. Lives in logs/, so it's erased on unlink —
    including a station-mode unplug. Callers pass only safe text (counts, error
    categories), never group names/ids, numbers, message text, or raw output."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    out = LOGS_DIR / f"activity-{datetime.now():%Y-%m-%d}.txt"
    with out.open("a", encoding="utf-8") as fh:
        fh.write(f"{datetime.now():%H:%M:%S}  {line}\n")


def append_debug(line: str) -> None:
    """Append raw signal-cli output to a debug log for troubleshooting — only when
    config.toml has debug = true. Unlike the activity log this CAN contain group ids
    or numbers, which is why it's opt-in; it lives in logs/, so unlink and a
    station-mode unplug still erase it."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    out = LOGS_DIR / f"debug-{datetime.now():%Y-%m-%d}.txt"
    with out.open("a", encoding="utf-8") as fh:
        fh.write(f"{datetime.now():%H:%M:%S}  {line}\n")


def clear_logs() -> None:
    """Delete everything in logs/ — activity logs, debug logs, the last-send summary,
    the last-run marker — keeping only the tracked .gitkeep. The Security tab's
    'Clear logs' button calls this; it does not touch the Signal link or your groups."""
    _clear_dir(LOGS_DIR, keep={".gitkeep"})


# --------------------------------------------------------------------------- #
# Power source
# --------------------------------------------------------------------------- #
def on_ac_power() -> bool:
    """True if the Mac is on AC (wall) power. On any read failure, assume AC — a
    transient glitch must never be the thing that triggers a wipe. Only macOS is
    queried (via pmset); the station-mode watcher that uses this is macOS-only, so on
    any other platform we simply assume AC and never shell out."""
    if not IS_DARWIN:
        return True
    try:
        r = subprocess.run(["pmset", "-g", "ps"], capture_output=True, text=True, timeout=5)
    except Exception:
        return True
    if r.returncode != 0:
        return True
    return "AC Power" in r.stdout


# --------------------------------------------------------------------------- #
# Daily schedule (macOS launchd)
# --------------------------------------------------------------------------- #
def parse_times(times: list[str]) -> list[dict]:
    """Validate 'HH:MM' strings into launchd {Hour, Minute} entries."""
    entries = []
    for raw in times:
        t = str(raw).strip()
        try:
            hh, mm = t.split(":")
            h, m = int(hh), int(mm)
            if not (0 <= h < 24 and 0 <= m < 60):
                raise ValueError
        except ValueError:
            raise BroadcastError(f"Invalid time: '{t}'. Use 24-hour HH:MM, e.g. 09:00.")
        entries.append({"Hour": h, "Minute": m})
    if not entries:
        raise BroadcastError("Add at least one send time.")
    return entries


# Tag on the crontab lines this app owns, so the Linux/Termux schedule scripts replace only
# their own entries and leave any other cron jobs alone.
CRON_TAG = "# signal-broadcast"


def format_cron_line(hour: int, minute: int, tag: str = CRON_TAG) -> str:
    """One crontab line that runs broadcast.py at HH:MM. Shared by webui.py and
    scripts/schedule-termux.sh so the command + log path never diverge between them."""
    return (f"{minute} {hour} * * * cd {PROJECT_DIR} && "
            f"/usr/bin/python3 broadcast.py >> logs/cron.log 2>&1  {tag}")


def build_plist(times: list[str], python_exe: str) -> dict:
    """launchd job: run broadcast.py at each time, wrapped in caffeinate so the
    Mac stays awake through the send.

    NOTE on missed runs: StartCalendarInterval fires on local wall-clock time. If the
    Mac is asleep or off at a scheduled time, launchd runs the job ONCE at the next
    wake — and COALESCES all slots missed during sleep into that single run. So a day's
    sends don't each fire late; at most one catch-up fires after a long sleep, and the
    cooldown gate (cooldown_hours) then suppresses any further catch-ups in that window.
    A send can therefore land at a different local time than scheduled after a long
    sleep. This is intended (better one late send than none); if you need strict
    "skip if the window was missed" behaviour, gate it in broadcast.run() on the
    current time vs send_times. Times are also re-interpreted after a DST change."""
    return {
        "Label": SCHEDULE_LABEL,
        "ProgramArguments": ["/usr/bin/caffeinate", "-i",
                             python_exe, str(PROJECT_DIR / "broadcast.py")],
        "WorkingDirectory": str(PROJECT_DIR),
        "StartCalendarInterval": parse_times(times),
        # launchd's default PATH omits Homebrew; give the job signal-cli + qrencode.
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
        "RunAtLoad": False,
        "StandardOutPath": str(LOGS_DIR / "launchd.out"),
        "StandardErrorPath": str(LOGS_DIR / "launchd.err"),
    }


def write_plist(times: list[str], python_exe: str | None = None, dest: Path = LOCAL_PLIST) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as fh:
        plistlib.dump(build_plist(times, python_exe or sys.executable), fh)
    return dest


# launchd is macOS-only. These are called by the Tkinter GUI (macOS) and by unlink().
# On any other platform they must be safe no-ops — scheduling on Linux/Termux is cron,
# set up via scripts/schedule-termux.sh or the web UI's Schedule tab — because launchctl
# doesn't exist there and would otherwise crash (e.g. unlink() calls disable_schedule()).
def schedule_enabled() -> bool:
    if not IS_DARWIN:
        return False
    r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{SCHEDULE_LABEL}"],
                       capture_output=True, text=True)
    return r.returncode == 0


def enable_schedule(times: list[str], python_exe: str | None = None) -> None:
    if not IS_DARWIN:
        raise BroadcastError("On this platform scheduling uses cron, not launchd — use "
                             "the Schedule tab or scripts/schedule-termux.sh.")
    parse_times(times)  # validate before touching anything
    write_plist(times, python_exe, dest=INSTALLED_PLIST)
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{SCHEDULE_LABEL}"], capture_output=True)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(INSTALLED_PLIST)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise BroadcastError("Could not turn on the schedule:\n" + (r.stderr or r.stdout).strip())


def disable_schedule() -> None:
    if not IS_DARWIN:
        return  # no launchd here; cron is managed separately (web UI / schedule-termux.sh)
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{SCHEDULE_LABEL}"], capture_output=True)
    INSTALLED_PLIST.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Station-mode watcher (macOS launchd) — wipe on unplug
# --------------------------------------------------------------------------- #
def build_watcher_plist(python_exe: str) -> dict:
    """launchd job that keeps watcher.py running whenever you're logged in,
    restarting it if it ever exits. The watcher wipes the app's data on unplug."""
    return {
        "Label": WATCHER_LABEL,
        "ProgramArguments": [python_exe, str(PROJECT_DIR / "watcher.py")],
        "WorkingDirectory": str(PROJECT_DIR),
        "RunAtLoad": True,
        "KeepAlive": True,
        # launchd's default PATH omits Homebrew; give the wipe pmset + caffeinate.
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
        "StandardOutPath": str(LOGS_DIR / "watcher.out"),
        "StandardErrorPath": str(LOGS_DIR / "watcher.err"),
    }


def enable_watcher(python_exe: str | None = None) -> None:
    if not IS_DARWIN:
        raise BroadcastError("Station mode (wipe-on-unplug) is macOS-only.")
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    WATCHER_PLIST.parent.mkdir(parents=True, exist_ok=True)
    with WATCHER_PLIST.open("wb") as fh:
        plistlib.dump(build_watcher_plist(python_exe or sys.executable), fh)
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{WATCHER_LABEL}"], capture_output=True)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(WATCHER_PLIST)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise BroadcastError("Could not arm station mode:\n" + (r.stderr or r.stdout).strip())


def disable_watcher() -> None:
    if not IS_DARWIN:
        return
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{WATCHER_LABEL}"], capture_output=True)
    WATCHER_PLIST.unlink(missing_ok=True)


def watcher_enabled() -> bool:
    if not IS_DARWIN:
        return False
    r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{WATCHER_LABEL}"],
                       capture_output=True, text=True)
    return r.returncode == 0


# --------------------------------------------------------------------------- #
# Unlink + wipe (security: leave nothing behind on a borrowed Mac)
# --------------------------------------------------------------------------- #
def _clear_dir(path: Path, keep: frozenset[str] | set[str] = frozenset()) -> None:
    if not path.exists():
        return
    for item in path.iterdir():
        if item.name in keep:
            continue
        if item.is_dir():
            shutil.rmtree(item, ignore_errors=True)
        else:
            item.unlink(missing_ok=True)


def _delete_listed_attachments(path: Path = ATTACHMENTS_FILE) -> None:
    """Delete the original image files that attachments.txt points at — the user's own
    files, wherever they picked them (Desktop, Downloads, …). Part of the privacy wipe:
    the app holds only paths, not copies, so without this the images survive a wipe.
    Reads the raw paths (not read_attachments, which raises on a missing file) and is
    best-effort: a file already gone or undeletable never blocks the wipe."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        p = Path(line)
        if not p.is_absolute():
            p = PROJECT_DIR / p
        try:
            if p.is_file():
                p.unlink(missing_ok=True)
        except OSError:
            pass  # best-effort — a wipe must always finish


def unlink() -> None:
    """Sign this Mac out of Signal and erase every local trace: the link keys and
    signal-cli's cached groups/contacts, the group list, the message, the attached
    image files, the schedule, and any logs. Leaves no personal data behind — use
    before handing the Mac to someone else. The phone (primary device) is untouched;
    to also drop this device from the phone, remove it under Signal → Linked Devices."""
    disable_schedule()
    LOCAL_PLIST.unlink(missing_ok=True)
    shutil.rmtree(DATA_DIR, ignore_errors=True)        # link keys + account.db cache
    # Delete the original attached images before dropping the list that points at them.
    _delete_listed_attachments()
    # Delete config.toml outright — it holds the number — then recreate a fresh
    # placeholder from the template, so no local copy of the number survives.
    for f in (GROUPS_FILE, MESSAGE_FILE, ATTACHMENTS_FILE, CONFIG_FILE):
        f.unlink(missing_ok=True)
    _clear_dir(LOGS_DIR, keep={".gitkeep"})
    ensure_config()
