"""Restored Mac workflows, using disposable storage and simulated Signal work."""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import engine
import mac_retry
import mac_worker
from mac_security import SecurityError
from runtime import isolated_engine
import test_mac_security


class RetryTests(unittest.TestCase):
    def setUp(self):
        scope = isolated_engine()
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        engine.set_config_value("account", "+19999999999")
        engine.write_message("Saved message")
        engine.write_attachments([])
        engine.set_config_value("message_style", "bold")
        self.results = [engine.GroupSendResult("sent", "Sent", True),
                        engine.GroupSendResult("failed", "Failed", False),
                        engine.GroupSendResult("skipped", "Skipped", False, skipped=True),
                        engine.GroupSendResult("uncertain", "Uncertain", False, uncertain=True)]
        mac_retry.save(self.results, "Saved message", [], "bold")

    def test_only_definite_failures_are_retryable(self):
        self.assertEqual(mac_retry.groups(), [("failed", "Failed")])
        self.assertEqual(mac_retry.available_count(), 1)

    def test_changed_text_photos_or_style_disable_retry(self):
        (engine.RUNTIME_DIR / "new.png").write_bytes(b"fixture")
        for message, photos, style in [("Changed", [], "bold"), ("Saved message", [str(engine.RUNTIME_DIR / "new.png")], "bold"),
                                       ("Saved message", [], "none")]:
            with self.subTest(message=message, photos=photos, style=style):
                engine.write_message(message)
                engine.write_attachments(photos)
                engine.set_config_value("message_style", style)
                self.assertEqual(mac_retry.available_count(), 0)
                with self.assertRaisesRegex(engine.BroadcastError, "changed"):
                    mac_retry.groups()

    def test_corrupt_retry_record_does_not_break_snapshot(self):
        for record in ('{', '[]', '{"groups": [2]}'):
            (engine.RUNTIME_DIR / "retry.json").write_text(record)
            self.assertEqual(mac_retry.available_count(), 0)

    def test_worker_counts_out_of_order_completions_and_clears_old_retry_first(self):
        events = []
        def broadcast(**kwargs):
            self.assertEqual(mac_retry.available_count(), 0)
            kwargs["on_progress"](3, 3, "Skip", "skipped", 0)
            kwargs["on_group_start"](1, "First")
            kwargs["on_group_start"](2, "Second")
            kwargs["on_progress"](2, 3, "Second", "sent", 1)
            kwargs["on_progress"](1, 3, "First", "failed", 2)
            return self.results
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "mounted" / "store"
            root.mkdir(parents=True)
            with mock.patch("pathlib.Path.is_mount", return_value=True), \
                 mock.patch.dict(mac_worker.os.environ), \
                 mock.patch.object(mac_worker, "configure_storage"), \
                 mock.patch.object(engine, "read_groups", return_value=[("a", "First"), ("b", "Second"), ("c", "Skip")]), \
                 mock.patch.object(engine, "broadcast", side_effect=broadcast), \
                 mock.patch.object(mac_worker, "emit", side_effect=lambda kind, value: events.append((kind, value))):
                mac_worker.run({"root": str(root), "job": "send"})
        self.assertEqual([v["done"] for k, v in events if k == "progress"], [1, 2, 3])
        self.assertEqual([v["active"] for k, v in events if k == "send_status"], [0, 1, 2, 1, 0])
        self.assertEqual([v for k, v in events if k == "phase"], ["preparing", "sending"])
        self.assertEqual(mac_retry.groups(), [("failed", "Failed")])


class ServiceRestorationTests(unittest.TestCase):
    setUp = test_mac_security.SecurityTests.setUp
    setup = test_mac_security.SecurityTests.setup
    request = test_mac_security.SecurityTests.request

    def test_formatting_is_saved_with_draft_and_retained_after_lock(self):
        self.setup()
        engine.set_config_value("account", "+19999999999")
        self.request("save", message="Styled message", attachments=[], message_style="italic")
        self.service.lock()
        self.token = self.service.authenticate(test_mac_security.PASSWORD)["token"]
        snapshot = self.request("snapshot")
        self.assertEqual(snapshot["message"], "Styled message")
        self.assertEqual(snapshot["config"]["message_style"], "italic")

    def test_downloaded_update_blocks_jobs_and_schedules_until_restart(self):
        self.setup()
        self.service.update_state = {"changed": True, "needs_setup": False}
        self.request("schedule", enabled=True, times=["10:00"])
        with mock.patch.object(self.service, "spawn") as spawn:
            with self.assertRaisesRegex(SecurityError, "Restart"):
                self.request("job", kind="send")
            self.service.tick(datetime(2026, 9, 5, 10, 0))
            spawn.assert_not_called()
        restarted = mock.Mock()
        self.service.on_restart = restarted
        self.assertEqual(self.request("restart_update"), {"restarting": True})
        self.service.tick()
        restarted.assert_called_once()

    def test_update_cannot_interrupt_an_active_worker(self):
        self.setup()
        self.service.job = {"kind": "send"}
        with self.assertRaisesRegex(SecurityError, "busy"):
            self.request("job", kind="update")
        with self.assertRaises(SecurityError):
            self.request("settings", values={"concurrent_sends": 2})

    def test_setup_changes_and_locked_clients_cannot_directly_restart(self):
        self.setup()
        self.service.update_state = {"changed": True, "needs_setup": True}
        with self.assertRaisesRegex(SecurityError, "Setup"):
            self.request("restart_update")
        self.service.lock()
        with self.assertRaisesRegex(SecurityError, "Locked"):
            self.request("restart_update")

    def test_progress_survives_window_lock_and_reopen(self):
        self.setup()
        self.service.send_progress = {"active": 2, "completed": 3, "total": 8}
        self.service.phase = "sending"
        self.service.lock()
        self.token = self.service.authenticate(test_mac_security.PASSWORD)["token"]
        snapshot = self.request("snapshot")
        self.assertEqual(snapshot["send_progress"], {"active": 2, "completed": 3, "total": 8})
        self.assertEqual(snapshot["phase"], "sending")


class UpdateTests(unittest.TestCase):
    def test_failed_dependency_diff_requires_setup(self):
        responses = [mock.Mock(returncode=0, stdout="old", stderr=""),
                     mock.Mock(returncode=0, stdout="Updated", stderr=""),
                     mock.Mock(returncode=0, stdout="new", stderr=""),
                     mock.Mock(returncode=1, stdout="", stderr="failed")]
        with mock.patch.object(engine.subprocess, "run", side_effect=responses):
            result = engine.git_pull()
        self.assertTrue(result.changed)
        self.assertTrue(result.needs_setup)

    def test_git_failure_is_distinct_from_already_current(self):
        with mock.patch.object(engine, "_git_head", return_value="old"), \
             mock.patch.object(engine.subprocess, "run", return_value=mock.Mock(returncode=1, stdout="", stderr="Offline")):
            result = engine.git_pull()
        self.assertTrue(result.error)
        self.assertFalse(result.changed)
