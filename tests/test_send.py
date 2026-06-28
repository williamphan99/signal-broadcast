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
import threading
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
    _lock = threading.Lock()   # calls[] is bumped from K worker threads in parallel mode

    def __init__(self, account, start_timeout=30.0):
        pass

    def is_running(self) -> bool:
        return True

    def send(self, gid, msg, atts):
        with FakeDaemon._lock:
            FakeDaemon.calls[gid] = FakeDaemon.calls.get(gid, 0) + 1
            n = FakeDaemon.calls[gid]
        seq = FakeDaemon.plan.get(gid, [(True, False, "")])
        return seq[min(n - 1, len(seq) - 1)]

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


class PacingTests(unittest.TestCase):
    """Speed presets + the adaptive pause. The gap between groups is a MINIMUM and a
    send's own duration counts toward it, so a big group (45–90s to fan out) is followed
    immediately — large groups run at full speed whatever the preset. The 10s hard floor
    is never breached, so no preset can burst fast enough to risk a ban."""

    def setUp(self):
        # Other test classes zero MIN_DELAY_S to avoid sleeping; pin the shipped floor.
        self._floor = engine.MIN_DELAY_S
        engine.MIN_DELAY_S = 10.0

    def tearDown(self):
        engine.MIN_DELAY_S = self._floor

    def test_pace_delay_never_below_floor(self):
        # Even a reckless config can't pace two quick sends below the floor.
        for base, jitter in [(10, 3), (16, 6), (24, 6), (10, 0), (1, 50)]:
            for _ in range(200):
                self.assertGreaterEqual(engine._pace_delay(base, jitter), engine.MIN_DELAY_S)

    def test_big_send_adds_no_extra_pause(self):
        # gap = max(0, target - send_time): a send longer than the target self-spaces,
        # so the user's 47–90s groups are followed by the next one with zero added wait.
        for _ in range(200):
            target = engine._pace_delay(10, 3)        # the tightest (Large groups) preset
            self.assertEqual(max(0.0, target - 47.0), 0.0, "a 47s send must add no pause")

    def test_quick_send_still_waits_the_floor(self):
        # A near-instant group (e.g. an empty one) still waits at least the floor.
        for _ in range(200):
            gap = max(0.0, engine._pace_delay(10, 3) - 0.0)
            self.assertGreaterEqual(gap, engine.MIN_DELAY_S)

    def test_ui_presets_respect_the_floor(self):
        # Tie the Security-tab presets to the safety guarantee: no preset may be wired
        # to pace below the engine floor. Skips if Tk isn't importable (headless CI).
        try:
            import gui
        except Exception as exc:  # noqa: BLE001 — tkinter missing → skip, don't fail
            self.skipTest(f"gui import unavailable: {exc}")
        self.assertIn("large", gui.App.SPEED_PRESETS, "the Large-groups preset must exist")
        for key, (_label, base, _jit) in gui.App.SPEED_PRESETS.items():
            self.assertGreaterEqual(base, engine.MIN_DELAY_S,
                                    f"preset {key!r} base {base}s is below the {engine.MIN_DELAY_S}s floor")


class AttachmentWipeTests(unittest.TestCase):
    """The privacy wipe deletes the ORIGINAL image files attachments.txt points at —
    the user's own files, not copies — so a wipe leaves no images behind."""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.att_file = self.tmp / "attachments.txt"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_deletes_the_original_image_files(self):
        img = self.tmp / "flyer.png"
        img.write_bytes(b"png")
        self.att_file.write_text(f"# header\n{img}\n", encoding="utf-8")
        engine._delete_listed_attachments(self.att_file)
        self.assertFalse(img.exists())

    def test_missing_or_commented_paths_do_not_raise(self):
        gone = self.tmp / "gone.png"  # listed but never existed
        self.att_file.write_text(f"# comment\n\n{gone}\n", encoding="utf-8")
        engine._delete_listed_attachments(self.att_file)  # must not raise

    def test_no_attachments_file_is_a_noop(self):
        engine._delete_listed_attachments(self.tmp / "nope.txt")  # must not raise


class ConcurrentSendTests(unittest.TestCase):
    """Parallel sending (concurrent_sends > 1). The whole point of these is the one
    invariant that must never break under concurrency: every group is sent EXACTLY
    once — never twice (which would be spammy) and never zero times. A timed-out send
    must still be 'uncertain' and never retried, just like the sequential path."""

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
        engine.MIN_DELAY_S = 0.0
        engine.NON_THROTTLE_WAIT_S = 0.0

    def tearDown(self):
        (engine.signal_cli_bin, engine.unsendable_groups, engine.SignalCliDaemon,
         engine._send_one, engine.MIN_DELAY_S, engine.NON_THROTTLE_WAIT_S) = self._orig
        engine.clear_run_progress()

    def _run(self, groups, K, **kw):
        cfg = engine.Config(account="+test", base_delay_seconds=0.0, jitter_seconds=0.0,
                            cooldown_hours=0, max_retries=4, send_times=[],
                            concurrent_sends=K)
        return engine.broadcast(config=cfg, groups=groups, message="m",
                                attachments=[], **kw)

    def test_each_group_sent_exactly_once_under_load(self):
        groups = [(f"g{i:03d}", f"n{i}") for i in range(50)]
        for K in (2, 3, 5):
            FakeDaemon.calls = {}
            res = self._run(groups, K=K)
            self.assertEqual(len(res), len(groups), f"K={K}: one result per group")
            self.assertTrue(all(r.ok for r in res), f"K={K}: all should succeed")
            self.assertEqual(sorted(r.group_id for r in res),
                             sorted(g for g, _ in groups), f"K={K}: every group present once")
            self.assertTrue(all(c == 1 for c in FakeDaemon.calls.values()),
                            f"K={K}: no group sent more than once")
            self.assertEqual(len(FakeDaemon.calls), len(groups),
                             f"K={K}: no group skipped")

    def test_duplicate_group_still_sent_once_in_parallel(self):
        # The dedup guard must hold under concurrency too — a list with a repeat sends once.
        res = self._run([("g1", "a"), ("g1", "a"), ("g2", "b")], K=3)
        self.assertEqual(FakeDaemon.calls.get("g1"), 1, "a duplicate must still send once")
        self.assertEqual(FakeDaemon.calls.get("g2"), 1)
        self.assertEqual(len(res), 2, "deduped to two groups")

    def test_timeout_is_uncertain_and_not_retried_in_parallel(self):
        FakeDaemon.plan = {"g1": [(False, False, "daemon timed out after 300s")]}
        res = self._run([("g1", "a"), ("g2", "b")], K=2)
        self.assertEqual(FakeDaemon.calls["g1"], 1, "a timeout must NOT be retried")
        g1 = next(r for r in res if r.group_id == "g1")
        self.assertTrue(g1.uncertain)
        self.assertFalse(g1.ok)
        # uncertain stays out of the resend file (resending could duplicate it)
        self.assertEqual(engine.write_failures([r for r in res if not r.ok and not r.uncertain
                                                and not r.skipped]), None)

    def test_admin_only_group_skipped_in_parallel(self):
        engine.unsendable_groups = lambda account: {"g2"}
        res = self._run([("g1", "a"), ("g2", "b"), ("g3", "c")], K=2)
        self.assertNotIn("g2", FakeDaemon.calls, "an admin-only group must never be sent")
        g2 = next(r for r in res if r.group_id == "g2")
        self.assertTrue(g2.skipped)
        self.assertEqual({r.group_id for r in res}, {"g1", "g2", "g3"})

    def test_progress_fires_once_per_group_in_parallel(self):
        groups = [(f"g{i}", f"n{i}") for i in range(20)]
        seen = []
        lock = threading.Lock()

        def on_prog(done, total, name, status, secs):
            with lock:
                seen.append((done, total))
        self._run(groups, K=3, on_progress=on_prog)
        self.assertEqual(len(seen), len(groups), "one progress callback per group")
        self.assertEqual(sorted(d for d, _ in seen), list(range(1, len(groups) + 1)),
                         "the done-counter advances 1..N with no gaps or repeats")
        self.assertTrue(all(t == len(groups) for _, t in seen), "total stays N")

    def test_falls_back_to_sequential_without_a_daemon(self):
        # No daemon (start returns None) must NOT run parallel one-shots — they would
        # deadlock on the account lock. K is forced to 1 and the per-send path is used.
        engine.SignalCliDaemon = lambda *a, **k: (_ for _ in ()).throw(engine.BroadcastError("no daemon"))
        seen = []
        engine._send_one = lambda b, acc, gid, m, a: (seen.append(gid) or (True, False, ""))
        res = self._run([("g1", "a"), ("g2", "b")], K=2)
        self.assertEqual(sorted(seen), ["g1", "g2"], "per-send used for every group")
        self.assertTrue(all(r.ok for r in res))


if __name__ == "__main__":
    unittest.main()
