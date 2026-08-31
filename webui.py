#!/usr/bin/env python3
"""Mobile web UI for Signal Broadcast — the Android/Pixel counterpart of the Tkinter
gui.py. Same engine and daily workflows, including groups, notes, scheduling, and unlink.

It runs *inside* the proot-distro Debian guest and binds to 127.0.0.1 only, so it's
reachable from the phone's own browser and nowhere else — nothing is exposed to the
network, there is no account or login, and linking is the same secondary-device flow the
Mac uses. Launch it with scripts/webui-termux.sh (which also holds a wake lock).

Design notes:
  * All Signal work goes through engine.py (unchanged), so behaviour matches the Mac app.
  * Long operations (link, group sync, the broadcast itself) run in background threads;
    the page polls small JSON endpoints for progress. Flask runs threaded.
  * create_app() builds the app so tests can drive it with Flask's test client and
    monkeypatch the engine — no real signal-cli or network needed (see tests/test_webui.py).
"""
from __future__ import annotations

import base64
import functools
import json
import struct
import subprocess
import threading
import time
import zlib
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, Response, jsonify, request
from werkzeug.utils import secure_filename

import engine

UPLOAD_DIR = engine.PROJECT_DIR / "webui-uploads"
DEVICE_NAME = "pixel-broadcast"
# We do NOT cut an attempt short to "refresh" — that could kill a scan already in
# progress. Each signal-cli link runs until it either links (scan) or Signal expires the
# code and closes the socket (then we loop for a fresh QR). This guard only kills a truly
# hung attempt, and is set far above normal completion so a real scan is never interrupted.
LINK_HANG_GUARD_S = 200
LINK_MAX_ATTEMPTS = 8      # loop fresh QRs for a while, then ask the user to retry
LINK_TOTAL_S = 900         # keep issuing fresh codes for up to 15 min (single-phone linking
                           # is fiddly; the user drives it, so don't give up after N attempts)
# How long a link-health verdict stays good. The check spawns signal-cli (seconds, and
# it takes the account lock), while /api/state is polled every couple of seconds — so
# it must be cached hard. A device only ever gets unlinked from the phone, which the
# user then has to act on, so a few minutes of staleness costs nothing; a failed run
# forces an immediate re-check anyway.
LINK_CHECK_TTL_S = 300
LINK_CHECK_BUSY_RETRY_S = 1


# --------------------------------------------------------------------------- #
# Shared background state (one server, one user — a simple module-level model).
# --------------------------------------------------------------------------- #
class _State:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        # Cached result of engine.link_is_broken(). That call spawns signal-cli, which
        # /api/state (polled every couple of seconds) must never wait on, so the check
        # runs on a worker and the poll only ever reads this.
        self.link_broken = False
        self.link_checked_at = 0.0   # monotonic; 0 = never checked
        self.link_checking = False
        self.reset_send()
        self.reset_link()
        self.reset_refresh()
        self.reset_notes()

    def reset_send(self) -> None:
        self.send_running = False
        self.send_done = 0
        self.send_total = 0
        self.send_current = ""      # name of the group most recently in flight (live view)
        self.send_log: list[str] = []
        self.send_summary: dict | None = None
        self.send_error: str | None = None
        self.failed: list[tuple[str, str]] = []
        self.stop = threading.Event()

    def reset_link(self) -> None:
        self.link_running = False
        self.link_uri: str | None = None
        self.link_uri_ts = 0.0            # when the current URI was issued (codes live ~60s)
        self.link_qr: str | None = None   # base64 PNG data (no prefix)
        self.link_scanned = False         # True once signal-cli reports post-QR activity (a scan)
        self.link_linked = False
        self.link_error: str | None = None
        self.link_proc: subprocess.Popen | None = None  # live `signal-cli link` proc (to force a fresh code)

    def reset_refresh(self) -> None:
        self.refresh_running = False
        self.refresh_count: int | None = None
        self.refresh_error: str | None = None
        self.refresh_status = ""

    def reset_notes(self) -> None:
        self.notes_running = False
        self.notes_result: dict | None = None
        self.notes_error: str | None = None


@functools.lru_cache(maxsize=2)
def _icon_png(size: int) -> bytes:
    """Small dependency-free teal app icon, generated at the manifest's real size."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    teal = bytes((31, 179, 188, 255))
    dark = bytes((11, 14, 20, 255))
    rows = []
    center, inner, outer = size / 2, size * .12, size * .34
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            distance = ((x - center) ** 2 + (y - center) ** 2) ** .5
            row.extend(teal if distance < inner or outer - size * .025 < distance < outer
                       else dark)
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 9)
    return (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)) +
            chunk(b"IDAT", raw) + chunk(b"IEND", b""))


def create_app(state: _State | None = None) -> Flask:
    app = Flask(__name__)
    # Cap request bodies so a stray or hostile multipart POST can't fill the phone's
    # storage (Flask returns 413 past this). 64 MB comfortably covers a batch of photos.
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024
    st = state or _State()
    app.config["STATE"] = st

    # There is no login (single user, localhost), so the one thing we must defend against
    # is the victim's OWN browser being used against us: a random web page can POST to
    # 127.0.0.1:8787 (CSRF) and DNS-rebinding can make a foreign origin same-origin. Binding
    # to loopback does NOT stop either. Reject any request whose Host isn't loopback, or
    # whose Origin (when present) isn't loopback — closing blind /api/send, /api/unlink,
    # and reads of the live link URI from a cross-site page.
    @app.before_request
    def _guard_local_only():
        if not _local_request(request.host, request.headers.get("Origin")):
            return jsonify(error="Forbidden: this server only answers the phone's own "
                                 "browser (localhost)."), 403

    # ---------------------------------------------------------------- helpers
    def _safe(fn, default):
        try:
            return fn()
        except Exception:
            return default

    def _check_link_health(force: bool = False) -> None:
        """Refresh the cached link_is_broken() verdict on a worker thread.

        Without this the Pixel shows a perfectly normal app whose every send fails,
        because on-disk keys look like a link even after the phone removes this device
        from Linked Devices. engine.link_is_broken() only returns True when signal-cli
        POSITIVELY reports no registered account, so a transient error can't bounce a
        healthy install to the link screen. Re-checked at most every LINK_CHECK_TTL_S,
        and immediately after a run fails (force), never on the poll's own thread."""
        with st.lock:
            if st.link_checking:
                return
            fresh = (time.monotonic() - st.link_checked_at) < LINK_CHECK_TTL_S
            if fresh and not force and st.link_checked_at:
                return
            st.link_checking = True

        def work() -> None:
            broken = _safe(engine.link_is_broken, None)
            with st.lock:
                if broken is not None:
                    st.link_broken = broken
                    st.link_checked_at = time.monotonic()
                else:
                    st.link_checked_at = (time.monotonic() - LINK_CHECK_TTL_S
                                          + LINK_CHECK_BUSY_RETRY_S)
                st.link_checking = False

        threading.Thread(target=work, daemon=True).start()

    def _linked_account() -> str | None:
        """The real linked number, or None. Single source of truth for "are we linked?"
        used by BOTH /api/state and /api/link so they never disagree. Requires on-disk keys
        AND a saved real number — load_config() rejects the placeholder, and save_account()
        only runs on a *successful* link, so a merely-started/aborted link reads as None
        (which fixes the premature "Linked!" that left the page stuck on the link screen)."""
        if not _safe(engine.is_linked, False):
            return None
        # On-disk keys survive the phone removing this device from Linked Devices, so
        # they alone don't prove a usable link. Kick off (or reuse) the cached health
        # check and treat a POSITIVELY broken link as not linked, which routes the page
        # to the link screen instead of an app where every send fails.
        _check_link_health()
        with st.lock:
            if st.link_broken:
                return None
        cfg = _safe(engine.load_config, None)
        return getattr(cfg, "account", None) if cfg else None

    # --------------------------------------------------------------- top page
    @app.get("/")
    def index():
        return PAGE

    @app.get("/manifest.webmanifest")
    def manifest():
        return Response(json.dumps({
            "name": "Signal Broadcast",
            "short_name": "Broadcast",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "background_color": "#0B0E14",
            "theme_color": "#0B0E14",
            "icons": [
                {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
            ],
        }), mimetype="application/manifest+json")

    @app.get("/icon-<int:size>.png")
    def icon(size: int):
        if size not in (192, 512):
            return jsonify(error="Unknown icon size."), 404
        return Response(_icon_png(size), mimetype="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/sw.js")
    def service_worker():
        # Cache only the shell and icons. API data is private and must always come from
        # the live localhost process; when it is down, the shell disables write actions.
        source = """
const CACHE='signal-broadcast-shell-v1';
const SHELL=['/','/manifest.webmanifest','/icon-192.png','/icon-512.png'];
self.addEventListener('install',event=>event.waitUntil(caches.open(CACHE).then(c=>c.addAll(SHELL))));
self.addEventListener('activate',event=>event.waitUntil(self.clients.claim()));
self.addEventListener('fetch',event=>{
  if(event.request.method!=='GET'||new URL(event.request.url).pathname.startsWith('/api/'))return;
  event.respondWith(fetch(event.request).then(response=>{
    const copy=response.clone(); caches.open(CACHE).then(c=>c.put(event.request,copy)); return response;
  }).catch(()=>caches.match(event.request)));
});
""".strip()
        return Response(source, mimetype="application/javascript",
                        headers={"Service-Worker-Allowed": "/"})

    # ------------------------------------------------------------------ state
    @app.get("/api/state")
    def api_state():
        cfg = _safe(engine.load_config, None)
        account = _linked_account()  # single source of truth for "are we linked?"
        linked = bool(account)
        entries = _safe(engine.read_group_entries, [])
        with st.lock:
            send = {
                "running": st.send_running,
                "done": st.send_done,
                "total": st.send_total,
                "summary": st.send_summary,
                "error": st.send_error,
                "failed_count": len(st.failed),
            }
        return jsonify({
            "linked": linked,
            "account": account,
            "groups_total": len(entries),
            "groups_enabled": sum(1 for e in entries if e.enabled),
            "message": _safe(engine.read_message, ""),
            "message_style": getattr(cfg, "message_style", engine.DEFAULT_MESSAGE_STYLE)
                             if cfg else engine.DEFAULT_MESSAGE_STYLE,
            "styles": [{"key": k, "label": l} for k, l in engine.MESSAGE_STYLE_LABELS],
            "attachments": [Path(p).name for p in _safe(engine.read_attachments, [])],
            "base_delay": getattr(cfg, "base_delay_seconds", 10) if cfg else 10,
            "jitter": getattr(cfg, "jitter_seconds", 3) if cfg else 3,
            "cooldown_hours": getattr(cfg, "cooldown_hours", 0) if cfg else 0,
            "send": send,
        })

    # ---------------------------------------------------------------- message
    @app.post("/api/message")
    def api_message():
        data = request.get_json(force=True, silent=True) or {}
        engine.write_message(str(data.get("message", "")))
        return jsonify(ok=True)

    @app.post("/api/style")
    def api_style():
        """The whole-message text style. Kept separate from /api/message because that
        one fires on every keystroke (debounced) — the style only changes on a tap."""
        data = request.get_json(force=True, silent=True) or {}
        style = engine.normalize_message_style(data.get("style"))
        engine.set_config_value("message_style", style)
        return jsonify(ok=True, style=style)

    @app.post("/api/upload")
    def api_upload():
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        saved = list(_safe(engine.read_attachments, []))
        for f in request.files.getlist("images"):
            if not f.filename:
                continue
            # The UI only offers image/* files; ignore anything else so a crafted POST
            # can't drop scripts or arbitrary types into the uploads dir.
            if not (f.mimetype or "").startswith("image/"):
                continue
            name = secure_filename(f.filename)  # strips path separators + odd chars
            if not name:
                continue
            dest = UPLOAD_DIR / name
            f.save(str(dest))
            if str(dest) not in saved:
                saved.append(str(dest))
        engine.write_attachments(saved)
        return jsonify(attachments=[Path(p).name for p in saved])

    @app.post("/api/attachments/clear")
    def api_attachments_clear():
        engine.write_attachments([])
        return jsonify(ok=True)

    # ------------------------------------------------------------------- send
    def _run_broadcast(cfg, groups, message, attachments):
        def on_log(msg):
            with st.lock:
                st.send_log.append(str(msg))
                st.send_log[:] = st.send_log[-400:]

        def on_progress(done, total, name, status, secs):
            with st.lock:
                st.send_done, st.send_total = done, total
                if name:
                    st.send_current = str(name)  # for the live "Sending to N — <group>" line

        try:
            results = engine.broadcast(
                config=cfg, groups=groups, message=message, attachments=attachments,
                on_log=on_log, on_progress=on_progress,
                should_stop=lambda: st.stop.is_set(),
            )
            engine.stamp_run()
            engine.write_run_summary(results)
            failed = [(r.group_id, r.name) for r in results
                      if not r.ok and not r.skipped and not r.uncertain]
            with st.lock:
                st.failed = failed
                st.send_summary = {
                    "sent": sum(1 for r in results if r.ok),
                    "failed": len(failed),
                    "uncertain": sum(1 for r in results if r.uncertain),
                    "skipped": sum(1 for r in results if r.skipped),
                    "breakdown": engine.failure_breakdown(results),
                }
            # A run where nothing got through is the signal that the link may be dead.
            # Re-check now rather than waiting out the TTL, so the very next poll can
            # route to the link screen instead of leaving them to retry a dead install.
            if failed and not any(r.ok for r in results):
                _check_link_health(force=True)
        except Exception as exc:  # BroadcastError or anything unexpected
            with st.lock:
                st.send_error = str(exc)
            _check_link_health(force=True)
        finally:
            with st.lock:
                st.send_running = False

    @app.post("/api/send")
    def api_send():
        data = request.get_json(force=True, silent=True) or {}
        with st.lock:
            if st.send_running:
                return jsonify(error="A send is already running."), 409
        try:
            cfg = engine.load_config()
            message = engine.read_message()
            attachments = engine.read_attachments()
            if data.get("only_failed"):
                with st.lock:
                    groups = list(st.failed)
                if not groups:
                    return jsonify(error="No failed groups to resend."), 400
            else:
                groups = engine.read_groups()
        except engine.BroadcastError as exc:
            return jsonify(error=str(exc)), 400
        if not message.strip():
            return jsonify(error="Write a message first."), 400

        if not data.get("force"):
            blocked = engine.cooldown_blocks_run(getattr(cfg, "cooldown_hours", 0))
            if blocked:
                return jsonify(cooldown=blocked), 200

        with st.lock:
            if st.send_running:
                return jsonify(error="A send is already running."), 409
            st.reset_send()
            st.send_running = True
            st.send_total = len(groups)
        threading.Thread(target=_run_broadcast,
                         args=(cfg, groups, message, attachments), daemon=True).start()
        return jsonify(started=True, total=len(groups))

    @app.get("/api/progress")
    def api_progress():
        with st.lock:
            return jsonify({
                "running": st.send_running,
                "done": st.send_done,
                "total": st.send_total,
                "current": st.send_current,
                "log": st.send_log[-60:],
                "summary": st.send_summary,
                "error": st.send_error,
                "failed_count": len(st.failed),
            })

    @app.post("/api/stop")
    def api_stop():
        with st.lock:
            st.stop.set()
        return jsonify(ok=True)

    # ----------------------------------------------------------------- groups
    @app.get("/api/groups")
    def api_groups():
        entries = _safe(engine.read_group_entries, [])
        return jsonify(groups=[{"id": e.group_id, "name": e.name, "enabled": e.enabled}
                               for e in entries])

    @app.post("/api/groups")
    def api_groups_save():
        with st.lock:
            if st.refresh_running:
                return jsonify(error="Wait for the group sync to finish."), 409
        data = request.get_json(force=True, silent=True) or {}
        engine.write_group_selection(set(data.get("enabled", [])))
        return jsonify(ok=True)

    def _run_refresh(account):
        def progress(message: str) -> None:
            with st.lock:
                st.refresh_status = message

        try:
            count = engine.sync_groups(account, on_log=progress)
            with st.lock:
                st.refresh_count = count
        except Exception as exc:
            with st.lock:
                st.refresh_error = str(exc)
        finally:
            with st.lock:
                st.refresh_running = False

    def _start_refresh(account: str) -> bool:
        with st.lock:
            if st.refresh_running:
                return False
            if st.notes_running:
                raise engine.BroadcastError("Wait for the notes check to finish.")
            st.reset_refresh()
            st.refresh_running = True
        threading.Thread(target=_run_refresh, args=(account,), daemon=True).start()
        return True

    @app.post("/api/groups/refresh")
    def api_groups_refresh():
        acct = _linked_account()
        if not acct:
            return jsonify(error="Not linked yet."), 400
        try:
            if not _start_refresh(acct):
                return jsonify(running=True)
        except engine.BroadcastError as exc:
            return jsonify(error=str(exc)), 409
        return jsonify(started=True)

    @app.get("/api/groups/refresh")
    def api_groups_refresh_status():
        with st.lock:
            return jsonify(running=st.refresh_running, count=st.refresh_count,
                           error=st.refresh_error, status=st.refresh_status)

    # ------------------------------------------------------------------ notes
    def _browser_note(note: dict) -> dict:
        return {
            "ts": int(note.get("ts", 0)),
            "text": str(note.get("text") or ""),
            "photos": len(note.get("photos") or []),
            "missing_photos": int(note.get("missing_photos") or 0),
            "view_once_photos": int(note.get("view_once_photos") or 0),
            "missing_body": bool(note.get("missing_body")),
        }

    @app.get("/api/notes")
    def api_notes():
        return jsonify(notes=[_browser_note(note)
                              for note in _safe(engine.read_notes, [])])

    def _run_notes(account: str) -> None:
        try:
            report = engine.fetch_notes(account, on_log=lambda *_: None)
            with st.lock:
                st.notes_result = {key: int(report.get(key, 0))
                                   for key in ("transcripts", "notes", "new")}
        except Exception as exc:
            with st.lock:
                st.notes_error = str(exc)
        finally:
            with st.lock:
                st.notes_running = False

    @app.post("/api/notes/refresh")
    def api_notes_refresh():
        account = _linked_account()
        if not account:
            return jsonify(error="Not linked yet."), 400
        with st.lock:
            if st.refresh_running:
                return jsonify(error="Wait for the groups sync to finish."), 409
            if st.notes_running:
                return jsonify(running=True)
            st.reset_notes()
            st.notes_running = True
        threading.Thread(target=_run_notes, args=(account,), daemon=True).start()
        return jsonify(started=True)

    @app.get("/api/notes/refresh")
    def api_notes_refresh_status():
        with st.lock:
            return jsonify(running=st.notes_running, result=st.notes_result,
                           error=st.notes_error)

    @app.post("/api/notes/use")
    def api_notes_use():
        data = request.get_json(force=True, silent=True) or {}
        try:
            timestamp = int(data.get("ts"))
        except (TypeError, ValueError):
            return jsonify(error="Choose a note first."), 400
        note = next((item for item in _safe(engine.read_notes, [])
                     if int(item.get("ts", 0)) == timestamp), None)
        if not note:
            return jsonify(error="That note is no longer here."), 404
        missing = int(note.get("missing_photos") or 0)
        missing_parts = []
        if note.get("missing_body"):
            missing_parts.append("the complete text")
        if missing:
            missing_parts.append(f"{missing} photo{'s' if missing != 1 else ''}")
        if missing_parts:
            return jsonify(error=(
                f"This note is missing {' and '.join(missing_parts)}. Forward the original "
                "message to Note to Self again, then check for new notes.")), 409
        stored_photos = list(note.get("photos") or [])
        photos = [str(photo["path"]) for photo in stored_photos
                  if photo.get("path") and Path(photo["path"]).is_file()]
        gone = len(stored_photos) - len(photos)
        if gone:
            return jsonify(error=(
                f"{gone} photo file{'s' if gone != 1 else ''} disappeared from this Pixel. "
                "Forward the original message to Note to Self again, then check for new notes.")), 409
        engine.write_message(str(note.get("text") or ""))
        engine.write_attachments(photos)
        return jsonify(ok=True, message=str(note.get("text") or ""),
                       attachments=[Path(path).name for path in photos])

    @app.delete("/api/notes/<int:timestamp>")
    def api_notes_delete(timestamp: int):
        engine.delete_note(timestamp)
        return jsonify(ok=True)

    # --------------------------------------------------------------- schedule
    @app.get("/api/schedule")
    def api_schedule():
        cfg = _safe(engine.load_config, None)
        times = getattr(cfg, "send_times", []) if cfg else []
        enabled = _cron_installed()
        return jsonify(times=times, enabled=enabled,
                       # Mirror the Mac Schedule tab: show when it'll next fire and how the
                       # last run went, in plain language.
                       next_send=(_next_send_str(times) if enabled else None),
                       last_send=_last_send_dict())

    @app.post("/api/schedule")
    def api_schedule_save():
        data = request.get_json(force=True, silent=True) or {}
        times = [str(t).strip() for t in data.get("times", []) if str(t).strip()]
        try:
            engine.parse_times(times)  # validate HH:MM
        except engine.BroadcastError as exc:
            return jsonify(error=str(exc)), 400
        engine.save_send_times(times)
        if data.get("enabled"):
            if not _cron_write(times):
                # crontab missing or the write failed — do NOT report "on", or the user
                # trusts a schedule that will never fire.
                return jsonify(error="Couldn't install the cron schedule — is cron running "
                                     "in the guest? Run scripts/setup-termux.sh, then retry."), 500
        else:
            _cron_clear()  # best-effort: if there's no crontab there's nothing to turn off
        return jsonify(ok=True, enabled=bool(data.get("enabled")),
                       next_send=(_next_send_str(times) if data.get("enabled") else None),
                       note=("Scheduled sends run in the background. To make them reliable, "
                             "keep the phone plugged in and finish the one-time background "
                             "setup (Termux:Boot + wake lock) — see the Scheduling section "
                             "of PIXEL-SETUP.md."))

    # -------------------------------------------------------------- link/unlink
    def _linklog(msg: str) -> None:
        """Append raw link diagnostics to logs/link-debug.txt (readable via docker exec).
        Helps see exactly what signal-cli reports when a QR is scanned."""
        try:
            p = engine.LOGS_DIR / "link-debug.txt"
            p.parent.mkdir(parents=True, exist_ok=True)
            if p.exists() and p.stat().st_size > 1_000_000:  # reset so retries can't grow it forever
                p.unlink()
            with open(p, "a", encoding="utf-8") as f:
                f.write(time.strftime("%H:%M:%S ") + msg.rstrip("\n") + "\n")
        except Exception:
            pass

    def _one_link_attempt() -> bool:
        """Run a single `signal-cli link`, publishing its QR, and let it run to its natural
        end — a scan (success) or Signal expiring the code and closing the socket. Returns
        True if it linked. We never terminate early for a "refresh", so a scan already in
        progress is never cut off; the caller just issues a fresh QR once this one ends."""
        argv, env = engine.signal_cli_command(
            "--config", str(engine.DATA_DIR), "link", "-n", DEVICE_NAME)
        with st.lock:
            # The previous attempt's code is dead the moment its process ends — never let the
            # page (or a mid-flight "Open Signal" tap) grab it while the new JVM boots, which
            # takes tens of seconds under proot on the phone.
            st.link_uri = None
            st.link_qr = None
            st.link_scanned = False
        proc = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
        with st.lock:
            st.link_proc = proc  # so /api/link/fresh can end this attempt and issue a new code

        _linklog("--- attempt start ---")

        # A post-QR line that signals a genuine scan/provisioning vs. one that's just an
        # error (the link code expiring prints "Link request error: Connection closed!",
        # which must NOT masquerade as "Scanned ✓").
        _ERR_WORDS = ("error", "closed", "exception", "expired", "timeout", "timed out",
                      "failed", "invalid", "refused", "reset", "warn", "unable")

        def _reader():  # grab the sgnl:// URI as soon as signal-cli prints it
            assert proc.stdout is not None
            try:
                have_uri = False
                for line in proc.stdout:
                    line = line.strip()
                    if line.startswith("sgnl://linkdevice") or line.startswith("tsdevice:"):
                        have_uri = True
                        _linklog("URI generated")
                        with st.lock:
                            st.link_uri = line
                            st.link_uri_ts = time.time()
                            st.link_qr = _qr_png_b64(line)
                            st.link_scanned = False
                    elif line:
                        _linklog("out: " + line[:160])
                        if have_uri and not any(w in line.lower() for w in _ERR_WORDS):
                            with st.lock:
                                st.link_scanned = True
            finally:
                proc.stdout.close()
        rt = threading.Thread(target=_reader, daemon=True)
        rt.start()

        try:
            proc.wait(timeout=LINK_HANG_GUARD_S)
        except subprocess.TimeoutExpired:
            proc.terminate()                      # only reached if signal-cli truly hung
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            rt.join(timeout=2)
            _linklog("attempt hung → terminated")
            return False
        rt.join(timeout=2)  # make sure the URI/QR was captured before we decide
        _linklog(f"attempt ended rc={proc.returncode}")
        # Process ended within the window: a scan (success) or the server closed it.
        acct = engine.detect_account()
        _linklog(f"detect_account -> {acct!r}")
        if acct:
            engine.save_account(acct)
            with st.lock:
                st.link_linked = True
                # A fresh link supersedes any earlier "broken" verdict. Clearing it
                # here (rather than waiting out the TTL) is what lets the page leave
                # the link screen immediately after a successful re-link.
                st.link_broken = False
                st.link_checked_at = time.monotonic()
            return True
        return False

    def _run_link():
        # Auto-refreshing loop: keep issuing a fresh QR until the user links or LINK_TOTAL_S
        # elapses, so the code on screen never goes stale. Each signal-cli link code is only
        # valid ~60s; /api/link/fresh ends the current attempt so the loop hands out a new one
        # right when the user is about to confirm (the key to single-phone linking).
        deadline = time.time() + LINK_TOTAL_S
        try:
            while time.time() < deadline:
                if st.link_linked:
                    return
                with engine.signal_cli_operation("linking"):
                    linked = _one_link_attempt()
                if linked:
                    # Linking is complete once the account is saved. Group import is
                    # useful but optional and can take minutes on a large backlog, so
                    # run it independently after the UI is free to enter the app.
                    account = engine.load_config().account
                    _start_refresh(account)
                    return
            with st.lock:
                if not st.link_linked and not st.link_error:
                    st.link_error = "Linking timed out. Tap Start linking to try again."
        except Exception as exc:
            with st.lock:
                st.link_error = str(exc)
        finally:
            with st.lock:
                st.link_running = False
                st.link_proc = None

    @app.post("/api/link/start")
    def api_link_start():
        if _linked_account():
            return jsonify(linked=True)
        with st.lock:
            if st.link_running:
                return jsonify(running=True)
            st.reset_link()
            st.link_running = True
        threading.Thread(target=_run_link, daemon=True).start()
        return jsonify(started=True)

    @app.post("/api/link/fresh")
    def api_link_fresh():
        """Force a brand-new link code. Tapping 'Open Signal' calls this first so the code
        the user is about to confirm has its full ~60s validity window (the fix for the
        single-phone 'Connection closed' race). Starts the loop if it isn't running yet."""
        if _linked_account():
            return jsonify(linked=True)
        with st.lock:
            running = st.link_running
            p = st.link_proc
            if not running:
                st.reset_link()
                st.link_running = True
        if not running:
            threading.Thread(target=_run_link, daemon=True).start()
            return jsonify(started=True)
        if p is not None and p.poll() is None:
            with st.lock:
                # Kill the on-screen code the instant we abandon it, so nothing can open
                # Signal with a code whose provisioning socket is already gone.
                st.link_uri = None
                st.link_qr = None
                st.link_scanned = False
            try:
                p.terminate()  # ends the current attempt → the loop immediately issues a fresh code
            except Exception:
                pass
        return jsonify(ok=True)

    @app.get("/api/link")
    def api_link_status():
        # "linked" here means the SAME thing /api/state means (a real account was saved),
        # so the page never shows "Linked!" while state still says otherwise.
        with st.lock:
            return jsonify(running=st.link_running, uri=st.link_uri, qr=st.link_qr,
                           age=(round(time.time() - st.link_uri_ts, 1) if st.link_uri else None),
                           scanned=st.link_scanned,
                           linked=bool(st.link_linked or _linked_account()),
                           error=st.link_error)

    @app.post("/api/unlink")
    def api_unlink():
        try:
            engine.unlink()
        except engine.BroadcastError as exc:
            return jsonify(error=str(exc)), 409
        _cron_clear()      # also remove any scheduled cron (engine.unlink handles launchd only)
        with st.lock:
            st.reset_send()
            st.reset_link()
            st.reset_refresh()
        return jsonify(ok=True)

    return app


# --------------------------------------------------------------------------- #
# small utilities
# --------------------------------------------------------------------------- #
_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


def _hostname_only(host_value: str | None) -> str | None:
    """The bare hostname from a Host/Origin value: port stripped, IPv6 brackets kept
    ("[::1]:8787" -> "[::1]", "127.0.0.1:8787" -> "127.0.0.1", "localhost" -> "localhost")."""
    if not host_value:
        return None
    h = host_value.strip()
    if h.startswith("["):
        return h.split("]", 1)[0] + "]"
    return h.rsplit(":", 1)[0] if ":" in h else h


def _local_request(host: str | None, origin: str | None) -> bool:
    """Trust a request only if its Host is loopback AND (when sent) its Origin is loopback.
    The Host check defeats DNS-rebinding (a foreign name resolved to 127.0.0.1); the Origin
    check defeats cross-site CSRF POSTs. Both attacks originate in the victim's own browser,
    which is why binding to 127.0.0.1 alone doesn't stop them."""
    if _hostname_only(host) not in _ALLOWED_HOSTS:
        return False
    if origin and _hostname_only(urlparse(origin).netloc) not in _ALLOWED_HOSTS:
        return False
    return True


def _qr_png_b64(text: str) -> str | None:
    try:
        out = subprocess.run([engine.qrencode_bin(), "-o", "-", "-t", "PNG", "-s", "6", text],
                             capture_output=True, timeout=15)
        if out.returncode == 0 and out.stdout:
            return base64.b64encode(out.stdout).decode("ascii")
    except Exception:
        return None
    return None


CRON_TAG = engine.CRON_TAG


def _crontab_read() -> str:
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def _cron_installed() -> bool:
    return CRON_TAG in _crontab_read()


def _cron_write(times: list[str]) -> bool:
    lines = [ln for ln in _crontab_read().splitlines() if CRON_TAG not in ln]
    for e in engine.parse_times(times):
        lines.append(engine.format_cron_line(e["Hour"], e["Minute"]))
    return _crontab_set("\n".join(ln for ln in lines if ln.strip()) + "\n")


def _cron_clear() -> bool:
    lines = [ln for ln in _crontab_read().splitlines() if CRON_TAG not in ln]
    return _crontab_set("\n".join(ln for ln in lines if ln.strip()) + "\n")


def _crontab_set(text: str) -> bool:
    """Install a crontab; return True only on a confirmed success. A missing `crontab`
    binary or a non-zero exit returns False so callers can surface it — the Schedule tab
    must not report "on" when the write silently failed and no send will ever fire."""
    try:
        r = subprocess.run(["crontab", "-"], input=text, text=True,
                           capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def _next_send_str(times: list[str]) -> str | None:
    """Plain-language 'next fire' for the saved times, on the server clock — which is the
    same clock cron uses, so this matches when a send will actually go out. e.g.
    'today 16:30' / 'tomorrow 09:00'. None if there are no valid times."""
    now = datetime.now()
    best: datetime | None = None
    for t in times:
        try:
            hh, mm = str(t).split(":")
            cand = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        except (ValueError, TypeError):
            continue
        if cand <= now:
            cand += timedelta(days=1)
        if best is None or cand < best:
            best = cand
    if best is None:
        return None
    if best.date() == now.date():
        day = "today"
    elif best.date() == (now + timedelta(days=1)).date():
        day = "tomorrow"
    else:
        day = best.strftime("%a")
    return f"{day} {best:%H:%M}"


def _last_send_dict() -> dict | None:
    """Counts-only summary of the last broadcast, formatted for the Schedule tab. Reuses
    engine.read_run_summary() (cross-platform), so nothing macOS-specific is touched."""
    try:
        s = engine.read_run_summary()
    except Exception:
        s = None
    if not s:
        return None
    try:
        at = datetime.fromisoformat(str(s.at)).strftime("%b %d, %H:%M")
    except (ValueError, TypeError):
        at = str(getattr(s, "at", ""))
    return {"at": at, "total": s.total, "sent": s.sent, "failed": s.failed,
            "skipped": s.skipped, "uncertain": s.uncertain}


# --------------------------------------------------------------------------- #
# The single-page UI (self-contained: no external CSS/JS/fonts, works offline).
# --------------------------------------------------------------------------- #
PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0B0E14">
<meta name="color-scheme" content="dark">
<meta name="mobile-web-app-capable" content="yes">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/icon-192.png">
<title>Broadcast</title>
<style>
  :root{
    --bg:#0B0E14; --bg2:#0e131c; --card:#141925; --card2:#1a2131; --line:#232a3a;
    --fg:#EEF2F7; --muted:#8b93a7; --faint:#5b6274;
    --accent:#2fc7d4;                 /* solid accent for icons/links/focus */
    --grad:linear-gradient(135deg,#2AD9C0 0%,#2B8BFF 100%);
    --ok:#3AD07A; --warn:#F0B52C; --err:#FF5C57;
    --r:16px; --tap:48px;
  }
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  html,body{margin:0}
  body{background:var(--bg);color:var(--fg);min-height:100dvh;
    font:16px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
    padding-bottom:calc(72px + env(safe-area-inset-bottom))}
  /* atmosphere: soft violet + teal glow behind everything */
  body::before{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;
    background:
      radial-gradient(120% 60% at 85% -10%, rgba(124,108,255,.16), transparent 60%),
      radial-gradient(90% 50% at -10% 0%, rgba(43,200,190,.10), transparent 55%);}
  .glyph{width:26px;height:26px;flex:0 0 auto}
  /* ---------- header ---------- */
  header{position:sticky;top:0;z-index:20;display:flex;align-items:center;justify-content:space-between;
    gap:10px;padding:calc(10px + env(safe-area-inset-top)) 16px 10px;
    background:rgba(11,14,20,.82);backdrop-filter:blur(12px);border-bottom:1px solid var(--line)}
  .brand{display:flex;align-items:center;gap:9px;min-width:0}
  .wm{font-weight:800;letter-spacing:.14em;font-size:14px}
  .wm b{background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent}
  .hright{display:flex;align-items:center;gap:6px;min-width:0}
  .acct{font-size:12px;max-width:38vw;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .iconbtn{background:none;border:0;color:var(--muted);width:40px;height:40px;border-radius:12px;
    display:grid;place-items:center;cursor:pointer}
  .iconbtn:active{background:var(--card2)}
  .iconbtn svg{width:20px;height:20px}
  /* ---------- layout ---------- */
  main{max-width:520px;margin:0 auto;padding:16px}
  .card{background:linear-gradient(180deg,var(--card),var(--bg2));border:1px solid var(--line);
    border-radius:var(--r);padding:16px;margin-bottom:14px}
  .card-h{display:flex;align-items:center;justify-content:space-between;gap:8px;
    font-size:13px;font-weight:700;letter-spacing:.02em;color:var(--fg);margin-bottom:12px}
  .muted{color:var(--muted)} .small{font-size:13px} .center{text-align:center}
  .ok{color:var(--ok)} .err{color:var(--err)} .warn{color:var(--warn)}
  code{background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:1px 6px;font-size:13px}
  /* ---------- inputs ---------- */
  textarea,input[type=text],input[type=time]{width:100%;background:var(--bg);color:var(--fg);
    border:1px solid var(--line);border-radius:12px;padding:13px 14px;font-size:16px;font-family:inherit}
  textarea{min-height:120px;resize:vertical;line-height:1.5}
  textarea:focus,input:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px rgba(47,199,212,.18)}
  /* ---------- buttons ---------- */
  .btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;width:100%;
    min-height:var(--tap);padding:12px 16px;border:0;border-radius:14px;font-size:16px;font-weight:650;
    font-family:inherit;cursor:pointer;color:#06121a;background:var(--grad);text-decoration:none}
  .btn svg{width:19px;height:19px}
  .btn.big{min-height:56px;font-size:17px;border-radius:16px}
  .btn.ghost{background:transparent;border:1px solid var(--line);color:var(--fg);font-weight:600}
  .btn.ghost:active{background:var(--card2)}
  .btn.danger{background:transparent;border:1px solid rgba(255,92,87,.5);color:var(--err);font-weight:600}
  .btn.sm{min-height:var(--tap);padding:8px 12px;font-size:14px;border-radius:11px}
  .btn:disabled{opacity:.45;cursor:default}
  .btn.primary:not(:disabled){box-shadow:0 6px 20px -8px rgba(43,139,255,.6)}
  /* subtle idle sheen on the big send button */
  @media (prefers-reduced-motion:no-preference){
    .btn.big:not(:disabled){position:relative;overflow:hidden}
    .btn.big:not(:disabled)::after{content:"";position:absolute;inset:0;
      background:linear-gradient(115deg,transparent 30%,rgba(255,255,255,.28) 50%,transparent 70%);
      transform:translateX(-120%);animation:sheen 4.5s ease-in-out infinite}
    @keyframes sheen{0%,60%{transform:translateX(-120%)}80%,100%{transform:translateX(120%)}}
  }
  .btn:active{transform:translateY(1px)}
  .btnrow{display:flex;gap:8px}
  .btnrow .btn{flex:1}
  /* ---------- send tab ---------- */
  .saved{color:var(--ok);font-weight:600;font-size:12px;transition:opacity .3s}
  .attrow{display:flex;gap:8px;margin-top:10px}
  .attrow .btn{flex:1}
  .stylerow{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
  .stylebtn{border-radius:999px;padding:7px 13px;min-height:var(--tap);font-size:13px;font-weight:600;cursor:pointer;
    background:transparent;border:1px solid var(--line);color:var(--muted)}
  .stylebtn.on{color:var(--fg);border-color:var(--accent);background:rgba(47,199,212,.14)}
  .stylebtn:active{transform:translateY(1px)}
  /* Live preview: the box shows roughly what Signal will render. Spoiler has no
     equivalent (Signal hides it until tapped) and blurring the box you're typing in
     would be unusable, so it previews as plain text and the hint explains it. */
  #msg.s-italic{font-style:italic}
  #msg.s-bold{font-weight:700}
  #msg.s-bold_italic{font-weight:700;font-style:italic}
  #msg.s-monospace{font-family:ui-monospace,Menlo,Consolas,monospace}
  #msg.s-strikethrough{text-decoration:line-through}
  .atts{margin-top:10px;display:flex;flex-wrap:wrap;gap:6px}
  .pill{display:inline-flex;align-items:center;gap:5px;background:var(--bg);border:1px solid var(--line);
    border-radius:999px;padding:4px 11px;font-size:13px;max-width:100%}
  .pill span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .est{margin-top:10px;text-align:center;font-size:13px}
  .coach{display:flex;gap:10px;align-items:flex-start;border-color:rgba(47,199,212,.4);
    background:linear-gradient(180deg,rgba(47,199,212,.08),var(--bg2))}
  .coach .btn{width:auto;margin-top:8px}
  /* live console */
  .console{margin-top:14px}
  .console-top{display:flex;align-items:baseline;justify-content:space-between;gap:8px;margin-bottom:8px}
  .console-top b{font-size:15px}
  .bar{height:10px;background:var(--bg);border:1px solid var(--line);border-radius:99px;overflow:hidden}
  .bar>span{display:block;height:100%;width:0;background:var(--grad);border-radius:99px;transition:width .4s ease}
  @media (prefers-reduced-motion:no-preference){
    .bar.live>span{background:linear-gradient(90deg,#2AD9C0,#2B8BFF,#2AD9C0);background-size:200% 100%;
      animation:flow 1.3s linear infinite}
    @keyframes flow{to{background-position:-200% 0}}
  }
  .cgroup{margin-top:8px;font-size:13px;min-height:18px}
  .logwrap{margin-top:10px}
  .logwrap summary{cursor:pointer;color:var(--muted);font-size:13px;list-style:none}
  .logwrap summary::-webkit-details-marker{display:none}
  .logwrap summary::before{content:"▸ ";color:var(--accent)}
  .logwrap[open] summary::before{content:"▾ "}
  #log{font:12px/1.5 ui-monospace,Menlo,Consolas,monospace;background:var(--bg);border:1px solid var(--line);
    border-radius:10px;padding:10px;max-height:34vh;overflow:auto;white-space:pre-wrap;margin-top:8px}
  /* result */
  .result{margin-top:14px;border:1px solid var(--line);border-radius:12px;padding:14px;background:var(--bg)}
  .result.good{border-color:rgba(58,208,122,.5)} .result.bad{border-color:rgba(240,181,44,.5)}
  .result h3{margin:0 0 8px;font-size:16px}
  .chips{display:flex;flex-wrap:wrap;gap:7px;margin-top:8px}
  .chip{border-radius:999px;padding:4px 11px;font-size:13px;font-weight:600;border:1px solid var(--line)}
  .chip.s{color:var(--ok);border-color:rgba(58,208,122,.4)}
  .chip.f{color:var(--err);border-color:rgba(255,92,87,.4)}
  .chip.u{color:var(--warn);border-color:rgba(240,181,44,.4)}
  .chip.k{color:var(--muted)}
  /* ---------- groups ---------- */
  .search{margin-top:12px}
  .grow{display:flex;align-items:center;gap:12px;padding:12px 4px;border-bottom:1px solid var(--line);cursor:pointer}
  .grow:last-child{border:0}
  .grow input{width:22px;height:22px;flex:0 0 auto;accent-color:var(--accent)}
  .grow span{flex:1;font-size:15px;overflow:hidden;text-overflow:ellipsis}
  .groups{margin:6px 0 14px;max-height:52vh;overflow:auto}
  .selrow{display:flex;gap:8px;margin-top:12px}
  .selrow .btn{flex:1}
  .empty{padding:22px 6px;text-align:center;color:var(--muted)}
  /* ---------- notes ---------- */
  .notes{display:flex;flex-direction:column;gap:10px;margin-top:12px}
  .note{background:var(--bg);border:1px solid var(--line);border-radius:12px;padding:12px}
  .note-meta{font-size:12px;color:var(--muted);margin-bottom:7px}
  .note-text{white-space:pre-wrap;overflow-wrap:anywhere;max-height:9em;overflow:auto}
  .note-actions{display:flex;gap:7px;margin-top:10px}
  .note-actions .btn{flex:1;width:auto}
  /* ---------- schedule ---------- */
  .sched-status{font-size:17px;font-weight:700;display:flex;align-items:center;gap:8px}
  .dotr{width:9px;height:9px;border-radius:99px;background:var(--faint)}
  .sched-status.on .dotr{background:var(--ok);box-shadow:0 0 0 4px rgba(58,208,122,.18)}
  .timechips{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0}
  .tchip{display:inline-flex;align-items:center;gap:8px;background:var(--bg);border:1px solid var(--line);
    border-radius:999px;padding:7px 8px 7px 14px;font-size:15px;font-variant-numeric:tabular-nums}
  .tchip button{background:none;border:0;color:var(--muted);width:var(--tap);height:var(--tap);border-radius:99px;
    font-size:16px;cursor:pointer;display:grid;place-items:center}
  .tchip button:active{background:var(--card2);color:var(--err)}
  .addtime{display:flex;gap:8px;align-items:center}
  .addtime input[type=time]{flex:1}
  .explain{margin-top:14px;border-top:1px solid var(--line);padding-top:12px}
  .explain summary{cursor:pointer;color:var(--accent);font-size:14px;font-weight:600;list-style:none}
  .explain summary::-webkit-details-marker{display:none}
  .explain summary::before{content:"❔ "}
  .explain p{color:var(--muted);font-size:13.5px;margin:10px 0 0}
  a.ext{color:var(--accent)}
  /* ---------- settings ---------- */
  .kv{display:flex;justify-content:space-between;gap:12px;padding:9px 0;border-bottom:1px solid var(--line);font-size:15px}
  .kv:last-of-type{border:0}
  .kv .v{text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  /* ---------- link screen ---------- */
  .hero{text-align:center;padding:22px 8px 6px}
  .hero .glyph{width:58px;height:58px;margin-bottom:12px}
  .hero h1{font-size:23px;margin:0 0 8px;font-weight:750}
  .hero p{color:var(--muted);margin:0 auto;max-width:340px;font-size:14.5px}
  .qrwrap{position:relative;width:min(74vw,290px);margin:6px auto 4px;padding:12px;background:#fff;border-radius:16px}
  img.qr{width:100%;display:block;image-rendering:pixelated;border-radius:6px}
  @media (prefers-reduced-motion:no-preference){
    .qrwrap::after{content:"";position:absolute;inset:0;border-radius:16px;
      box-shadow:0 0 0 0 rgba(47,199,212,.5);animation:pulse 2.2s ease-out infinite}
    @keyframes pulse{0%{box-shadow:0 0 0 0 rgba(47,199,212,.45)}100%{box-shadow:0 0 0 16px rgba(47,199,212,0)}}
  }
  .linkstatus{display:flex;align-items:center;justify-content:center;gap:8px;margin:12px 0;font-size:14px}
  .linkstatus .dot{width:8px;height:8px;border-radius:99px;background:var(--accent)}
  @media (prefers-reduced-motion:no-preference){.linkstatus .dot{animation:blink 1.2s ease-in-out infinite}}
  @keyframes blink{50%{opacity:.25}}
  .linkstatus.done{color:var(--ok)} .linkstatus.done .dot{background:var(--ok);animation:none}
  .linkstatus.working{color:var(--accent)}
  .linkstatus.working .dot{width:16px;height:16px;background:none;border:2px solid rgba(47,199,212,.3);
    border-top-color:var(--accent);animation:spin .8s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}
  /* ---------- bottom nav ---------- */
  nav{position:fixed;left:0;right:0;bottom:0;z-index:20;display:flex;
    padding-bottom:env(safe-area-inset-bottom);
    background:rgba(13,17,25,.9);backdrop-filter:blur(12px);border-top:1px solid var(--line)}
  nav button{flex:1;background:none;border:0;color:var(--muted);cursor:pointer;
    display:flex;flex-direction:column;align-items:center;gap:3px;padding:9px 4px 8px;font-size:11px;font-weight:600;
    position:relative}
  nav button svg{width:23px;height:23px}
  nav button.active{color:var(--accent)}
  nav button.active::before{content:"";position:absolute;top:0;left:50%;transform:translateX(-50%);width:26px;height:3px;border-radius:0 0 4px 4px;background:var(--grad)}
  /* ---------- toast + banner ---------- */
  .toasts{position:fixed;left:0;right:0;bottom:calc(78px + env(safe-area-inset-bottom));z-index:40;
    display:flex;flex-direction:column;align-items:center;gap:8px;pointer-events:none}
  .toast{max-width:88vw;background:var(--card2);border:1px solid var(--line);border-radius:12px;
    padding:11px 15px;font-size:14px;box-shadow:0 10px 30px -10px rgba(0,0,0,.7)}
  .toast.ok{border-color:rgba(58,208,122,.5)} .toast.err{border-color:rgba(255,92,87,.5)}
  @media (prefers-reduced-motion:no-preference){.toast{animation:rise .25s ease both}}
  .netbanner{position:sticky;top:0;z-index:30;background:var(--warn);color:#231a02;text-align:center;
    font-size:13px;font-weight:600;padding:6px}
  @media (prefers-reduced-motion:no-preference){
    .card{animation:rise .4s ease both}
    .tabpane>.card:nth-child(2){animation-delay:.05s}
    .tabpane>.card:nth-child(3){animation-delay:.1s}
    @keyframes rise{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
  }
  .hidden{display:none!important}
</style></head>
<body>
<!-- shared gradient for all glyphs -->
<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>
  <linearGradient id="tg" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="#2AD9C0"/><stop offset="1" stop-color="#2B8BFF"/></linearGradient>
</defs></svg>

<div id="netbanner" class="netbanner hidden">Reconnecting to the app…</div>

<header>
  <div class="brand">
    <svg class="glyph" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="12" cy="12" r="2.2" fill="url(#tg)"/>
      <path d="M8.3 8.3a5.2 5.2 0 0 0 0 7.4" stroke="url(#tg)" stroke-width="1.8" stroke-linecap="round"/>
      <path d="M15.7 8.3a5.2 5.2 0 0 1 0 7.4" stroke="url(#tg)" stroke-width="1.8" stroke-linecap="round"/>
      <path d="M5.5 5.5a9.2 9.2 0 0 0 0 13" stroke="url(#tg)" stroke-width="1.8" stroke-linecap="round" opacity=".5"/>
      <path d="M18.5 5.5a9.2 9.2 0 0 1 0 13" stroke="url(#tg)" stroke-width="1.8" stroke-linecap="round" opacity=".5"/>
    </svg>
    <span class="wm"><b>BROADCAST</b></span>
  </div>
  <div class="hright">
    <span id="acct" class="acct muted"></span>
    <button class="iconbtn" onclick="refreshState(true)" aria-label="Refresh">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">
        <path d="M21 4v6h-6"/><path d="M3 20v-6h6"/><path d="M20 10a8 8 0 0 0-14-3L3 10"/><path d="M4 14a8 8 0 0 0 14 3l3-3"/></svg>
    </button>
  </div>
</header>

<main>
  <!-- ===================== LINK SCREEN ===================== -->
  <section id="linkScreen" class="hidden">
    <div class="hero">
      <svg class="glyph" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <circle cx="12" cy="12" r="2.2" fill="url(#tg)"/>
        <path d="M8.3 8.3a5.2 5.2 0 0 0 0 7.4" stroke="url(#tg)" stroke-width="1.6" stroke-linecap="round"/>
        <path d="M15.7 8.3a5.2 5.2 0 0 1 0 7.4" stroke="url(#tg)" stroke-width="1.6" stroke-linecap="round"/>
        <path d="M5.5 5.5a9.2 9.2 0 0 0 0 13" stroke="url(#tg)" stroke-width="1.6" stroke-linecap="round" opacity=".5"/>
        <path d="M18.5 5.5a9.2 9.2 0 0 1 0 13" stroke="url(#tg)" stroke-width="1.6" stroke-linecap="round" opacity=".5"/>
      </svg>
      <h1>Connect your Signal</h1>
      <p>Links this phone as a device — just like Signal Desktop. No login, no new account,
         and your number never leaves your phone.</p>
    </div>
    <div class="card">
      <button id="linkBtn" class="btn primary big" onclick="startLink()">Start linking</button>
      <div id="linkOut" class="hidden">
        <div id="linkStatus" class="linkstatus"><span class="dot"></span> <span id="linkStatusTxt">Getting your secure code ready…</span></div>
        <div id="linkReady" class="hidden">
          <button id="deep" class="btn primary" onclick="openInSignal()">Open Signal on this phone</button>
          <p class="muted small center" style="margin:10px 0 0"><b>On this phone:</b> tap the button
             above — Signal opens, then tap <b>Link device</b> <u>right away</u> and come back here.</p>
          <details class="explain" style="margin-top:12px">
            <summary>Linking from another device instead?</summary>
            <div class="qrwrap"><img id="qr" class="qr" alt="Signal link code"></div>
            <p class="muted small center">On your other device open <b>Signal → Settings → Linked
               devices → ＋</b> and scan this code. <a class="ext" href="#" onclick="freshCode();return false">Get a fresh code</a></p>
          </details>
        </div>
        <p id="linkMsg" class="small center"></p>
      </div>
    </div>
  </section>

  <!-- ===================== MAIN APP ===================== -->
  <section id="app" class="hidden">
    <!-- SEND -->
    <div id="tab-send" class="tabpane">
      <div id="coach" class="card coach hidden"></div>
      <div class="card">
        <div class="card-h">Your message <span id="saved" class="saved hidden">Saved ✓</span></div>
        <textarea id="msg" placeholder="Type the message you want to broadcast…" oninput="onMsgInput()"></textarea>
        <div id="styleRow" class="stylerow"></div>
        <div id="styleHint" class="muted small" style="margin-top:6px"></div>
        <div class="attrow">
          <button class="btn ghost sm" onclick="pickImgs()">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8.5" cy="8.5" r="1.6"/><path d="m21 15-5-5L5 21"/></svg>
            Add photos</button>
          <button id="clearImgBtn" class="btn ghost sm hidden" onclick="clearImgs()">Clear photos</button>
        </div>
        <input type="file" id="imgs" accept="image/*" multiple class="hidden" onchange="upload()">
        <div id="atts" class="atts"></div>
      </div>
      <div class="card">
        <button id="sendBtn" class="btn primary big" onclick="send(false,false)">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2 11 13"/><path d="M22 2 15 22l-4-9-9-4Z"/></svg>
          <span id="sendLabel">Send</span></button>
        <div id="est" class="est muted"></div>

        <div id="console" class="console hidden">
          <div class="console-top"><b id="cStat">Sending…</b><span id="cTime" class="muted small"></span></div>
          <div class="bar live" id="barWrap"><span id="bar"></span></div>
          <div id="cGroup" class="cgroup muted"></div>
          <button class="btn ghost" style="margin-top:12px" onclick="stopSend()">Stop</button>
          <details class="logwrap"><summary>Show activity</summary><div id="log"></div></details>
        </div>

        <div id="result" class="result hidden"></div>
        <button id="resendBtn" class="btn ghost hidden" style="margin-top:12px" onclick="send(false,true)">Resend failed groups</button>
      </div>
    </div>

    <!-- GROUPS -->
    <div id="tab-groups" class="tabpane hidden">
      <div class="card">
        <div class="card-h">Groups <span id="grpCount" class="muted small"></span></div>
        <button class="btn primary" id="syncBtn" onclick="refreshGroups()">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M21 4v6h-6"/><path d="M3 20v-6h6"/><path d="M20 10a8 8 0 0 0-14-3L3 10"/><path d="M4 14a8 8 0 0 0 14 3l3-3"/></svg>
          <span id="syncLabel">Sync from phone</span></button>
        <div id="lastSync" class="muted small" style="margin-top:8px;text-align:center"></div>
        <input id="grpSearch" class="search hidden" type="text" placeholder="Search groups…" oninput="filterGroups()">
        <div class="selrow">
          <button class="btn ghost sm" onclick="selectAll(true)">Select all</button>
          <button class="btn ghost sm" onclick="selectAll(false)">Select none</button>
        </div>
        <div id="groups" class="groups"></div>
        <button class="btn primary" onclick="saveGroups()">Save selection</button>
        <div id="grpMsg" class="muted small center" style="margin-top:10px"></div>
      </div>
    </div>

    <!-- NOTES -->
    <div id="tab-notes" class="tabpane hidden">
      <div class="card">
        <div class="card-h">Note to Self</div>
        <button class="btn primary" id="notesBtn" onclick="refreshNotes()">Check for new notes</button>
        <div id="notesStatus" class="muted small center" style="margin-top:9px"></div>
        <div id="notes" class="notes"></div>
      </div>
    </div>

    <!-- SCHEDULE -->
    <div id="tab-schedule" class="tabpane hidden">
      <div class="card">
        <div class="card-h">Daily auto-send</div>
        <div id="schStatus" class="sched-status"><span class="dotr"></span> <span id="schStatusTxt">Off</span></div>
        <div id="schNext" class="muted small" style="margin-top:6px"></div>
        <div id="schLast" class="muted small" style="margin-top:2px"></div>

        <div id="timeChips" class="timechips"></div>
        <div class="addtime">
          <input type="time" id="newTime" value="09:00">
          <button class="btn ghost sm" onclick="addTime()" style="width:auto">Add time</button>
        </div>

        <div class="btnrow" style="margin-top:14px">
          <button class="btn primary" onclick="saveSchedule(true)">Turn on</button>
          <button class="btn ghost" onclick="saveSchedule(false)">Turn off</button>
        </div>
        <button class="btn ghost sm" style="margin-top:8px" onclick="updateTimes()">Update times</button>
        <div id="schMsg" class="small" style="margin-top:10px"></div>

        <details class="explain">
          <summary>Will a scheduled send always go out?</summary>
          <p>Scheduled sends run in the background on the phone. Android can pause background
             work to save battery, so treat this as <b>best-effort</b>. To make it reliable:
             keep the phone <b>plugged in</b>, and do the one-time background setup
             (Termux:Boot + wake lock) described in the Scheduling section of
             <b>PIXEL-SETUP.md</b>. For anything you truly can't miss, send it manually.</p>
        </details>
      </div>
    </div>

    <!-- SETTINGS -->
    <div id="tab-settings" class="tabpane hidden">
      <div class="card">
        <div class="card-h">This device</div>
        <div class="kv"><span class="muted">Signal number</span><span id="acct2" class="v">—</span></div>
        <div class="kv"><span class="muted">Connected as</span><span class="v">linked device</span></div>
        <p class="muted small" style="margin:12px 0 0">Private by design: everything runs on this
           phone (127.0.0.1). No account, no login — nothing leaves the device.</p>
        <p class="muted small">To update the app, tap the <b>Update Signal Broadcast</b> icon on
           your home screen.</p>
      </div>
      <div class="card">
        <div class="card-h">Unlink</div>
        <button class="btn danger" onclick="unlink()">Unlink &amp; erase this app's data</button>
        <p class="muted small" style="margin:10px 0 0">Removes this linked device and wipes the
           app's local data. Your Signal account and phone are untouched.</p>
      </div>
    </div>
  </section>
</main>

<nav id="nav" class="hidden">
  <button data-tab="send" class="active" aria-current="page" onclick="tab('send')">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2 11 13"/><path d="M22 2 15 22l-4-9-9-4Z"/></svg>Send</button>
  <button data-tab="groups" onclick="tab('groups')">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9.5" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.9"/><path d="M16 3.1a4 4 0 0 1 0 7.8"/></svg>Groups</button>
  <button data-tab="notes" onclick="tab('notes')">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M5 3h11l3 3v15H5z"/><path d="M16 3v4h4M8 11h8M8 15h8"/></svg>Notes</button>
  <button data-tab="schedule" onclick="tab('schedule')">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/></svg>Schedule</button>
  <button data-tab="settings" onclick="tab('settings')">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 21v-6M4 11V3M12 21v-9M12 8V3M20 21v-4M20 13V3M1 15h6M9 8h6M17 17h6"/></svg>Settings</button>
</nav>

<div id="toasts" class="toasts"></div>

<script>
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
let S={}, offline=false, times=[], schedEnabled=false, sendStart=0, msgTimer=null, heartbeat=null, curUri='', curAge=null;
const sleep=ms=>new Promise(r=>setTimeout(r,ms));

async function api(path,opts){
  try{const r=await fetch(path,opts); setOffline(false); return await r.json().catch(()=>({}));}
  catch(e){ setOffline(true); return {__neterr:true}; }
}
function setOffline(v){
  if(v===offline)return;
  offline=v;
  $('#netbanner').classList.toggle('hidden',!v);
  $$('#app button, #linkScreen button').forEach(button=>{
    if(v){ button.dataset.offlineDisabled=button.disabled?'kept':'added'; button.disabled=true; }
    else{ if(button.dataset.offlineDisabled==='added')button.disabled=false;
      delete button.dataset.offlineDisabled; }
  });
}
function toast(msg,kind){ const t=document.createElement('div'); t.className='toast '+(kind||'');
  t.textContent=msg; $('#toasts').appendChild(t);
  setTimeout(()=>{t.style.opacity='0';t.style.transition='opacity .3s';setTimeout(()=>t.remove(),300);}, 2400); }
function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}

function tab(t){
  $$('#app .tabpane').forEach(d=>d.classList.toggle('hidden',d.id!=='tab-'+t));
  $$('#nav button').forEach(b=>{ const active=b.dataset.tab===t;
    b.classList.toggle('active',active);
    if(active)b.setAttribute('aria-current','page'); else b.removeAttribute('aria-current'); });
  if(t==='groups')loadGroups();
  if(t==='notes')loadNotes();
  if(t==='schedule')loadSchedule();
}

// ---------------- state ----------------
async function refreshState(manual){
  const st=await api('/api/state');
  if(st.__neterr){ if(manual)toast('Can’t reach the app — is it still running?','err'); return; }
  S=st;
  const linked=!!S.linked;
  $('#linkScreen').classList.toggle('hidden',linked);
  $('#app').classList.toggle('hidden',!linked);
  $('#nav').classList.toggle('hidden',!linked);
  $('#acct').textContent=linked?(S.account||''):'';
  if(!linked){
    // reset the link screen to its initial state (e.g. after unlink) — but never while a
    // link attempt is live, or we'd yank the QR out from under a scan in progress.
    if(!linkTimer){ $('#linkBtn').classList.remove('hidden'); $('#linkBtn').disabled=false;
      $('#linkBtn').textContent='Start linking'; $('#linkOut').classList.add('hidden'); }
    return;
  }
  $('#acct2').textContent=S.account||'—';
  if(document.activeElement!==$('#msg'))$('#msg').value=S.message||'';
  renderStyles();
  renderAtts(S.attachments||[]);
  updateSendUI();
  if(S.send&&S.send.running){startPolling();}
  else{renderResult(S.send);}
}

function updateSendUI(){
  const total=S.groups_total||0, en=S.groups_enabled||0;
  const btn=$('#sendBtn'), label=$('#sendLabel'), coach=$('#coach');
  // first-run coaching
  if(total===0){
    coach.classList.remove('hidden');
    coach.innerHTML='<div><b>First, add your groups.</b><div class="muted small" style="margin-top:4px">'+
      'Pull your Signal groups onto this phone, then choose who to send to.</div>'+
      '<button class="btn primary sm" onclick="tab(\'groups\')">Go to Groups →</button></div>';
  }else if(en===0){
    coach.classList.remove('hidden');
    coach.innerHTML='<div><b>No groups selected.</b><div class="muted small" style="margin-top:4px">'+
      'Pick which groups to broadcast to.</div>'+
      '<button class="btn primary sm" onclick="tab(\'groups\')">Choose groups →</button></div>';
  }else{ coach.classList.add('hidden'); }
  const running=S.send&&S.send.running;
  label.textContent = en>0 ? ('Send to '+en+' group'+(en===1?'':'s')) : 'Send';
  btn.disabled = en===0 || running;
  const est=Math.round((en*(S.base_delay||10))/60);
  $('#est').textContent = (en>0 && !running) ? ('About '+(est<1?'under a minute':(est+' min'))+' · keep the phone on') : '';
}

// ---------------- message style ----------------
// Signal ships formatting as separate range metadata, not inside the text, so this
// picker is the only way to send italics — pasting styled text can't carry it.
function renderStyles(){
  const opts=S.styles||[], cur=S.message_style||'none', row=$('#styleRow');
  // Rebuild only when the option set changes; the state poll runs every couple of
  // seconds and blowing away the buttons each time would eat taps.
  if(row.childElementCount!==opts.length){
    row.innerHTML=opts.map(s=>'<button class="stylebtn" data-k="'+esc(s.key)+'" onclick="setStyle(\''+s.key+'\')">'+esc(s.label)+'</button>').join('');
  }
  $$('#styleRow .stylebtn').forEach(b=>b.classList.toggle('on',b.dataset.k===cur));
  $('#msg').className='s-'+cur;
  const label=((opts.find(s=>s.key===cur)||{}).label||cur).toLowerCase();
  $('#styleHint').textContent = cur==='spoiler'
    ? 'Sent hidden — recipients tap to reveal it.'
    : (cur==='none' ? '' : 'The whole message is sent as '+label+'.');
}
async function setStyle(key){
  S.message_style=key; renderStyles();   // optimistic: the tap should feel instant
  const r=await api('/api/style',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({style:key})});
  if(r.__neterr){ toast('Couldn’t save the style','err'); }
}

function renderAtts(a){
  $('#clearImgBtn').classList.toggle('hidden',!a.length);
  $('#atts').innerHTML=a.length
    ? a.map(n=>'<span class="pill"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#8b93a7" stroke-width="1.8"><rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8.5" cy="8.5" r="1.6"/><path d="m21 15-5-5L5 21"/></svg><span>'+esc(n)+'</span></span>').join('')
    : '<span class="muted small">No photos attached</span>';
}

// ---------------- send ----------------
let poll=null;
function pickImgs(){ $('#imgs').click(); }
function onMsgInput(){ if(msgTimer)clearTimeout(msgTimer); msgTimer=setTimeout(saveMsg,600); }
async function saveMsg(){
  const r=await api('/api/message',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:$('#msg').value})});
  if(!r.__neterr){ const s=$('#saved'); s.classList.remove('hidden'); s.style.opacity='1';
    setTimeout(()=>{s.style.opacity='0';},1400); }
}
async function send(force,onlyFailed){
  await saveMsg();
  if(!onlyFailed){
    if((S.groups_enabled||0)===0){ toast('Choose groups first','err'); return; }
    const est=Math.round((S.groups_enabled*(S.base_delay||10))/60);
    const first=($('#msg').value.split('\n')[0]||'').slice(0,70);
    if(!confirm('Broadcast to '+S.groups_enabled+' group'+(S.groups_enabled===1?'':'s')+'?\n\n“'+first+'”\n\nAbout '+(est<1?'a minute':est+' min')+'. Keep the phone awake.'))return;
  }
  const r=await api('/api/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({force,only_failed:onlyFailed})});
  if(r.__neterr){ toast('Couldn’t start — app unreachable','err'); return; }
  if(r.error){ toast(r.error,'err'); return; }
  if(r.cooldown){ if(confirm(r.cooldown+'\n\nSend anyway?'))return send(true,onlyFailed); return; }
  $('#result').classList.add('hidden');
  startPolling();
}
function fmtElapsed(ms){ const s=Math.floor(ms/1000); return Math.floor(s/60)+':'+String(s%60).padStart(2,'0'); }
function startPolling(){
  if(!sendStart)sendStart=Date.now();
  $('#sendBtn').disabled=true;
  $('#console').classList.remove('hidden'); $('#result').classList.add('hidden'); $('#resendBtn').classList.add('hidden');
  $('#barWrap').classList.add('live');
  if(poll)clearInterval(poll);
  poll=setInterval(async()=>{
    const p=await api('/api/progress');
    if(p.__neterr)return;
    const pct=p.total?Math.round(p.done/p.total*100):0;
    $('#bar').style.width=pct+'%';
    $('#cStat').textContent = p.running ? ('Sending — '+p.done+' of '+p.total) : 'Finishing up…';
    $('#cTime').textContent = fmtElapsed(Date.now()-sendStart);
    $('#cGroup').textContent = (p.running && p.current) ? ('Now: '+p.current) : '';
    $('#log').textContent=(p.log||[]).join('\n'); $('#log').scrollTop=1e9;
    if(!p.running){ clearInterval(poll); poll=null; sendStart=0;
      $('#barWrap').classList.remove('live'); $('#console').classList.add('hidden');
      renderResult(p); refreshState(); }
  },1200);
}
function renderResult(p){
  if(!p || (!p.summary && !p.error)){ $('#result').classList.add('hidden'); return; }
  const box=$('#result'); box.classList.remove('hidden');
  if(p.error){ box.className='result bad'; box.innerHTML='<h3 class="err">Send stopped</h3><div class="muted small">'+esc(p.error)+'</div>'; }
  else{ const s=p.summary; const bad=(s.failed||0)>0||(s.uncertain||0)>0;
    box.className='result '+(bad?'bad':'good');
    let chips='<span class="chip s">✓ '+s.sent+' sent</span>';
    if(s.failed)chips+='<span class="chip f">'+s.failed+' failed</span>';
    if(s.uncertain)chips+='<span class="chip u">'+s.uncertain+' uncertain</span>';
    if(s.skipped)chips+='<span class="chip k">'+s.skipped+' skipped</span>';
    box.innerHTML='<h3>'+(bad?'Done, with some issues':'All sent ✓')+'</h3><div class="chips">'+chips+'</div>'+
      (s.breakdown?'<div class="muted small" style="margin-top:8px">'+esc(s.breakdown)+'</div>':'');
  }
  $('#resendBtn').classList.toggle('hidden',!(p.failed_count>0));
}
async function upload(){
  const files=$('#imgs').files; if(!files.length)return;
  toast('Adding '+files.length+' photo'+(files.length===1?'':'s')+'…');
  const fd=new FormData(); for(const f of files)fd.append('images',f);
  const r=await api('/api/upload',{method:'POST',body:fd});
  if(!r.__neterr){ renderAtts(r.attachments||[]); toast('Photos added','ok'); }
  $('#imgs').value='';
}
async function clearImgs(){ await api('/api/attachments/clear',{method:'POST'}); renderAtts([]); toast('Photos cleared'); }
async function stopSend(){ await api('/api/stop',{method:'POST'}); $('#cStat').textContent='Stopping…'; }

// ---------------- groups ----------------
let allGroups=[];
let groupsById=new Map();
async function loadGroups(){
  const r=await api('/api/groups'); if(r.__neterr)return;
  allGroups=r.groups||[];
  groupsById=new Map(allGroups.map(g=>[g.id,g]));
  renderGroups();
  const ls=localStorage.getItem('sb_last_sync');
  $('#lastSync').textContent = ls ? ('Last synced '+ls) : 'Not synced yet on this phone';
  $('#grpSearch').classList.toggle('hidden', allGroups.length<8);
  const refresh=await api('/api/groups/refresh');
  if(!refresh.__neterr&&refresh.running){
    $('#syncBtn').disabled=true; $('#syncLabel').textContent='Syncing…';
    $('#grpMsg').textContent=refresh.status||'Syncing your groups from your phone…';
    watchGroupRefresh();
  }
}
function renderGroups(){
  const q=($('#grpSearch').value||'').toLowerCase();
  const list=allGroups.filter(g=>!q||(g.name||'').toLowerCase().includes(q));
  const el=$('#groups');
  if(!allGroups.length){ el.innerHTML='<div class="empty">No groups yet.<br>Tap <b>Sync from phone</b> above.</div>'; }
  else if(!list.length){ el.innerHTML='<div class="empty">No groups match “'+esc(q)+'”</div>'; }
  else{ el.innerHTML=list.map(g=>'<label class="grow"><input type="checkbox" data-id="'+esc(g.id)+'" '+(g.enabled?'checked':'')+' onchange="setGroupEnabled(this)"><span>'+esc(g.name)+'</span></label>').join(''); }
  updCount();
}
let groupFilterTimer=null;
function filterGroups(){ clearTimeout(groupFilterTimer); groupFilterTimer=setTimeout(renderGroups,120); }
function setGroupEnabled(box){ const group=groupsById.get(box.dataset.id); if(group)group.enabled=box.checked; updCount(); }
function updCount(){
  const sel=allGroups.filter(g=>g.enabled).length;
  $('#grpCount').textContent = allGroups.length ? (sel+' of '+allGroups.length+' selected') : '';
}
function selectAll(v){ $$('#groups input').forEach(c=>{ c.checked=v; const group=groupsById.get(c.dataset.id); if(group)group.enabled=v; }); updCount(); }
async function saveGroups(){
  const enabled=allGroups.filter(g=>g.enabled).map(g=>g.id);
  const r=await api('/api/groups',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled})});
  if(r.__neterr){ toast('Couldn’t save','err'); return; }
  if(r.error){ toast(r.error,'err'); return; }
  toast('Saved '+enabled.length+' group'+(enabled.length===1?'':'s'),'ok');
  refreshState();
}
let grpTimer=null;
function watchGroupRefresh(){
  if(grpTimer)clearInterval(grpTimer);
  checkGroupRefresh();
  grpTimer=setInterval(checkGroupRefresh,1500);
}
async function checkGroupRefresh(){
  const s=await api('/api/groups/refresh'); if(s.__neterr)return;
  if(s.status)$('#grpMsg').textContent=s.status;
  if(s.running)return;
  if(grpTimer)clearInterval(grpTimer); grpTimer=null;
  $('#syncBtn').disabled=false; $('#syncLabel').textContent='Sync from phone';
  await loadNotes();
  if(s.error){ $('#grpMsg').innerHTML='<span class="err">'+esc(s.error)+'</span>';
    toast('Sync failed: '+s.error,'err'); loadGroups(); }
  else{ const now=new Date(); localStorage.setItem('sb_last_sync', now.toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}));
    $('#grpMsg').textContent='Updated — '+(s.count||0)+' groups.';
    toast('Synced '+(s.count||0)+' group'+(s.count===1?'':'s'),'ok'); loadGroups(); refreshState(); }
}
async function refreshGroups(){
  $('#syncBtn').disabled=true; $('#syncLabel').textContent='Syncing…';
  $('#grpMsg').textContent='Syncing your groups from your phone…';
  const r=await api('/api/groups/refresh',{method:'POST'});
  if(r.error){ toast(r.error,'err'); $('#syncBtn').disabled=false; $('#syncLabel').textContent='Sync from phone'; return; }
  watchGroupRefresh();
}

// ---------------- notes ----------------
let allNotes=[], notesTimer=null;
async function loadNotes(){
  const r=await api('/api/notes'); if(r.__neterr)return;
  allNotes=r.notes||[]; renderNotes();
  const status=await api('/api/notes/refresh');
  if(!status.__neterr&&status.running){
    $('#notesBtn').disabled=true; $('#notesBtn').textContent='Checking…';
    $('#notesStatus').textContent='Checking your phone…'; watchNotesRefresh();
  }
}
function renderNotes(){
  const el=$('#notes');
  if(!allNotes.length){ el.innerHTML='<div class="empty">No notes yet.<br>Write one in Note to Self, then check.</div>'; return; }
  el.innerHTML=allNotes.map(note=>{
    const when=new Date(note.ts).toLocaleString([], {day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'});
    const kept=note.photos||0, missing=note.missing_photos||0, transient=note.view_once_photos||0;
    const details=[];
    if(kept)details.push(kept+' photo'+(kept===1?'':'s')+' attached');
    if(missing)details.push(missing+' photo'+(missing===1?'':'s')+' not downloaded');
    if(transient)details.push(transient+' view-once photo'+(transient===1?'':'s')+' not kept');
    if(note.missing_body)details.push('full text not downloaded');
    return '<article class="note"><div class="note-meta">'+esc(when+(details.length?' · '+details.join(' · '):''))+'</div>'+
      '<div class="note-text">'+esc(note.text||'(photo only)')+'</div><div class="note-actions">'+
      '<button class="btn primary sm" onclick="useNote('+note.ts+')">Use</button>'+
      '<button class="btn ghost sm" onclick="copyNote('+note.ts+')">Copy</button>'+
      '<button class="btn ghost sm" onclick="deleteNote('+note.ts+')">Delete</button></div></article>';
  }).join('');
}
function watchNotesRefresh(){
  if(notesTimer)clearInterval(notesTimer);
  checkNotesRefresh(); notesTimer=setInterval(checkNotesRefresh,1000);
}
async function checkNotesRefresh(){
  const s=await api('/api/notes/refresh'); if(s.__neterr||s.running)return;
  if(notesTimer)clearInterval(notesTimer); notesTimer=null;
  $('#notesBtn').disabled=false; $('#notesBtn').textContent='Check for new notes';
  await loadNotes();
  if(s.error){ $('#notesStatus').innerHTML='<span class="err">'+esc(s.error)+'</span>'; return; }
  const result=s.result||{}, fresh=result.new||0;
  if(fresh)$('#notesStatus').innerHTML='<span class="ok">'+fresh+' new note'+(fresh===1?'':'s')+'.</span>';
  else if(result.notes)$('#notesStatus').textContent='Nothing new — those notes are already here.';
  else if(result.transcripts)$('#notesStatus').textContent='Messages arrived, but none were notes to yourself.';
  else $('#notesStatus').textContent='Signal had nothing waiting. Write a note on your phone, wait a few seconds, then check again.';
}
async function refreshNotes(){
  $('#notesBtn').disabled=true; $('#notesBtn').textContent='Checking…';
  $('#notesStatus').textContent='Checking your phone…';
  const r=await api('/api/notes/refresh',{method:'POST'});
  if(r.error){ $('#notesBtn').disabled=false; $('#notesBtn').textContent='Check for new notes';
    $('#notesStatus').innerHTML='<span class="err">'+esc(r.error)+'</span>'; return; }
  watchNotesRefresh();
}
async function useNote(ts){
  const r=await api('/api/notes/use',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ts})});
  if(r.error){toast(r.error,'err');return;}
  $('#msg').value=r.message||''; S.message=r.message||''; renderAtts(r.attachments||[]);
  tab('send'); toast('Note loaded into the message','ok');
}
async function copyNote(ts){
  const note=allNotes.find(item=>item.ts===ts); if(!note)return;
  try{await navigator.clipboard.writeText(note.text||''); toast('Copied','ok');}
  catch(_error){toast('Couldn’t copy this note','err');}
}
async function deleteNote(ts){
  if(!confirm('Delete this note from the Pixel? Your phone keeps it.'))return;
  const r=await api('/api/notes/'+ts,{method:'DELETE'}); if(r.error){toast(r.error,'err');return;}
  await loadNotes(); $('#notesStatus').textContent='Note deleted from this Pixel. Your phone keeps it.';
}

// ---------------- schedule ----------------
function normTime(t){ const m=/^(\d{1,2}):(\d{2})$/.exec((t||'').trim()); if(!m)return null;
  let h=+m[1], mi=+m[2]; if(h>23||mi>59)return null; return String(h).padStart(2,'0')+':'+m[2]; }
async function loadSchedule(){
  const r=await api('/api/schedule'); if(r.__neterr)return;
  times=(r.times||[]).map(normTime).filter(Boolean); schedEnabled=!!r.enabled;
  renderChips();
  const st=$('#schStatus'), txt=$('#schStatusTxt');
  st.classList.toggle('on',schedEnabled);
  txt.textContent = schedEnabled ? ('On — daily at '+(times.join(', ')||'—')) : 'Off';
  $('#schNext').textContent = (schedEnabled && r.next_send) ? ('Next send: '+r.next_send) : '';
  const l=r.last_send;
  $('#schLast').textContent = l ? ('Last sent '+l.at+' — '+l.sent+' sent'+(l.failed?', '+l.failed+' failed':'')) : '';
  $('#schMsg').textContent='';
}
function renderChips(){
  times=[...new Set(times)].sort();
  $('#timeChips').innerHTML = times.length
    ? times.map(t=>'<span class="tchip">'+t+'<button onclick="removeTime(\''+t+'\')" aria-label="Remove">×</button></span>').join('')
    : '<span class="muted small">No times yet — add one below.</span>';
}
function addTime(){ const t=normTime($('#newTime').value); if(!t){ toast('Pick a valid time','err'); return; }
  if(!times.includes(t))times.push(t); renderChips(); }
function removeTime(t){ times=times.filter(x=>x!==t); renderChips(); }
async function saveSchedule(on){
  if(on && !times.length){ toast('Add at least one time','err'); return; }
  const r=await api('/api/schedule',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({times,enabled:on})});
  if(r.__neterr){ toast('Couldn’t save schedule','err'); return; }
  if(r.error){ $('#schMsg').innerHTML='<span class="err">'+esc(r.error)+'</span>'; return; }
  schedEnabled=on;
  toast(on?'Schedule turned on':'Schedule turned off','ok');
  if(on && r.note){ $('#schMsg').innerHTML='<span class="muted">'+esc(r.note)+'</span>'; } else { $('#schMsg').textContent=''; }
  loadSchedule();
}
function updateTimes(){ saveSchedule(schedEnabled); }

// ---------------- link / unlink ----------------
let linkTimer=null;
function setLinkStatus(mode,text){
  const el=$('#linkStatus');
  el.classList.toggle('working', mode==='working');
  el.classList.toggle('done', mode==='done');
  $('#linkStatusTxt').textContent=text;
}
// Single-phone handoff. Chrome only honours a custom-scheme (sgnl://) navigation while the
// tap's "user activation" is alive (~5s) — ANY await before location.href gets the launch
// silently dropped ("Open Signal → nothing"). So: if the on-screen code is still young,
// navigate synchronously in the tap. If it's stale, request a fresh one and have the user
// tap again once it lands (the poller flips the button back when the new code is ready —
// a fresh code needs a new signal-cli JVM, which can take 30s+ under proot on the phone).
const FRESH_S=25;                          // codes live ~60s; open only within their youth
let wantOpen=false;
function codeIsFresh(){ return curUri && curAge!==null && curAge<FRESH_S; }
function openInSignal(){
  if(codeIsFresh()){
    setLinkStatus('wait','Signal is opening — tap “Link device” now, then come back.');
    window.location.href=curUri;           // synchronous: inside the user gesture
    return;
  }
  wantOpen=true;
  setLinkStatus('working','Getting a fresh code — takes up to a minute on the phone…');
  api('/api/link/fresh',{method:'POST'});
}
async function freshCode(){                // for the "another device" QR path
  setLinkStatus('working','Getting a fresh code…');
  await api('/api/link/fresh',{method:'POST'});
  for(let i=0;i<120;i++){ await sleep(500); if(codeIsFresh()) break; }  // JVM restart is slow under proot
  setLinkStatus('wait','Fresh code ready — scan it now.');
  toast('Fresh code ready');
}
async function startLink(){
  $('#linkBtn').classList.add('hidden'); $('#linkMsg').textContent='';
  $('#linkOut').classList.remove('hidden'); $('#linkReady').classList.add('hidden');
  $('#linkStatus').classList.remove('done'); $('#linkStatusTxt').textContent='Getting your secure code ready…';
  await api('/api/link/start',{method:'POST'});
  let lastQr='';
  if(linkTimer)clearInterval(linkTimer);
  linkTimer=setInterval(async()=>{
    const s=await api('/api/link'); if(s.__neterr)return;
    // Track the live code + its age; a cleared/rotated code must never be openable.
    if(s.uri){ curUri=s.uri; curAge=(s.age==null?null:s.age); } else { curUri=''; curAge=null; }
    // Show the link options only while we're still waiting for a scan.
    if(s.uri && !s.scanned && !s.linked){ $('#linkReady').classList.remove('hidden');
      if(s.qr && s.qr!==lastQr){ lastQr=s.qr; $('#qr').src='data:image/png;base64,'+s.qr; } }
    // A fresh code the user asked for just landed → re-arm the button (we can't auto-open
    // Signal: the launch needs a real tap).
    if(wantOpen && codeIsFresh() && !s.scanned && !s.linked){ wantOpen=false;
      setLinkStatus('wait','Fresh code ready — tap “Open Signal on this phone” now.');
      toast('Code ready — tap Open Signal now','ok'); }
    if(s.error){ clearInterval(linkTimer); linkTimer=null; $('#linkOut').classList.add('hidden');
      $('#linkBtn').classList.remove('hidden'); $('#linkBtn').disabled=false; $('#linkBtn').textContent='Try again';
      toast(s.error,'err'); return; }
    // Status priority: linked  >  scanned (provisioning)  >  waiting for a scan.
    if(s.linked){ clearInterval(linkTimer); linkTimer=null;
      $('#linkReady').classList.add('hidden'); setLinkStatus('done','Linked! Setting things up…');
      toast('Linked to Signal','ok'); setTimeout(()=>refreshState(),900); }
    else if(s.scanned){ $('#linkReady').classList.add('hidden'); setLinkStatus('working','Scanned ✓ — finishing linking…'); }
    else if(s.uri){ setLinkStatus('wait','Waiting for you to scan…'); }
  },1500);
}
async function unlink(){
  if(!confirm('Unlink and erase this app’s data?\n\nYour Signal account and phone are unaffected.'))return;
  const r=await api('/api/unlink',{method:'POST'});
  if(r.error){ toast(r.error,'err'); return; }
  toast('Unlinked'); refreshState();
}

// ---------------- boot ----------------
refreshState();
heartbeat=setInterval(()=>{ if(document.visibilityState==='visible'&&!(S.send&&S.send.running)) refreshState(); }, 5000);
document.addEventListener('visibilitychange',()=>{ if(document.visibilityState==='visible')refreshState(); });
if('serviceWorker' in navigator)navigator.serviceWorker.register('/sw.js').catch(()=>{});
</script>
</body></html>"""


if __name__ == "__main__":
    import os
    port = int(os.environ.get("SB_WEBUI_PORT", "8787"))
    host = os.environ.get("SB_WEBUI_HOST", "127.0.0.1")  # localhost only = private
    create_app().run(host=host, port=port, threaded=True)
