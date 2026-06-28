#!/usr/bin/env python3
"""Send-path regression tests. No network, no real signal-cli — a fake transport is
injected. Run with:  python3 -m unittest discover -s tests

These cover the bugs that bit us in the field:
  * a timed-out send must be reported "uncertain" and NEVER auto-retried/resent
  * both front-ends' progress callbacks must match engine.ProgressFn (the CLI one
    silently drifted to the wrong arity and broke every scheduled run)
"""
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

    def is_running(self):
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

    def _run(self, groups, **kw):
        cfg = engine.Config(account="+test", base_delay_seconds=0.0, jitter_seconds=0.0,
                            cooldown_hours=0, max_retries=4, send_times=[])
        return engine.broadcast(config=cfg, groups=groups, message="m",
                                attachments=[], **kw)

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
