"""Sudoku front, panic code, idle sealing and unlink erasure, with disposable data."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import engine
import mac_service
import sudoku
from mac_security import SecurityError, WrongPassword, is_puzzle_code
import test_mac_security as base
from test_mac_security import PASSWORD

PANIC = "rainy tuesday"


class Fixture(unittest.TestCase):
    """The security tests' disposable vault and service, without re-running their tests."""
    setUp = base.SecurityTests.setUp
    setup = base.SecurityTests.setup
    request = base.SecurityTests.request


class FrontServiceTests(Fixture):
    def test_front_defaults_on_and_arms_after_the_first_unlock(self):
        self.assertTrue(self.service.status()["front"])  # Fresh: nothing to protect, show the game.
        self.setup()
        self.assertTrue(self.store.record["front_armed"])
        self.assertTrue(self.service.status()["front"])

    def test_existing_user_sees_the_password_screen_until_one_unlock(self):
        self.setup()
        record = self.store.load()
        record.pop("front_armed")  # A record as the previous version wrote it.
        self.store.save(record)
        self.request("lock")
        status = self.service.status()
        self.assertFalse(status["front"])
        self.assertTrue(status["disguise"])
        result = self.service.authenticate(PASSWORD)
        self.assertTrue(result["front_armed_now"])
        self.assertTrue(self.service.status()["front"])
        self.assertFalse(self.service.authenticate(PASSWORD)["front_armed_now"])

    def test_disguise_can_be_turned_off_only_when_unlocked(self):
        self.setup()
        with self.assertRaisesRegex(SecurityError, "Locked"):
            self.service.handle({"op": "disguise", "enabled": False})
        self.request("disguise", enabled=False)
        self.assertFalse(self.service.status()["front"])
        self.assertFalse(self.request("snapshot")["security"]["disguise"])

    def test_panic_code_erases_and_answers_like_a_wrong_password(self):
        self.setup()
        self.request("panic_code", code=PANIC)
        self.request("lock")
        with self.assertRaises(WrongPassword) as error:
            self.service.handle({"op": "unlock", "password": PANIC})
        self.assertEqual(error.exception.code, "wrong_password")
        self.assertIsNone(self.store.record)
        self.assertFalse(self.vault.image.exists())
        self.assertTrue(self.service.status()["front"])

    def test_panic_code_rules_never_count_as_attempts(self):
        self.setup()
        for code, reason in (("123456", "Six digits"), ("#123456", "Six digits"), (PASSWORD, "cannot be your password"),
                             ("abc", "4 to 64"), (" padded ", "4 to 64")):
            with self.assertRaisesRegex(SecurityError, reason):
                self.request("panic_code", code=code)
        self.assertEqual(self.store.record["failures"], 0)
        self.request("panic_code", code=PANIC)
        self.assertTrue(self.request("snapshot")["security"]["panic_code"])
        self.request("panic_code", code=None)
        self.assertNotIn("panic", self.store.record)

    def test_password_change_keeps_front_settings_and_rejects_the_panic_code(self):
        self.setup()
        self.request("panic_code", code=PANIC)
        self.request("disguise", enabled=False)
        with self.assertRaisesRegex(SecurityError, "panic code"):
            self.request("change_password", current=PASSWORD, new=PANIC)
        self.request("change_password", current=PASSWORD, new="another long passphrase")
        record = self.store.record
        self.assertFalse(record["disguise"])
        self.assertTrue(record["front_armed"])
        self.assertIn("panic", record)
        self.request("lock")
        self.service.authenticate("another long passphrase")

    def test_wrong_codes_still_erase_on_the_third_attempt(self):
        self.setup()
        self.request("lock")
        for _ in range(3):
            with self.assertRaises(WrongPassword):
                self.service.handle({"op": "unlock", "password": "not the password"})
        self.assertIsNone(self.store.record)

    def test_failures_before_unlock_are_reported_once_unlocked(self):
        self.setup()
        self.request("lock")
        with self.assertRaises(WrongPassword):
            self.service.authenticate("not the password")
        self.token = self.service.authenticate(PASSWORD)["token"]
        self.assertEqual(self.request("snapshot")["security"]["failures_before_unlock"], 1)


class SealingTests(Fixture):
    def test_locked_idle_vault_detaches_and_unlock_reattaches(self):
        self.setup()
        self.request("lock")
        self.service.tick(datetime(2026, 1, 1, 8))
        self.assertFalse(self.vault.attached)
        self.assertFalse(self.service.mounted)
        self.assertEqual(self.service.status()["state"], "screen_locked")
        self.token = self.service.authenticate(PASSWORD)["token"]
        self.assertTrue(self.vault.attached)
        self.request("snapshot")

    def test_waiting_send_or_pending_update_never_holds_the_vault_open(self):
        self.setup()
        engine.set_config_value("account", "+19999999999")
        engine.GROUPS_FILE.write_text("fixture\tFixture\n")
        engine.write_message("Disposable scheduled draft")
        self.request("schedule", times=["09:00"], enabled=True)
        self.service.update_state = {"changed": True}  # A downloaded update waits; sends wait too.
        self.request("lock")
        clock = [1000.0]
        with mock.patch("mac_service.time.monotonic", side_effect=lambda: clock[0]):
            self.service.tick(datetime(2026, 1, 1, 9))
            self.assertFalse(self.vault.attached)  # Sealed although the 09:00 send is pending.
            self.assertTrue(self.service.sealed_schedule["pending"])
            clock[0] += 10
            self.service.tick(datetime(2026, 1, 1, 9, 0, 10))
            self.assertFalse(self.vault.attached)  # Not reopened before the retry delay.
            attach = mock.Mock(wraps=self.vault.attach)
            with mock.patch.object(self.vault, "attach", attach):
                clock[0] += mac_service.WAKE_RETRY_S
                self.service.tick(datetime(2026, 1, 1, 9, 2, 10))
            attach.assert_called_once()
            self.assertFalse(self.vault.attached)  # Checked, still waiting, sealed again.

    def test_failed_background_mount_backs_off(self):
        self.setup()
        self.request("lock")
        self.service.tick(datetime(2026, 1, 1, 8))
        self.service.health_due = 0
        with mock.patch.object(self.vault, "attach", side_effect=SecurityError("busy")) as attach:
            self.service.tick(datetime(2026, 1, 1, 8))
            self.service.tick(datetime(2026, 1, 1, 8))
        attach.assert_called_once()

    def test_failed_unlock_after_mounting_detaches_again(self):
        self.setup()
        self.request("lock")
        self.service.tick(datetime(2026, 1, 1, 8))
        self.assertFalse(self.vault.attached)
        with mock.patch.object(engine, "discard_notes", side_effect=PermissionError("notes.json")):
            with self.assertRaises(PermissionError):
                self.service.authenticate(PASSWORD)
        self.assertFalse(self.vault.attached)
        self.assertFalse(self.service.mounted)
        self.assertIsNone(self.service.token)

    def test_unlocked_or_busy_vault_stays_mounted(self):
        self.setup()
        self.service.tick(datetime(2026, 1, 1, 8))
        self.assertTrue(self.vault.attached)
        self.request("lock")
        self.service.job = {"kind": "send"}
        self.service.tick(datetime(2026, 1, 1, 8))
        self.assertTrue(self.vault.attached)
        self.service.job = None

    def test_failed_detach_retries_later_instead_of_failing(self):
        self.setup()
        self.request("lock")
        self.vault.fail_detach = True
        self.service.tick(datetime(2026, 1, 1, 8))
        self.assertTrue(self.service.mounted)
        self.vault.fail_detach = False
        self.service.seal_retry_at = 0
        self.service.tick(datetime(2026, 1, 1, 8))
        self.assertFalse(self.service.mounted)

    def test_sealed_vault_reattaches_for_a_due_schedule(self):
        self.setup()
        engine.set_config_value("account", "+19999999999")
        engine.GROUPS_FILE.write_text("fixture\tFixture\n")
        engine.write_message("Disposable scheduled draft")
        self.request("schedule", times=["09:00"], enabled=True)
        self.request("lock")
        self.service.tick(datetime(2026, 1, 1, 8, 58))
        self.assertFalse(self.vault.attached)
        started = []
        def start(kind):
            started.append(kind)
            self.service.job = {"kind": kind}
        self.service._start_job = start
        self.service.tick(datetime(2026, 1, 1, 8, 59))
        self.assertFalse(self.vault.attached)
        self.service.tick(datetime(2026, 1, 1, 9))
        self.assertEqual(started, ["send"])
        self.assertTrue(self.vault.attached)  # Stays open while the scheduled send runs.
        self.service.job = None
        self.service.tick(datetime(2026, 1, 1, 9, 1))
        self.assertFalse(self.vault.attached)  # And closes once it has finished.


class UnlinkTests(Fixture):
    def finish_health(self, state, at):
        with mock.patch("mac_service.time.monotonic", return_value=at):
            self.service._link_state(state)

    def test_repeated_removed_answers_over_an_hour_erase_everything(self):
        self.setup()
        hour = mac_service.HEALTH_CONFIRM_S
        for at in (1000, 1000 + hour / 2):
            self.finish_health("removed", at)
            self.service.tick(datetime(2026, 1, 1, 8))
            self.assertIsNotNone(self.store.record)
        self.assertTrue(self.service.link_broken)
        self.finish_health("removed", 1000 + hour)
        self.service.tick(datetime(2026, 1, 1, 8))
        self.assertIsNone(self.store.record)
        self.assertFalse(self.vault.image.exists())

    def test_quick_repeats_unknown_or_recovered_answers_never_erase(self):
        self.setup()
        hour = mac_service.HEALTH_CONFIRM_S
        for at in (1000, 1010, 1020, 1030):  # Many answers, but not an hour apart.
            self.finish_health("removed", at)
        self.finish_health("unknown", 1000 + 2 * hour)
        self.service.tick(datetime(2026, 1, 1, 8))
        self.assertIsNotNone(self.store.record)
        self.finish_health("ok", 1000 + 2 * hour)
        self.assertFalse(self.service.link_broken)
        self.finish_health("removed", 1000 + 10 * hour)  # The count restarts after "ok".
        self.service.tick(datetime(2026, 1, 1, 8))
        self.assertIsNotNone(self.store.record)

    def test_link_check_runs_soon_after_unlock(self):
        self.setup()
        engine.set_config_value("account", "+19999999999")
        started = []
        self.service._start_job = lambda kind: started.append(kind)
        with mock.patch("mac_service.engine.is_linked", return_value=True), \
             mock.patch("mac_service.time.monotonic", return_value=self.service.health_due + 1):
            self.service.tick(datetime(2026, 1, 1, 8))
        self.assertEqual(started, ["health"])


class NotesRemovalTests(Fixture):
    def test_unlock_discards_old_note_copies_and_their_photos(self):
        self.setup()
        photo = engine.DATA_DIR / "attachments" / "note-photo"
        photo.parent.mkdir(parents=True, exist_ok=True)
        photo.write_bytes(b"note image")
        outside = self.base / "outside.png"
        outside.write_bytes(b"not ours")
        engine.NOTES_FILE.write_text(json.dumps([{"ts": 1, "text": "private", "photos": [
            {"path": str(photo)}, {"path": str(outside)}]}]))
        engine.NOTES_CORRUPT_FILE.write_text("damaged")
        self.request("lock")
        self.token = self.service.authenticate(PASSWORD)["token"]
        self.assertFalse(engine.NOTES_FILE.exists())
        self.assertFalse(engine.NOTES_CORRUPT_FILE.exists())
        self.assertFalse(photo.exists())
        self.assertTrue(outside.exists())  # Only files inside the Signal store are removed.
        self.assertNotIn("notes", self.request("snapshot"))
        with self.assertRaisesRegex(SecurityError, "Unsupported"):
            self.request("job", kind="notes")
        with self.assertRaisesRegex(SecurityError, "Unsupported"):
            self.request("delete_note", timestamp=1)

    def test_discarding_notes_keeps_photos_the_saved_draft_uses(self):
        self.setup()
        attachments = engine.DATA_DIR / "attachments"
        attachments.mkdir(parents=True, exist_ok=True)
        used, unused = attachments / "used", attachments / "unused"
        used.write_bytes(b"in the draft")
        unused.write_bytes(b"only in a note")
        engine.write_attachments([str(used)])
        engine.NOTES_FILE.write_text(json.dumps([{"ts": 1, "photos": [{"path": str(used)}, {"path": str(unused)}]},
                                                 {"ts": 2, "photos": None}, "not a note"]))
        leftover = engine.NOTES_FILE.with_name(engine.NOTES_FILE.name + ".123.tmp")
        leftover.write_text("half-written note text")
        self.assertEqual(engine.discard_notes(), 2)
        self.assertTrue(used.exists())
        self.assertFalse(unused.exists())
        self.assertFalse(leftover.exists())
        self.assertEqual(engine.read_attachments(), [str(used)])

    def test_received_note_media_is_deleted_when_notes_are_off(self):
        self.setup()
        media = engine.DATA_DIR / "attachments" / "incoming"
        media.parent.mkdir(parents=True, exist_ok=True)
        media.write_bytes(b"note image")
        note = {"photos": [], "missing_photos": 0}
        with mock.patch.object(engine, "KEEP_NOTES", False):
            engine._apply_downloaded_note_attachment(note, {"contentType": "image/png"}, media)
        self.assertFalse(media.exists())
        self.assertEqual(note["photos"], [])


class HealthSchedulingTests(Fixture):
    def test_stopping_a_link_check_schedules_the_next_one(self):
        self.setup()
        self.service.job, self.service.health_due = {"kind": "health", "proc": mock.Mock()}, None
        with mock.patch("mac_service.terminate_group"), mock.patch.object(self.service, "reap_worker"):
            self.request("stop")
        self.assertIsNotNone(self.service.health_due)

    def test_a_send_where_every_group_failed_triggers_a_link_check(self):
        self.setup()
        summary = engine.RunSummary(at="2026-01-01T08:00:00", total=3, sent=0, failed=3)
        with mock.patch.object(engine, "read_run_summary", return_value=summary):
            self.assertTrue(self.service._job_failed({"kind": "send"}, 0))
        with mock.patch.object(engine, "read_run_summary", return_value=engine.RunSummary(
                at="2026-01-01T08:00:00", total=3, sent=2, failed=1)):
            self.assertFalse(self.service._job_failed({"kind": "send"}, 0))
        self.assertTrue(self.service._job_failed({"kind": "sync"}, 1))

    def test_worker_start_errors_during_a_link_check_do_not_stop_the_service(self):
        self.setup()
        self.service.health_due = 0
        with mock.patch.object(engine, "is_linked", return_value=True), \
             mock.patch.object(self.service, "_start_job", side_effect=OSError("disk full")):
            self.service.tick(datetime(2026, 1, 1, 8))
        self.assertGreater(self.service.health_due, 0)


class ProbeTests(unittest.TestCase):
    def test_removed_pattern_ignores_network_and_bare_status_codes(self):
        for text in ("Connection closed", "timed out", "[403] Forbidden", "401 Unauthorized",
                     "Failed to get response", "authentication timeout"):
            self.assertIsNone(engine.LINK_REMOVED_PATTERN.search(text), text)
        for text in ("User +61400000000 is not registered.", "Authorization failed!"):
            self.assertIsNotNone(engine.LINK_REMOVED_PATTERN.search(text), text)

    def test_probe_link_classifies_signal_answers(self):
        def run(returncode, stderr=""):
            return mock.Mock(returncode=returncode, stdout="", stderr=stderr)
        cases = ((run(0), True, "ok"), (run(1, "Authorization failed!"), True, "removed"),
                 (run(1, "Authorization failed!"), False, "unknown"),  # Rejection while offline proves nothing.
                 (run(1, "Connection closed"), True, "unknown"))
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(engine, "LOGS_DIR", Path(directory)), \
             mock.patch.object(engine, "SIGNAL_CLI_LOCK_FILE", Path(directory) / "lock"), \
             mock.patch.object(engine, "_link_is_broken_unlocked", return_value=True) as accounts, \
             mock.patch.object(engine, "signal_cli_bin", return_value="signal-cli"), \
             mock.patch.object(engine, "load_config", return_value=mock.Mock(account="+19999999999")):
            for result, reachable, expected in cases:
                with mock.patch.object(engine.subprocess, "run", return_value=result), \
                     mock.patch.object(engine, "signal_reachable", return_value=reachable):
                    self.assertEqual(engine.probe_link(), expected)
            accounts.assert_not_called()  # An empty local account list is never evidence.
            with mock.patch.object(engine.subprocess, "run", side_effect=engine.subprocess.TimeoutExpired("x", 1)):
                self.assertEqual(engine.probe_link(), "unknown")


class PuzzleTests(unittest.TestCase):
    def test_puzzle_codes_never_collide_with_passwords(self):
        self.assertTrue(is_puzzle_code("482913"))
        self.assertTrue(is_puzzle_code(" #482913 "))
        for text in ("48291", "4829131", "a long local passphrase", "12345678901234", "   123456   "):
            self.assertFalse(is_puzzle_code(text))

    def test_codes_make_repeatable_puzzles_with_one_solution(self):
        for code, clues in (("123456", 40), ("254321", 32), ("398765", 26)):
            puzzle, solution = sudoku.make_puzzle(code)
            self.assertEqual((puzzle, solution), sudoku.make_puzzle(code))
            self.assertEqual(sum(1 for value in puzzle if value), clues)
            self.assertEqual(sudoku._search(puzzle[:], 2), 1)
            self.assertTrue(all(not value or value == solution[i] for i, value in enumerate(puzzle)))
            for group in (sudoku.ROW, sudoku.COL, sudoku.BOX):
                for unit in range(9):
                    self.assertEqual(sorted(solution[i] for i in range(81) if group[i] == unit), list(range(1, 10)))

    def test_random_codes_match_their_difficulty(self):
        for name in sudoku.DIFFICULTIES:
            code = sudoku.random_code(name)
            self.assertTrue(is_puzzle_code(code))
            self.assertEqual(sudoku.difficulty_for(code), name)


if __name__ == "__main__":
    unittest.main()
