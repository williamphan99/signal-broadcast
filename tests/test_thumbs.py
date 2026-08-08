#!/usr/bin/env python3
"""Thumbnail cache tests (the Send tab's photo strip).

macOS-only: thumbs.py shells out to /usr/bin/sips, which doesn't exist in the Android
(proot-distro Debian) guest — the Pixel has its own web UI and never builds thumbnails.
Skipped rather than failed there, so `python3 -m unittest discover -s tests` stays green
on both.

Run with:  python3 -m unittest discover -s tests
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import thumbs  # noqa: E402

darwin_only = unittest.skipUnless(sys.platform == "darwin", "sips is macOS-only")


@darwin_only
class ThumbnailCache(unittest.TestCase):
    def setUp(self):
        thumbs.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(thumbs.clear)
        # A real image to convert, made with the same tool the app relies on.
        # sips takes -z as HEIGHT then WIDTH, so this fixture is 200 wide × 300 tall.
        self.png = Path(self.tmp.name) / "source.png"
        subprocess.run([thumbs.SIPS, "-s", "format", "png", "-z", "300", "200",
                        "/System/Library/CoreServices/DefaultDesktop.heic",
                        "--out", str(self.png)], capture_output=True, check=False)
        if not self.png.exists():
            self.skipTest("no system image available to build a fixture from")

    def test_makes_a_png_tk_can_read_fitted_to_the_box(self):
        out = thumbs.make(str(self.png), 92)
        self.assertIsNotNone(out)
        self.assertEqual(out.suffix, ".png")
        dims = subprocess.run([thumbs.SIPS, "-g", "pixelWidth", "-g", "pixelHeight", str(out)],
                              capture_output=True, text=True).stdout
        w = int([l for l in dims.splitlines() if "pixelWidth" in l][0].split(":")[1])
        h = int([l for l in dims.splitlines() if "pixelHeight" in l][0].split(":")[1])
        self.assertLessEqual(max(w, h), 92)              # fitted inside the tile
        self.assertAlmostEqual(w / h, 200 / 300, places=1)  # aspect kept, not squashed

    def test_second_call_is_served_from_cache(self):
        first = thumbs.make(str(self.png), 92)
        stamp = first.stat().st_mtime_ns
        self.assertEqual(thumbs.make(str(self.png), 92), first)
        self.assertEqual(first.stat().st_mtime_ns, stamp)  # not re-converted

    def test_an_edited_photo_is_reconverted(self):
        """The cache key carries the source mtime, so replacing a file at the same path
        doesn't leave the old picture on screen."""
        first = thumbs.make(str(self.png), 92)
        subprocess.run([thumbs.SIPS, "-s", "format", "png", "-z", "120", "400",
                        str(self.png), "--out", str(self.png)], capture_output=True, check=False)
        self.assertNotEqual(thumbs.make(str(self.png), 92), first)

    def test_sizes_are_cached_separately(self):
        self.assertNotEqual(thumbs.make(str(self.png), 92), thumbs.make(str(self.png), 720))

    def test_a_file_that_is_not_an_image_returns_none(self):
        junk = Path(self.tmp.name) / "notes.txt"
        junk.write_text("this is not a picture")
        self.assertIsNone(thumbs.make(str(junk), 92))

    def test_a_missing_file_returns_none(self):
        self.assertIsNone(thumbs.make(str(Path(self.tmp.name) / "gone.jpg"), 92))

    def test_clear_removes_the_copies_of_the_users_photos(self):
        out = thumbs.make(str(self.png), 92)
        cache = out.parent
        thumbs.clear()
        self.assertFalse(cache.exists())
        # and the next thumbnail still works, in a fresh directory
        again = thumbs.make(str(self.png), 92)
        self.assertTrue(again.exists())
        self.assertNotEqual(again.parent, cache)

    def test_thumbnails_never_land_in_the_project_folder(self):
        out = thumbs.make(str(self.png), 92)
        project = Path(__file__).resolve().parent.parent
        self.assertNotIn(project, out.parents)


if __name__ == "__main__":
    unittest.main()
