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
        self.send_log: list[str] = []
        self.send_summary: dict | None = None
        self.send_error: str | None = None
        self.failed: list[tuple[str, str]] = []
        self.stop = threading.Event()

    def reset_link(self) -> None:
        self.link_running = False
        self.link_uri: str | None = None
        self.link_qr: str | None = None   # base64 PNG data (no prefix)
        self.link_linked = False
        self.link_error: str | None = None

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
        return jsonify(times=getattr(cfg, "send_times", []) if cfg else [],
                       enabled=_cron_installed())

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
                       note=("Also do the host-side steps (wake lock + Termux:Boot) — "
                             "see PIXEL-SETUP.md. Scheduled sending is best-effort on Android."))

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

        _linklog("--- attempt start ---")

        def _reader():  # grab the sgnl:// URI as soon as signal-cli prints it
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("sgnl://linkdevice") or line.startswith("tsdevice:"):
                    _linklog("URI generated")
                    with st.lock:
                        st.link_uri = line
                        st.link_qr = _qr_png_b64(line)
                elif line:
                    _linklog("out: " + line[:160])  # non-URI lines (scan/associate/errors)
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
        # Auto-refreshing loop: keep issuing a fresh QR (~every LINK_ATTEMPT_S) until the
        # user scans one or we hit LINK_MAX_ATTEMPTS, so the code on screen never goes stale.
        try:
            for _ in range(LINK_MAX_ATTEMPTS):
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

    @app.get("/api/link")
    def api_link_status():
        # "linked" here means the SAME thing /api/state means (a real account was saved),
        # so the page never shows "Linked!" while state still says otherwise.
        with st.lock:
            return jsonify(running=st.link_running, uri=st.link_uri, qr=st.link_qr,
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


# --------------------------------------------------------------------------- #
# The single-page UI (self-contained: no external CSS/JS/fonts, works offline).
# --------------------------------------------------------------------------- #
PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>Signal Broadcast</title>
<style>
  :root{--bg:#0f1115;--card:#1a1d24;--line:#2a2f3a;--fg:#e8eaed;--muted:#9aa0ab;
        --accent:#3a76f0;--ok:#2ea043;--err:#e5534b;--warn:#d29922}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.4 -apple-system,Roboto,Segoe UI,sans-serif}
  header{padding:14px 16px;background:var(--card);border-bottom:1px solid var(--line);
         display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:5}
  header b{font-size:17px}
  header small{color:var(--muted)}
  main{padding:16px;max-width:640px;margin:0 auto}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:16px}
  h2{font-size:15px;margin:0 0 10px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
  textarea,input[type=text]{width:100%;background:var(--bg);color:var(--fg);border:1px solid var(--line);
        border-radius:8px;padding:10px;font-size:16px}
  textarea{min-height:120px;resize:vertical}
  button{background:var(--accent);color:#fff;border:0;border-radius:8px;padding:12px 16px;font-size:16px;
        font-weight:600;cursor:pointer;width:100%}
  button.secondary{background:transparent;border:1px solid var(--line);color:var(--fg);font-weight:500}
  button:disabled{opacity:.5}
  .row{display:flex;gap:8px;margin-top:10px}
  .row button{width:auto;flex:1}
  nav{display:flex;background:var(--card);border-top:1px solid var(--line);position:sticky;bottom:0}
  nav button{background:none;color:var(--muted);border:0;border-radius:0;padding:12px 4px;font-weight:500;font-size:13px}
  nav button.active{color:var(--accent);box-shadow:inset 0 -2px 0 var(--accent)}
  .g{display:flex;align-items:center;gap:10px;padding:9px 4px;border-bottom:1px solid var(--line)}
  .g:last-child{border:0}
  .g input{width:20px;height:20px;flex:0 0 auto}
  .g label{flex:1;font-size:15px}
  .muted{color:var(--muted)} .ok{color:var(--ok)} .err{color:var(--err)} .warn{color:var(--warn)}
  .bar{height:8px;background:var(--bg);border-radius:6px;overflow:hidden;margin:10px 0}
  .bar>span{display:block;height:100%;background:var(--accent);width:0}
  #log{font:12px/1.5 ui-monospace,Menlo,monospace;background:var(--bg);border:1px solid var(--line);
       border-radius:8px;padding:10px;max-height:200px;overflow:auto;white-space:pre-wrap;margin-top:10px}
  .pill{display:inline-block;background:var(--bg);border:1px solid var(--line);border-radius:999px;
        padding:2px 10px;font-size:13px;margin:2px 4px 2px 0}
  .hidden{display:none}
  img.qr{width:min(72vw,300px);image-rendering:pixelated;background:#fff;padding:10px;border-radius:8px;display:block;margin:10px auto}
  a.link{color:var(--accent);word-break:break-all}
</style></head>
<body>
<header><div><b>Signal Broadcast</b> <small id="acct"></small></div>
  <button class="secondary" style="width:auto;padding:6px 10px" onclick="refreshState()">↻</button>
</header>
<main>
  <!-- LINK SCREEN -->
  <section id="linkScreen" class="hidden">
    <div class="card">
      <h2>Link this phone to Signal</h2>
      <p class="muted">No account or login — this links as a secondary device, exactly like
      Signal Desktop. Your number stays on your phone.</p>
      <button id="linkBtn" onclick="startLink()">Start linking</button>
      <div id="linkOut" class="hidden">
        <img id="qr" class="qr" alt="link QR">
        <p class="ok" style="text-align:center">🔄 This QR refreshes automatically — just scan whatever is shown.</p>
        <p class="muted"><b>To link:</b> on your phone open <b>Signal → Settings → Linked
        Devices → ＋</b> and scan the QR above.</p>
        <p class="muted">On the phone itself you can instead tap
        <a class="link" id="deep" href="#">Open in Signal →</a> (does nothing in a desktop browser).</p>
        <p id="linkMsg" class="muted"></p>
      </div>
    </div>
  </section>

  <!-- MAIN APP -->
  <section id="app" class="hidden">
    <div id="tab-send">
      <div class="card">
        <h2>Message</h2>
        <textarea id="msg" placeholder="Type your message…"></textarea>
        <div class="row">
          <button class="secondary" onclick="document.getElementById('imgs').click()">Add images…</button>
          <button class="secondary" onclick="clearImgs()">Clear images</button>
        </div>
        <input type="file" id="imgs" accept="image/*" multiple class="hidden" onchange="upload()">
        <div id="atts" class="muted" style="margin-top:8px"></div>
      </div>
      <div class="card">
        <button id="sendBtn" onclick="send(false,false)">Send to <span id="gc">0</span> groups</button>
        <div class="row hidden" id="sendingRow">
          <button class="secondary" onclick="stopSend()">Stop</button>
        </div>
        <div class="bar hidden" id="barWrap"><span id="bar"></span></div>
        <div id="status" class="muted" style="margin-top:8px"></div>
        <button id="resendBtn" class="secondary hidden" style="margin-top:10px" onclick="send(false,true)">Resend failed</button>
        <div id="log" class="hidden"></div>
      </div>
    </div>

    <div id="tab-groups" class="hidden">
      <div class="card">
        <h2>Groups</h2>
        <div class="row">
          <button class="secondary" onclick="selectAll(true)">Select all</button>
          <button class="secondary" onclick="selectAll(false)">None</button>
          <button class="secondary" onclick="refreshGroups()">Update from phone</button>
        </div>
        <div id="groups" style="margin-top:10px"></div>
        <button style="margin-top:12px" onclick="saveGroups()">Save selection</button>
        <div id="grpMsg" class="muted" style="margin-top:8px"></div>
      </div>
    </div>

    <div id="tab-schedule" class="hidden">
      <div class="card">
        <h2>Daily schedule</h2>
        <p class="muted">Comma-separated 24-hour times, e.g. <code>09:00, 16:30</code></p>
        <input type="text" id="times" placeholder="09:00, 16:30">
        <div class="row">
          <button onclick="saveSchedule(true)">Turn on</button>
          <button class="secondary" onclick="saveSchedule(false)">Turn off</button>
        </div>
        <div id="schMsg" class="muted" style="margin-top:8px"></div>
        <p class="warn" style="margin-top:10px">Best-effort on Android — also do the host-side
        wake-lock + Termux:Boot steps in PIXEL-SETUP.md.</p>
      </div>
    </div>

    <div id="tab-settings" class="hidden">
      <div class="card">
        <h2>Settings</h2>
        <p class="muted">Account: <span id="acct2"></span></p>
        <button class="secondary" onclick="unlink()">Unlink &amp; erase this app's data</button>
      </div>
    </div>
  </section>
</main>

<nav id="nav" class="hidden">
  <button data-tab="send" class="active" onclick="tab('send')">Send</button>
  <button data-tab="groups" onclick="tab('groups')">Groups</button>
  <button data-tab="schedule" onclick="tab('schedule')">Schedule</button>
  <button data-tab="settings" onclick="tab('settings')">Settings</button>
</nav>

<script>
const $=s=>document.querySelector(s), $$=s=>document.querySelectorAll(s);
let S={};
async function api(path,opts){const r=await fetch(path,opts);return r.json().catch(()=>({}));}
function tab(t){$$('#app>div').forEach(d=>d.classList.add('hidden'));$('#tab-'+t).classList.remove('hidden');
  $$('#nav button').forEach(b=>b.classList.toggle('active',b.dataset.tab===t));
  if(t==='groups')loadGroups(); if(t==='schedule')loadSchedule();}

async function refreshState(){
  S=await api('/api/state');
  const linked=S.linked;
  $('#linkScreen').classList.toggle('hidden',linked);
  $('#app').classList.toggle('hidden',!linked);
  $('#nav').classList.toggle('hidden',!linked);
  $('#acct').textContent=linked?(S.account||''):'';
  if(!linked)return;
  $('#acct2').textContent=S.account||'—';
  $('#gc').textContent=S.groups_enabled;
  if(document.activeElement!==$('#msg'))$('#msg').value=S.message||'';
  renderAtts(S.attachments||[]);
  if(S.send&&S.send.running){startPolling();}
  else{renderSummary(S.send);}
}
function renderAtts(a){$('#atts').innerHTML=a.length?a.map(n=>'<span class="pill">🖼 '+esc(n)+'</span>').join(''):'<span class="muted">No images</span>';}

// ---- send ----
let poll=null;
async function saveMsg(){await api('/api/message',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:$('#msg').value})});}
async function send(force,onlyFailed){
  await saveMsg();
  if(!onlyFailed){
    const est=Math.round((S.groups_enabled*(S.base_delay||10))/60);
    const first=($('#msg').value.split('\n')[0]||'').slice(0,60);
    if(!confirm('Send to '+S.groups_enabled+' groups?\n\n"'+first+'"\n\n~'+est+' min. Keep the phone awake.'))return;
  }
  const r=await api('/api/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({force,only_failed:onlyFailed})});
  if(r.error){$('#status').innerHTML='<span class="err">'+r.error+'</span>';return;}
  if(r.cooldown){if(confirm(r.cooldown+'\n\nSend anyway?'))return send(true,onlyFailed);return;}
  startPolling();
}
function startPolling(){
  $('#sendBtn').disabled=true;$('#sendingRow').classList.remove('hidden');
  $('#barWrap').classList.remove('hidden');$('#log').classList.remove('hidden');$('#resendBtn').classList.add('hidden');
  if(poll)clearInterval(poll);
  poll=setInterval(async()=>{
    const p=await api('/api/progress');
    const pct=p.total?Math.round(p.done/p.total*100):0;
    $('#bar').style.width=pct+'%';
    $('#status').textContent=p.running?('Sending… '+p.done+' / '+p.total):'';
    $('#log').textContent=(p.log||[]).join('\n');$('#log').scrollTop=1e9;
    if(!p.running){clearInterval(poll);poll=null;$('#sendBtn').disabled=false;$('#sendingRow').classList.add('hidden');renderSummary(p);}
  },1500);
}
function renderSummary(p){
  if(!p)return;
  if(p.error){$('#status').innerHTML='<span class="err">'+p.error+'</span>';}
  else if(p.summary){const s=p.summary;
    $('#status').innerHTML='<span class="ok">Done.</span> Sent '+s.sent+', failed '+s.failed+
      (s.uncertain?', uncertain '+s.uncertain:'')+(s.skipped?', skipped '+s.skipped:'')+
      (s.breakdown?'<br><span class="muted">'+s.breakdown+'</span>':'');
    $('#barWrap').classList.remove('hidden');$('#bar').style.width='100%';}
  $('#resendBtn').classList.toggle('hidden',!(p.failed_count>0));
}
async function upload(){
  const fd=new FormData();for(const f of $('#imgs').files)fd.append('images',f);
  const r=await api('/api/upload',{method:'POST',body:fd});renderAtts(r.attachments||[]);$('#imgs').value='';
}
async function clearImgs(){await api('/api/attachments/clear',{method:'POST'});renderAtts([]);}
async function stopSend(){await api('/api/stop',{method:'POST'});$('#status').textContent='Stopping…';}

// ---- groups ----
async function loadGroups(){const r=await api('/api/groups');const el=$('#groups');
  el.innerHTML=(r.groups||[]).map((g,i)=>'<div class="g"><input type="checkbox" id="g'+i+'" data-id="'+g.id+'" '+(g.enabled?'checked':'')+'><label for="g'+i+'">'+esc(g.name)+'</label></div>').join('')||'<p class="muted">No groups yet — Update from phone.</p>';}
function selectAll(v){$$('#groups input').forEach(c=>c.checked=v);}
async function saveGroups(){const ids=[...$$('#groups input')].filter(c=>c.checked).map(c=>c.dataset.id);
  await api('/api/groups',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:ids})});
  $('#grpMsg').textContent='Saved '+ids.length+' groups.';refreshState();}
async function refreshGroups(){$('#grpMsg').textContent='Syncing from phone…';
  const r=await api('/api/groups/refresh',{method:'POST'});if(r.error){$('#grpMsg').innerHTML='<span class="err">'+r.error+'</span>';return;}
  const t=setInterval(async()=>{const s=await api('/api/groups/refresh');if(!s.running){clearInterval(t);
    $('#grpMsg').textContent=s.error?('Error: '+s.error):('Synced '+(s.count||0)+' groups.');loadGroups();refreshState();}},1500);}

// ---- schedule ----
async function loadSchedule(){const r=await api('/api/schedule');$('#times').value=(r.times||[]).join(', ');
  $('#schMsg').textContent=r.enabled?'Currently ON.':'Currently off.';}
async function saveSchedule(on){const times=$('#times').value.split(',').map(s=>s.trim()).filter(Boolean);
  const r=await api('/api/schedule',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({times,enabled:on})});
  $('#schMsg').innerHTML=r.error?'<span class="err">'+r.error+'</span>':((on?'Turned on. ':'Turned off. ')+'<span class="muted">'+(r.note||'')+'</span>');}

// ---- link / unlink ----
async function startLink(){$('#linkBtn').disabled=true;$('#linkMsg').textContent='Requesting a link code…';
  await api('/api/link/start',{method:'POST'});
  const t=setInterval(async()=>{const s=await api('/api/link');
    if(s.uri){$('#linkOut').classList.remove('hidden');$('#deep').href=s.uri;
      if(s.qr)$('#qr').src='data:image/png;base64,'+s.qr;}
    if(s.error){$('#linkMsg').innerHTML='<span class="err">'+s.error+'</span>';$('#linkBtn').disabled=false;}
    if(s.linked){clearInterval(t);$('#linkMsg').innerHTML='<span class="ok">Linked!</span>';setTimeout(refreshState,800);}
  },1500);}
async function unlink(){if(!confirm('Unlink and erase this app\'s local data? Your phone is unaffected.'))return;
  await api('/api/unlink',{method:'POST'});refreshState();}

function esc(s){return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
refreshState();
</script>
</body></html>"""


if __name__ == "__main__":
    import os
    port = int(os.environ.get("SB_WEBUI_PORT", "8787"))
    host = os.environ.get("SB_WEBUI_HOST", "127.0.0.1")  # localhost only = private
    create_app().run(host=host, port=port, threaded=True)
