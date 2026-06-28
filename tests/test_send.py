#!/usr/bin/env python3
"""Send-path regression tests. No network, no real signal-cli — a fake transport is
injected. Run with:  python3 -m unittest discover -s tests

These cover the bugs that bit us in the field:
  * a timed-out send must be reported "uncertain" and NEVER auto-retried/resent
  * both front-ends' progress callbacks must match engine.ProgressFn (the CLI one
    silently drifted to the wrong arity and broke every scheduled run)
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import engine  # noqa: E402
import broadcast  # noqa: E402


class FakeDaemon:
    """Injected in place of SignalCliDaemon. send() returns whatever the test queued
    per group id; records call counts so we can assert "no retry"."""
    plan: dict = {}            # gid -> list of (ok, throttled, err) to return in order
    calls: dict = {}           # gid -> attempt count

    def __init__(self, account, start_timeout=30.0):
        pass

    def is_running(self) -> bool:
        return True

    def send(self, gid, msg, atts):
        FakeDaemon.calls[gid] = FakeDaemon.calls.get(gid, 0) + 1
        seq = FakeDaemon.plan.get(gid, [(True, False, "")])
        return seq[min(FakeDaemon.calls[gid] - 1, len(seq) - 1)]

    def close(self):
        pass


class SendPathTests(unittest.TestCase):
    def setUp(self):
        FakeDaemon.plan = {}
        FakeDaemon.calls = {}
        self._orig = (engine.signal_cli_bin, engine.unsendable_groups,
                      engine.SignalCliDaemon, engine._send_one,
                      engine.MIN_DELAY_S, engine.NON_THROTTLE_WAIT_S)
        engine.signal_cli_bin = lambda: "/usr/bin/true"
        engine.unsendable_groups = lambda account: set()
        engine.SignalCliDaemon = FakeDaemon
        engine._send_one = lambda *a: (True, False, "")
        engine.MIN_DELAY_S = 0.0          # don't sleep between sends in tests
        engine.NON_THROTTLE_WAIT_S = 0.0  # don't sleep between retries in tests

    def tearDown(self):
        (engine.signal_cli_bin, engine.unsendable_groups, engine.SignalCliDaemon,
         engine._send_one, engine.MIN_DELAY_S, engine.NON_THROTTLE_WAIT_S) = self._orig
        engine.clear_run_progress()  # don't leave a test's crash marker behind

    def _run(self, groups, attachments=None, **kw):
        cfg = engine.Config(account="+test", base_delay_seconds=0.0, jitter_seconds=0.0,
                            cooldown_hours=0, max_retries=4, send_times=[])
        return engine.broadcast(config=cfg, groups=groups, message="m",
                                attachments=attachments or [], **kw)

    def test_timeout_is_uncertain_and_not_retried(self):
        FakeDaemon.plan = {"g1": [(False, False, "daemon timed out after 120s")]}
        res = self._run([("g1", "G1")])
        self.assertTrue(res[0].uncertain, "timeout must be uncertain")
        self.assertFalse(res[0].ok)
        self.assertEqual(FakeDaemon.calls["g1"], 1, "a timeout must NOT be retried")

    def test_clean_error_is_failed_and_retried(self):
        FakeDaemon.plan = {"g1": [(False, False, "untrusted identity")]}
        res = self._run([("g1", "G1")])
        self.assertFalse(res[0].ok)
        self.assertFalse(res[0].uncertain)
        self.assertGreater(FakeDaemon.calls["g1"], 1, "a clean error should be retried")

    def test_uncertain_excluded_from_failures_file(self):
        FakeDaemon.plan = {"g1": [(False, False, "daemon timed out after 120s")]}
        res = self._run([("g1", "G1")])
        failed = [r for r in res if not r.ok and not r.skipped and not r.uncertain]
        self.assertEqual(failed, [], "uncertain groups must not be treated as failed")

    def test_send_lock_blocks_a_second_sender(self):
        with engine.send_lock():
            with self.assertRaises(engine.BroadcastError):
                with engine.send_lock():
                    pass
        # released — can acquire again afterwards
        with engine.send_lock():
            pass

    def test_progress_recorded_and_cleared_on_normal_finish(self):
        FakeDaemon.plan = {"g1": [(True, False, "")], "g2": [(True, False, "")]}
        self._run([("g1", "G1"), ("g2", "G2")])
        # A clean finish must leave no crash marker.
        self.assertIsNone(engine.read_interrupted_run())

    def test_interrupted_run_resumes_only_unsent_and_failed(self):
        # Simulate a crash: write progress for a 4-group run where g1 sent, g2 is a
        # clean failure, g3 is uncertain (may have sent), g4 never attempted.
        groups = [("g1", "G1"), ("g2", "G2"), ("g3", "G3"), ("g4", "G4")]
        engine.begin_run_progress(groups, "fp123")
        engine.record_group_progress("g1", "sent")
        engine.record_group_progress("g2", "failed")
        engine.record_group_progress("g3", "uncertain")
        run = engine.read_interrupted_run()
        self.assertIsNotNone(run)
        assert run is not None  # narrow for type-checkers
        remaining_ids = [g for g, _ in run.remaining]
        self.assertEqual(remaining_ids, ["g2", "g4"], "resume = clean failures + unattempted only")
        self.assertNotIn("g1", remaining_ids, "a sent group must never be resent")
        self.assertNotIn("g3", remaining_ids, "an uncertain group must never be resent")
        self.assertEqual(run.fingerprint, "fp123")
        engine.clear_run_progress()
        self.assertIsNone(engine.read_interrupted_run())

    def test_marker_survives_a_run_that_aborts_with_an_error(self):
        class Boom(FakeDaemon):
            def send(self, gid, msg, atts):
                raise RuntimeError("boom")
        engine.SignalCliDaemon = Boom
        with self.assertRaises(Exception):
            self._run([("g1", "G1")])
        self.assertIsNotNone(engine.read_interrupted_run(),
                             "an aborted run must keep its resume marker")

    def test_missing_attachments_detected(self):
        self.assertEqual(engine.missing_attachments(["/no/such/file.jpg"]), ["/no/such/file.jpg"])
        self.assertEqual(engine.missing_attachments([__file__]), [])  # this test file exists

    def test_broadcast_aborts_before_sending_on_missing_attachment(self):
        FakeDaemon.plan = {"g1": [(True, False, "")]}
        with self.assertRaises(engine.BroadcastError):
            self._run([("g1", "G1")], attachments=["/no/such/file.jpg"])
        self.assertNotIn("g1", FakeDaemon.calls, "must abort before any send")

    def test_duplicate_groups_are_sent_only_once(self):
        FakeDaemon.plan = {"g1": [(True, False, "")], "g2": [(True, False, "")]}
        res = self._run([("g1", "G1"), ("g1", "G1-dup"), ("g2", "G2")])
        self.assertEqual(FakeDaemon.calls.get("g1"), 1, "a duplicate group must be sent once")
        self.assertEqual(len(res), 2, "the duplicate is dropped from the run")

    def test_timeout_with_dead_daemon_is_not_resent(self):
        # Daemon both times out AND reports dead: the fallback must NOT re-send (the
        # timed-out message may already have gone out) — it stays uncertain.
        resends = []
        engine._send_one = lambda b, a, gid, m, at: (resends.append(gid), (True, False, ""))[1]

        class DeadAfterTimeout(FakeDaemon):
            def is_running(self):
                return False

            def send(self, gid, msg, atts):
                FakeDaemon.calls[gid] = FakeDaemon.calls.get(gid, 0) + 1
                return (False, False, "daemon timed out after 300s")

        engine.SignalCliDaemon = DeadAfterTimeout
        res = self._run([("g1", "G1")])
        self.assertTrue(res[0].uncertain, "timeout+dead daemon must be uncertain")
        self.assertEqual(resends, [], "must NOT re-send a timed-out group via per-send")

    def test_group_marked_attempting_before_the_send(self):
        # The progress marker must be written BEFORE the send leaves the machine, so a
        # kill in the dispatch->record gap is recoverable (treated as uncertain).
        observed = {}

        class Spy(FakeDaemon):
            def send(self, gid, msg, atts):
                data = json.loads(engine.RUN_PROGRESS_FILE.read_text(encoding="utf-8"))
                observed[gid] = data["done"].get(gid)
                return (True, False, "")

        engine.SignalCliDaemon = Spy
        self._run([("g1", "G1")])
        self.assertEqual(observed.get("g1"), "attempting",
                         "the group must be marked attempting BEFORE its send")

    def test_attempting_group_is_uncertain_not_resumed(self):
        # A crash AFTER a send dispatched but BEFORE its outcome was recorded leaves the
        # group "attempting": it may have gone out, so it is surfaced as uncertain and
        # NEVER auto-resent.
        groups = [("g1", "G1"), ("g2", "G2"), ("g3", "G3")]
        engine.begin_run_progress(groups, "fp")
        engine.record_group_progress("g1", "sent")
        engine.record_group_progress("g2", "attempting")  # killed mid-send
        run = engine.read_interrupted_run()
        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual([g for g, _ in run.remaining], ["g3"], "only g3 is safely resumable")
        self.assertIn("g2", [g for g, _ in run.uncertain], "g2 must be surfaced as uncertain")
        self.assertNotIn("g2", [g for g, _ in run.remaining], "g2 must never be auto-resent")

    def test_clear_run_progress_if_idle_refuses_while_a_send_holds_the_lock(self):
        # A launchd fire (different message) must not wipe the resume marker of a GUI
        # run that is mid-send and holding the lock.
        engine.begin_run_progress([("g1", "G1")], "fp")
        with engine.send_lock():  # stand in for another sender mid-send
            self.assertFalse(engine.clear_run_progress_if_idle(),
                             "must not clear a live run's marker")
            self.assertTrue(engine.RUN_PROGRESS_FILE.exists(), "marker must survive")
        # lock free again -> safe to clear
        self.assertTrue(engine.clear_run_progress_if_idle())
        self.assertFalse(engine.RUN_PROGRESS_FILE.exists())

    def test_daemon_write_failure_is_uncertain_not_resent(self):
        # A write that broke after the request line went out may already have dispatched;
        # it must be uncertain, never re-sent one-shot.
        resends = []
        engine._send_one = lambda b, a, gid, m, at: (resends.append(gid), (True, False, ""))[1]

        class WriteBroke(FakeDaemon):
            def is_running(self):
                return False

            def send(self, gid, msg, atts):
                FakeDaemon.calls[gid] = FakeDaemon.calls.get(gid, 0) + 1
                return (False, False, "daemon send may have dispatched (write failed): EPIPE")

        engine.SignalCliDaemon = WriteBroke
        res = self._run([("g1", "G1")])
        self.assertTrue(res[0].uncertain, "a post-write daemon failure is uncertain")
        self.assertEqual(resends, [], "must not re-send a maybe-dispatched group")

    def test_dead_daemon_before_write_falls_back_to_one_shot(self):
        # If the daemon was already down before we wrote, nothing left us — re-sending
        # this one group via per-send is safe (no duplicate risk).
        oneshot = []
        engine._send_one = lambda b, a, gid, m, at: (oneshot.append(gid), (True, False, ""))[1]

        class DeadNotRunning(FakeDaemon):
            def is_running(self):
                return False

            def send(self, gid, msg, atts):
                FakeDaemon.calls[gid] = FakeDaemon.calls.get(gid, 0) + 1
                return (False, False, "signal-cli daemon is not running")

        engine.SignalCliDaemon = DeadNotRunning
        res = self._run([("g1", "G1")])
        self.assertTrue(res[0].ok, "a never-dispatched send is safe to re-send one-shot")
        self.assertEqual(oneshot, ["g1"])

    def test_daemon_timeout_retires_daemon_for_the_rest(self):
        # A timeout leaves signal-cli still processing the request; reusing the daemon
        # for the next group would run two sends at once on one account. The daemon is
        # retired and the rest of the run goes one-shot.
        closed = []
        oneshot = []
        engine._send_one = lambda b, a, gid, m, at: (oneshot.append(gid), (True, False, ""))[1]

        class TimeoutThenOk(FakeDaemon):
            def close(self):
                closed.append(True)

            def send(self, gid, msg, atts):
                FakeDaemon.calls[gid] = FakeDaemon.calls.get(gid, 0) + 1
                if gid == "g1":
                    return (False, False, "daemon timed out after 120s")
                return (True, False, "")  # would succeed via daemon — must NOT be used

        engine.SignalCliDaemon = TimeoutThenOk
        res = self._run([("g1", "G1"), ("g2", "G2")])
        self.assertTrue(res[0].uncertain)
        self.assertTrue(closed, "daemon must be retired after a timeout")
        self.assertNotIn("g2", FakeDaemon.calls, "g2 must not use the retired daemon")
        self.assertIn("g2", oneshot, "g2 must go one-shot after the daemon was retired")
        self.assertTrue(res[1].ok)

    def test_progress_callbacks_match_engine_signature(self):
        # Drive the REAL callback each front-end passes; a wrong arity raises here.
        seen = []
        broadcast._log_progress(1, 3, "G1", "sent", 2.0)          # CLI front-end
        broadcast._log_progress(2, 3, "G2", "uncertain", 120.0)
        gui_cb = lambda d, t, n, status, secs: seen.append(status)  # GUI front-end shape
        FakeDaemon.plan = {"g1": [(True, False, "")]}
        self._run([("g1", "G1")], on_progress=gui_cb)
        self.assertEqual(seen, ["sent"])


if __name__ == "__main__":
    unittest.main()
