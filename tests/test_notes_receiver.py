"""Installation and launch boundaries for selective media downloads."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import engine
import notes_receiver
from runtime import isolated_engine
from test_receive import Process
from test_notes import envelope, note


class ReceiverInstallation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="notes-install-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "bin").mkdir()
        (self.root / "lib").mkdir()
        (self.root / "notes-receiver").mkdir()
        self.original = self.root / "bin/signal-cli"
        self.original.write_text("#!/bin/sh\nCLASSPATH=stock\n")
        self.library = self.root / f"lib/libsignal-cli-{notes_receiver.VERSION}.jar"
        self.library.write_bytes(b"stock library")
        self.jar = self.root / "notes-receiver/notes-receiver.jar"
        self.jar.write_bytes(b"compiled overlay")
        self.launcher = self.root / "bin/signal-cli-notes"
        self.launcher.write_text("#!/bin/sh\nCLASSPATH=overlay:stock\n")
        self.launcher.chmod(0o755)
        self.manifest = self.root / "notes-receiver/manifest.json"
        self.manifest.write_text(json.dumps({
            "version": notes_receiver.VERSION, "source": notes_receiver.SOURCE_SHA256,
            "patch": notes_receiver.digest(notes_receiver.PATCH),
            "original_launcher": notes_receiver.digest(self.original),
            "library": notes_receiver.digest(self.library),
            "launcher": notes_receiver.digest(self.launcher), "jar": notes_receiver.digest(self.jar),
        }))

    def test_valid_receiver_is_selected_and_reused_without_a_rebuild(self):
        self.assertEqual(notes_receiver.launcher_for(str(self.original)), str(self.launcher))
        with mock.patch.object(notes_receiver.subprocess, "run") as run:
            self.assertEqual(notes_receiver.install(self.root, Path("unused-jdk")), self.launcher)
        run.assert_not_called()

    def test_changed_or_missing_build_components_fall_back_to_stock(self):
        for path in (self.jar, self.launcher, self.original, self.library, self.manifest):
            with self.subTest(path=path.name):
                saved = path.read_bytes()
                path.write_bytes(b"changed")
                self.assertIsNone(notes_receiver.launcher_for(str(self.original)))
                path.unlink()
                self.assertIsNone(notes_receiver.launcher_for(str(self.original)))
                path.write_bytes(saved)
                self.launcher.chmod(0o755)
        self.assertEqual(notes_receiver.launcher_for(str(self.original)), str(self.launcher))

    def test_source_mismatch_cannot_modify_stock_or_publish_a_new_receiver(self):
        self.manifest.unlink()
        source = self.root / "bad.java"
        source.write_text("unverified source")
        before = self.original.read_bytes(), self.library.read_bytes(), self.jar.read_bytes()
        with mock.patch.object(notes_receiver.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                notes_receiver.install(self.root, Path("unused-jdk"), source)
        run.assert_not_called()
        self.assertEqual(before, (self.original.read_bytes(), self.library.read_bytes(), self.jar.read_bytes()))
        self.assertIsNone(notes_receiver.launcher_for(str(self.original)))

    def test_receive_uses_overlay_with_jvm_flags_and_records_mode(self):
        with isolated_engine(), \
             mock.patch.object(engine, "signal_cli_bin", return_value=str(self.original)), \
             mock.patch.object(engine, "_jvm_signal_cli", return_value=self.original), \
             mock.patch.object(engine.subprocess, "Popen", return_value=Process(envelope(note("kept", ts=1)))) as spawn:
            report = engine.fetch_notes("+19999999999")
            command = spawn.call_args.args[0]
            self.assertEqual(command[0], str(self.launcher))
            self.assertNotIn("-XX:StackSize=16m", command)
            self.assertNotIn("--ignore-attachments", command)
            self.assertIn("-Xss32m", spawn.call_args.kwargs["env"]["JAVA_OPTS"])
            self.assertTrue(report["complete"])
            self.assertEqual(report["new"], 1)
            self.assertIn("media=note-to-self", engine.NOTES_DEBUG_FILE.read_text())
            self.assertEqual(engine.signal_cli_command("--version")[0][0], str(self.original))

    def test_missing_overlay_uses_full_downloads_and_explains_fallback(self):
        self.manifest.unlink()
        with isolated_engine(), \
             mock.patch.object(engine, "signal_cli_bin", return_value=str(self.original)), \
             mock.patch.object(engine, "_jvm_signal_cli", return_value=self.original), \
             mock.patch.object(engine.subprocess, "Popen", return_value=Process()) as spawn:
            logs = []
            engine.fetch_notes("+19999999999", logs.append)
            self.assertEqual(spawn.call_args.args[0][0], str(self.original))
            self.assertNotIn("--ignore-attachments", spawn.call_args.args[0])
            self.assertIn("compatibility", " ".join(logs))
            self.assertIn("media=all", engine.NOTES_DEBUG_FILE.read_text())


@unittest.skipUnless(os.environ.get("SB_SIGNAL_CLI_TEST_DIST"), "Requires disposable pinned Signal JVM distribution")
class RealReceiverTests(unittest.TestCase):
    def test_actual_handler_skips_unrelated_media_and_preserves_large_notes(self):
        distribution = Path(os.environ["SB_SIGNAL_CLI_TEST_DIST"]).resolve()
        java_home = Path(engine._java_home())
        launcher = notes_receiver.install(distribution, java_home,
                                          Path(os.environ["SB_SIGNAL_CLI_TEST_SOURCE"]))
        self.assertEqual(notes_receiver.launcher_for(str(distribution / "bin/signal-cli")), str(launcher))
        subprocess.run([str(launcher), "--version"], check=True, capture_output=True,
                       env={**os.environ, "JAVA_HOME": str(java_home)}, timeout=30)
        probe = engine.PROJECT_DIR / "scripts/signal-cli-notes/SelectiveProbe.java"
        with tempfile.TemporaryDirectory(prefix="notes-java-test-") as directory:
            subprocess.run([str(java_home / "bin/javac"), "-cp", str(distribution / "lib/*"),
                            "-d", directory, str(probe)], check=True, timeout=30)
            for mode, prefix in (("baseline", ""), ("selective", str(distribution / "notes-receiver/notes-receiver.jar") + os.pathsep)):
                classpath = prefix + directory + os.pathsep + str(distribution / "lib/*")
                result = subprocess.run([str(java_home / "bin/java"), "--enable-native-access=ALL-UNNAMED",
                                         "-cp", classpath, "SelectiveProbe", mode],
                                        check=True, capture_output=True, text=True, timeout=60)
                print(result.stdout, end="")
                self.assertIn(f"unrelated_download_calls={202 if mode == 'baseline' else 0} PASS", result.stdout)
