"""Receive reliability with disposable files and a simulated Signal process."""
import io
import json
import sys
import threading
import time
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
        self.assertTrue(3599 < process.wait_limits[0] <= 3600)

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
        self.assertTrue(3599 < processes[0].wait_limits[0] <= 3600)

    def test_thirty_mixed_media_files_and_large_unicode_body_are_saved_intact(self):
        directory = engine.DATA_DIR / "attachments"
        directory.mkdir(parents=True)
        body = "Long caption with café, 中文 and 🦷.\nNext paragraph.\n" * 12000
        (directory / "body.txt").write_text(body, encoding="utf-8")
        attachments = [{"id": "body.txt", "contentType": "text/x-signal-plain"}]
        payload = bytes(range(256)) * 256
        for i in range(30):
            mime = "video/mp4" if i % 5 == 0 else "image/jpeg"
            filename = f"media-{i}"
            (directory / filename).write_bytes(payload)
            attachments.append({"id": filename, "contentType": mime})
        stream = envelope(note("short caption", ts=42, attachments=attachments))
        report, _ = self.receive(Process(stream))
        saved = engine.read_notes()[0]
        self.assertTrue(report["complete"])
        self.assertEqual(saved["text"], body)
        self.assertEqual(len(saved["photos"]), 30)
        for media in saved["photos"]:
            self.assertEqual(Path(media["path"]).read_bytes(), payload)
        report, _ = self.receive(Process(stream))
        self.assertEqual(report["new"], 0)
        self.assertEqual(len(engine.read_notes()), 1)

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

    def test_structured_receive_errors_are_not_success_or_saved_as_notes(self):
        for with_envelope in (False, True):
            with self.subTest(with_envelope=with_envelope):
                value = {"exception": {"type": "InvalidMessageException", "message": "private detail"}}
                if with_envelope:
                    value.update(json.loads(envelope(note("untrusted content", ts=9))))
                report, _ = self.receive(Process(json.dumps(value)))
                self.assertFalse(report["complete"])
                self.assertEqual(report["receive_errors"], 1)
                self.assertEqual(engine.read_notes(), [])
                self.assertIn("decrypt", report["warning"])
                self.assertNotIn("private detail", engine.NOTES_DEBUG_FILE.read_text())

    def test_sixteen_images_and_full_text_behind_large_backlog(self):
        stream, body = self.large_note(16)
        unrelated = json.dumps({"envelope": {"dataMessage": {"message": "unrelated"}}}) + "\n"
        report, _ = self.receive(Process(unrelated * 100000 + stream))
        saved = engine.read_notes()[0]
        self.assertEqual(report["envelopes"], 100001)
        self.assertEqual(len(saved["photos"]), 16)
        self.assertEqual(saved["text"], body)
        self.assertTrue(report["complete"])

    def test_upstream_swallowed_processing_error_is_incomplete(self):
        report, _ = self.receive(Process(error="ERROR Unknown error when handling messages: private"))
        self.assertFalse(report["complete"])
        self.assertEqual(report["receive_errors"], 1)
        self.assertNotIn("private", report["warning"])

    def test_authorization_warning_is_incomplete_even_with_successful_exit(self):
        report, _ = self.receive(Process(error="WARN Authorization failed: private-token\n"))
        self.assertFalse(report["complete"])
        self.assertIn("authorization", report["warning"].lower())
        self.assertNotIn("private-token", engine.NOTES_DEBUG_FILE.read_text())

    def test_unclassified_stderr_error_is_not_success(self):
        report, _ = self.receive(Process(error="ERROR Unexpected media failure private-token\n"))
        self.assertFalse(report["complete"])
        self.assertNotIn("private-token", report["warning"])

    def test_parallel_media_timings_are_logged_without_message_data(self):
        diagnostics = ("SB_NOTE_MEDIA files=30 workers=4\n"
                       "SB_NOTE_MEDIA file=2 total=30 elapsed_ms=82\n"
                       "SB_NOTE_MEDIA file=1 total=30 elapsed_ms=150\n"
                       "SB_NOTE_MEDIA files=30 workers=4 elapsed_ms=650\n"
                       "SB_NOTE_MEDIA private-caption\n"
                       "SB_NOTE_MEDIA file=1 total=30 elapsed_ms=1 private-filename\n")
        report, _ = self.receive(Process(envelope(note("private-caption", ts=1)), error=diagnostics))
        debug = engine.NOTES_DEBUG_FILE.read_text()
        self.assertTrue(report["complete"])
        self.assertIn("receive media: files=30 workers=4", debug)
        self.assertIn("receive media: file=2 total=30 elapsed_ms=82", debug)
        self.assertIn("receive media: files=30 workers=4 elapsed_ms=650", debug)
        self.assertNotIn("private-", debug)

    def test_parallel_media_timings_do_not_hide_download_failures(self):
        stream, body = self.large_note(16)
        report, _ = self.receive(Process(stream, error=(
            "SB_NOTE_MEDIA files=17 workers=4\n"
            "WARN Failed to download attachment (private-id), ignoring: unavailable\n"
            "SB_NOTE_MEDIA files=17 workers=4 elapsed_ms=900\n")))
        self.assertFalse(report["complete"])
        self.assertIn("attachment download failed", report["warning"])
        self.assertEqual(engine.read_notes()[0]["text"], body)
        self.assertEqual(len(engine.read_notes()[0]["photos"]), 16)
        self.assertNotIn("private-id", engine.NOTES_DEBUG_FILE.read_text())

    def test_informational_warning_does_not_fail_a_successful_note(self):
        report, _ = self.receive(Process(envelope(note("kept", ts=1)),
                                         error="WARN Optional setting unavailable\n"))
        self.assertTrue(report["complete"])
        self.assertEqual(report["new"], 1)

    def test_attachment_warning_is_not_erased_by_a_successful_reconnect(self):
        marker = engine.RUNTIME_DIR / "attempt"
        script = ("from pathlib import Path; import time,sys\n"
                  f"marker=Path({str(marker)!r})\n"
                  "if not marker.exists():\n"
                  " marker.touch()\n"
                  " print('WARN Attachment download failed private-token', file=sys.stderr, flush=True)\n"
                  " time.sleep(30)\n")
        report = self.run_child(script, [])
        self.assertFalse(report["complete"])
        self.assertIn("attachment download failed", report["warning"])
        self.assertEqual(report["attempts"], 1)

    def test_progress_explains_filter_and_errors_before_receive_finishes(self):
        dm = note("private-message", ts=1,
                  attachments=[{"id": "private-file", "contentType": "image/jpeg"}])
        dm["destinationUuid"] = "someone-else"
        dm["destinationNumber"] = None
        error = json.dumps({"exception": {"type": "NoSessionException", "message": "private-token"}})
        stream = "\n".join([envelope(dm), error, envelope(note("private-note", ts=2))])
        script = ("import time\n"
                  f"print({stream!r}, flush=True)\n"
                  "time.sleep(0.2)\n")
        logs = []
        report = self.run_child(script, logs)
        self.assertFalse(report["complete"])
        self.assertEqual(report["new"], 1)
        debug = engine.NOTES_DEBUG_FILE.read_text()
        progress = next(line for line in debug.splitlines() if "receive progress:" in line
                        and "messages=2" in line)
        for field in ("transcripts=2", "notes=1", "other_attachments=1", "receive_errors=1",
                      "last_warning_age_seconds=", "warning_events="):
            self.assertIn(field, progress)
        self.assertIn("encrypted backlog session unavailable", debug)
        self.assertIn("receive note:", debug)
        for secret in ("private-message", "private-file", "private-token", "private-note"):
            self.assertNotIn(secret, debug)

    def test_repeated_errors_do_not_keep_a_stalled_receive_alive(self):
        script = ("import time,json\n"
                  "for i in range(300):\n"
                  " print(json.dumps({'exception': {'type': 'NoSessionException'}}), flush=True)\n"
                  " time.sleep(0.025)\n")
        logs = []
        started = time.monotonic()
        report = self.run_child(script, logs)
        self.assertLess(time.monotonic() - started, 4)
        self.assertFalse(report["complete"])
        self.assertEqual(report["envelopes"], 0)
        self.assertGreater(report["receive_errors"], 0)
        self.assertIn("no progress", report["warning"].lower())
        self.assertNotIn("one-hour", report["warning"])

    def test_receive_temporary_media_stays_in_store_and_is_cleaned(self):
        for jvm in (True, False):
            with self.subTest(jvm=jvm), mock.patch.object(engine, "_is_jvm_build", return_value=jvm):
                _, spawn = self.receive(Process())
                env = spawn.call_args.kwargs["env"]
                temporary = Path(env["TMPDIR"])
                self.assertTrue(temporary.is_relative_to(engine.RUNTIME_DIR))
                self.assertIn(str(temporary), env["JAVA_TOOL_OPTIONS"])
                self.assertFalse(temporary.exists())
                if not jvm:
                    self.assertIn(f"-Djava.io.tmpdir={temporary}", spawn.call_args.args[0])

    def run_child(self, script, logs):
        with mock.patch.object(engine, "_cli", return_value=[sys.executable, "-u", "-c", script]), \
             mock.patch.object(engine, "RECEIVE_PROGRESS_S", 0.02), \
             mock.patch.object(engine, "RECEIVE_STALL_S", 1), \
             mock.patch.object(engine, "RECEIVE_TIMEOUT_S", 8):
            return engine.fetch_notes("+19999999999", logs.append)

    def test_stall_reconnects_once_then_saves_complete_album(self):
        stream, body = self.large_note(16)
        marker = engine.RUNTIME_DIR / "attempt"
        first_note = envelope(note("saved before stall", ts=1))
        script = ("from pathlib import Path; import time,sys\n"
                  f"marker=Path({str(marker)!r})\n"
                  "if not marker.exists():\n"
                  " marker.touch()\n"
                  f" print({first_note!r}, flush=True)\n"
                  " print('Connection closed unexpectedly private-token', file=sys.stderr, flush=True)\n"
                  " time.sleep(30)\n"
                  f"else: print({stream!r}, flush=True)\n")
        logs = []
        report = self.run_child(script, logs)
        self.assertTrue(report["complete"], report)
        self.assertEqual(report["attempts"], 2)
        self.assertEqual(report["new"], 2)
        self.assertEqual(len(engine.read_notes()[0]["photos"]), 16)
        self.assertEqual(engine.read_notes()[0]["text"], body)
        debug = engine.NOTES_DEBUG_FILE.read_text()
        self.assertIn("receive progress:", debug)
        self.assertIn("reconnecting", " ".join(logs).lower())
        self.assertNotIn("private-token", debug)

    def test_download_activity_keeps_slow_album_alive(self):
        stream, _ = self.large_note(16)
        script = ("import os,time; from pathlib import Path\n"
                  "file=Path(os.environ['TMPDIR']) / 'signal-cli_tmp_test.tmp'\n"
                  "for i in range(30):\n"
                  " file.write_bytes(b'x' * (i+1)); time.sleep(0.05)\n"
                  f"print({stream!r}, flush=True)\n")
        logs = []
        report = self.run_child(script, logs)
        self.assertTrue(report["complete"], report)
        self.assertEqual(report["attempts"], 1)
        self.assertEqual(len(engine.read_notes()[0]["photos"]), 16)
        self.assertTrue(any("download" in line.lower() for line in logs))

    def test_permanent_stall_is_bounded_and_explained(self):
        logs = []
        started = time.monotonic()
        report = self.run_child("import time; time.sleep(30)", logs)
        self.assertLess(time.monotonic() - started, 5)
        self.assertFalse(report["complete"])
        self.assertEqual(report["attempts"], 2)
        self.assertIn("no progress", report["warning"].lower())
        self.assertIn("connection", report["warning"].lower())
