"""Receive reliability with disposable files and a simulated Signal process."""
import io
import json
import sys
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import engine
from runtime import isolated_engine
from test_notes import envelope, note


class Process:
    def __init__(self, output="", *, error="", returncode=0, timeout=False):
        self.stdout = io.StringIO(output)
        self.stderr = io.StringIO(error)
        self.returncode = returncode
        self.timeout = timeout
        self.killed = False
        self.wait_limits = []

    def wait(self, timeout=None):
        self.wait_limits.append(timeout)
        if self.timeout and not self.killed:
            raise engine.subprocess.TimeoutExpired("simulated receive", timeout)
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


class ReceiveReliability(unittest.TestCase):
    def setUp(self):
        self.scope = ExitStack()
        self.addCleanup(self.scope.close)
        self.scope.enter_context(isolated_engine())
        self.scope.enter_context(mock.patch.object(engine, "signal_cli_bin", return_value="simulated-signal"))
        self.scope.enter_context(mock.patch.object(engine, "_signal_env", return_value={}))

    def receive(self, process, on_log=lambda *_: None):
        with mock.patch.object(engine.subprocess, "Popen", return_value=process) as spawn:
            report = engine.fetch_notes("+19999999999", on_log)
        return report, spawn

    def large_note(self, count):
        directory = engine.DATA_DIR / "attachments"
        directory.mkdir(parents=True)
        body = "Full note text. " * 3000
        (directory / "body.txt").write_text(body)
        attachments = [{"id": "body.txt", "contentType": "text/x-signal-plain"}]
        for i in range(count):
            name = f"photo-{i}.jpg"
            (directory / name).write_bytes(b"synthetic image")
            attachments.append({"id": name, "contentType": "image/jpeg"})
        return envelope(note("short preview", ts=42, attachments=attachments)), body

    def test_notes_receive_keeps_eleven_photos_and_full_text(self):
        stream, body = self.large_note(11)
        process = Process(stream)
        report, spawn = self.receive(process)
        saved = engine.read_notes()[0]
        self.assertEqual(len(saved["photos"]), 11)
        self.assertEqual(saved["text"], body)
        self.assertEqual(saved["missing_photos"], 0)
        self.assertTrue(report["complete"])
        self.assertNotIn("--ignore-attachments", spawn.call_args.args[0])
        self.assertEqual(process.wait_limits[0], 3600)

    def test_group_refresh_keeps_fifteen_photos_and_full_text(self):
        stream, body = self.large_note(15)
        processes = [Process(stream), Process(), Process()]
        with mock.patch.object(engine, "_request_sync"), \
             mock.patch.object(engine, "_catalog_progress", return_value=None), \
             mock.patch.object(engine, "pull_groups", return_value=4), \
             mock.patch.object(engine.subprocess, "Popen", side_effect=processes) as spawn:
            self.assertEqual(engine.sync_groups("+19999999999"), 4)
        saved = engine.read_notes()[0]
        self.assertEqual(saved["text"], body)
        self.assertEqual(len(saved["photos"]), 15)
        for call in spawn.call_args_list:
            self.assertNotIn("--ignore-attachments", call.args[0])
        self.assertEqual(processes[0].wait_limits[0], 3600)

    def test_a_note_is_durable_before_receive_finishes(self):
        ready, release = threading.Event(), threading.Event()
        process = Process()

        class Stream:
            def __iter__(self):
                yield envelope(note("saved during download", ts=1))
                ready.set()  # parser has persisted the previous yield
                release.wait(2)
            def close(self):
                pass

        process.stdout = Stream()
        def wait(timeout=None):
            try:
                self.assertTrue(ready.wait(2))
                self.assertEqual(engine.read_notes()[0]["text"], "saved during download")
                self.assertFalse(process.killed)
            finally:
                release.set()
        process.wait = wait
        self.receive(process)

    def test_one_malformed_note_does_not_discard_good_notes(self):
        stream = "\n".join([envelope(note("first", ts=1)),
                             envelope(note("invalid", ts="not a timestamp")),
                             envelope(note("last", ts=3))])
        report, _ = self.receive(Process(stream))
        self.assertEqual([n["text"] for n in engine.read_notes()], ["last", "first"])
        self.assertEqual(report["invalid"], 1)
        self.assertFalse(report["complete"])
        self.assertIn("malformed", report["warning"])

    def test_empty_timeout_is_not_success(self):
        report, _ = self.receive(Process(timeout=True))
        self.assertFalse(report["complete"])
        self.assertIn("one-hour", report["warning"])
        debug = engine.NOTES_DEBUG_FILE.read_text()
        self.assertIn("timeout=True", debug)
        self.assertNotIn("+19999999999", debug)

    def test_connection_failure_keeps_partial_notes_and_reports_incomplete(self):
        report, _ = self.receive(Process(envelope(note("keep", ts=1)),
                                         returncode=3, error="Connection closed +19999999999"))
        self.assertEqual(engine.read_notes()[0]["text"], "keep")
        self.assertFalse(report["complete"])
        self.assertIn("connection", report["warning"])
        self.assertNotIn("+19999999999", report["warning"])

    def test_complete_redelivery_repairs_old_note_without_downgrading_it(self):
        old = {"ts": 1, "text": "preview", "photos": [], "missing_photos": 11, "missing_body": True}
        full = {"ts": 1, "text": "complete text", "photos": [{"path": str(i)} for i in range(11)], "missing_photos": 0}
        engine.store_notes([old])
        self.assertEqual(engine.store_notes([full]), 1)
        self.assertEqual(engine.read_notes(), [full])
        self.assertEqual(engine.store_notes([old]), 0)
        self.assertEqual(engine.read_notes(), [full])

    def test_storage_failure_stops_receiving_and_keeps_already_saved_notes(self):
        original = engine.store_notes
        def store(found):
            if found[0]["ts"] == 2:
                raise OSError("simulated full disk")
            return original(found)
        process = Process("\n".join(envelope(note(str(i), ts=i)) for i in (1, 2, 3)))
        with mock.patch.object(engine, "store_notes", side_effect=store):
            report, _ = self.receive(process)
        self.assertTrue(process.killed)
        self.assertEqual([n["ts"] for n in engine.read_notes()], [1])
        self.assertIn("disk space", report["warning"])
        self.assertFalse(report["complete"])

    def test_large_non_note_backlog_is_streamed_and_not_stored(self):
        def messages():
            for i in range(20000):
                yield json.dumps({"envelope": {"dataMessage": {"message": "private"}}})
            yield envelope(note("last message", ts=1))
        process = Process()
        process.stdout = mock.Mock()
        process.stdout.__iter__ = lambda _: messages()
        report, _ = self.receive(process)
        self.assertEqual(report["envelopes"], 20001)
        self.assertEqual(len(engine.read_notes()), 1)
        self.assertNotIn("private", engine.NOTES_FILE.read_text())

    def test_actual_process_timeout_keeps_a_note_written_before_termination(self):
        # This process only prints a synthetic envelope and sleeps. No Signal transport.
        script = "import time; print(" + repr(envelope(note("already received", ts=1))) + ", flush=True); time.sleep(30)"
        with mock.patch.object(engine, "_cli", return_value=[sys.executable, "-u", "-c", script]), \
             mock.patch.object(engine, "RECEIVE_TIMEOUT_S", 0.3), \
             mock.patch.object(engine, "RECEIVE_PROGRESS_S", 0.05):
            logs = []
            report = engine.fetch_notes("+19999999999", logs.append)
        self.assertFalse(report["complete"])
        self.assertEqual(engine.read_notes()[0]["text"], "already received")
        self.assertTrue(any(line.startswith("Receiving:") for line in logs))

    def test_missing_download_is_reported_instead_of_success(self):
        stream = envelope(note("caption", ts=1, attachments=[{"id": "absent.jpg", "contentType": "image/jpeg"}]))
        report, _ = self.receive(Process(stream))
        self.assertEqual(report["missing_attachments"], 1)
        self.assertFalse(report["complete"])
        self.assertIn("Forward the original", report["warning"])
