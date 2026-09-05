"""Memory-only thumbnail conversion and cancellation, using disposable files."""
import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import zlib
from pathlib import Path
from unittest import mock

from mac_photos import HELPER, Thumbnails


def png_bytes(width=400, height=200):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    rows = (b"\0" + bytes((30, 160, 210)) * width) * height
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


class ThumbnailLifecycleTests(unittest.TestCase):
    def test_close_kills_pending_decoder_and_discards_late_image(self):
        started, finished = threading.Event(), threading.Event()
        proc = mock.Mock(returncode=0)
        def communicate(data, timeout):
            self.assertEqual(json.loads(data), {"path": "/private/fixture/photo.heic", "size": 92})
            started.set()
            self.assertTrue(finished.wait(3))
            return png_bytes(), None
        proc.communicate.side_effect = communicate
        proc.kill.side_effect = finished.set
        renderer = Thumbnails()
        results = []
        with mock.patch("mac_photos.subprocess.Popen", return_value=proc) as launch:
            thread = threading.Thread(target=lambda: results.append(renderer.make("/private/fixture/photo.heic", 92)))
            thread.start()
            try:
                self.assertTrue(started.wait(3))
                renderer.close()
            finally:
                finished.set()
                thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(results, [None])
            proc.kill.assert_called_once()
            self.assertEqual(launch.call_args.args, ([str(HELPER)],))
            self.assertIsNone(renderer.make("/private/fixture/photo.heic", 92))
            self.assertEqual(launch.call_count, 1)
            self.assertFalse(renderer.processes)

    def test_decoder_timeout_is_killed_and_reaped(self):
        proc = mock.Mock()
        proc.communicate.side_effect = [subprocess.TimeoutExpired("fixture", 20), (b"", None)]
        renderer = Thumbnails()
        with mock.patch("mac_photos.subprocess.Popen", return_value=proc):
            self.assertIsNone(renderer.make("fixture.png", 92))
        proc.kill.assert_called_once()
        self.assertEqual(proc.communicate.call_count, 2)
        self.assertFalse(renderer.processes)


@unittest.skipUnless(sys.platform == "darwin" and HELPER.exists() and os.environ.get("SB_RUN_MAC_PHOTOS") == "1",
                     "Opt-in native image decoder test; build helper with Setup.command")
class NativeThumbnailTests(unittest.TestCase):
    def test_png_jpeg_and_heic_decode_without_thumbnail_files(self):
        with tempfile.TemporaryDirectory(prefix="sb-photo-native-") as directory:
            root = Path(directory)
            source = root / "fixture.png"
            source.write_bytes(png_bytes())
            renderer = Thumbnails()
            self.addCleanup(renderer.close)
            for format in ("png", "jpeg", "heic"):
                with self.subTest(format=format):
                    path = root / ("fixture." + format)
                    if format != "png":
                        subprocess.run(["/usr/bin/sips", "-s", "format", format, str(source), "--out", str(path)],
                                       capture_output=True, check=True)
                    before = set(root.iterdir())
                    for size in (92, 720):
                        result = renderer.make(str(path), size)
                        self.assertIsNotNone(result)
                        self.assertEqual(result[:8], b"\x89PNG\r\n\x1a\n")
                        width, height = struct.unpack(">II", result[16:24])
                        self.assertLessEqual(max(width, height), size)
                        self.assertAlmostEqual(width / height, 2, places=1)
                    self.assertEqual(set(root.iterdir()), before)

    def test_invalid_or_missing_image_is_not_displayed(self):
        with tempfile.TemporaryDirectory(prefix="sb-photo-invalid-") as directory:
            path = Path(directory) / "invalid.png"
            path.write_text("not an image")
            renderer = Thumbnails()
            self.addCleanup(renderer.close)
            self.assertIsNone(renderer.make(str(path), 92))
            self.assertIsNone(renderer.make(str(path.with_name("missing.png")), 92))
