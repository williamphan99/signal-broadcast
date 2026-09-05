"""Opt-in real Tk widgets against a disposable local service, no Signal traffic."""
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from mac_service import Client, Service, serve, socket_path
from runtime import isolated_engine
from test_mac_security import DirectoryVault, MemoryKeychain, PASSWORD


@unittest.skipUnless(os.environ.get("SB_RUN_MAC_UI") == "1", "Opt-in native Tk UI test")
class MacUITests(unittest.TestCase):
    def test_photo_drag_preview_save_and_lock(self):
        from types import SimpleNamespace
        from mac_app import App
        from test_mac_photos import png_bytes

        class FixtureApp(App):
            def request(self, operation, callback=None, on_error=None, **values):
                self.requests.append((operation, values))
                self.callbacks[operation] = callback
                self.errors[operation] = on_error
                if operation == "save" and callback:
                    callback({"saved": True})
                if operation == "snapshot" and callback:
                    callback(self.fixture_snapshot)

        with tempfile.TemporaryDirectory(prefix="sb-photo-ui-") as directory:
            paths = []
            for name in ("first", "second", "third"):
                path = Path(directory) / (name + ".png")
                path.write_bytes(png_bytes())
                paths.append(str(path))
            app = FixtureApp.__new__(FixtureApp)
            app.requests = []
            app.callbacks, app.errors = {}, {}
            FixtureApp.__init__(app, SimpleNamespace(token="fixture"))
            data = {"linked": True, "message": "Photo order fixture", "attachments": paths,
                    "schedule": {"times": [], "enabled": False}, "config": {
                        "base_delay_seconds": 1, "jitter_seconds": 0,
                        "cooldown_hours": 0, "concurrent_sends": 1},
                    "groups": [{"group_id": "fixture", "name": "Fixture", "enabled": True}],
                    "notes": [{"ts": 1, "text": "Full note " * 30, "photos": []}], "job": None, "events": [], "sequence": 0}
            try:
                app.initial_snapshot(data)
                app.update()
                app.fixture_snapshot = data
                self.assertEqual(app.note_preview.get("1.0", "end-1c"), "Full note " * 30)
                self.assertEqual(app.update_button.cget("text"), "Update")
                app.group_query.set("No match")
                app.save_groups()
                self.assertIn(("groups", {"enabled": ["fixture"]}), app.requests)
                app.group_query.set("")
                app.select_groups(False)
                app.save_groups()
                self.assertEqual(app.requests[-1], ("groups", {"enabled": []}))
                app.select_groups(True)
                app.style.set("Bold")
                app.preview_style()
                app.save()
                self.assertEqual(app.requests[-1][1]["message_style"], "bold")
                strip = app.photo_strip
                deadline = time.monotonic() + 10
                while strip._pending and time.monotonic() < deadline:
                    app.update()
                    time.sleep(0.01)
                self.assertTrue(all(strip._photos.get(path) is not None for path in paths))
                strip.set_open(True)
                app.update()
                x, y = strip._slot_xy(0)
                target_x, target_y = strip._slot_xy(2)
                strip.canvas.event_generate("<Button-1>", x=x+40, y=y+40)
                strip.canvas.event_generate("<B1-Motion>", x=target_x+40, y=target_y+40)
                strip.canvas.event_generate("<ButtonRelease-1>", x=target_x+40, y=target_y+40)
                app.update()
                ordered = [paths[1], paths[2], paths[0]]
                self.assertEqual(app.images, ordered)
                strip._preview_selected()
                deadline = time.monotonic() + 10
                while not strip._previews and time.monotonic() < deadline:
                    app.update()
                    time.sleep(0.01)
                self.assertTrue(strip._previews)
                with mock.patch("mac_app.messagebox.askyesno", return_value=True):
                    app.send()
                saves = [values for op, values in app.requests if op == "save"]
                self.assertEqual(saves[-1]["attachments"], ordered)
                self.assertIn(("job", {"kind": "send"}), app.requests)
                app.fixture_snapshot = {**data, "job": "send", "sequence": 1,
                                        "events": [{"id": 1, "kind": "started", "value": "send"}]}
                app.callbacks["job"]({"started": "send"})
                self.assertEqual(app.operation_text.get(), "Sending…")
                self.assertTrue(app.operation_progress.winfo_manager())
                self.assertEqual(str(app.stop_button.cget("state")), "normal")
                self.assertEqual(str(app.update_button.cget("state")), "disabled")
                app.data["send_progress"] = {"active": 2, "completed": 1, "total": 3}
                app.refresh_elapsed()
                self.assertIn("2 sends in progress", app.activity_hint.get())
                before = app.operation_progress.cget("value")
                deadline = time.monotonic() + 0.15
                while time.monotonic() < deadline:
                    app.update()
                    time.sleep(0.01)
                self.assertNotEqual(app.operation_progress.cget("value"), before)
                app.apply_snapshot({**app.fixture_snapshot, "sequence": 3, "events": [
                    {"id": 2, "kind": "log", "value": "INTERNAL TRACE MUST NOT APPEAR"},
                    {"id": 3, "kind": "progress", "value": {"done": 1, "total": 3, "status": "sent"}}]})
                self.assertIn("1 of 3 groups processed", app.activity.get("1.0", "end"))
                self.assertNotIn("INTERNAL TRACE", app.activity.get("1.0", "end"))
                app.stop()
                self.assertIn("Stopping…", app.operation_text.get())
                self.assertEqual(str(app.stop_button.cget("state")), "disabled")
                self.assertTrue(app.operation_progress.winfo_manager())
                stops = sum(op == "stop" for op, _ in app.requests)
                app.stop()
                self.assertEqual(sum(op == "stop" for op, _ in app.requests), stops)
                from mac_security import SecurityError
                app.errors["stop"](SecurityError("The worker has not exited."))
                self.assertIn("Stop not confirmed", app.notice.get())
                self.assertEqual(app.operation_text.get(), "Sending…")
                app.stop()
                app.fixture_snapshot = {**data, "sequence": 4, "last_operation": {"kind": "send", "outcome": "stopped"},
                    "interrupted": {"remaining": [["fixture", "Fixture"]]},
                    "events": [{"id": 4, "kind": "stopped", "value": "send"}]}
                app.callbacks["stop"]({"stopped": True})
                self.assertIn("Broadcast stopped", app.operation_text.get())
                self.assertIn("Broadcast stopped", app.notice.get())
                self.assertFalse(app.operation_progress.winfo_manager())
                self.assertFalse(app.stop_button.winfo_manager())
                self.assertTrue(app.recovery.winfo_manager())
                # A snapshot started before Stop must not bring back Sending.
                app.apply_snapshot({**data, "sequence": 1, "job": "send"})
                self.assertIn("Broadcast stopped", app.operation_text.get())
                app.apply_snapshot({**data, "sequence": 5})
                app.images = paths * 5
                app.refresh_images()
                app.update()
                self.assertLessEqual(strip.canvas.winfo_height(), 216)
                for geometry in ("760x780", "620x650"):
                    app.geometry(geometry)
                    app.update()
                    for button in (app.send_button, app.save_button):
                        self.assertTrue(button.winfo_viewable(), geometry)
                        self.assertLess(button.winfo_rooty() + button.winfo_height(), app.winfo_rooty() + app.winfo_height())
                strip._tips[0].show()
                self.assertIsNotNone(strip._tips[0].window)
                app.show_login()
                app.update()
                app.login_status({"state": "sealed", "setup_required": False, "attempts_remaining": 3,
                                  "background_running": False, "updating": False, "update": None})
                self.assertTrue(app.update_button.winfo_viewable())
                self.assertEqual(str(app.update_button.cget("state")), "normal")
                app.check_update()
                self.assertEqual(app.requests[-1][0], "update")
                self.assertEqual(str(app.login_button.cget("state")), "disabled")
                self.assertTrue(app.login_update_progress.winfo_manager())
                app.callbacks["update"]({"started": "update"})
                app.callbacks["status"]({"state": "sealed", "setup_required": False, "attempts_remaining": 3,
                    "background_running": False, "updating": False, "update": {"changed": True, "needs_setup": False}})
                self.assertEqual(app.update_button.cget("text"), "Finish update")
                self.assertFalse(app.login_update_progress.winfo_manager())
                with mock.patch("mac_app.messagebox.askyesno", return_value=True):
                    app.check_update()
                self.assertEqual(app.requests[-1][0], "restart_update")
                self.assertFalse(any(op == "erase" for op, _ in app.requests))
                self.assertIsNone(strip._tips[0].window)
                self.assertEqual(app.images, [])
                self.assertFalse(strip._photos)
                self.assertFalse(strip._previews)
                self.assertTrue(strip._ready.empty())
                self.assertFalse(strip.winfo_exists())
            finally:
                app.destroy()

    def test_login_manual_lock_close_reopen_and_erase(self):
        from mac_app import App
        with isolated_engine(), tempfile.TemporaryDirectory(prefix="sb-ui-tests-") as directory:
            root = Path(directory).resolve()
            project = root / "project"
            (project / "signal-cli-data/data").mkdir(parents=True)
            (project / "signal-cli-data/data/account.db").write_text("disposable fixture")
            (project / "config.toml").write_text('account = "+19999999999"\n')
            (project / "groups.txt").write_text("fixture-group\tFixture group\n")
            (project / "message.txt").write_text("PRIVATE UI FIXTURE")
            vault = DirectoryVault(root / "vault", project, MemoryKeychain())
            service = Service(vault, retire=lambda _: None)
            service.authenticate(PASSWORD, setup=True)
            service.lock()
            server = serve(service, socket_path(vault.root))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            app = App(Client(vault.root))
            def pump(predicate, timeout=10):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    app.update()
                    if predicate():
                        return
                    time.sleep(0.01)
                self.fail(f"UI condition not reached; screen={app.screen}, notice={app.notice.get()}")
            try:
                pump(lambda: str(app.login_button.cget("state")) == "normal")
                self.assertEqual(app.screen, "login")
                self.assertEqual(app.data, {})
                app.password.insert(0, PASSWORD)
                app.login()
                pump(lambda: app.screen == "main")
                self.assertEqual(app.message.get("1.0", "end-1c"), "PRIVATE UI FIXTURE")
                # A job is deliberately retained across every interface lifecycle event.
                job = {"kind": "send", "proc": object()}
                service.job = job
                app.lock()
                pump(lambda: app.screen == "login" and service.token is None)
                self.assertIs(service.job, job)
                self.assertEqual(app.data, {})
                self.assertIsNone(app.notes_signature)
                pump(lambda: str(app.login_button.cget("state")) == "normal")
                app.password.insert(0, PASSWORD)
                app.login()
                pump(lambda: app.screen == "main")
                app.tk.call(app.protocol("WM_DELETE_WINDOW"))
                self.assertIsNone(service.token)
                self.assertIs(service.job, job)
                app = App(Client(vault.root))
                pump(lambda: str(app.login_button.cget("state")) == "normal")
                self.assertEqual(app.screen, "login")
                app.password.insert(0, PASSWORD)
                app.login()
                pump(lambda: app.screen == "main")
                app.tk.call("tk::mac::Quit")
                self.assertIsNone(service.token)
                self.assertIs(service.job, job)
                app = App(Client(vault.root))
                pump(lambda: str(app.login_button.cget("state")) == "normal")
                started, release, authenticated = threading.Event(), threading.Event(), threading.Event()
                authenticate = service.authenticate
                def delayed_authentication(*args, **kwargs):
                    started.set()
                    release.wait(5)
                    result = authenticate(*args, **kwargs)
                    authenticated.set()
                    return result
                with mock.patch.object(service, "authenticate", side_effect=delayed_authentication):
                    app.password.insert(0, PASSWORD)
                    app.login()
                    try:
                        pump(started.is_set)
                        app.close()
                    finally:
                        release.set()
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline and not (authenticated.is_set() and service.token is None):
                        time.sleep(0.01)
                    self.assertTrue(authenticated.is_set())
                    self.assertIsNone(service.token)
                    self.assertIs(service.job, job)
                app = App(Client(vault.root))
                pump(lambda: str(app.login_button.cget("state")) == "normal")
                service.job = None
                with mock.patch("mac_app.messagebox.askyesno", return_value=True):
                    app.erase()
                pump(lambda: app.screen == "login" and app.setup_required)
                self.assertFalse(vault.image.exists())
                self.assertIsNone(vault.keychain.load())
            finally:
                service.job = None
                app.destroy()
                server.shutdown()
                server.server_close()
                socket_path(vault.root).unlink(missing_ok=True)
