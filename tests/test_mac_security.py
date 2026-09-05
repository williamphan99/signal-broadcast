"""Security behavior with real password cryptography and disposable filesystem data."""
import copy
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import engine
from mac_security import SecurityError, Vault, WrongPassword, atomic_json, copy_import, delete_original, dispatch_guard
from mac_service import Service, terminate_group
from runtime import isolated_engine

PASSWORD = "a long local passphrase"


class MemoryKeychain:
    def __init__(self):
        self.record = None
        self.fail_writes = False

    def load(self):
        return copy.deepcopy(self.record)

    def save(self, record):
        if self.fail_writes:
            raise SecurityError("Keychain unavailable")
        self.record = copy.deepcopy(record)

    def delete(self):
        if self.fail_writes:
            raise SecurityError("Keychain unavailable")
        self.record = None


class DirectoryVault(Vault):
    """Only the OS disk layer is replaced. Migration and erasure use actual files."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.attached = False
        self.fail_detach = False

    def create_image(self, password):
        self.prepare()
        self.image.mkdir(exist_ok=True)
        self.attach(password)

    def attach(self, password):
        self.data.mkdir(parents=True, exist_ok=True)
        self.attached = True

    def detach(self):
        if self.fail_detach:
            raise SecurityError("Unmount failed")
        self.attached = False


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolated_engine()
        self.scope.__enter__()
        self.addCleanup(self.scope.__exit__, None, None, None)
        self.temp = tempfile.TemporaryDirectory(prefix="sb-security-tests-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.project = self.base / "project"
        self.project.mkdir()
        self.store = MemoryKeychain()
        self.vault = DirectoryVault(self.base / "private", self.project, self.store)
        self.service = Service(self.vault, retire=lambda _: None)

    def setup(self):
        result = self.service.handle({"op": "setup", "password": PASSWORD})
        self.token = result["token"]

    def request(self, op, **values):
        return self.service.handle({"op": op, "token": self.token, **values})

    def test_password_required_for_reads_and_manual_jobs(self):
        for operation in ("snapshot", "job", "save", "groups", "settings", "schedule", "import"):
            with self.assertRaisesRegex(SecurityError, "Locked"):
                self.service.handle({"op": operation})

    def test_saved_photo_order_survives_lock_and_reopen(self):
        self.setup()
        paths = []
        for name in ("third.png", "first.png", "second.png"):
            path = self.vault.data / name
            path.write_bytes(b"disposable attachment")
            paths.append(str(path))
        self.request("save", message="Ordered photos", attachments=paths)
        self.service.lock()
        self.token = self.service.authenticate(PASSWORD)["token"]
        self.assertEqual(self.request("snapshot")["attachments"], paths)
        self.assertEqual(engine.read_attachments(), paths)

    @unittest.skipUnless(os.environ.get("SB_RUN_MAC_PROCESSES") == "1", "Opt-in native worker teardown test")
    def test_stop_reports_stopped_only_after_worker_exit(self):
        self.setup()
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        def cleanup():
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        self.addCleanup(cleanup)
        self.service.job = {"kind": "send", "proc": proc}
        self.request("stop")
        self.assertIsNotNone(proc.poll())
        state = self.request("snapshot")
        self.assertIsNone(state["job"])
        self.assertEqual(state["last_operation"], {"kind": "send", "outcome": "stopped"})
        self.assertEqual(state["events"][-1]["kind"], "stopped")

    def test_failed_stop_keeps_running_state(self):
        self.setup()
        self.service.job = {"kind": "send", "proc": object()}
        with mock.patch("mac_service.terminate_group", side_effect=SecurityError("Worker did not exit")):
            with self.assertRaisesRegex(SecurityError, "Worker did not exit"):
                self.request("stop")
        state = self.request("snapshot")
        self.assertEqual(state["job"], "send")
        self.assertIsNone(state["last_operation"])
        self.assertFalse(any(event["kind"] == "stopped" for event in state["events"]))

    def test_worker_exit_distinguishes_finished_from_failed(self):
        self.setup()
        for returncode, outcome in ((0, "completed"), (1, "failed")):
            with self.subTest(returncode=returncode):
                proc = mock.Mock()
                proc.stdout = mock.Mock()
                proc.stdout.__iter__ = mock.Mock(return_value=iter([]))
                proc.wait.return_value = returncode
                job = {"kind": "send", "proc": proc}
                self.service.job = job
                with mock.patch("mac_service.terminate_group"):
                    self.service._read_job(job)
                self.assertEqual(self.request("snapshot")["last_operation"], {"kind": "send", "outcome": outcome})

    def test_setup_rejects_short_password_without_creating_any_record(self):
        with self.assertRaises(SecurityError):
            self.service.authenticate("short", setup=True)
        self.assertIsNone(self.store.record)

    def test_lock_revokes_read_access_but_preserves_job_and_vault(self):
        self.setup()
        job = {"kind": "send", "proc": object()}
        self.service.job = job
        self.request("lock")
        self.assertTrue(self.service.open)
        self.assertTrue(self.vault.attached)
        self.assertIs(self.service.job, job)
        with self.assertRaisesRegex(SecurityError, "Locked"):
            self.request("snapshot")
        self.service.job = None

    def test_no_idle_lock_during_or_after_broadcast(self):
        self.setup()
        start = datetime(2026, 1, 1, 8)
        for active in (True, False):
            self.service.job = {"kind": "send"} if active else None
            for minute in (0, 5, 10, 60, 360):
                self.service.tick(start + timedelta(minutes=minute))
                self.assertEqual(self.service.token, self.token)
        self.service.job = None

    def test_two_failures_preserve_work_third_erases(self):
        self.setup()
        self.request("lock")
        stopped = []
        self.service.stop_job = lambda: stopped.append(True)
        for expected in (2, 1, 0):
            with self.assertRaises(WrongPassword) as error:
                self.service.authenticate("wrong password")
            self.assertEqual(error.exception.remaining, expected)
            if expected:
                self.assertTrue(self.service.open)
                self.assertEqual(stopped, [])
        self.assertEqual(stopped, [True])
        self.assertFalse(self.vault.image.exists())
        self.assertIsNone(self.store.record)
        self.assertEqual(self.service.status()["state"], "unlinked")

    def test_failures_survive_service_restart_and_success_resets(self):
        self.setup()
        with self.assertRaises(WrongPassword):
            self.service.authenticate("wrong")
        service = Service(self.vault, retire=lambda _: None)
        service.recover()
        self.assertFalse(service.open)
        self.assertEqual(service.status()["attempts_remaining"], 2)
        service.authenticate(PASSWORD)
        self.assertEqual(self.store.record["failures"], 0)

    def test_concurrent_attempts_cannot_lose_an_increment(self):
        self.setup()
        def wrong(_):
            try:
                self.service.handle({"op": "unlock", "password": "wrong"})
            except WrongPassword as error:
                return error.remaining
        with ThreadPoolExecutor(max_workers=3) as pool:
            values = list(pool.map(wrong, range(3)))
        self.assertEqual(sorted(values), [0, 1, 2])
        self.assertFalse(self.vault.image.exists())

    def test_third_attempt_cannot_restore_auth_if_wipe_is_interrupted(self):
        self.setup()
        self.store.record["failures"] = 2
        self.vault.fail_detach = True
        with self.assertRaises(SecurityError):
            self.service.authenticate("wrong")
        self.assertTrue(self.vault.marker.exists())
        self.assertIsNone(self.service.token)
        self.assertFalse(self.service.open)
        self.vault.fail_detach = False
        restarted = Service(self.vault, retire=lambda _: None)
        restarted.recover()
        self.assertFalse(self.vault.marker.exists())
        self.assertEqual(restarted.status()["state"], "unlinked")

    def test_keychain_write_failure_never_returns_success(self):
        self.setup()
        self.store.fail_writes = True
        with self.assertRaisesRegex(SecurityError, "Keychain unavailable"):
            self.service.authenticate("wrong")
        self.assertTrue(self.vault.image.exists())
        self.assertEqual(self.store.record["failures"], 0)

    def test_missing_security_record_does_not_enable_setup(self):
        self.setup()
        self.store.record = None
        with self.assertRaises(SecurityError):
            self.service.status()
        with self.assertRaises(SecurityError):
            self.service.authenticate(PASSWORD, setup=True)

    def test_password_change_counts_current_failures_and_rewraps_key(self):
        self.setup()
        with self.assertRaises(WrongPassword):
            self.request("change_password", current="wrong", new="another sufficiently long password")
        self.assertEqual(self.store.record["failures"], 1)
        self.request("change_password", current=PASSWORD, new="another sufficiently long password")
        self.assertEqual(self.store.record["failures"], 0)
        self.service.authenticate("another sufficiently long password")
        with self.assertRaises(WrongPassword):
            self.service.authenticate(PASSWORD)

    def test_invalid_new_password_is_not_an_authentication_attempt(self):
        self.setup()
        with self.assertRaises(SecurityError):
            self.request("change_password", current="wrong", new="short")
        self.assertEqual(self.store.record["failures"], 0)

    def test_unlock_and_password_change_share_the_three_attempt_limit(self):
        self.setup()
        job = {"kind": "send"}
        self.service.job = job
        with mock.patch.object(self.service, "stop_job", side_effect=lambda: setattr(self.service, "job", None)) as stop:
            for operation, remaining in (("change_password", 2), ("unlock", 1), ("change_password", 0)):
                with self.assertRaises(WrongPassword) as error:
                    self.request(operation, password="wrong", current="wrong", new="another long password")
                self.assertEqual(error.exception.remaining, remaining)
                if remaining:
                    self.assertEqual(self.store.record["failures"], 3 - remaining)
                    self.assertIs(self.service.job, job)
                    self.assertTrue(self.vault.attached)
                    stop.assert_not_called()
            stop.assert_called_once()
        self.assertIsNone(self.store.record)
        self.assertFalse(self.vault.image.exists())
        self.assertEqual(self.service.status()["state"], "unlinked")

    def test_logout_on_lock_screen_needs_confirmation_not_password(self):
        self.setup()
        self.request("lock")
        with self.assertRaises(SecurityError):
            self.service.handle({"op": "erase"})
        self.service.handle({"op": "erase", "confirmed": True})
        self.assertEqual(self.service.status()["state"], "unlinked")

    def test_locked_schedule_runs_once_per_due_minute(self):
        self.setup()
        self.request("schedule", times=["09:00"], enabled=True)
        self.request("lock")
        started = []
        self.service._start_job = lambda kind: started.append(kind)
        now = datetime(2026, 1, 1, 9)
        self.service.tick(now)
        self.service.tick(now + timedelta(seconds=20))
        self.service.tick(now + timedelta(minutes=5))
        self.assertEqual(started, ["send"])

    def test_network_job_error_does_not_count_as_password_failure(self):
        self.setup()
        self.service._event("error", "Network unavailable")
        self.assertEqual(self.service.status()["attempts_remaining"], 3)

    def test_unpadded_schedule_times_execute_while_locked(self):
        self.setup()
        self.request("schedule", times=["9:0"], enabled=True)
        self.assertEqual(self.service.schedule()["times"], ["09:00"])
        self.request("lock")
        with mock.patch.object(self.service, "_start_job") as start:
            self.service.tick(datetime(2026, 9, 4, 9, 0))
        start.assert_called_once_with("send")

    def test_safe_status_never_returns_personal_data_or_token(self):
        self.setup()
        status = json.dumps(self.service.status())
        self.assertNotIn(PASSWORD, status)
        self.assertNotIn(self.token, status)
        self.assertNotIn("wrapped", status)

    def test_migration_preserves_link_and_rewrites_notes_and_attachments(self):
        (self.project / "signal-cli-data/data").mkdir(parents=True)
        (self.project / "signal-cli-data/data/account.db").write_bytes(b"fake link state")
        (self.project / "notes.json").write_text(json.dumps([{"path": str(self.project / "signal-cli-data/photo.png")}]))
        (self.project / "logs").mkdir()
        (self.project / "logs/.gitkeep").touch()
        photo = self.base / "original.png"
        photo.write_bytes(b"sample image")
        (self.project / "attachments.txt").write_text(str(photo) + "\n")
        self.setup()
        self.assertEqual((self.vault.data / "signal-cli-data/data/account.db").read_bytes(), b"fake link state")
        self.assertFalse((self.project / "signal-cli-data").exists())
        self.assertFalse(photo.exists())
        self.assertTrue((self.project / "logs/.gitkeep").exists())
        paths = (self.vault.data / "attachments.txt").read_text().splitlines()
        self.assertEqual(Path(paths[0]).read_bytes(), b"sample image")
        self.assertIn(str(self.vault.data), (self.vault.data / "notes.json").read_text())

    def test_failed_migration_verification_retains_originals(self):
        path = self.project / "message.txt"
        path.write_text("private message")
        with mock.patch.object(self.vault, "verify_tree", side_effect=SecurityError("failed verification")):
            with self.assertRaises(SecurityError):
                self.setup()
        self.assertEqual(path.read_text(), "private message")
        self.assertFalse(self.service.open)
        self.assertEqual(self.store.record["phase"], "migrating")
        self.service.authenticate(PASSWORD)
        self.assertFalse(path.exists())

    def test_import_verifies_before_deleting_original(self):
        self.setup()
        image = self.base / "photo.png"
        image.write_bytes(b"example pixels")
        imported = self.request("import", path=str(image))["path"]
        self.assertEqual(Path(imported).read_bytes(), b"example pixels")
        self.assertFalse(image.exists())

    def test_replaced_source_is_not_deleted(self):
        image = self.base / "photo.png"
        image.write_bytes(b"original")
        _, receipt = copy_import(image, self.base / "imports")
        image.unlink()
        image.write_bytes(b"replacement")
        with self.assertRaises(SecurityError):
            delete_original(receipt)
        self.assertEqual(image.read_bytes(), b"replacement")

    def test_symlink_import_does_not_delete_target(self):
        self.setup()
        original = self.base / "original.png"
        original.write_bytes(b"image")
        link = self.base / "link.png"
        link.symlink_to(original)
        with self.assertRaises(SecurityError):
            self.request("import", path=str(link))
        self.assertTrue(original.exists())

    def test_erasing_blocks_new_operations(self):
        self.setup()
        atomic_json(self.vault.marker, {"erasing": True})
        self.service.token = None
        with self.assertRaisesRegex(SecurityError, "Locked"):
            self.request("job", kind="send")

    def test_mac_web_interface_is_blocked(self):
        import webui
        with mock.patch.object(engine, "IS_DARWIN", True):
            client = webui.create_app().test_client()
            self.assertEqual(client.get("/api/state").status_code, 403)
            self.assertEqual(client.post("/api/send", json={}).status_code, 403)

    def test_marker_failure_still_stops_work_and_revokes_access(self):
        self.setup()
        with mock.patch("mac_service.atomic_json", side_effect=OSError("disk full")), \
             mock.patch.object(self.service, "stop_job") as stop:
            with self.assertRaises(OSError):
                self.request("erase", confirmed=True)
        stop.assert_called_once()
        self.assertTrue(self.service.erasing)
        self.assertEqual(self.store.record["phase"], "erasing")
        with self.assertRaises(SecurityError):
            self.request("job", kind="send")

    def test_interrupted_import_is_registered_before_source_cleanup(self):
        self.setup()
        original = self.base / "original.png"
        original.write_bytes(b"protected image")
        with mock.patch("mac_security.delete_original", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                self.request("import", path=str(original))
        self.assertTrue(original.exists())
        imported = Path(engine.read_attachments()[0])
        self.assertEqual(imported.read_bytes(), original.read_bytes())
        self.service.authenticate(PASSWORD)
        self.assertFalse(original.exists())
        self.assertEqual(engine.read_attachments(), [str(imported)])
        self.assertEqual(self.store.record["pending_originals"], [])

    def test_pending_original_can_be_erased_without_unlocking_after_restart(self):
        self.setup()
        original = self.base / "original.png"
        original.write_bytes(b"protected image")
        with mock.patch("mac_security.copy_import", side_effect=OSError("interrupted copy")):
            with self.assertRaises(OSError):
                self.request("import", path=str(original))
        restarted = Service(self.vault, retire=lambda _: None)
        restarted.recover()
        restarted.handle({"op": "erase", "confirmed": True})
        self.assertFalse(original.exists())

    def test_ready_installation_does_not_follow_injected_legacy_attachment_paths(self):
        self.setup()
        unrelated = self.base / "unrelated.png"
        unrelated.write_bytes(b"must remain")
        (self.project / "attachments.txt").write_text(str(unrelated))
        self.request("erase", confirmed=True)
        self.assertEqual(unrelated.read_bytes(), b"must remain")

    def test_enabled_legacy_schedule_is_preserved(self):
        (self.project / "config.toml").write_text('send_times = ["9:0"]\n')
        self.service.retire = lambda _: True
        self.setup()
        self.assertEqual(self.service.schedule(), {"enabled": True, "times": ["09:00"], "last": ""})

    def test_dispatch_guard_denies_work_after_erase_marker(self):
        self.setup()
        atomic_json(self.vault.marker, {"erasing": True})
        with self.assertRaises(SecurityError):
            with dispatch_guard(self.vault.root):
                self.fail("Dispatch must not begin")

    def test_corrupt_progress_blocks_a_new_send(self):
        self.setup()
        engine.RUN_PROGRESS_FILE.parent.mkdir(exist_ok=True)
        engine.RUN_PROGRESS_FILE.write_text("interrupted write {")
        with self.assertRaises(engine.BroadcastError):
            engine.read_interrupted_run()

    def test_progress_failure_is_fatal_before_dispatch(self):
        self.setup()
        engine.begin_run_progress([("group", "Test")], "fingerprint")
        with mock.patch("mac_security.atomic_json", side_effect=OSError("disk full")):
            with self.assertRaises(engine.BroadcastError):
                engine.record_group_progress("group", "attempting")
        self.assertEqual(engine.read_interrupted_run().remaining, [("group", "")])

    def test_resume_dispatches_the_draft_that_passed_fingerprint_verification(self):
        from mac_worker import run

        self.setup()
        engine.CONFIG_FILE.write_text('account = "+19999999999"\n')
        engine.GROUPS_FILE.write_text("group\tTest group\n")
        engine.write_message("Verified draft")
        photo = self.vault.data / "photo.png"
        photo.write_bytes(b"disposable image")
        engine.write_attachments([str(photo)])
        fingerprint = engine.message_fingerprint
        engine.begin_run_progress([("group", "Test group")], fingerprint("Verified draft", [str(photo)]))

        def verify_then_replace(message, attachments):
            result = fingerprint(message, attachments)
            engine.write_message("Changed after verification")
            engine.write_attachments([])
            return result

        with mock.patch.object(Path, "is_mount", return_value=True), mock.patch.dict(os.environ), \
             mock.patch.object(engine, "message_fingerprint", side_effect=verify_then_replace), \
             mock.patch.object(engine, "broadcast", return_value=[]) as broadcast, mock.patch("mac_worker.emit"):
            run({"root": str(self.vault.data), "job": "resume"})
        self.assertEqual(broadcast.call_args.kwargs["message"], "Verified draft")
        self.assertEqual(broadcast.call_args.kwargs["attachments"], [str(photo)])

    def test_clock_rollback_does_not_repeat_a_consumed_schedule(self):
        self.setup()
        self.request("schedule", times=["09:00", "09:10"], enabled=True)
        self.request("lock")
        started = []
        self.service._start_job = lambda kind: started.append(kind)
        for minute in (0, 10, 0, 10):
            self.service.tick(datetime(2026, 1, 1, 9, minute))
        self.assertEqual(started, ["send", "send"])

    def test_migration_preserves_note_text_and_moves_a_checkout_attachment(self):
        original = self.project / "my-photo.png"
        original.write_bytes(b"image outside managed legacy directories")
        (self.project / "attachments.txt").write_text(str(original))
        text = "Message mentioning " + str(self.project)
        (self.project / "notes.json").write_text(json.dumps([{"text": text}]))
        self.setup()
        self.assertEqual(json.loads((self.vault.data / "notes.json").read_text())[0]["text"], text)
        self.assertFalse(original.exists())
        self.assertEqual(Path(engine.read_attachments()[0]).read_bytes(), b"image outside managed legacy directories")

    def test_group_permission_error_is_only_ignored_when_no_live_member_remains(self):
        with mock.patch("mac_service.os.killpg", side_effect=PermissionError), \
             mock.patch("mac_service.subprocess.run", return_value=mock.Mock(stdout="123 Z\n")):
            terminate_group(123)
        with mock.patch("mac_service.os.killpg", side_effect=PermissionError), \
             mock.patch("mac_service.subprocess.run", return_value=mock.Mock(stdout="123 S\n")):
            with self.assertRaises(SecurityError):
                terminate_group(123)

    def test_group_kill_waits_for_descendants_after_the_leader_exits(self):
        proc = mock.Mock(pid=123)
        with mock.patch("mac_service.os.killpg") as kill, \
             mock.patch("mac_service.subprocess.run", side_effect=[mock.Mock(stdout="123 S\n"), mock.Mock(stdout="123 Z\n")]) as listing, \
             mock.patch("mac_service.time.sleep"):
            terminate_group(proc, seconds=0)
        kill.assert_any_call(123, signal.SIGKILL)
        proc.wait.assert_called_once_with(timeout=3)
        self.assertEqual(listing.call_count, 2)

    def test_recovery_retains_worker_record_if_group_does_not_exit(self):
        import mac_service
        self.vault.prepare()
        worker = self.vault.root / "worker.json"
        atomic_json(worker, {"pid": 123})
        listing = [mock.Mock(stdout=f"123 123 S {mac_service.PROJECT / 'mac_worker.py'}\n"), mock.Mock(stdout="123 S\n")]
        with mock.patch("mac_service.os.killpg"), \
             mock.patch("mac_service.subprocess.run", side_effect=listing), \
             mock.patch("mac_service.time.monotonic", side_effect=[0, 1, 2, 6]):
            with self.assertRaisesRegex(SecurityError, "group did not stop"):
                self.service.reap_worker()
        self.assertTrue(worker.exists())


if __name__ == "__main__":
    unittest.main()
