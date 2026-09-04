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
