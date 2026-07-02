#!/usr/bin/env python3
"""Mobile web UI for Signal Broadcast — the Android/Pixel counterpart of the Tkinter
gui.py. Same engine, same three tabs (Send / Groups / Schedule) plus linking and unlink.

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
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, request
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


# --------------------------------------------------------------------------- #
# Shared background state (one server, one user — a simple module-level model).
# --------------------------------------------------------------------------- #
class _State:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.reset_send()
        self.reset_link()
        self.reset_refresh()

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
        self.link_qr: str | None = None   # base64 PNG data (no prefix)
        self.link_scanned = False         # True once signal-cli reports post-QR activity (a scan)
        self.link_linked = False
        self.link_error: str | None = None
        self.link_proc: subprocess.Popen | None = None  # live `signal-cli link` proc (to force a fresh code)

    def reset_refresh(self) -> None:
        self.refresh_running = False
        self.refresh_count: int | None = None
        self.refresh_error: str | None = None


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

    def _linked_account() -> str | None:
        """The real linked number, or None. Single source of truth for "are we linked?"
        used by BOTH /api/state and /api/link so they never disagree. Requires on-disk keys
        AND a saved real number — load_config() rejects the placeholder, and save_account()
        only runs on a *successful* link, so a merely-started/aborted link reads as None
        (which fixes the premature "Linked!" that left the page stuck on the link screen)."""
        if not _safe(engine.is_linked, False):
            return None
        cfg = _safe(engine.load_config, None)
        return getattr(cfg, "account", None) if cfg else None

    # --------------------------------------------------------------- top page
    @app.get("/")
    def index():
        return PAGE

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
        except Exception as exc:  # BroadcastError or anything unexpected
            with st.lock:
                st.send_error = str(exc)
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
        data = request.get_json(force=True, silent=True) or {}
        engine.write_group_selection(set(data.get("enabled", [])))
        return jsonify(ok=True)

    def _run_refresh(account):
        try:
            count = engine.sync_groups(account, on_log=lambda *_: None)
            with st.lock:
                st.refresh_count = count
        except Exception as exc:
            with st.lock:
                st.refresh_error = str(exc)
        finally:
            with st.lock:
                st.refresh_running = False

    @app.post("/api/groups/refresh")
    def api_groups_refresh():
        acct = _linked_account()
        if not acct:
            return jsonify(error="Not linked yet."), 400
        with st.lock:
            if st.refresh_running:
                return jsonify(running=True)
            st.reset_refresh()
            st.refresh_running = True
        threading.Thread(target=_run_refresh, args=(acct,), daemon=True).start()
        return jsonify(started=True)

    @app.get("/api/groups/refresh")
    def api_groups_refresh_status():
        with st.lock:
            return jsonify(running=st.refresh_running, count=st.refresh_count,
                           error=st.refresh_error)

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
                f.write(msg.rstrip("\n") + "\n")
        except Exception:
            pass

    def _one_link_attempt() -> bool:
        """Run a single `signal-cli link`, publishing its QR, and let it run to its natural
        end — a scan (success) or Signal expiring the code and closing the socket. Returns
        True if it linked. We never terminate early for a "refresh", so a scan already in
        progress is never cut off; the caller just issues a fresh QR once this one ends."""
        argv, env = engine.signal_cli_command(
            "--config", str(engine.DATA_DIR), "link", "-n", DEVICE_NAME)
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
            have_uri = False
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("sgnl://linkdevice") or line.startswith("tsdevice:"):
                    have_uri = True
                    _linklog("URI generated")
                    with st.lock:
                        st.link_uri = line
                        st.link_qr = _qr_png_b64(line)
                        st.link_scanned = False  # fresh code on screen → back to "waiting to scan"
                elif line:
                    _linklog("out: " + line[:160])  # non-URI lines (scan/associate/errors)
                    # signal-cli is silent after printing the QR until the phone scans it, so a
                    # post-URI line usually means provisioning began — UNLESS it's an error line
                    # (an expiring code prints "Connection closed!"), which is NOT a scan.
                    if have_uri and not any(w in line.lower() for w in _ERR_WORDS):
                        with st.lock:
                            st.link_scanned = True
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
            _safe(lambda: engine.sync_groups(acct, on_log=lambda *_: None), None)
            with st.lock:
                st.link_linked = True
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
                if st.link_linked or _one_link_attempt():
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
                           scanned=st.link_scanned,
                           linked=bool(st.link_linked or _linked_account()),
                           error=st.link_error)

    @app.post("/api/unlink")
    def api_unlink():
        _cron_clear()      # also remove any scheduled cron (engine.unlink handles launchd only)
        engine.unlink()
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
  .btn.sm{min-height:40px;padding:8px 12px;font-size:14px;border-radius:11px}
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
  /* ---------- schedule ---------- */
  .sched-status{font-size:17px;font-weight:700;display:flex;align-items:center;gap:8px}
  .dotr{width:9px;height:9px;border-radius:99px;background:var(--faint)}
  .sched-status.on .dotr{background:var(--ok);box-shadow:0 0 0 4px rgba(58,208,122,.18)}
  .timechips{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0}
  .tchip{display:inline-flex;align-items:center;gap:8px;background:var(--bg);border:1px solid var(--line);
    border-radius:999px;padding:7px 8px 7px 14px;font-size:15px;font-variant-numeric:tabular-nums}
  .tchip button{background:none;border:0;color:var(--muted);width:24px;height:24px;border-radius:99px;
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
  <button data-tab="send" class="active" onclick="tab('send')">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2 11 13"/><path d="M22 2 15 22l-4-9-9-4Z"/></svg>Send</button>
  <button data-tab="groups" onclick="tab('groups')">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9.5" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.9"/><path d="M16 3.1a4 4 0 0 1 0 7.8"/></svg>Groups</button>
  <button data-tab="schedule" onclick="tab('schedule')">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/></svg>Schedule</button>
  <button data-tab="settings" onclick="tab('settings')">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 21v-6M4 11V3M12 21v-9M12 8V3M20 21v-4M20 13V3M1 15h6M9 8h6M17 17h6"/></svg>Settings</button>
</nav>

<div id="toasts" class="toasts"></div>

<script>
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
let S={}, offline=false, times=[], schedEnabled=false, sendStart=0, msgTimer=null, heartbeat=null, curUri='';
const sleep=ms=>new Promise(r=>setTimeout(r,ms));

async function api(path,opts){
  try{const r=await fetch(path,opts); setOffline(false); return await r.json().catch(()=>({}));}
  catch(e){ setOffline(true); return {__neterr:true}; }
}
function setOffline(v){ if(v===offline)return; offline=v; $('#netbanner').classList.toggle('hidden',!v); }
function toast(msg,kind){ const t=document.createElement('div'); t.className='toast '+(kind||'');
  t.textContent=msg; $('#toasts').appendChild(t);
  setTimeout(()=>{t.style.opacity='0';t.style.transition='opacity .3s';setTimeout(()=>t.remove(),300);}, 2400); }
function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}

function tab(t){
  $$('#app .tabpane').forEach(d=>d.classList.toggle('hidden',d.id!=='tab-'+t));
  $$('#nav button').forEach(b=>b.classList.toggle('active',b.dataset.tab===t));
  if(t==='groups')loadGroups();
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
async function loadGroups(){
  const r=await api('/api/groups'); if(r.__neterr)return;
  allGroups=r.groups||[]; renderGroups();
  const ls=localStorage.getItem('sb_last_sync');
  $('#lastSync').textContent = ls ? ('Last synced '+ls) : 'Not synced yet on this phone';
  $('#grpSearch').classList.toggle('hidden', allGroups.length<8);
}
// Pull the current on-screen checkbox states back into allGroups, so re-rendering
// (search filter, select-all) never loses selections that aren't saved yet.
function syncShown(){ $$('#groups input').forEach(c=>{ const g=allGroups.find(x=>x.id===c.dataset.id); if(g)g.enabled=c.checked; }); }
function renderGroups(){
  const q=($('#grpSearch').value||'').toLowerCase();
  const list=allGroups.filter(g=>!q||(g.name||'').toLowerCase().includes(q));
  const el=$('#groups');
  if(!allGroups.length){ el.innerHTML='<div class="empty">No groups yet.<br>Tap <b>Sync from phone</b> above.</div>'; }
  else if(!list.length){ el.innerHTML='<div class="empty">No groups match “'+esc(q)+'”</div>'; }
  else{ el.innerHTML=list.map(g=>'<label class="grow"><input type="checkbox" data-id="'+esc(g.id)+'" '+(g.enabled?'checked':'')+' onchange="updCount()"><span>'+esc(g.name)+'</span></label>').join(''); }
  updCount();
}
function filterGroups(){ syncShown(); renderGroups(); }
function updCount(){
  syncShown();
  const sel=allGroups.filter(g=>g.enabled).length;
  $('#grpCount').textContent = allGroups.length ? (sel+' of '+allGroups.length+' selected') : '';
}
function selectAll(v){ $$('#groups input').forEach(c=>c.checked=v); updCount(); }
async function saveGroups(){
  syncShown();
  const enabled=allGroups.filter(g=>g.enabled).map(g=>g.id);
  const r=await api('/api/groups',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled})});
  if(r.__neterr){ toast('Couldn’t save','err'); return; }
  toast('Saved '+enabled.length+' group'+(enabled.length===1?'':'s'),'ok');
  refreshState();
}
let grpTimer=null;
async function refreshGroups(){
  $('#syncBtn').disabled=true; $('#syncLabel').textContent='Syncing…';
  const r=await api('/api/groups/refresh',{method:'POST'});
  if(r.error){ toast(r.error,'err'); $('#syncBtn').disabled=false; $('#syncLabel').textContent='Sync from phone'; return; }
  if(grpTimer)clearInterval(grpTimer);
  grpTimer=setInterval(async()=>{
    const s=await api('/api/groups/refresh'); if(s.__neterr||s.running)return;
    clearInterval(grpTimer); grpTimer=null;
    $('#syncBtn').disabled=false; $('#syncLabel').textContent='Sync from phone';
    if(s.error){ toast('Sync failed: '+s.error,'err'); }
    else{ const now=new Date(); localStorage.setItem('sb_last_sync', now.toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}));
      toast('Synced '+(s.count||0)+' group'+(s.count===1?'':'s'),'ok'); loadGroups(); refreshState(); }
  },1500);
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
// Get a brand-new code (full ~60s window) and open Signal with it, so the code the user
// confirms hasn't expired — the fix for the single-phone "Connection closed" race.
async function openInSignal(){
  const before=curUri;
  setLinkStatus('working','Getting a fresh code…');
  await api('/api/link/fresh',{method:'POST'});
  for(let i=0;i<24;i++){                 // wait up to ~12s for the new code to appear
    await sleep(500);
    if(curUri && curUri!==before) break;
  }
  setLinkStatus('wait','Signal is opening — tap “Link device” now, then come back.');
  if(curUri) window.location.href=curUri;
  else toast('Couldn’t get a code — tap Start linking again','err');
}
async function freshCode(){                // for the "another device" QR path
  setLinkStatus('working','Getting a fresh code…');
  const before=curUri;
  await api('/api/link/fresh',{method:'POST'});
  for(let i=0;i<24;i++){ await sleep(500); if(curUri&&curUri!==before) break; }
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
    // Show the link options only while we're still waiting for a scan.
    if(s.uri && !s.scanned && !s.linked){ curUri=s.uri; $('#linkReady').classList.remove('hidden');
      if(s.qr && s.qr!==lastQr){ lastQr=s.qr; $('#qr').src='data:image/png;base64,'+s.qr; } }
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
  await api('/api/unlink',{method:'POST'});
  toast('Unlinked'); refreshState();
}

// ---------------- boot ----------------
refreshState();
heartbeat=setInterval(()=>{ if(!(S.send&&S.send.running)) refreshState(); }, 5000);
</script>
</body></html>"""


if __name__ == "__main__":
    import os
    port = int(os.environ.get("SB_WEBUI_PORT", "8787"))
    host = os.environ.get("SB_WEBUI_HOST", "127.0.0.1")  # localhost only = private
    create_app().run(host=host, port=port, threaded=True)
