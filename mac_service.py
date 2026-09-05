"""Per-user, local-only Mac security and job service. No TCP listener or telemetry."""
from __future__ import annotations

import collections
import fcntl
import json
import os
import resource
import secrets
import signal
import socket
import mac_retry
import socketserver
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import engine
from mac_security import (HELPER, PROJECT, ROOT, SecurityError, Vault, WrongPassword,
                          atomic_json, unwrap_password, wrap_password)
from mac_worker import configure_storage

LABEL = "com.user.signal-broadcast.service"
MAX_REQUEST = 1024 * 1024


def socket_path(root: Path = ROOT) -> Path:
    # Darwin's sockaddr_un path limit is 104 bytes. Keep the actual socket short,
    # in the owner's private temporary directory; its name contains no account ID.
    import hashlib
    import tempfile
    directory = Path(tempfile.gettempdir()).resolve() / f"sb-{os.getuid()}"
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_uid != os.getuid():
        raise SecurityError("Unsafe local service directory.")
    os.chmod(directory, 0o700)
    return directory / (hashlib.sha256(str(root).encode()).hexdigest()[:12] + ".sock")


def terminate_group(proc, seconds: float = 3):
    pid = proc if isinstance(proc, int) else proc.pid
    poll = getattr(proc, "poll", lambda: None)
    def live_members():
        listing = subprocess.run(["ps", "-axo", "pgid=,stat="], capture_output=True, text=True, check=True)
        members = [row.split() for row in listing.stdout.splitlines() if row.strip()]
        return any(int(group) == pid and not state.startswith("Z") for group, state in members)
    def send(sig):
        try:
            os.killpg(pid, sig)
            return True
        except ProcessLookupError:
            return False
        except PermissionError as exc:
            if live_members():
                raise SecurityError("A worker could not be stopped. Erasure remains pending.") from exc
            return False
    try:
        if not send(signal.SIGTERM):
            poll()
            return
    except ProcessLookupError:
        return
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        poll()
        try:
            if not send(0):
                return
        except ProcessLookupError:
            return
        time.sleep(0.05)
    try:
        send(signal.SIGKILL)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 3
    try:
        if not isinstance(proc, int):
            proc.wait(timeout=3)
    except subprocess.TimeoutExpired as exc:
        raise SecurityError("A worker did not stop. Erasure remains pending.") from exc
    while live_members():
        if time.monotonic() >= deadline:
            raise SecurityError("A worker group did not stop. Erasure remains pending.")
        poll()
        time.sleep(0.05)


def retire_legacy(project: Path):
    import shlex
    enabled = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{engine.SCHEDULE_LABEL}"],
                             capture_output=True).returncode == 0
    # Remove only this application's previous launchd labels.
    for label in (engine.SCHEDULE_LABEL, engine.WATCHER_LABEL):
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True)
        (Path.home() / "Library/LaunchAgents" / (label + ".plist")).unlink(missing_ok=True)
    (project / (engine.SCHEDULE_LABEL + ".plist")).unlink(missing_ok=True)
    listing = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, check=True)
    owned = {str(project / name) for name in ("gui.py", "broadcast.py", "watcher.py")}
    pids = []
    for row in listing.stdout.splitlines():
        pid_text, _, command = row.strip().partition(" ")
        try:
            pid, args = int(pid_text), shlex.split(command)
        except ValueError:
            continue
        if pid in (os.getpid(), os.getppid()):
            continue
        legacy_cli = "--config" in args and str(project / "signal-cli-data") in args
        if owned.intersection(args) or legacy_cli:
            try:
                os.kill(pid, signal.SIGTERM)
                pids.append(pid)
            except ProcessLookupError:
                pass
    if pids:
        engine._wait_for_pids_exit(pids, deadline_s=3)
    return enabled


class Service:
    def __init__(self, vault: Vault, *, retire=retire_legacy, spawn=subprocess.Popen):
        self.vault, self.retire, self.spawn = vault, retire, spawn
        self.mutex = threading.RLock()
        self.token = None
        self.open = False
        self.job = None
        self.events = collections.deque(maxlen=250)
        self.sequence = 0
        self.link_broken = False
        self.shutting_down = False
        self.erasing = False
        self.recovery_error = None
        self.on_erased = None
        self.last_operation = None
        self.update_state = None
        self.restart_requested = False
        self.on_restart = None
        self.version = engine.app_version()
        self.send_progress = None
        self.phase = None

    def recover(self):
        self.vault.prepare()
        self.reap_worker()
        # launchd restarts sealed. No credential is automatically recovered from Keychain.
        self.vault.detach()
        record = self.vault.keychain.load()
        if self.vault.marker.exists() or (record and (record["failures"] >= 3 or record["phase"] == "erasing")):
            self.erase()

    def _authorized(self, request):
        token = request.get("token")
        if self.erasing or self.vault.marker.exists() or not self.open or not self.token or not isinstance(token, str) or not secrets.compare_digest(token, self.token):
            raise SecurityError("Locked. Enter your password.", code="locked")

    def _event(self, kind, value):
        self.sequence += 1
        self.events.append({"id": self.sequence, "kind": kind, "value": value})

    def lock(self):
        self.token = None

    def status(self):
        if self.recovery_error:
            raise SecurityError(self.recovery_error)
        record = self.vault.keychain.load()
        exists = self.vault.image.exists()
        if self.erasing or self.vault.marker.exists():
            state = "erasing"
        elif not record and not exists:
            state = "unlinked"
        elif not record:
            raise SecurityError("Vault credentials are missing. Erase to start again.")
        elif self.open:
            state = "unlocked" if self.token else "screen_locked"
        else:
            state = "sealed"
        return {"state": state, "attempts_remaining": max(0, 3 - record["failures"]) if record else 3,
                "background_running": bool(self.job), "setup_required": not record and not exists,
                "updating": bool(self.job and self.job["kind"] == "update"),
                "update": ({key: bool(self.update_state.get(key)) for key in ("changed", "needs_setup", "error")}
                           if self.update_state else None)}

    def authenticate(self, password: str, setup=False):
        if self.recovery_error:
            raise SecurityError(self.recovery_error)
        if not isinstance(password, str) or len(password.encode()) > 4096:
            raise SecurityError("Password is too long.")
        if self.erasing or self.vault.marker.exists():
            self.erase()
            raise SecurityError("Erasure completed. Set a new password.")
        record = self.vault.keychain.load()
        if setup:
            if record or self.vault.image.exists():
                raise SecurityError("An installation already exists. Unlock or erase it.")
            volume_password = secrets.token_hex(32).encode()
            record = wrap_password(password, volume_password)
            self.vault.prepare()
            record["legacy_schedule_enabled"] = bool(self.retire(self.vault.project))
            self.vault.keychain.save(record)
        else:
            if not record:
                raise SecurityError("Security credentials are missing. Erase to start again.")
            if record["failures"] >= 3:
                self.erase()
                raise SecurityError("Data erased after three incorrect attempts.")
            volume_password = self._unwrap_password(password, record)
        if record["phase"] == "migrating":
            self.retire(self.vault.project)
            self.vault.create_image(volume_password)
            self.vault.migrate(volume_password)
        elif not self.open:
            self.vault.attach(volume_password)
        self.vault.recover_imports()
        record = self.vault.keychain.load()
        record["failures"] = 0
        self.vault.keychain.save(record)
        configure_storage(self.vault.data)
        engine.PRIVATE_TRANSPORT = True
        self.open = True
        engine.ensure_config()
        engine.set_config_value("wipe_on_close", False)
        self.token = secrets.token_urlsafe(32)
        return {"token": self.token}

    def _unwrap_password(self, password, record):
        try:
            return unwrap_password(password, record)
        except WrongPassword:
            record["failures"] += 1
            self.vault.keychain.save(record)
            if record["failures"] >= 3:
                self.erase()
            raise

    def change_password(self, current, new):
        # Validate the new choice without touching the established retry counter.
        if not isinstance(new, str) or len(new) < 12 or len(new.encode()) > 4096:
            raise SecurityError("New password must have at least 12 characters.")
        record = self.vault.keychain.load()
        secret = self._unwrap_password(current, record)
        replacement = wrap_password(new, secret)
        replacement["phase"] = "ready"
        replacement["pending_originals"] = record.get("pending_originals", [])
        self.vault.keychain.save(replacement)
        return {"changed": True}

    def stop_job(self):
        job = self.job
        if job:
            terminate_group(job["proc"])
            self.job = None
        self.reap_worker()

    def reap_worker(self):
        path = self.vault.root / "worker.json"
        if not path.exists():
            return
        pid = int(json.loads(path.read_text())["pid"])
        listing = subprocess.run(["ps", "-axo", "pid=,pgid=,stat=,command="], capture_output=True, text=True, check=True)
        members = []
        for row in listing.stdout.splitlines():
            fields = row.strip().split(None, 3)
            if len(fields) == 4 and int(fields[1]) == pid and not fields[2].startswith("Z"):
                members.append(fields[3])
        if members:
            if not any(str(PROJECT / "mac_worker.py") in cmd or str(self.vault.data / "signal-cli-data") in cmd for cmd in members):
                raise SecurityError("Cannot establish ownership of an old worker. Access remains sealed.")
            terminate_group(pid, seconds=0.1)
        path.unlink(missing_ok=True)

    def erase(self):
        self.token = None
        self.events.clear()
        self.open = False
        self.erasing = True
        try:
            self.vault.prepare()
            with (self.vault.root / "dispatch.lock").open("a") as lease:
                try:
                    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    # A request already being handed to Signal may complete. Stop
                    # a blocked pipe writer before waiting for its dispatch lease.
                    self.stop_job()
                    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    atomic_json(self.vault.marker, {"erasing": True})
                except OSError:
                    # A full data disk must not turn logout into a reversible
                    # in-memory flag. Keep a second durable intent in Keychain.
                    record = self.vault.keychain.load()
                    if record:
                        record["legacy_imports"] = record["phase"] == "migrating"
                        record["phase"] = "erasing"
                        self.vault.keychain.save(record)
                    raise
        finally:
            try:
                self.retire(self.vault.project)
            finally:
                self.stop_job()
        self.vault.erase()
        self.link_broken = False
        self.erasing = False
        self.recovery_error = None
        engine._GROUP_ENTRIES_CACHE = None
        engine._GROUP_PERMISSION_CACHE = ("", set())
        if self.on_erased:
            self.on_erased()

    def snapshot(self, after=0):
        def safe(fn, default):
            try:
                return fn()
            except engine.BroadcastError:
                return default
        cfg = safe(engine.load_config, None)
        notes = engine.read_notes()
        interrupted = safe(engine.read_interrupted_run, None) if not self.job else None
        return {"linked": engine.is_linked() and cfg is not None and not self.link_broken,
                "config": asdict(cfg) if cfg else None,
                "groups": [asdict(group) for group in engine.read_group_entries()],
                "notes": notes, "message": safe(engine.read_message, ""),
                "attachments": safe(engine.read_attachments, []),
                "schedule": self.schedule(), "job": self.job["kind"] if self.job else None,
                "last_operation": self.last_operation,
                "update": self.update_state, "version": self.version,
                "retry_count": mac_retry.available_count(),
                "send_progress": self.send_progress, "phase": self.phase,
                "events": [event for event in self.events if event["id"] > after],
                "sequence": self.sequence,
                "interrupted": asdict(interrupted) if interrupted else None,
                "summary": asdict(summary) if (summary := engine.read_run_summary()) else None}

    def schedule(self):
        path = self.vault.data / "schedule.json"
        return json.loads(path.read_text()) if path.exists() else {"enabled": False, "times": [], "last": ""}

    def _start_job(self, kind):
        if self.job:
            raise SecurityError("Signal is busy. Wait for the current operation.")
        if self.update_state and self.update_state.get("changed"):
            raise SecurityError("Restart or finish installing the downloaded update first.")
        if kind not in {"link", "sync", "notes", "send", "resume", "retry", "health", "update"}:
            raise SecurityError("Unsupported job.")
        if kind in ("send", "resume", "retry"):
            cfg = engine.load_config()
            if self.link_broken:
                raise SecurityError("This Signal link is broken. Erase and link again.")
            blocked = engine.cooldown_blocks_run(cfg.cooldown_hours)
            if blocked and kind != "retry":
                raise SecurityError(blocked)
            if not engine.read_groups():
                raise SecurityError("Choose at least one group.")
            if kind in ("send", "retry") and engine.read_interrupted_run():
                raise SecurityError("Review and resume or discard the interrupted broadcast first.")
            if kind == "retry":
                mac_retry.groups()
        proc = self.spawn([sys.executable, str(PROJECT / "mac_worker.py")],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          text=True, start_new_session=True)
        job = {"kind": kind, "proc": proc}
        if kind == "update":
            self.update_state = {"changed": True, "needs_setup": True,
                                 "message": "Update interrupted. Run Setup before continuing."}
        self.job = job
        try:
            atomic_json(self.vault.root / "worker.json", {"pid": proc.pid})
            proc.stdin.write(json.dumps({"root": str(self.vault.data), "job": kind}))
            proc.stdin.close()
        except Exception:
            terminate_group(proc)
            proc.stdin.close()
            proc.stdout.close()
            self.job = None
            raise
        self.last_operation = None
        self.send_progress = None
        self.phase = None
        self._event("started", kind)
        threading.Thread(target=self._read_job, args=(job,), daemon=True).start()
        return {"started": kind}

    def _read_job(self, job):
        proc = job["proc"]
        returncode = None
        try:
            for line in proc.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                with self.mutex:
                    if self.job is not job or self.vault.marker.exists():
                        continue
                    if event["kind"] == "send_status":
                        self.send_progress = event["value"]
                    if event["kind"] == "phase":
                        self.phase = event["value"]
                    if event["kind"] == "update":
                        self.update_state = event["value"]
                    if event["kind"] == "link_broken" and event["value"] is True:
                        self.link_broken = True
                    if event["kind"] in {"log", "progress", "results", "qr", "error", "done", "phase", "receive_status"}:
                        self._event(event["kind"], event["value"])
            returncode = proc.wait()
        finally:
            proc.stdout.close()
            with self.mutex:
                if self.job is job:
                    terminate_group(proc, seconds=0.1)
                    self.job = None
                    self.last_operation = {"kind": job["kind"],
                                           "outcome": "completed" if returncode == 0 else "failed"}
                    (self.vault.root / "worker.json").unlink(missing_ok=True)
                    if job["kind"] == "link":
                        self.link_broken = False
                    self._event("finished", job["kind"])

    def tick(self, now: datetime | None = None):
        with self.mutex:
            if self.restart_requested:
                if self.on_restart:
                    self.on_restart()
                return
            if not self.open or self.job or self.vault.marker.exists() or self.shutting_down:
                return
            if self.update_state and self.update_state.get("changed"):
                return
            schedule = self.schedule()
            now = now or datetime.now()
            slot = now.strftime("%Y-%m-%d %H:%M")
            if not schedule["enabled"] or now.strftime("%H:%M") not in schedule["times"] or schedule["last"] == slot or slot in schedule.get("consumed", []):
                return
            # Consume the slot before dispatch, even if a preflight fails. No crash replay.
            schedule["last"] = slot
            schedule["consumed"] = [*schedule.get("consumed", []), slot][-10080:]
            atomic_json(self.vault.data / "schedule.json", schedule)
            try:
                self._start_job("send")
            except (SecurityError, engine.BroadcastError):
                self._event("log", "Scheduled send skipped. Review the draft and previous run.")

    def handle(self, request):
        with self.mutex:
            op = request.get("op")
            if self.shutting_down:
                raise SecurityError("Service is stopping.")
            if op == "erase":
                if request.get("confirmed") is not True:
                    raise SecurityError("Confirm Log out and erase first.")
                self.erase()
                return {"erased": True}
            if op == "status":
                return self.status()
            if op in ("update", "restart_update"):
                if self.erasing or self.vault.marker.exists():
                    raise SecurityError("Finish erasing before updating.")
                if op == "update":
                    return self._start_job("update")
                if self.job or not self.update_state or not self.update_state.get("changed"):
                    raise SecurityError("Wait for the update to finish before restarting.")
                if self.update_state.get("needs_setup"):
                    raise SecurityError("Finish installing this update with Setup first.")
                self.restart_requested = True
                return {"restarting": True}
            if op in ("unlock", "setup"):
                return self.authenticate(request.get("password", ""), setup=op == "setup")
            self._authorized(request)
            if op == "lock":
                self.lock()
                return {"locked": True}
            if op == "snapshot":
                return self.snapshot(int(request.get("after", 0)))
            if op == "change_password":
                return self.change_password(request.get("current", ""), request.get("new", ""))
            if op == "job":
                return self._start_job(request.get("kind"))
            if op == "stop":
                kind = self.job["kind"] if self.job else None
                self.stop_job()
                if kind:
                    self.last_operation = {"kind": kind, "outcome": "stopped"}
                    self._event("stopped", kind)
                return {"stopped": True}
            if op == "save":
                if self.job and self.job["kind"] in ("send", "resume", "retry", "update"):
                    raise SecurityError("Wait for the broadcast before replacing its saved draft.")
                paths = request.get("attachments", [])
                if not isinstance(paths, list) or len(paths) > 100:
                    raise SecurityError("Invalid attachments.")
                for raw in paths:
                    path = Path(raw)
                    if not path.resolve().is_relative_to(self.vault.data.resolve()) or path.is_symlink() or not path.is_file():
                        raise SecurityError("Import each attachment into the vault first.")
                engine.write_message(str(request.get("message", "")))
                engine.write_attachments(paths)
                if "message_style" in request:
                    engine.set_config_value("message_style", engine.normalize_message_style(request["message_style"]))
                return {"saved": True}
            if op == "import":
                if self.job:
                    raise SecurityError("Wait for the active operation before importing images.")
                path = self.vault.import_image(Path(request["path"]))
                return {"path": path}
            if op == "groups":
                engine.write_group_selection(set(request["enabled"]))
                return {"saved": True}
            if op == "schedule":
                times = request["times"]
                if request["enabled"] or times:
                    times = [f"{entry['Hour']:02d}:{entry['Minute']:02d}" for entry in engine.parse_times(times)]
                previous = self.schedule()
                atomic_json(self.vault.data / "schedule.json", {
                    "enabled": bool(request["enabled"]), "times": times, "last": previous["last"],
                    "consumed": previous.get("consumed", [])})
                return {"saved": True}
            if op == "settings":
                for key, value in request["values"].items():
                    if self.job:
                        raise SecurityError("Wait for the active operation before changing settings.")
                    if key == "message_style":
                        engine.set_config_value(key, engine.normalize_message_style(value))
                    elif key in ("base_delay_seconds", "jitter_seconds", "cooldown_hours", "max_retries", "concurrent_sends"):
                        number = float(value)
                        if not 0 <= number <= 3600:
                            raise SecurityError("Setting outside the allowed range.")
                        if key == "base_delay_seconds":
                            number = max(engine.MIN_DELAY_S, number)
                        if key == "concurrent_sends":
                            number = min(engine.MAX_CONCURRENT_SENDS, max(1, int(number)))
                        engine.set_config_value(key, number)
                    else:
                        raise SecurityError("Unsupported setting.")
                return {"saved": True}
            if op == "delete_note":
                engine.delete_note(int(request["timestamp"]))
                return {"deleted": True}
            if op == "discard":
                if self.job:
                    raise SecurityError("Wait for the active job.")
                engine.clear_run_progress()
                return {"discarded": True}
            if op == "clear_logs":
                if self.job:
                    raise SecurityError("Wait for the active job.")
                engine.clear_logs()
                self.events.clear()
                return {"cleared": True}
            raise SecurityError("Unsupported operation.")

    def shutdown(self):
        with self.mutex:
            self.shutting_down = True
            self.token = None
            self.stop_job()
            self.events.clear()
            self.vault.detach()
            self.open = False


class Client:
    def __init__(self, root: Path = ROOT, timeout: float = 240):
        self.root, self.token = root, None
        self.timeout = timeout

    def call(self, op: str, **values):
        request = {"op": op, "token": self.token, **values}
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                sock.connect(str(socket_path(self.root)))
                with sock.makefile("rwb") as stream:
                    stream.write(json.dumps(request).encode() + b"\n")
                    stream.flush()
                    response = json.loads(stream.readline(8 * MAX_REQUEST))
        except (OSError, ValueError) as exc:
            raise SecurityError("Local service is unavailable. Run Setup.command or reopen the app.", code="unavailable") from exc
        if "error" in response:
            raise SecurityError(response["error"], code=response.get("error_code"))
        result = response["result"]
        if op in ("setup", "unlock"):
            self.token = result["token"]
        elif op in ("lock", "erase"):
            self.token = None
        return result


def serve(service: Service, path: Path):
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(10)
            try:
                if hasattr(self.connection, "getpeereid") and self.connection.getpeereid()[0] != os.getuid():
                    return
                raw = self.rfile.readline(MAX_REQUEST + 1)
                if len(raw) > MAX_REQUEST or not raw.endswith(b"\n"):
                    return
                request = json.loads(raw)
                if not isinstance(request, dict):
                    return
                result = service.handle(request)
                response = {"result": result}
            except (SecurityError, engine.BroadcastError) as exc:
                response = {"error": str(exc), "error_code": getattr(exc, "code", None)}
            except Exception:
                response = {"error": "Operation could not complete. Access remains restricted."}
            try:
                payload = json.dumps(response).encode() + b"\n"
                self.connection.settimeout(1)  # A stalled local reader must not hold up revocation for the input timeout.
                with service.mutex:
                    if "error" in response and (service.erasing or service.vault.marker.exists()):
                        response["error_code"] = "locked"
                        payload = json.dumps(response).encode() + b"\n"
                    if "result" in response and request["op"] not in ("status", "lock", "erase", "update", "restart_update"):
                        # Serialization can race a lock or logout. Recheck at delivery
                        # and keep revocation serialized with the socket write.
                        authorization = ({"token": result["token"]} if request["op"] in ("setup", "unlock") else request)
                        try:
                            service._authorized(authorization)
                        except SecurityError as exc:
                            payload = json.dumps({"error": str(exc), "error_code": exc.code}).encode() + b"\n"
                    self.wfile.write(payload)
            except OSError:
                pass

    class Server(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True
        def handle_error(self, request, client_address):
            pass  # Never write request contents to launchd logs.

    path.unlink(missing_ok=True)
    server = Server(str(path), Handler)
    os.chmod(path, 0o600)
    return server


def main():
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (ROOT / "service.lock").open("a") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        service = Service(Vault())
        try:
            service.recover()
        except SecurityError as exc:
            service.recovery_error = str(exc)
        server = serve(service, socket_path())
        stop = threading.Event()
        def restart_after_erasure():
            service.shutting_down = True
            stop.set()  # launchd starts a fresh process without the old Python heap.
        service.on_erased = restart_after_erasure
        service.on_restart = restart_after_erasure
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        observer = subprocess.Popen([str(HELPER), "observe-lock"], stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True)
        def observe():
            for _ in observer.stdout:
                with service.mutex:
                    service.lock()
            with service.mutex:
                service.lock()
        threading.Thread(target=observe, daemon=True).start()
        try:
            while not stop.wait(1):
                if observer.poll() is not None:
                    raise SecurityError("Mac lock observer stopped.")
                service.tick()
        finally:
            server.shutdown()
            server.server_close()
            observer.terminate()
            service.shutdown()
            socket_path().unlink(missing_ok=True)


if __name__ == "__main__":
    main()
