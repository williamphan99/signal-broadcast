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
import contextlib
import json
import multiprocessing
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import engine  # noqa: E402


def _hold_group_transaction(groups_file, lock_file, ready, release):
    engine.GROUPS_FILE = Path(groups_file)
    engine.GROUPS_LOCK_FILE = Path(lock_file)
    with engine._groups_transaction():
        ready.set()
        release.wait(timeout=5)


def _write_group_selection_process(groups_file, lock_file, attempting, done):
    engine.GROUPS_FILE = Path(groups_file)
    engine.GROUPS_LOCK_FILE = Path(lock_file)
    attempting.set()
    engine.write_group_selection({"g1"})
    done.set()


class _FakeCatalogProc:
    """Stand-in for the `signal-cli listGroups` Popen: communicate() times out
    ``busy_polls`` times (the read is still running), then returns the catalog."""

    def __init__(self, stdout="[]", stderr="", rc=0, busy_polls=0, poll_sleep=0.0):
        self.stdout_text, self.stderr_text, self.returncode = stdout, stderr, rc
        self.busy_polls, self.poll_sleep = busy_polls, poll_sleep
        self.polls = 0
        self.killed = False

    def communicate(self, timeout=None):
        if self.killed:
            return "", ""
        self.polls += 1
        if self.poll_sleep:
            time.sleep(self.poll_sleep)
        if self.polls <= self.busy_polls:
            raise engine.subprocess.TimeoutExpired(cmd="listGroups", timeout=timeout)
        return self.stdout_text, self.stderr_text

    def kill(self):
        self.killed = True


def _catalog_popen(stdout, stderr="", rc=0):
    return mock.patch.object(engine.subprocess, "Popen",
                             return_value=_FakeCatalogProc(stdout, stderr, rc))


class GroupCatalogReadTests(unittest.TestCase):
    """The first listGroups after (re)linking fetches every group's state from Signal,
    one network round-trip per group, saving each as it goes. A fixed five-minute kill
    threw that run away on big accounts and blamed the connection — the reported bug.
    The read must now only stop when it makes NO progress."""

    def _read(self, proc, progress, **limits):
        logs = []
        patches = [mock.patch.object(engine.subprocess, "Popen", return_value=proc),
                   mock.patch.object(engine, "_catalog_progress", side_effect=progress),
                   mock.patch.object(engine, "GROUP_CATALOG_POLL_S", 0.001)]
        for name, value in limits.items():
            patches.append(mock.patch.object(engine, name, value))
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            result = engine._run_group_catalog_read(["listGroups"], None, "+1", logs.append)
        return result, logs

    def test_keeps_going_while_groups_are_still_being_prepared(self):
        ready = iter(range(100, 100_000))
        proc = _FakeCatalogProc('[{"id":"g1","name":"One"}]', busy_polls=30, poll_sleep=0.005)
        # 30 polls × 5 ms is well past the 20 ms "no progress" limit — but every poll
        # shows another group prepared, so the read is never cut off.
        result, logs = self._read(proc, lambda _a: (next(ready), 640),
                                  GROUP_CATALOG_TIMEOUT_S=0.02)
        self.assertEqual(result.returncode, 0)
        self.assertFalse(proc.killed)
        self.assertTrue(any("of 640 ready" in line for line in logs), logs)
        self.assertTrue(any("min left" in line for line in logs), logs)

    def test_stops_when_no_group_is_prepared_for_the_limit_and_names_the_counts(self):
        limit = 0.03
        proc = _FakeCatalogProc(busy_polls=10_000, poll_sleep=0.005)
        with self.assertRaises(engine.GroupCatalogStalled) as ctx:
            self._read(proc, lambda _a: (120, 640), GROUP_CATALOG_TIMEOUT_S=limit)
        self.assertTrue(proc.killed)
        message = str(ctx.exception)
        self.assertIn("120 ready", message)
        self.assertIn("640 groups", message)
        # The wait names the limit actually in force, so retuning the constant can't
        # leave stale prose behind (it read "five minutes" regardless, once).
        self.assertIn(engine._minutes_phrase(limit), message)
        self.assertIn("every prepared group is kept", message)
        self.assertEqual((ctx.exception.ready, ctx.exception.total), (120, 640))
        self.assertFalse(ctx.exception.progressed)

    def test_progress_then_stall_says_progress_is_saved(self):
        counts = iter([100, 101, 102, 103, 104, 105])
        proc = _FakeCatalogProc(busy_polls=10_000, poll_sleep=0.005)
        with self.assertRaises(engine.GroupCatalogStalled) as ctx:
            self._read(proc, lambda _a: (next(counts, 105), 640), GROUP_CATALOG_TIMEOUT_S=0.03)
        self.assertTrue(ctx.exception.progressed)
        self.assertIn("105 of 640 ready", str(ctx.exception))
        self.assertIn("Progress is saved", str(ctx.exception))

    def test_ceiling_stops_even_a_progressing_read_and_says_how_to_continue(self):
        ready = iter(range(1, 100_000))
        proc = _FakeCatalogProc(busy_polls=10_000, poll_sleep=0.005)
        with self.assertRaises(engine.GroupCatalogStalled) as ctx:
            self._read(proc, lambda _a: (next(ready), 5000),
                       GROUP_CATALOG_TIMEOUT_S=60, GROUP_CATALOG_MAX_S=0.03)
        self.assertTrue(proc.killed)
        self.assertIn("Progress is saved", str(ctx.exception))
        self.assertIn("again to continue", str(ctx.exception))

    def test_unreadable_store_falls_back_to_the_plain_timeout(self):
        limit = 0.03
        proc = _FakeCatalogProc(busy_polls=10_000, poll_sleep=0.005)
        with self.assertRaises(engine.GroupCatalogStalled) as ctx:
            self._read(proc, lambda _a: None, GROUP_CATALOG_TIMEOUT_S=limit)
        self.assertIsNone(ctx.exception.ready)
        self.assertIn("Timed out fetching groups", str(ctx.exception))
        self.assertIn(engine._minutes_phrase(limit), str(ctx.exception))

    def test_a_one_minute_limit_is_not_described_as_1_minutes(self):
        self.assertEqual(engine._minutes_phrase(60), "1 minute")
        self.assertEqual(engine._minutes_phrase(300), "5 minutes")
        self.assertEqual(engine._minutes_phrase(0.5), "1 minute")

    def test_catalog_progress_counts_prepared_groups_in_the_signal_cli_store(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "data"
            (data / "946136.d").mkdir(parents=True)
            (data / "accounts.json").write_text(json.dumps({
                "accounts": [{"path": "946136", "number": "+1"}], "version": 2}))
            con = sqlite3.connect(data / "946136.d" / "account.db")
            con.execute("""CREATE TABLE group_v2 (
                _id INTEGER PRIMARY KEY, storage_id BLOB UNIQUE, storage_record BLOB,
                group_id BLOB UNIQUE NOT NULL, master_key BLOB NOT NULL, group_data BLOB,
                distribution_id BLOB UNIQUE NOT NULL,
                endorsement_expiration_time INTEGER NOT NULL DEFAULT 0,
                blocked INTEGER NOT NULL DEFAULT FALSE,
                profile_sharing INTEGER NOT NULL DEFAULT FALSE,
                permission_denied INTEGER NOT NULL DEFAULT FALSE) STRICT""")
            rows = [(b"a", b"k", b"state", b"d1", 0), (b"b", b"k", None, b"d2", 0),
                    (b"c", b"k", None, b"d3", 1), (b"d", b"k", b"state", b"d4", 0)]
            con.executemany("INSERT INTO group_v2 (group_id, master_key, group_data, "
                            "distribution_id, permission_denied) VALUES (?,?,?,?,?)", rows)
            con.commit()
            con.close()
            with mock.patch.object(engine, "DATA_DIR", Path(directory)):
                # b is the only group still to prepare: c is denied, so never fetched.
                self.assertEqual(engine._catalog_progress("+1"), (3, 4))
                self.assertIsNone(engine._catalog_progress("+2"))
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(engine, "DATA_DIR", Path(directory)):
            self.assertIsNone(engine._catalog_progress("+1"))


class GroupSnapshotTests(unittest.TestCase):
    def test_one_group_read_also_populates_the_permission_cache(self):
        groups = [
            {"id": "g1", "name": "One", "permissionSendMessage": "EVERY_MEMBER",
             "members": [{"number": "+1", "isAdmin": False}]},
            {"id": "g2", "name": "Two", "permissionSendMessage": "ONLY_ADMINS",
             "members": [{"number": "+1", "isAdmin": False}]},
        ]
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(engine, "GROUPS_FILE", Path(directory) / "groups.txt"), \
             mock.patch.object(engine, "GROUPS_LOCK_FILE", Path(directory) / "groups.lock"), \
             mock.patch.object(engine, "GROUP_PERMISSIONS_FILE", Path(directory) / "permissions.json"), \
             mock.patch.object(engine, "signal_cli_bin", return_value="/bin/true"), \
             _catalog_popen(json.dumps(groups)) as popen:
            count = engine.pull_groups("+1")

        self.assertEqual(count, 2)
        self.assertEqual(engine.cached_unsendable_groups("+1"), {"g2"})
        self.assertEqual(engine.cached_unsendable_groups("+other"), set())
        self.assertEqual(popen.call_count, 1)

    def test_permissions_survive_a_process_cache_reset(self):
        groups = [
            {"id": "g1", "name": "One", "permissionSendMessage": "ONLY_ADMINS",
             "members": [{"number": "+1", "isAdmin": False}]},
        ]
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(engine, "GROUPS_FILE", Path(directory) / "groups.txt"), \
             mock.patch.object(engine, "GROUPS_LOCK_FILE", Path(directory) / "groups.lock"), \
             mock.patch.object(engine, "GROUP_PERMISSIONS_FILE", Path(directory) / "permissions.json"), \
             mock.patch.object(engine, "signal_cli_bin", return_value="/bin/true"), \
             _catalog_popen(json.dumps(groups)):
            engine.pull_groups("+1")
            engine._GROUP_PERMISSION_CACHE = ("", set())
            self.assertEqual(engine.stored_unsendable_groups("+1"), {"g1"})

    def test_permission_cache_write_failure_does_not_fail_group_sync(self):
        groups = [{"id": "g1", "name": "One"}]
        with tempfile.TemporaryDirectory() as directory:
            group_file = Path(directory) / "groups.txt"
            permission_file = Path(directory) / "permissions.json"
            original_write = engine._atomic_write_text

            def write(path, body):
                if path == permission_file:
                    raise OSError("disk full")
                return original_write(path, body)

            with mock.patch.object(engine, "GROUPS_FILE", group_file), \
                 mock.patch.object(engine, "GROUPS_LOCK_FILE", Path(directory) / "groups.lock"), \
                 mock.patch.object(engine, "GROUP_PERMISSIONS_FILE", permission_file), \
                 mock.patch.object(engine, "signal_cli_bin", return_value="/bin/true"), \
                 _catalog_popen(json.dumps(groups)), \
                 mock.patch.object(engine, "_atomic_write_text", side_effect=write):
                self.assertEqual(engine.pull_groups("+1"), 1)
            self.assertTrue(group_file.exists())
            self.assertFalse(permission_file.exists())

    def test_idle_group_sync_stays_within_five_process_launches(self):
        calls = []

        def fake_run(argv, **_kwargs):
            calls.append(argv)
            return mock.Mock(returncode=0, stdout="", stderr="")

        def fake_popen(argv, **_kwargs):   # listGroups is the only Popen in a sync
            calls.append(argv)
            return _FakeCatalogProc('[{"id":"g1","name":"One"}]')

        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(engine, "GROUPS_FILE", Path(directory) / "groups.txt"), \
             mock.patch.object(engine, "GROUPS_LOCK_FILE", Path(directory) / "groups.lock"), \
             mock.patch.object(engine, "GROUP_PERMISSIONS_FILE", Path(directory) / "permissions.json"), \
             mock.patch.object(engine, "signal_cli_bin", return_value="/bin/true"), \
             mock.patch.object(engine, "_catalog_progress", return_value=None), \
             mock.patch.object(engine, "_sync_log"), \
             mock.patch.object(engine.subprocess, "run", side_effect=fake_run), \
             mock.patch.object(engine.subprocess, "Popen", side_effect=fake_popen):
            self.assertEqual(engine.sync_groups("+1"), 1)

        self.assertEqual(len(calls), 5, "one nudge plus two quiet receive/listGroups rounds")


class GroupFileTransactionTests(unittest.TestCase):
    def test_interrupted_selection_write_keeps_the_previous_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            groups = Path(directory) / "groups.txt"
            lock = Path(directory) / "groups.lock"
            groups.write_text("g1\tOne\ng2\tTwo\n", encoding="utf-8")
            original = groups.read_text(encoding="utf-8")
            with mock.patch.object(engine, "GROUPS_FILE", groups), \
                 mock.patch.object(engine, "GROUPS_LOCK_FILE", lock), \
                 mock.patch("os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    engine.write_group_selection({"g1"})
            self.assertEqual(groups.read_text(encoding="utf-8"), original)
            self.assertFalse(any(groups.parent.glob("groups.txt.*.tmp")))

    def test_fifty_thousand_groups_round_trip_without_losing_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            groups = Path(directory) / "groups.txt"
            lock = Path(directory) / "groups.lock"
            groups.write_text("".join(f"g{i}\tGroup {i}\n" for i in range(50_000)),
                              encoding="utf-8")
            enabled = {f"g{i}" for i in range(0, 50_000, 2)}
            with mock.patch.object(engine, "GROUPS_FILE", groups), \
                 mock.patch.object(engine, "GROUPS_LOCK_FILE", lock):
                engine.write_group_selection(enabled)
                entries = engine.read_group_entries()
            self.assertEqual(len(entries), 50_000)
            self.assertEqual(sum(entry.enabled for entry in entries), 25_000)

    def test_a_second_process_waits_for_the_group_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            groups = Path(directory) / "groups.txt"
            lock = Path(directory) / "groups.lock"
            groups.write_text("g1\tOne\ng2\tTwo\n", encoding="utf-8")
            ctx = multiprocessing.get_context("spawn")
            ready, release = ctx.Event(), ctx.Event()
            attempting, done = ctx.Event(), ctx.Event()
            holder = ctx.Process(target=_hold_group_transaction,
                                 args=(str(groups), str(lock), ready, release))
            writer = ctx.Process(target=_write_group_selection_process,
                                 args=(str(groups), str(lock), attempting, done))
            try:
                holder.start()
                self.assertTrue(ready.wait(timeout=5))
                writer.start()
                self.assertTrue(attempting.wait(timeout=5))
                self.assertFalse(done.wait(timeout=0.25))
            finally:
                release.set()
                for process in (holder, writer):
                    process.join(timeout=5)
                    if process.is_alive():
                        process.terminate()
                        process.join(timeout=5)
            self.assertEqual((holder.exitcode, writer.exitcode), (0, 0))
            self.assertTrue(done.is_set())

    def test_repeated_large_catalog_reads_parse_once_and_atomic_write_invalidates(self):
        with tempfile.TemporaryDirectory() as directory:
            groups = Path(directory) / "groups.txt"
            lock = Path(directory) / "groups.lock"
            groups.write_text("".join(f"g{i}\tGroup {i}\n" for i in range(50_000)),
                              encoding="utf-8")
            engine._GROUP_ENTRIES_CACHE = None
            original_parser = engine._read_group_entries_file
            with mock.patch.object(engine, "GROUPS_FILE", groups), \
                 mock.patch.object(engine, "GROUPS_LOCK_FILE", lock), \
                 mock.patch.object(engine, "_read_group_entries_file",
                                   wraps=original_parser) as parser:
                for _ in range(20):
                    self.assertEqual(len(engine.read_group_entries()), 50_000)
                self.assertEqual(parser.call_count, 1)
                engine.write_group_selection({"g0"})
                entries = engine.read_group_entries()
                self.assertEqual(parser.call_count, 3)  # writer merge, then new inode read
            self.assertEqual(sum(entry.enabled for entry in entries), 1)


class AppUpdateTests(unittest.TestCase):
    @staticmethod
    def _proc(returncode=0, stdout="", stderr=""):
        return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)

    def test_setup_change_is_reported_after_pull(self):
        runs = [
            self._proc(stdout="old\n"),
            self._proc(stdout="Updating old..new\n"),
            self._proc(stdout="new\n"),
            self._proc(stdout="Setup.command\n"),
        ]
        with mock.patch.object(engine.subprocess, "run", side_effect=runs):
            result = engine.git_pull()
        self.assertTrue(result.changed)
        self.assertTrue(result.needs_setup)

    def test_code_only_update_can_restart_directly(self):
        runs = [
            self._proc(stdout="old\n"),
            self._proc(stdout="Updating old..new\n"),
            self._proc(stdout="new\n"),
            self._proc(stdout=""),
        ]
        with mock.patch.object(engine.subprocess, "run", side_effect=runs):
            result = engine.git_pull()
        self.assertTrue(result.changed)
        self.assertFalse(result.needs_setup)


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
    """Broken is positive evidence; lease contention is explicitly unknown."""

    @staticmethod
    def _completed(rc: int, stdout: str):
        proc = mock.Mock()
        proc.returncode = rc
        proc.stdout = stdout
        return proc

    def _run(self, is_linked: bool, rc: int, stdout: str) -> bool | None:
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

    def test_busy_signal_operation_is_unknown(self):
        with mock.patch.object(engine, "signal_cli_operation",
                               side_effect=engine.BroadcastError("busy")):
            self.assertIsNone(engine.link_is_broken())


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

        def fake_pull(_acct, *_on_log):
            item = next(seq, pull_side_effect[-1])
            if isinstance(item, Exception):
                raise item
            return item

        with mock.patch.object(engine, "signal_cli_bin", lambda: "/bin/true"), \
             mock.patch.object(engine, "_request_sync", lambda *a, **k: None), \
             mock.patch.object(engine, "_catalog_progress", lambda *_a: None), \
             mock.patch.object(engine, "pull_groups", fake_pull), \
             mock.patch.object(engine.subprocess, "run", lambda *a, **k: recv):
            return engine.sync_groups("+1", on_log=lambda *_: None)

    def test_all_failures_raise_with_reason(self):
        err = engine.BroadcastError("Could not fetch groups:\nUser +1 is not registered.")
        with self.assertRaises(engine.BroadcastError) as ctx:
            self._sync(self._Recv(rc=1, err="User +1 is not registered."), [err])
        self.assertIn("no longer linked", str(ctx.exception))

    def test_connected_catalog_failures_are_not_reported_as_a_large_backlog(self):
        failed = engine.BroadcastError("Could not fetch groups: account already in use")
        with self.assertRaises(engine.BroadcastError) as ctx:
            self._sync(self._Recv(rc=0, err="server timestamp received"), [failed])
        message = str(ctx.exception).lower()
        self.assertIn("group list could not be read", message)
        self.assertNotIn("large backlog", message)

    def test_catalog_timeout_names_the_no_progress_limit(self):
        timed_out = engine.GroupCatalogStalled(
            engine._catalog_stall_message(None, None, False, False), None, None, False)
        with self.assertRaises(engine.BroadcastError) as ctx:
            self._sync(self._Recv(rc=0, err="server timestamp received"), [timed_out])
        message = str(ctx.exception).lower()
        self.assertIn(engine._minutes_phrase(engine.GROUP_CATALOG_TIMEOUT_S), message)
        self.assertNotIn("other signal activity", message)

    def test_stalled_catalog_reports_counts_and_stops_instead_of_churning(self):
        # A stall means signal-cli prepared nothing new for five minutes: retrying in
        # the same run would only stall again. Surface exactly where it got to, once.
        calls = {"n": 0}
        stalled = engine.GroupCatalogStalled(
            engine._catalog_stall_message(118, 640, True, False), 118, 640, True)

        def fake_pull(_acct, *_on_log):
            calls["n"] += 1
            raise stalled

        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(engine, "SYNC_DEBUG_FILE", Path(directory) / "sync-debug.txt"), \
             mock.patch.object(engine, "signal_cli_bin", lambda: "/bin/true"), \
             mock.patch.object(engine, "_request_sync", lambda *a, **k: None), \
             mock.patch.object(engine, "_catalog_progress", lambda *_a: (118, 640)), \
             mock.patch.object(engine, "pull_groups", fake_pull), \
             mock.patch.object(engine.subprocess, "run", lambda *a, **k: self._Recv()):
            with self.assertRaises(engine.GroupCatalogStalled) as ctx:
                engine.sync_groups("+61400000000", on_log=lambda *_: None)
            debug = engine.SYNC_DEBUG_FILE.read_text(encoding="utf-8")
        self.assertEqual(calls["n"], 1)
        self.assertIn("118 of 640 ready", str(ctx.exception))
        self.assertIn("STALLED", debug)
        self.assertIn("prepared=118 of 640", debug)
        self.assertNotIn("+61400000000", debug)

    def test_success_returns_count(self):
        # Two stable reads settle a quiet queue and return the count.
        self.assertEqual(self._sync(self._Recv(), [5, 5, 5]), 5)

    def test_transient_error_then_success(self):
        # A non-permanent error is retried, not surfaced, and the eventual count wins.
        transient = engine.BroadcastError("connection reset by peer")
        self.assertEqual(self._sync(self._Recv(), [transient, 2, 2, 2]), 2)

    def test_sync_debug_does_not_store_receive_sender_metadata(self):
        private_output = (
            '{"envelope":{"source":"+61400000000",'
            '"sourceUuid":"private-uuid","sourceName":"Private Person"}}')

        def busy_receive(*_args, **_kwargs):
            raise engine.subprocess.TimeoutExpired(
                cmd="receive", timeout=15, stderr=private_output)

        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(engine, "SYNC_DEBUG_FILE", Path(directory) / "sync-debug.txt"), \
             mock.patch.object(engine, "signal_cli_bin", return_value="/bin/true"), \
             mock.patch.object(engine, "_request_sync", lambda *_a, **_k: None), \
             mock.patch.object(engine, "pull_groups", return_value=1), \
             mock.patch.object(engine, "_save_notes_seen_during", lambda *_a, **_k: None), \
             mock.patch.object(engine.subprocess, "run", side_effect=busy_receive):
            self.assertEqual(engine._sync_groups_unlocked("+1"), 1)
            debug = engine.SYNC_DEBUG_FILE.read_text(encoding="utf-8")

        self.assertIn("receive busy-timeout", debug)
        self.assertNotIn("+61400000000", debug)
        self.assertNotIn("private-uuid", debug)
        self.assertNotIn("Private Person", debug)
        self.assertNotIn("envelope", debug)

    def test_sync_debug_redacts_account_from_catalog_errors(self):
        private_error = engine.BroadcastError(
            "Could not fetch groups: Error while checking account +61400000000: "
            "[403] Authorization failed!")
        recv = self._Recv(rc=0, err="")

        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(engine, "SYNC_DEBUG_FILE", Path(directory) / "sync-debug.txt"), \
             mock.patch.object(engine, "signal_cli_bin", return_value="/bin/true"), \
             mock.patch.object(engine, "_request_sync", lambda *_a, **_k: None), \
             mock.patch.object(engine, "pull_groups", side_effect=private_error), \
             mock.patch.object(engine.subprocess, "run", return_value=recv):
            with self.assertRaises(engine.BroadcastError):
                engine._sync_groups_unlocked("+61400000000")
            debug = engine.SYNC_DEBUG_FILE.read_text(encoding="utf-8")

        self.assertIn("authorization failed", debug.lower())
        self.assertNotIn("+61400000000", debug)

    def test_permanent_error_bails_fast(self):
        # A "not registered" error must break out immediately, not loop for SYNC_MAX_S.
        calls = {"n": 0}
        dead = engine.BroadcastError("User +1 is not registered.")

        def fake_pull(_acct, *_on_log):
            calls["n"] += 1
            raise dead

        with mock.patch.object(engine, "signal_cli_bin", lambda: "/bin/true"), \
             mock.patch.object(engine, "_request_sync", lambda *a, **k: None), \
             mock.patch.object(engine, "pull_groups", fake_pull), \
             mock.patch.object(engine.subprocess, "run", lambda *a, **k: self._Recv()):
            with self.assertRaises(engine.BroadcastError):
                engine.sync_groups("+1", on_log=lambda *_: None)
        self.assertLessEqual(calls["n"], 2)  # bailed, did not churn

    def _sync_recv_raises(self, exc_factory, pull_side_effect):
        """Like _sync but the `receive` subprocess RAISES (e.g. TimeoutExpired) each
        call. pull_groups still runs from pull_side_effect."""
        seq = iter(pull_side_effect)

        def fake_pull(_acct, *_on_log):
            item = next(seq, pull_side_effect[-1])
            if isinstance(item, Exception):
                raise item
            return item

        def fake_run(*a, **k):
            raise exc_factory()

        with mock.patch.object(engine, "signal_cli_bin", lambda: "/bin/true"), \
             mock.patch.object(engine, "_request_sync", lambda *a, **k: None), \
             mock.patch.object(engine, "_catalog_progress", lambda *_a: None), \
             mock.patch.object(engine, "pull_groups", fake_pull), \
             mock.patch.object(engine.subprocess, "run", fake_run):
            return engine.sync_groups("+1", on_log=lambda *_: None)

    def test_busy_receive_timeout_is_progress_not_a_connection_error(self):
        # receive keeps hitting its outer timeout WHILE producing output (downloading a
        # backlog). That must be treated as progress: keep draining, and the eventual
        # group count wins — NOT reported as "couldn't connect".
        def busy_timeout():
            return engine.subprocess.TimeoutExpired(
                cmd="receive", timeout=15,
                stderr="INFO IncomingMessageHandler - server timestamp received")
        # groups arrive after a couple of busy bursts, then hold steady
        self.assertEqual(self._sync_recv_raises(busy_timeout, [0, 4, 4, 4]), 4)

    def test_silent_receive_timeout_reports_connection_problem(self):
        # receive times out with NO output at all → a real connect hang → surface the
        # network/connection message, not a groups count.
        def silent_timeout():
            return engine.subprocess.TimeoutExpired(cmd="receive", timeout=15, stderr="")
        with self.assertRaises(engine.BroadcastError) as ctx:
            self._sync_recv_raises(silent_timeout, [0])
        self.assertIn("connect", str(ctx.exception).lower())


class MessageStyleTests(unittest.TestCase):
    """Signal measures style ranges in UTF-16 code units, not characters. Getting the
    length wrong doesn't error — it silently styles the wrong span — so pin it down."""

    def test_plain_is_no_metadata_at_all(self):
        self.assertEqual(engine.message_text_styles("hello", "none"), [])

    def test_italic_covers_the_whole_message(self):
        self.assertEqual(engine.message_text_styles("hello", "italic"), ["0:5:ITALIC"])

    def test_bold_italic_emits_two_overlapping_ranges(self):
        self.assertEqual(engine.message_text_styles("hi", "bold_italic"),
                         ["0:2:BOLD", "0:2:ITALIC"])

    def test_emoji_counts_as_two_utf16_units(self):
        # "hi 👋" is 4 Python characters but 5 UTF-16 code units — len() would style
        # one unit short and leave the last half of the emoji unformatted.
        self.assertEqual(engine.utf16_length("hi 👋"), 5)
        self.assertEqual(engine.message_text_styles("hi 👋", "bold"), ["0:5:BOLD"])

    def test_astral_only_message(self):
        self.assertEqual(engine.message_text_styles("👋👋", "italic"), ["0:4:ITALIC"])

    def test_empty_message_styles_nothing(self):
        self.assertEqual(engine.message_text_styles("", "bold"), [])

    def test_unknown_style_degrades_to_plain_rather_than_raising(self):
        # A hand-edited config must never be able to block a broadcast.
        self.assertEqual(engine.normalize_message_style("rainbow"), "none")
        self.assertEqual(engine.message_text_styles("hello", "rainbow"), [])

    def test_style_keys_are_forgiving_about_shape(self):
        for raw in ("BOLD_ITALIC", "bold-italic", "  Bold Italic  "):
            self.assertEqual(engine.normalize_message_style(raw), "bold_italic")

    def test_every_labelled_style_is_a_real_key(self):
        # The pickers are built from MESSAGE_STYLE_LABELS; a typo there would render a
        # button that silently sends plain text.
        self.assertEqual([k for k, _ in engine.MESSAGE_STYLE_LABELS],
                         list(engine.MESSAGE_STYLES))


class ErrorClassificationTests(unittest.TestCase):
    """These labels are the ONLY thing the user sees about a failure, so a wrong one
    sends them chasing the wrong problem. A bare "ssl" in the network pattern matched
    the "ssL" inside "ClassLoader", so every Java stack trace read as a network fault
    and a broken install hid behind "network or connection problem" for weeks."""

    BROKEN_INSTALL = (
        "java.lang.NoClassDefFoundError: org/signal/libsignal/internal/Native\n"
        "\tat org.asamk.signal.manager.Manager.isSignalClientAvailable(Manager.java:81)\n"
        "Caused by: java.lang.ClassNotFoundException: org.signal.libsignal.internal.Native\n"
        "\tat java.base/jdk.internal.loader.BuiltinClassLoader.loadClass(BuiltinClassLoader.java:580)")

    def test_classloader_stack_trace_is_not_a_network_problem(self):
        self.assertNotIn("network", engine.classify_error(self.BROKEN_INSTALL))

    def test_broken_install_says_so(self):
        self.assertIn("install", engine.classify_error(self.BROKEN_INSTALL))

    def test_wrong_java_version_is_also_a_broken_install(self):
        self.assertIn("install", engine.classify_error(
            "UnsupportedClassVersionError: org/asamk/signal/Main has been compiled by a "
            "more recent version of the Java Runtime"))

    def test_our_own_account_deregistered_is_not_blamed_on_the_recipient(self):
        # "User +61… is not registered" is OUR account being removed from the phone's
        # Linked Devices. Labelling it a recipient problem hides the one fix: re-link.
        label = engine.classify_error("User +61415747310 is not registered.")
        self.assertIn("re-link", label)
        self.assertNotIn("recipient", label)

    def test_genuine_network_errors_still_classify_as_network(self):
        for err in ("javax.net.ssl.SSLHandshakeException: bad cert",
                    "java.net.SocketException: Failed to get response for request",
                    "java.net.ConnectException: Connection refused",
                    "no route to host"):
            self.assertEqual(engine.classify_error(err), "network or connection problem", err)


class BundledBinaryTests(unittest.TestCase):
    """A vendored signal-cli whose lib/ is missing libsignal-client can't run at all.
    Preferring it over a working signal-cli on PATH makes every send fail until someone
    notices the directory — so an unusable bundle must report as absent."""

    def _vendor(self, tmp, jars):
        lib = Path(tmp) / "signal-cli-0.14.5" / "lib"
        lib.mkdir(parents=True)
        for j in jars:
            (lib / j).touch()
        binary = Path(tmp) / "signal-cli-0.14.5" / "bin" / "signal-cli"
        binary.parent.mkdir(parents=True)
        binary.touch()
        return binary

    def test_incomplete_bundle_reports_absent_so_we_fall_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._vendor(tmp, ["libsignal-cli-0.14.5.jar", "jackson-core-2.20.2.jar"])
            with mock.patch.object(engine, "VENDOR_DIR", Path(tmp)):
                self.assertIsNone(engine._jvm_signal_cli())

    def test_complete_bundle_is_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = self._vendor(tmp, ["libsignal-client-0.83.1.jar", "libsignal-cli-0.14.5.jar"])
            with mock.patch.object(engine, "VENDOR_DIR", Path(tmp)):
                self.assertEqual(engine._jvm_signal_cli(), binary)

    def test_newest_complete_bundle_wins_after_an_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._vendor(tmp, ["libsignal-client-0.83.1.jar", "libsignal-cli-0.14.5.jar"])
            new_lib = Path(tmp) / "signal-cli-0.14.7" / "lib"
            new_lib.mkdir(parents=True)
            (new_lib / "libsignal-client-0.99.1.jar").touch()
            new_binary = Path(tmp) / "signal-cli-0.14.7" / "bin" / "signal-cli"
            new_binary.parent.mkdir(parents=True)
            new_binary.touch()
            with mock.patch.object(engine, "VENDOR_DIR", Path(tmp)):
                self.assertEqual(engine._jvm_signal_cli(), new_binary)

    def test_missing_vendor_dir_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(engine, "VENDOR_DIR", Path(tmp) / "nope"):
                self.assertIsNone(engine._jvm_signal_cli())



# These tests must never use the installer's live Signal store.
from runtime import isolated_engine

def setUpModule():
    global _runtime
    _runtime = isolated_engine()
    _runtime.__enter__()

def tearDownModule():
    _runtime.__exit__(None, None, None)

if __name__ == "__main__":
    unittest.main()
