"""Opt-in REAL Mac vault/Keychain/IPC tests. No live Signal account is used.

SB_RUN_MAC_INTEGRATION=1 .venv/bin/python -m unittest discover -s tests -p test_mac_integration.py
SB_LONG_IDLE_SECONDS=310 additionally tests >5 minutes during AND after a job.
Every Keychain item uses a fresh test namespace. No installer's data or jobs are touched.
"""
import os
import json
import plistlib
import signal
import shutil
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import engine
from mac_security import HELPER, Keychain, SecurityError, Vault
from mac_service import Client, Service, serve, socket_path
from runtime import isolated_engine

PASSWORD = "disposable integration password"


@unittest.skipUnless(sys.platform == "darwin" and os.environ.get("SB_RUN_MAC_INTEGRATION") == "1",
                     "Opt-in macOS disk image and Keychain integration")
class MacIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_engine()
        self.scope.__enter__()
        self.addCleanup(self.scope.__exit__, None, None, None)
        self.temp = tempfile.TemporaryDirectory(prefix="sb-mac-integration-", dir="/private/tmp")
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.project.joinpath("config.toml").write_text('account = "+19999999999"\ncooldown_hours = 0\n')
        self.project.joinpath("groups.txt").write_text("test-group\tDisposable group\n")
        self.project.joinpath("message.txt").write_text("DISPOSABLE-PRIVATE-CONTENT")
        # Keep the test's helper immutable, even if Setup rebuilds the checkout's
        # helper during another test. Keychain ACLs bind to its code identity.
        helper = self.root / "mac-security"
        shutil.copy2(HELPER, helper)
        self.keychain = Keychain("com.user.signal-broadcast.test." + secrets.token_hex(16), helper)
        print("Disposable Keychain item:", self.keychain.service, flush=True)
        self.vault = Vault(self.root / "vault", self.project, self.keychain)
        def fake_spawn(argv, **kwargs):
            return subprocess.Popen([sys.executable, str(Path(__file__).with_name("mac_fake_worker.py")), *argv[1:]], **kwargs)
        self.service = Service(self.vault, retire=lambda _: None, spawn=fake_spawn)
        original_handle = self.service.handle
        def diagnose(request):
            try:
                return original_handle(request)
            except SecurityError:
                raise
            except Exception:
                import traceback
                traceback.print_exc()  # Disposable test requests only; never installed in the app.
                raise
        self.service.handle = diagnose
        self.addCleanup(self.cleanup)
        self.service.recover()
        self.server = serve(self.service, socket_path(self.vault.root))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = Client(self.vault.root)
        self.client.call("setup", password=PASSWORD)

    def cleanup(self):
        if hasattr(self, "server"):
            self.server.shutdown()
            self.server.server_close()
        try:
            self.service.stop_job()
        finally:
            try:
                self.keychain.delete()
            finally:
                self.vault.detach()
                socket_path(self.vault.root).unlink(missing_ok=True)
                self.temp.cleanup()

    def wait_until(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail("Condition did not become true within the deadline")

    def test_real_encryption_keychain_restart_and_erase(self):
        self.assertTrue(self.vault.mount.is_mount())
        self.assertFalse(self.project.joinpath("message.txt").exists())
        record = self.keychain.load()
        self.assertNotIn(PASSWORD, str(record))
        image_files = [p for p in self.vault.image.rglob("*") if p.is_file()]
        self.assertTrue(image_files)
        for path in image_files:
            self.assertNotIn(b"DISPOSABLE-PRIVATE-CONTENT", path.read_bytes())
        self.service.shutdown()
        self.assertFalse(self.vault.mount.is_mount())
        self.assertFalse(self.vault.data.joinpath("message.txt").exists())
        self.service.shutting_down = False
        self.client.call("unlock", password=PASSWORD)
        self.assertEqual(self.client.call("snapshot")["message"], "DISPOSABLE-PRIVATE-CONTENT")
        for remaining in (2, 1, 0):
            with self.assertRaises(SecurityError):
                self.client.call("unlock", password="incorrect")
        self.assertIsNone(self.keychain.load())
        self.assertFalse(self.vault.mount.is_mount())
        self.assertFalse(self.vault.image.exists())
        self.assertEqual(self.client.call("status")["state"], "unlinked")

    def test_bundled_signal_cli_offline_rpc_and_private_temp_path(self):
        temporary = self.vault.data / "temporary with spaces"
        temporary.mkdir()
        with mock.patch.dict(os.environ, {"TMPDIR": str(temporary),
            "JAVA_TOOL_OPTIONS": f'-Djava.io.tmpdir="{temporary}" -XX:ErrorFile="{temporary}/hs_err_pid%p.log"'}):
            daemon = engine.SignalCliDaemon("+19999999999")
            try:
                response = daemon._request("version", {}, timeout=10)
                self.assertIn("result", response)
                args = subprocess.run(["ps", "-p", str(daemon._proc.pid), "-o", "command="],
                                      capture_output=True, text=True, check=True).stdout
                self.assertNotIn("+19999999999", args)
                self.assertNotIn(PASSWORD, args)
            finally:
                daemon.close()
            self.assertTrue(daemon._proc.stdout.closed)
            self.assertTrue(daemon._proc.stderr.closed)

    def test_real_background_continuity_and_process_group_teardown(self):
        self.client.call("job", kind="send")
        trace = self.vault.data / "test-dispatch.txt"
        self.wait_until(lambda: trace.exists() and len(trace.read_text().splitlines()) >= 3)
        first_count = len(trace.read_text().splitlines())
        child_pid = int((self.vault.data / "test-child.pid").read_text())
        self.client.call("lock")
        with self.assertRaisesRegex(SecurityError, "Locked"):
            self.client.call("snapshot")
        # A brand-new client models closing and reopening the window.
        reopened = Client(self.vault.root)
        with self.assertRaisesRegex(SecurityError, "Locked"):
            reopened.call("snapshot")
        self.wait_until(lambda: len(trace.read_text().splitlines()) > first_count + 3)
        lines = trace.read_text().splitlines()
        self.assertEqual(len(lines), len(set(lines)))
        with self.assertRaises(SecurityError):
            reopened.call("unlock", password="incorrect")
        count = len(trace.read_text().splitlines())
        self.wait_until(lambda: len(trace.read_text().splitlines()) > count + 3)
        reopened.call("erase", confirmed=True)
        self.assertFalse(self.vault.mount.is_mount())
        self.assertFalse(self.vault.image.exists())
        result = subprocess.run(["ps", "-p", str(child_pid), "-o", "stat="], capture_output=True, text=True)
        self.assertTrue(result.returncode != 0 or result.stdout.strip().startswith("Z"))
        time.sleep(0.3)
        self.assertFalse(trace.exists(), "a stopped child must not recreate a plaintext mount directory")

    def test_real_engine_parallel_upload_and_retry_teardown(self):
        def spawn_engine(argv, **kwargs):
            return subprocess.Popen([sys.executable, str(Path(__file__).with_name("mac_engine_worker.py")), *argv[1:]], **kwargs)
        self.service.spawn = spawn_engine
        for mode, concurrency in (("sequential", 1), ("parallel", 3), ("upload", 3), ("retry", 1)):
            with self.subTest(mode=mode):
                if self.client.call("status")["setup_required"]:
                    self.client.call("setup", password=PASSWORD)
                    engine.save_account("+19999999999")
                engine.GROUPS_FILE.write_text("".join(f"group-{i}\tDisposable {i}\n" for i in range(8)))
                engine._GROUP_ENTRIES_CACHE = None
                engine.set_config_value("cooldown_hours", 0)
                engine.set_config_value("base_delay_seconds", 0)
                engine.set_config_value("jitter_seconds", 0)
                engine.set_config_value("concurrent_sends", concurrency)
                engine.write_message("DISPOSABLE-PRIVATE-CONTENT")
                (self.vault.data / "test-transport.json").write_text(json.dumps({
                    "response_delay": 10 if mode == "upload" else 0.1, "retry": mode == "retry"}))
                self.client.call("job", kind="send")
                trace = self.vault.data / "test-rpc-dispatch.jsonl"
                self.wait_until(lambda: trace.exists())
                self.client.call("lock")
                if mode == "parallel":
                    self.wait_until(lambda: len(trace.read_text().splitlines()) >= 3)
                listing = subprocess.run(["ps", "-axo", "command="], capture_output=True, text=True).stdout
                own = [line for line in listing.splitlines() if str(self.vault.data) in line]
                self.assertTrue(own)
                for line in own:
                    self.assertNotIn("DISPOSABLE-PRIVATE-CONTENT", line)
                    self.assertNotIn("+19999999999", line)
                    self.assertNotIn(PASSWORD, line)
                observed = []
                def paused_retire(_):
                    before = trace.read_text().splitlines()
                    time.sleep(0.3)
                    observed.append((before, trace.read_text().splitlines()))
                self.service.retire = paused_retire
                self.client.call("erase", confirmed=True)
                self.service.retire = lambda _: None
                self.assertEqual(observed[0][0], observed[0][1], "No dispatch may begin after the erase marker")
                self.assertFalse(self.vault.image.exists())
                self.assertFalse(trace.exists())

    def test_launchd_restart_requires_password_and_reaps_owned_worker(self):
        self.server.shutdown()
        self.server.server_close()
        self.service.shutdown()
        label = "com.user.signal-broadcast.test." + secrets.token_hex(8)
        target = f"gui/{os.getuid()}/{label}"
        settings = self.root / "service-test.json"
        settings.write_text(json.dumps({"root": str(self.vault.root), "project": str(self.project),
                                       "keychain": self.keychain.service, "helper": str(self.keychain.helper)}))
        plist = self.root / "service-test.plist"
        plist.write_bytes(plistlib.dumps({"Label": label, "RunAtLoad": True, "KeepAlive": True,
            "ThrottleInterval": 1, "ProgramArguments": [sys.executable,
            str(Path(__file__).with_name("mac_test_service.py")), str(settings)],
            "StandardOutPath": "/dev/null", "StandardErrorPath": "/dev/null"}))
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)], check=True, capture_output=True)
        self.addCleanup(lambda: subprocess.run(["launchctl", "bootout", target], capture_output=True))
        def status_is(expected):
            try:
                return self.client.call("status")["state"] == expected
            except SecurityError:
                return False
        self.wait_until(lambda: status_is("sealed"), timeout=30)
        self.client.call("unlock", password=PASSWORD)
        self.client.call("job", kind="send")
        trace = self.vault.data / "test-dispatch.txt"
        self.wait_until(lambda: trace.exists())
        pid = int((self.vault.root / "service.pid").read_text())
        os.kill(pid, signal.SIGKILL)
        self.wait_until(lambda: int((self.vault.root / "service.pid").read_text()) != pid, timeout=30)
        self.wait_until(lambda: status_is("sealed"), timeout=30)
        self.assertFalse(self.vault.mount.is_mount())
        self.assertFalse(self.client.call("status")["background_running"])
        self.client.call("unlock", password=PASSWORD)
        before = trace.read_text()
        time.sleep(0.3)
        self.assertEqual(trace.read_text(), before)
        pid = int((self.vault.root / "service.pid").read_text())
        self.client.call("erase", confirmed=True)
        self.wait_until(lambda: int((self.vault.root / "service.pid").read_text()) != pid, timeout=30)
        self.wait_until(lambda: status_is("unlinked"), timeout=30)

    @unittest.skipUnless(int(os.environ.get("SB_LONG_IDLE_SECONDS", "0")) > 300, "Opt-in wall-clock idle test")
    def test_wall_clock_idle_during_and_after_broadcast(self):
        duration = int(os.environ["SB_LONG_IDLE_SECONDS"])
        self.client.call("job", kind="send")
        for phase in ("during", "after"):
            if phase == "after":
                self.client.call("stop")
            print(f"Wall-clock idle check {phase} broadcast: {duration}s", flush=True)
            deadline = time.monotonic() + duration
            while time.monotonic() < deadline:
                time.sleep(1)
                self.assertEqual(self.service.token, self.client.token)
            self.assertEqual(self.client.call("status")["state"], "unlocked")


if __name__ == "__main__":
    unittest.main()
