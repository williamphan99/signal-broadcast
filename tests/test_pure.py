#!/usr/bin/env python3
"""Cross-platform pure-function tests. No network, no real signal-cli, no OS-specific
behaviour — these run identically on macOS and inside the Android (proot-distro Debian)
guest, and guard the small platform-aware seams added for the Pixel/Termux port:

  * the pacing floor, time parsing, fingerprinting and error classification the port
    reuses unchanged, and
  * the platform guards themselves (on_ac_power / _java_home / bin resolution) behaving
    correctly when IS_DARWIN is False.

Run with:  python3 -m unittest discover -s tests
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import engine  # noqa: E402


class PacingTests(unittest.TestCase):
    def test_never_below_floor(self):
        # Even a zero base with a big negative jitter draw can't go under the floor.
        for _ in range(200):
            self.assertGreaterEqual(engine._pace_delay(0.0, 100.0), engine.MIN_DELAY_S)

    def test_respects_larger_base(self):
        for _ in range(200):
            d = engine._pace_delay(30.0, 5.0)
            self.assertGreaterEqual(d, engine.MIN_DELAY_S)
            self.assertLessEqual(d, 35.0 + 1e-9)


class ParseTimesTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(
            engine.parse_times(["09:00", "16:30"]),
            [{"Hour": 9, "Minute": 0}, {"Hour": 16, "Minute": 30}],
        )

    def test_invalid_raises(self):
        for bad in (["24:00"], ["9am"], ["12:60"], ["-1:00"]):
            with self.assertRaises(engine.BroadcastError):
                engine.parse_times(bad)

    def test_empty_raises(self):
        with self.assertRaises(engine.BroadcastError):
            engine.parse_times([])


class FingerprintTests(unittest.TestCase):
    def test_deterministic(self):
        a = engine.message_fingerprint("hello", ["/x/a.jpg"])
        b = engine.message_fingerprint("hello", ["/x/a.jpg"])
        self.assertEqual(a, b)

    def test_sensitive_to_message_and_attachments(self):
        base = engine.message_fingerprint("hello", ["/x/a.jpg"])
        self.assertNotEqual(base, engine.message_fingerprint("hello!", ["/x/a.jpg"]))
        self.assertNotEqual(base, engine.message_fingerprint("hello", ["/x/b.jpg"]))
        self.assertNotEqual(base, engine.message_fingerprint("hello", []))


class ClassifyErrorTests(unittest.TestCase):
    def test_categories(self):
        cases = {
            "rate limit exceeded (429)": "rate limited",
            "java.net.SocketTimeoutException: timed out": "network or connection problem",
            "GroupError: only admins can send": "admin-only group (you can't post here)",
            "attachment upload failed": "attachment or upload problem",
            "HTTP 403 forbidden": "authorisation problem",
            "some totally novel gremlin": "unknown error",
        }
        for stderr, expected in cases.items():
            self.assertEqual(engine.classify_error(stderr), expected, stderr)


class LoadConfigTests(unittest.TestCase):
    def test_parses_temp_config(self):
        body = (
            'account            = "+61400000000"\n'
            "base_delay_seconds = 12\n"
            "jitter_seconds     = 4\n"
            "cooldown_hours     = 2\n"
            "max_retries        = 3\n"
            'send_times         = ["09:00", "17:00"]\n'
            "concurrent_sends   = 3\n"
        )
        with tempfile.TemporaryDirectory() as d:
            cfg_path = Path(d) / "config.toml"
            cfg_path.write_text(body, encoding="utf-8")
            with mock.patch.object(engine, "CONFIG_FILE", cfg_path):
                cfg = engine.load_config()
        self.assertEqual(cfg.account, "+61400000000")
        self.assertEqual(cfg.base_delay_seconds, 12)
        self.assertEqual(cfg.send_times, ["09:00", "17:00"])
        self.assertEqual(cfg.concurrent_sends, 3)

    def test_rejects_placeholder_account(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = Path(d) / "config.toml"
            cfg_path.write_text('account = "+61XXXXXXXXX"\nsend_times = ["12:00"]\n', encoding="utf-8")
            with mock.patch.object(engine, "CONFIG_FILE", cfg_path):
                with self.assertRaises(engine.BroadcastError):
                    engine.load_config()


class PlatformGuardTests(unittest.TestCase):
    """The seams that make the engine run off macOS (Android/Termux)."""

    def test_on_ac_power_true_off_darwin(self):
        # Off macOS we must never shell out to pmset; always report AC.
        with mock.patch.object(engine, "IS_DARWIN", False):
            self.assertTrue(engine.on_ac_power())

    def test_termux_prefix_is_str(self):
        self.assertIsInstance(engine._termux_prefix(), str)

    def test_launchd_fns_safe_off_darwin(self):
        # launchctl doesn't exist off macOS; these must no-op instead of crashing.
        # (unlink() calls disable_schedule(), so a crash here breaks the web UI's Unlink.)
        with mock.patch.object(engine, "IS_DARWIN", False):
            self.assertFalse(engine.schedule_enabled())
            self.assertFalse(engine.watcher_enabled())
            self.assertIsNone(engine.disable_schedule())   # no launchctl call
            self.assertIsNone(engine.disable_watcher())
            with self.assertRaises(engine.BroadcastError):
                engine.enable_schedule(["09:00"])
            with self.assertRaises(engine.BroadcastError):
                engine.enable_watcher()

    def test_java_home_uses_env_off_darwin(self):
        with tempfile.TemporaryDirectory() as d:
            java_home = Path(d) / "jdk21"
            (java_home / "bin").mkdir(parents=True)
            (java_home / "bin" / "java").write_text("#!/bin/sh\n")
            # Point VENDOR_DIR at an empty dir: _java_home() prefers a vendored jdk*
            # over $JAVA_HOME, so without this the test would return the real
            # vendor/jdk-* inside the Debian guest and fail (see engine._java_home).
            with mock.patch.object(engine, "IS_DARWIN", False), \
                 mock.patch.object(engine, "VENDOR_DIR", Path(d) / "novendor"), \
                 mock.patch.dict(os.environ, {"JAVA_HOME": str(java_home)}):
                self.assertEqual(engine._java_home(), str(java_home))


class LinkIsBrokenTests(unittest.TestCase):
    """link_is_broken() must say True ONLY on positive evidence (files on disk but
    signal-cli reports zero accounts); every error path must read as 'not broken' so
    a transient problem never bounces a healthy install to the link screen."""

    @staticmethod
    def _completed(rc: int, stdout: str):
        proc = mock.Mock()
        proc.returncode = rc
        proc.stdout = stdout
        return proc

    def _run(self, is_linked: bool, rc: int, stdout: str) -> bool:
        with mock.patch.object(engine, "is_linked", lambda: is_linked), \
             mock.patch.object(engine, "signal_cli_bin", lambda: "/bin/true"), \
             mock.patch.object(engine.subprocess, "run",
                               lambda *a, **k: self._completed(rc, stdout)):
            return engine.link_is_broken()

    def test_not_linked_is_not_broken(self):
        self.assertFalse(self._run(False, 0, "[]"))

    def test_no_accounts_is_broken(self):
        # The half-linked state: files exist, listAccounts succeeds with no account
        # (signal-cli logs "User is not registered" and returns an empty list).
        self.assertTrue(self._run(True, 0, "[]\n"))

    def test_registered_account_is_not_broken(self):
        self.assertFalse(self._run(True, 0, '[{"number": "+61400000000"}]'))

    def test_cli_failure_is_not_broken(self):
        self.assertFalse(self._run(True, 1, ""))

    def test_bad_json_is_not_broken(self):
        self.assertFalse(self._run(True, 0, "not json"))


class SyncGroupsTests(unittest.TestCase):
    """sync_groups must never report a total failure as '0 groups' — that made a dead
    account look identical to an account with no groups, which was the reported bug."""

    class _Recv:
        def __init__(self, rc=0, err=""):
            self.returncode, self.stderr, self.stdout = rc, err, ""

    def _sync(self, recv, pull_side_effect):
        """Run sync_groups with receive + pull_groups stubbed. pull_side_effect is a
        list consumed one per iteration; an item that's an Exception is raised."""
        seq = iter(pull_side_effect)

        def fake_pull(_acct):
            item = next(seq, pull_side_effect[-1])
            if isinstance(item, Exception):
                raise item
            return item

        with mock.patch.object(engine, "signal_cli_bin", lambda: "/bin/true"), \
             mock.patch.object(engine, "_request_sync", lambda *a, **k: None), \
             mock.patch.object(engine, "pull_groups", fake_pull), \
             mock.patch.object(engine.subprocess, "run", lambda *a, **k: recv):
            return engine.sync_groups("+1", on_log=lambda *_: None)

    def test_all_failures_raise_with_reason(self):
        err = engine.BroadcastError("Could not fetch groups:\nUser +1 is not registered.")
        with self.assertRaises(engine.BroadcastError) as ctx:
            self._sync(self._Recv(rc=1, err="User +1 is not registered."), [err])
        self.assertIn("not registered", str(ctx.exception))

    def test_success_returns_count(self):
        # Three stable reads settle the loop and return the count.
        self.assertEqual(self._sync(self._Recv(), [5, 5, 5]), 5)

    def test_transient_error_then_success(self):
        # A non-permanent error is retried, not surfaced, and the eventual count wins.
        transient = engine.BroadcastError("connection reset by peer")
        self.assertEqual(self._sync(self._Recv(), [transient, 2, 2, 2]), 2)

    def test_permanent_error_bails_fast(self):
        # A "not registered" error must break out immediately, not loop for SYNC_MAX_S.
        calls = {"n": 0}
        dead = engine.BroadcastError("User +1 is not registered.")

        def fake_pull(_acct):
            calls["n"] += 1
            raise dead

        with mock.patch.object(engine, "signal_cli_bin", lambda: "/bin/true"), \
             mock.patch.object(engine, "_request_sync", lambda *a, **k: None), \
             mock.patch.object(engine, "pull_groups", fake_pull), \
             mock.patch.object(engine.subprocess, "run", lambda *a, **k: self._Recv()):
            with self.assertRaises(engine.BroadcastError):
                engine.sync_groups("+1", on_log=lambda *_: None)
        self.assertLessEqual(calls["n"], 2)  # bailed, did not churn


if __name__ == "__main__":
    unittest.main()
