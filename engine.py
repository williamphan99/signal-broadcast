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
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterator

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
APP_VERSION = "1.9.10"


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
SEND_TIMEOUT_S = 300             # per-send ceiling. Big groups legitimately take
                                 # 90-120s+ to fan out to every member; 120s cut
                                 # those off and wrongly marked them "uncertain". 5
                                 # min leaves headroom (incl. signal-cli's own throttle
                                 # retries) while still bounding a genuinely stuck send.
# First-sync after linking. A big account's groups don't all arrive in one
# receive, so we drain in short bursts until the count stops growing (or the cap).
SYNC_BURST_S = 5                 # one receive burst while draining the phone's sync
SYNC_MAX_S = 60                  # overall cap — large accounts (100+ groups) take longer
SYNC_STABLE_ROUNDS = 2           # stop once the group count holds steady this many rounds
LISTGROUPS_TIMEOUT_S = 30        # listGroups is mostly local; guard against a network hang
MIN_DELAY_S = 10.0               # hard floor: never send faster than this, whatever the config


class BroadcastError(Exception):
    """Recoverable, user-facing problem (bad config, missing file, no signal-cli)."""


# Callback aliases. Defaults are no-ops so callers can pass only what they need.
LogFn = Callable[[str], None]
# status is one of: "sent" | "failed" | "skipped" | "uncertain" (timed out — may have sent)
ProgressFn = Callable[[int, int, str, str, float], None]  # done, total, name, status, seconds
StopFn = Callable[[], bool]


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
        base_delay_seconds=float(raw.get("base_delay_seconds", 12)),
        jitter_seconds=float(raw.get("jitter_seconds", 5)),
        cooldown_hours=float(raw.get("cooldown_hours", 0)),
        max_retries=int(raw.get("max_retries", 4)),
        send_times=[str(t) for t in raw.get("send_times", [])],
        debug=bool(raw.get("debug", False)),
        wipe_on_close=bool(raw.get("wipe_on_close", False)),
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
    next_ok = last + timedelta(hours=cooldown_hours)
    now = datetime.now()
    if now < next_ok:
        mins = round((next_ok - now).total_seconds() / 60)
        return f"last run was {last:%Y-%m-%d %H:%M}; cooldown clears in ~{mins} min"
    return None


def stamp_run() -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    LAST_RUN_FILE.write_text(datetime.now().isoformat(timespec="seconds"), encoding="utf-8")


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
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise BroadcastError("A send is already in progress (this app or the "
                                 "scheduler). Wait for it to finish, or Stop it first.")
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
    killed = 0
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
            killed += 1
        except OSError:
            pass
    if killed:
        on_log(f"Cleared {killed} leftover signal-cli process(es) holding the account lock.")
        time.sleep(1.0)  # let the OS release the account lock before we retry
    return killed


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


def signal_cli_bin() -> str:
    jvm = _jvm_signal_cli()
    if jvm:
        return str(jvm)
    found = shutil.which("signal-cli")
    if found:
        return found
    # launchd runs with a minimal PATH that excludes Homebrew; check known spots.
    for p in ("/opt/homebrew/bin/signal-cli", "/usr/local/bin/signal-cli"):
        if Path(p).exists():
            return p
    raise BroadcastError("signal-cli is not installed. Run Setup first.")


def qrencode_bin() -> str:
    """Locate qrencode the same way as signal-cli: PATH first, then Homebrew's bin.
    The Dock app and launchd run with a minimal PATH that excludes Homebrew, so a
    bare which() would wrongly report it missing even when it's installed."""
    found = shutil.which("qrencode")
    if found:
        return found
    for p in ("/opt/homebrew/bin/qrencode", "/usr/local/bin/qrencode"):
        if Path(p).exists():
            return p
    raise BroadcastError("qrencode is not installed. Run Setup first.")


def _is_jvm_build(binary: str) -> bool:
    jvm = _jvm_signal_cli()
    return jvm is not None and str(jvm) == binary


def _java_home() -> str | None:
    """A Java 25+ home for the JVM build. signal-cli 0.14.x is compiled for Java 25,
    so an older JDK would fail to load it — only offer @25 and the unversioned
    (newer) Homebrew kegs, never @21."""
    for base in ("/opt/homebrew/opt/openjdk@25", "/usr/local/opt/openjdk@25",
                 "/opt/homebrew/opt/openjdk", "/usr/local/opt/openjdk"):
        if (Path(base) / "bin" / "java").exists():
            return base
    return os.environ.get("JAVA_HOME")  # last resort: whatever the machine has set


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
    proc = subprocess.run(_cli(binary, "--config", str(DATA_DIR), "-o", "json", "listAccounts"),
                          capture_output=True, text=True, errors="replace", env=_signal_env(binary))
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


def is_linked() -> bool:
    data = DATA_DIR / "data"
    return data.exists() and any(data.iterdir())


def _request_sync(binary: str, account: str) -> None:
    """Best-effort nudge: ask the phone (primary) to (re)send contacts + groups.
    Ignored on failure — the phone usually pushes a sync on linking anyway."""
    subprocess.run(_cli(binary, "--config", str(DATA_DIR), "-a", account, "sendSyncRequest"),
                   capture_output=True, text=True, errors="replace", env=_signal_env(binary))


def sync_groups(account: str, on_log: LogFn = lambda *_: None) -> int:
    """Drain the phone's contacts/groups sync and (over)write groups.txt. A large
    account's groups arrive over several seconds, so nudge the phone then receive
    in short bursts until the count stops growing (or SYNC_MAX_S). Reports a running
    count so the wait is visibly progressing. Returns the final group count."""
    binary = signal_cli_bin()
    _request_sync(binary, account)
    on_log("Syncing your groups from your phone…")
    deadline = time.monotonic() + SYNC_MAX_S
    last, stable = -1, 0
    while time.monotonic() < deadline and stable < SYNC_STABLE_ROUNDS:
        subprocess.run(_cli(binary, "--config", str(DATA_DIR), "-a", account,
                            "receive", "--timeout", str(SYNC_BURST_S)),
                       capture_output=True, text=True, errors="replace", env=_signal_env(binary))
        try:
            count = pull_groups(account)
        except BroadcastError:
            continue  # transient fetch error — try another burst
        on_log(f"Syncing your groups from your phone… ({count} so far)")
        # Only settle on a non-zero count; while we still have nothing, keep
        # draining until the cap, since the phone's first sync can be slow.
        stable = stable + 1 if (count == last and count > 0) else 0
        last = count
    return max(last, 0)


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
        self._next_id = 0
        self._pending: dict[int, queue.Queue] = {}
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
            mid = msg.get("id")
            if mid is None:
                continue  # notification (incoming message/receipt) — not our response
            with self._lock:
                box = self._pending.pop(mid, None)
            if box is not None:
                box.put(msg)

    def is_running(self) -> bool:
        return self._proc.poll() is None

    def _request(self, method: str, params: dict, timeout: float) -> dict:
        with self._lock:
            if self._proc.poll() is not None:
                raise BroadcastError("signal-cli daemon is not running")
            self._next_id += 1
            mid = self._next_id
            box: queue.Queue = queue.Queue(maxsize=1)
            self._pending[mid] = box
        try:
            assert self._proc.stdin is not None
            self._proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method,
                                               "params": params, "id": mid}) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            with self._lock:
                self._pending.pop(mid, None)
            raise BroadcastError(f"daemon write failed: {exc}")
        try:
            return box.get(timeout=timeout)
        except queue.Empty:
            with self._lock:
                self._pending.pop(mid, None)
            raise BroadcastError(f"daemon timed out after {timeout:.0f}s")

    def send(self, group_id: str, message: str, attachments: list[str]) -> tuple[bool, bool, str]:
        """Send to one group via the running process. Same (ok, throttled, err) shape
        as the one-shot _send_one, so the retry/throttle logic is unchanged."""
        params: dict = {"groupId": group_id, "message": message}
        if attachments:
            params["attachment"] = list(attachments)
        try:
            resp = self._request("send", params, timeout=SEND_TIMEOUT_S)
        except BroadcastError as exc:
            return False, False, str(exc)
        err = resp.get("error")
        if err:
            text = str(err.get("message", "")).strip() or "send failed"
            return False, bool(THROTTLE_PATTERN.search(text)), text
        return True, False, ""

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
                r"no route to host|noroutetohost|ssl|certificate|\bcdn\b|\bdns\b", re.I),
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


def _deliver_to_group(send_one: SendFn, group_id: str,
                      message: str, attachments: list[str], max_retries: int,
                      on_log: LogFn, should_stop: StopFn, debug: bool = False) -> str:
    """Try one group with retries via ``send_one`` (one-shot or daemon — same shape).
    Returns "sent", "failed", or "uncertain". "uncertain" is a client-side timeout:
    we don't know whether it delivered, so we neither retry nor call it failed.
    Throttled sends back off exponentially; other clean errors get a couple of quick
    retries. Log lines carry no group name, id, or raw signal-cli output — only
    counts, retry timing, and a sanitised error category."""
    throttle_attempt = 0
    quick_attempt = 0
    while not should_stop():
        ok, throttled, err = send_one(group_id, message, attachments)
        if ok:
            return "sent"
        if debug and err:
            append_debug(f"group {group_id} (throttled={throttled}): {err}")
        if CLIENT_TIMEOUT_PATTERN.search(err):
            # We stopped waiting before signal-cli answered — it may have delivered.
            on_log("Send timed out — it may have gone through; not retrying, to avoid a duplicate.")
            return "uncertain"
        if throttled:
            throttle_attempt += 1
            if throttle_attempt > max_retries:
                on_log(f"Gave up after {max_retries} throttled retries")
                return "failed"
            wait = _throttle_wait(throttle_attempt, err)  # err parsed for retry-after, never logged
            on_log(f"Throttled — backing off {wait:.0f}s (retry {throttle_attempt}/{max_retries})")
            _interruptible_sleep(wait, should_stop)
        elif ADMIN_ONLY_PATTERN.search(err):
            # Non-admin in an announcement group — retrying can never succeed.
            on_log("Send failed — admin-only group (you can't post here).")
            return "failed"
        else:
            quick_attempt += 1
            reason = classify_error(err)
            if quick_attempt > NON_THROTTLE_RETRIES:
                on_log(f"Send failed — {reason}.")
                return "failed"
            on_log(f"Send error ({reason}) — retrying in {NON_THROTTLE_WAIT_S:.0f}s")
            _interruptible_sleep(NON_THROTTLE_WAIT_S, should_stop)
    return "failed"


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
            if daemon is not None:
                res = daemon.send(gid, msg, atts)
                if not res[0] and not daemon.is_running():
                    # Daemon died mid-run — switch to one-shot for the rest. But if THIS
                    # send timed out (ambiguous — it may already have gone out), do NOT
                    # re-send it: that could duplicate. Report the timeout as-is (the
                    # caller turns it into "uncertain") and only re-send clean failures.
                    on_log("signal-cli daemon stopped; falling back to per-send.")
                    timed_out = bool(CLIENT_TIMEOUT_PATTERN.search(res[2]))
                    try:
                        daemon.close()
                    finally:
                        daemon = None
                    if timed_out:
                        return res
                    return _send_one(binary, config.account, gid, msg, atts)
                return res
            return _send_one(binary, config.account, gid, msg, atts)

        try:
            on_log(f"Broadcasting to {total} groups | {len(attachments)} attachment(s) | "
                   f"~{delay:.0f}s minimum between sends")
            for i, (gid, name) in enumerate(groups, start=1):
                if should_stop():
                    on_log("Stopped.")
                    break
                if gid in blocked:
                    results.append(GroupSendResult(gid, name, ok=False, skipped=True, reason="admin-only"))
                    on_progress(i, total, name, "skipped", 0.0)  # not a failure — never attempted
                    continue  # no send, no pacing delay — nothing left the machine
                t0 = time.monotonic()
                status = _deliver_to_group(send_one, gid, message, attachments,
                                           config.max_retries, on_log, should_stop, config.debug)
                secs = time.monotonic() - t0  # wall time for this group (includes any retries)
                if status == "uncertain":
                    # Timed out — the message may have gone out. NOT a clean failure, so it
                    # is kept out of "Resend failed" (resending could duplicate it).
                    results.append(GroupSendResult(gid, name, ok=False, uncertain=True,
                                                   reason="may have sent (timed out)"))
                else:
                    results.append(GroupSendResult(gid, name, ok=(status == "sent")))
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


def write_failures(failures: list[GroupSendResult]) -> Path | None:
    """Persist failed groups so they can be resent. Returns the file path."""
    if not failures:
        return None
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    out = LOGS_DIR / f"failures-{datetime.now():%Y-%m-%d}.txt"
    # group_id only — it's needed to resend; names are never written to disk.
    out.write_text("".join(f"{r.group_id}\n" for r in failures), encoding="utf-8")
    return out


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
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    data = {"at": datetime.now().isoformat(timespec="seconds"), "fp": fingerprint,
            "groups": [[g, n] for g, n in groups], "done": {}}
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


def read_interrupted_run() -> InterruptedRun | None:
    """If a previous broadcast was interrupted (process died mid-run), describe what's
    left to send. Only groups that were never attempted or were a CLEAN failure are
    resumable — sent/uncertain/skipped are excluded so a resume can't duplicate a
    message that already went out. Returns None if nothing is pending."""
    if not RUN_PROGRESS_FILE.exists():
        return None
    try:
        data = json.loads(RUN_PROGRESS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    groups = [(str(g), str(n)) for g, n in data.get("groups", [])]
    done = data.get("done", {})
    remaining = [(g, n) for g, n in groups if done.get(g) in (None, "failed")]
    if not groups or not remaining:
        return None
    return InterruptedRun(fingerprint=str(data.get("fp", "")), total=len(groups),
                          done=len(groups) - len(remaining), remaining=remaining)


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
    transient glitch must never be the thing that triggers a wipe."""
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


def build_plist(times: list[str], python_exe: str) -> dict:
    """launchd job: run broadcast.py at each time, wrapped in caffeinate so the
    Mac stays awake through the send."""
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


def schedule_enabled() -> bool:
    r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{SCHEDULE_LABEL}"],
                       capture_output=True, text=True)
    return r.returncode == 0


def enable_schedule(times: list[str], python_exe: str | None = None) -> None:
    parse_times(times)  # validate before touching anything
    write_plist(times, python_exe, dest=INSTALLED_PLIST)
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{SCHEDULE_LABEL}"], capture_output=True)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(INSTALLED_PLIST)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise BroadcastError("Could not turn on the schedule:\n" + (r.stderr or r.stdout).strip())


def disable_schedule() -> None:
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
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{WATCHER_LABEL}"], capture_output=True)
    WATCHER_PLIST.unlink(missing_ok=True)


def watcher_enabled() -> bool:
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


def unlink() -> None:
    """Sign this Mac out of Signal and erase every local trace: the link keys and
    signal-cli's cached groups/contacts, the group list, the message, attachments,
    the schedule, and any logs. Leaves no personal data behind — use before handing
    the Mac to someone else. The phone (primary device) is untouched; to also drop
    this device from the phone, remove it under Signal → Linked Devices."""
    disable_schedule()
    LOCAL_PLIST.unlink(missing_ok=True)
    shutil.rmtree(DATA_DIR, ignore_errors=True)        # link keys + account.db cache
    # Delete config.toml outright — it holds the number — then recreate a fresh
    # placeholder from the template, so no local copy of the number survives.
    for f in (GROUPS_FILE, MESSAGE_FILE, ATTACHMENTS_FILE, CONFIG_FILE):
        f.unlink(missing_ok=True)
    _clear_dir(LOGS_DIR, keep={".gitkeep"})
    ensure_config()
