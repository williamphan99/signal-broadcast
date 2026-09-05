"""Owned Signal job process. Requests arrive on stdin, never through argv/env."""
from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
import threading
from dataclasses import asdict
from pathlib import Path
from functools import partial

import engine
import mac_retry
from mac_security import dispatch_guard


def configure_storage(root: Path):
    engine.RUNTIME_DIR = root
    paths = {
        "DATA_DIR": "signal-cli-data", "LOGS_DIR": "logs", "CONFIG_FILE": "config.toml",
        "GROUPS_FILE": "groups.txt", "GROUPS_LOCK_FILE": "groups.lock",
        "GROUP_PERMISSIONS_FILE": "group-permissions.json", "MESSAGE_FILE": "message.txt",
        "ATTACHMENTS_FILE": "attachments.txt", "NOTES_FILE": "notes.json",
        "NOTES_CORRUPT_FILE": "notes.corrupt.json", "NOTES_LOCK_FILE": "notes.lock",
        "LAST_RUN_FILE": "logs/last-run.txt", "LAST_SEND_FILE": "logs/last-send.json",
        "SIGNAL_CLI_LOCK_FILE": "logs/sending.lock", "SEND_LOCK_FILE": "logs/sending.lock",
        "RUN_PROGRESS_FILE": "logs/run-progress.json", "SYNC_DEBUG_FILE": "logs/sync-debug.txt",
        "NOTES_DEBUG_FILE": "logs/notes-debug.txt",
    }
    for name, relative in paths.items():
        setattr(engine, name, root / relative)
    engine._GROUP_ENTRIES_CACHE = None
    engine._GROUP_PERMISSION_CACHE = ("", set())


_emit_lock = threading.Lock()


def emit(kind: str, value):
    with _emit_lock:
        print(json.dumps({"kind": kind, "value": value}), flush=True)


def run(request):
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    kind = request["job"]
    if kind == "update":
        # App code can be updated while the vault is sealed. Never open user data.
        result = engine.git_pull()
        emit("update", asdict(result))
        if result.error:
            raise engine.ReceiveError("Could not download the update. Check the connection and local Git changes, then retry.")
        return
    root = Path(request["root"])
    if root.name != "store" or not root.parent.is_mount():
        raise engine.BroadcastError("The encrypted vault is not mounted.")
    configure_storage(root)
    engine.PRIVATE_TRANSPORT = True
    engine.DISPATCH_GUARD = partial(dispatch_guard, root.parents[1])
    temp = root / "temporary"
    temp.mkdir(mode=0o700, exist_ok=True)
    os.environ["TMPDIR"] = str(temp)
    os.environ["JAVA_TOOL_OPTIONS"] = f'-Djava.io.tmpdir="{temp}" -XX:ErrorFile="{temp}/hs_err_pid%p.log"'
    if kind == "link":
        with engine.signal_cli_operation("linking"):
            engine.DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
            command, env = engine.signal_cli_command("--config", str(engine.DATA_DIR), "link", "-n", "broadcast-laptop")
            proc = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            for line in proc.stdout:
                if line.startswith(("sgnl://linkdevice", "tsdevice:")):
                    # QR bytes travel in the private, authorized IPC response, not a temp file.
                    qr = subprocess.run([engine.qrencode_bin(), "-t", "PNG", "-o", "-"],
                                        input=line.strip().encode(), capture_output=True, check=True)
                    import base64
                    emit("qr", base64.b64encode(qr.stdout).decode())
            if proc.wait() != 0:
                raise engine.BroadcastError("Linking failed. Try a fresh QR code.")
            account = engine.wait_for_account()
            if not account:
                raise engine.BroadcastError("Signal did not confirm the link. Try again.")
            engine.save_account(account)
    elif kind in ("sync", "notes", "health"):
        if kind == "health":
            emit("link_broken", engine.link_is_broken())
        else:
            account = engine.load_config().account
            if engine.detect_account(require_single=True) != account:
                raise engine.AccountSelectionError("The linked account does not match the saved configuration.")
            if kind == "sync":
                emit("phase", "sync")
                emit("groups", engine.sync_groups(account, on_log=lambda text: emit("receive_status", text)))
            else:
                emit("phase", "notes")
                report = engine.fetch_notes(account, on_log=lambda text: emit("receive_status", text))
                emit("notes", report)
                if not report.get("complete", True):
                    raise engine.ReceiveError(report["warning"])
    elif kind in ("send", "resume", "retry"):
        emit("phase", "preparing")
        cfg = engine.load_config()
        groups = engine.read_groups()
        message = engine.read_message()
        attachments = engine.read_attachments()
        if kind == "retry":
            groups = mac_retry.groups()
        if kind == "resume":
            interrupted = engine.read_interrupted_run()
            if not interrupted:
                raise engine.BroadcastError("There is no interrupted broadcast to resume.")
            fingerprint = engine.message_fingerprint(message, attachments)
            if interrupted.fingerprint != fingerprint:
                raise engine.BroadcastError("Draft changed. Discard the interrupted run before a new send.")
            groups = interrupted.remaining
        groups = list({gid: name for gid, name in groups}.items())
        mac_retry.save([], message, attachments, cfg.message_style)
        active, completed = set(), set()
        progress_lock = threading.Lock()
        sending_started = False
        def status():
            emit("send_status", {"active": len(active), "completed": len(completed), "total": len(groups)})
        def started(position, _name):
            nonlocal sending_started
            with progress_lock:
                if not sending_started:
                    emit("phase", "sending")
                    sending_started = True
                active.add(position)
                status()
        def progressed(position, total, _name, outcome, seconds):
            with progress_lock:
                active.discard(position)
                completed.add(position)
                emit("progress", {"done": len(completed), "total": total, "status": outcome})
                status()
        results = engine.broadcast(config=cfg, groups=groups, message=message,
            attachments=attachments, on_log=lambda text: emit("log", text),
            should_stop=lambda: (root.parents[1] / "erase.json").exists(),
            on_group_start=started, on_progress=progressed)
        engine.stamp_run()
        engine.write_run_summary(results)
        mac_retry.save(results, message, attachments, cfg.message_style)
        emit("results", [asdict(result) for result in results])
    else:
        raise engine.BroadcastError("Unsupported job.")


if __name__ == "__main__":
    try:
        run(json.load(sys.stdin))
        emit("done", True)
    except (engine.AccountSelectionError, engine.ReceiveError) as exc:
        emit("error", str(exc))
        raise SystemExit(1)
    except Exception:
        # Engine exceptions can contain account names, paths or raw provider output.
        # Show a bounded category and retain only authorized normal progress events.
        emit("error", "Operation failed. Check the link, connectivity and available disk space.")
        raise SystemExit(1)
