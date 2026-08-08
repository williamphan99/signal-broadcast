#!/usr/bin/env python3
"""Notes-to-self parsing: what counts as a note, and — more importantly — what doesn't.

The envelopes here are the real shapes signal-cli 0.14.5 emits, captured from a live
account (identifiers replaced). A phone mirrors EVERY message you send as the same kind
of sync transcript, so the boundary between "my note" and "my private message to
someone else" is the only thing keeping private conversations out of this app.

Pure parsing — no signal-cli, no network — so these run on macOS and in the Android
guest alike.

Run with:  python3 -m unittest discover -s tests
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import engine  # noqa: E402

ME_UUID = "ac8ef43b-0000-0000-0000-000000000001"
ME_NUMBER = "+61400000000"
FRIEND_UUID = "eb0e2686-0000-0000-0000-000000000002"


def envelope(sent: dict, source_uuid: str | None = ME_UUID,
             source_number: str | None = ME_NUMBER) -> str:
    return json.dumps({"envelope": {
        "source": source_number, "sourceNumber": source_number, "sourceUuid": source_uuid,
        "sourceDevice": 1, "timestamp": sent.get("timestamp", 1),
        "syncMessage": {"sentMessage": sent}}})


def note(message: str | None = "a note", ts=1786159240837,
         attachments: list | None = None, expires=0) -> dict:
    return {"destination": ME_NUMBER, "destinationNumber": ME_NUMBER,
            "destinationUuid": ME_UUID, "timestamp": ts, "message": message,
            "expiresInSeconds": expires, "viewOnce": False,
            "attachments": attachments or []}


class WhatCountsAsANote(unittest.TestCase):
    def test_a_note_to_self_is_kept(self):
        found = engine.harvest_notes(envelope(note("buy milk")))
        self.assertEqual([n["text"] for n in found], ["buy milk"])

    def test_a_private_message_to_someone_else_is_never_a_note(self):
        """The one that matters. Same envelope, different destination — this is a DM
        the user sent from their phone, and it must not surface anywhere in the app."""
        dm = {"destination": FRIEND_UUID, "destinationNumber": None,
              "destinationUuid": FRIEND_UUID, "timestamp": 2,
              "message": "private", "expiresInSeconds": 28800}
        self.assertEqual(engine.harvest_notes(envelope(dm)), [])

    def test_null_identifiers_on_both_sides_do_not_match(self):
        """destinationNumber is null on UUID-addressed messages. Comparing two nulls as
        equal would treat every such message as a note."""
        dm = {"destination": FRIEND_UUID, "destinationNumber": None,
              "destinationUuid": FRIEND_UUID, "timestamp": 3, "message": "private"}
        self.assertEqual(engine.harvest_notes(envelope(dm, source_number=None)), [])

    def test_a_group_message_i_sent_is_not_a_note(self):
        sent = {"destination": None, "destinationNumber": None, "destinationUuid": None,
                "timestamp": 4, "message": "hi team",
                "groupInfo": {"groupId": "abc=", "type": "DELIVER"}}
        self.assertEqual(engine.harvest_notes(envelope(sent)), [])

    def test_an_incoming_message_from_someone_else_is_not_a_note(self):
        incoming = json.dumps({"envelope": {
            "source": "+61411111111", "sourceUuid": FRIEND_UUID, "timestamp": 5,
            "dataMessage": {"message": "hey", "timestamp": 5}}})
        self.assertEqual(engine.harvest_notes(incoming), [])

    def test_matched_on_number_when_uuids_are_absent(self):
        sent = {"destinationNumber": ME_NUMBER, "timestamp": 6, "message": "by number"}
        found = engine.harvest_notes(envelope(sent, source_uuid=None))
        self.assertEqual([n["text"] for n in found], ["by number"])

    def test_an_empty_transcript_is_ignored(self):
        self.assertEqual(engine.harvest_notes(envelope(note(message=None))), [])

    def test_junk_lines_are_skipped_not_fatal(self):
        stream = "\n".join(["not json", "{broken", "", envelope(note("survivor")), "  "])
        self.assertEqual([n["text"] for n in engine.harvest_notes(stream)], ["survivor"])

    def test_notes_come_back_oldest_first(self):
        stream = "\n".join([envelope(note("second", ts=200)), envelope(note("first", ts=100))])
        self.assertEqual([n["text"] for n in engine.harvest_notes(stream)], ["first", "second"])


class ExplainingAnEmptyCheck(unittest.TestCase):
    """A check that finds nothing has to say which of the three things went wrong,
    because the drain consumes what it reads — you don't get a second look."""

    def setUp(self):
        patch = mock.patch.object(engine, "_notes_log", lambda *_: None)
        patch.start()
        self.addCleanup(patch.stop)

    def test_nothing_waiting_at_all(self):
        self.assertEqual(engine.describe_receive(""),
                         {"envelopes": 0, "transcripts": 0, "notes": 0})

    def test_traffic_arrived_but_none_of_it_was_mine(self):
        incoming = json.dumps({"envelope": {"source": "+61411111111", "timestamp": 1,
                                            "dataMessage": {"message": "hey"}}})
        self.assertEqual(engine.describe_receive(incoming),
                         {"envelopes": 1, "transcripts": 0, "notes": 0})

    def test_i_sent_messages_but_none_to_myself(self):
        dm = {"destination": FRIEND_UUID, "destinationUuid": FRIEND_UUID,
              "destinationNumber": None, "timestamp": 2, "message": "hi"}
        self.assertEqual(engine.describe_receive(envelope(dm)),
                         {"envelopes": 1, "transcripts": 1, "notes": 0})

    def test_a_note_is_counted_as_all_three(self):
        self.assertEqual(engine.describe_receive(envelope(note("x"))),
                         {"envelopes": 1, "transcripts": 1, "notes": 1})

    def test_counts_match_what_was_harvested(self):
        stream = "\n".join([envelope(note("one", ts=1)), envelope(note("two", ts=2)),
                            envelope({"destination": FRIEND_UUID,
                                      "destinationUuid": FRIEND_UUID,
                                      "destinationNumber": None, "timestamp": 3,
                                      "message": "dm"})])
        self.assertEqual(engine.describe_receive(stream)["notes"],
                         len(engine.harvest_notes(stream)))


class NothingToSend(unittest.TestCase):
    """Photos with no caption are a real broadcast — a note written on the phone is
    often just pictures, and refusing to send it would make those notes useless."""

    def test_no_text_and_no_photos_is_empty(self):
        self.assertTrue(engine.nothing_to_send("   \n ", []))

    def test_photos_alone_are_sendable(self):
        self.assertFalse(engine.nothing_to_send("", ["/tmp/a.jpg"]))

    def test_text_alone_is_sendable(self):
        self.assertFalse(engine.nothing_to_send("hello", []))

    def test_both_are_sendable(self):
        self.assertFalse(engine.nothing_to_send("hello", ["/tmp/a.jpg"]))


class NotesWithPhotos(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        (self.data / "attachments").mkdir()
        self._real_data_dir = engine.DATA_DIR
        engine.DATA_DIR = self.data
        self.addCleanup(lambda: setattr(engine, "DATA_DIR", self._real_data_dir))
        self.att = {"contentType": "image/jpeg", "filename": "IMG_7461.jpg",
                    "id": "Q6N9lGRtRDNZDM6BffzA.jpg", "size": 363422}

    def test_a_downloaded_photo_is_pointed_at_by_path(self):
        (self.data / "attachments" / self.att["id"]).write_bytes(b"jpegbytes")
        found = engine.harvest_notes(envelope(note(None, attachments=[self.att])))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["photos"][0]["name"], "IMG_7461.jpg")
        self.assertTrue(Path(found[0]["photos"][0]["path"]).is_file())
        self.assertEqual(found[0]["missing_photos"], 0)

    def test_a_photo_only_note_is_still_a_note(self):
        (self.data / "attachments" / self.att["id"]).write_bytes(b"jpegbytes")
        found = engine.harvest_notes(envelope(note(None, attachments=[self.att])))
        self.assertEqual(found[0]["text"], "")

    def test_the_group_sync_keeps_the_text_and_flags_the_missing_photos(self):
        """The sync can't download media, so a note arriving during one keeps its words
        and admits the photos never landed, rather than pretending it had none."""
        found = engine.harvest_notes(envelope(note("see this", attachments=[self.att])),
                                     media_downloaded=False)
        self.assertEqual(found[0]["text"], "see this")
        self.assertEqual(found[0]["photos"], [])
        self.assertEqual(found[0]["missing_photos"], 1)

    def test_a_view_once_photo_is_deleted_not_kept(self):
        """View-once means one look. signal-cli writes the file before we ever see the
        envelope, so keeping it would leave a permanent copy of a photo that was
        promised to vanish — the same promise as a disappearing-message timer."""
        on_disk = self.data / "attachments" / self.att["id"]
        on_disk.write_bytes(b"jpegbytes")
        sent = note("look once", attachments=[self.att])
        sent["viewOnce"] = True
        found = engine.harvest_notes(envelope(sent))
        self.assertEqual(found[0]["photos"], [])
        self.assertEqual(found[0]["view_once_photos"], 1)
        self.assertFalse(on_disk.exists(), "the view-once file was left on disk")

    def test_a_view_once_note_still_appears_so_it_isnt_a_silent_disappearance(self):
        sent = note(None, attachments=[self.att])
        sent["viewOnce"] = True
        self.assertEqual(len(engine.harvest_notes(envelope(sent))), 1)

    def test_an_ordinary_photo_is_untouched_on_disk(self):
        on_disk = self.data / "attachments" / self.att["id"]
        on_disk.write_bytes(b"jpegbytes")
        engine.harvest_notes(envelope(note("keep", attachments=[self.att])))
        self.assertTrue(on_disk.exists())

    def test_an_attachment_whose_file_never_arrived_is_not_pointed_at(self):
        found = engine.harvest_notes(envelope(note("x", attachments=[self.att])))
        self.assertEqual(found[0]["photos"], [])
        self.assertEqual(found[0]["missing_photos"], 1)


class StoringNotes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._real = engine.NOTES_FILE
        self._real_corrupt = engine.NOTES_CORRUPT_FILE
        engine.NOTES_FILE = Path(self.tmp.name) / "notes.json"
        engine.NOTES_CORRUPT_FILE = Path(self.tmp.name) / "notes.corrupt.json"
        self.addCleanup(lambda: setattr(engine, "NOTES_FILE", self._real))
        self.addCleanup(lambda: setattr(engine, "NOTES_CORRUPT_FILE", self._real_corrupt))

    def test_missing_file_reads_as_no_notes(self):
        self.assertEqual(engine.read_notes(), [])

    def test_round_trips_newest_first(self):
        engine.write_notes([{"ts": 100, "text": "old"}, {"ts": 300, "text": "new"}])
        self.assertEqual([n["text"] for n in engine.read_notes()], ["new", "old"])

    def test_merge_ignores_a_note_already_stored(self):
        stored = [{"ts": 100, "text": "old"}]
        merged, new = engine.merge_notes(stored, [{"ts": 100, "text": "old"},
                                                  {"ts": 200, "text": "fresh"}])
        self.assertEqual(new, 1)
        self.assertEqual([n["text"] for n in merged], ["fresh", "old"])

    def test_a_second_drain_of_the_same_note_adds_nothing(self):
        line = envelope(note("once", ts=555))
        first, n1 = engine.merge_notes([], engine.harvest_notes(line))
        _, n2 = engine.merge_notes(first, engine.harvest_notes(line))
        self.assertEqual((n1, n2), (1, 0))

    def test_a_disappearing_note_is_dropped_once_its_timer_runs_out(self):
        """Notes inherit the chat's disappearing-message timer. Signal promises they
        vanish; keeping a copy on the Mac forever would break that quietly."""
        sent_at = 1_700_000_000
        vanishing = {"ts": sent_at * 1000, "text": "secret", "expires": 3600}
        forever = {"ts": sent_at * 1000, "text": "kept", "expires": 0}
        alive = engine.prune_notes([vanishing, forever], now=sent_at + 60)
        self.assertEqual([n["text"] for n in alive], ["secret", "kept"])
        later = engine.prune_notes([vanishing, forever], now=sent_at + 7200)
        self.assertEqual([n["text"] for n in later], ["kept"])

    def test_an_expired_note_is_not_returned_even_if_it_is_still_on_disk(self):
        engine.NOTES_FILE.write_text(json.dumps(
            [{"ts": 1_000_000_000_000, "text": "long gone", "expires": 60}]), encoding="utf-8")
        self.assertEqual(engine.read_notes(), [])

    def test_only_the_most_recent_notes_are_kept(self):
        many = [{"ts": i, "text": str(i)} for i in range(engine.NOTES_KEEP + 25)]
        self.assertEqual(len(engine.prune_notes(many)), engine.NOTES_KEEP)

    def test_a_corrupt_notes_file_reads_as_empty_rather_than_crashing(self):
        engine.NOTES_FILE.write_text("{not json", encoding="utf-8")
        self.assertEqual(engine.read_notes(), [])

    def test_a_corrupt_file_is_set_aside_instead_of_overwritten(self):
        """Notes can't be re-fetched — each message reaches this device once — so a
        damaged file must survive long enough to be salvaged by hand."""
        engine.NOTES_FILE.write_text('[{"ts": 1, "text": "truncat', encoding="utf-8")
        engine.read_notes()
        self.assertFalse(engine.NOTES_FILE.exists())
        self.assertIn("truncat", engine.NOTES_CORRUPT_FILE.read_text(encoding="utf-8"))
        engine.write_notes([{"ts": 2, "text": "new"}])          # next write is unimpeded
        self.assertEqual([n["text"] for n in engine.read_notes()], ["new"])

    def test_a_write_leaves_no_temp_file_behind(self):
        engine.write_notes([{"ts": 1, "text": "a"}])
        self.assertEqual(sorted(p.name for p in engine.NOTES_FILE.parent.iterdir()),
                         ["notes.json"])

    def test_an_interrupted_write_cannot_destroy_the_previous_notes(self):
        """The rename is the commit point: readers see the old file or the new one,
        never a half-written one."""
        engine.write_notes([{"ts": 1, "text": "safe"}])
        original = engine.NOTES_FILE.read_text(encoding="utf-8")
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                engine.write_notes([{"ts": 2, "text": "doomed"}])
        self.assertEqual(engine.NOTES_FILE.read_text(encoding="utf-8"), original)
        self.assertEqual([n["text"] for n in engine.read_notes()], ["safe"])

    def test_a_file_that_is_json_but_not_a_list_is_not_trusted(self):
        engine.NOTES_FILE.write_text('{"ts": 1}', encoding="utf-8")
        self.assertEqual(engine.read_notes(), [])


class NotesSurviveTheGroupSync(unittest.TestCase):
    """A message is delivered to this device once. The group sync drains the same queue,
    so anything it doesn't keep is gone for good — that's how notes used to be lost."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._real = engine.NOTES_FILE
        engine.NOTES_FILE = Path(self.tmp.name) / "notes.json"
        self.addCleanup(lambda: setattr(engine, "NOTES_FILE", self._real))

    def test_a_note_seen_during_a_sync_is_saved(self):
        engine._save_notes_seen_during(envelope(note("written mid-sync", ts=42)))
        self.assertEqual([n["text"] for n in engine.read_notes()], ["written mid-sync"])

    def test_a_dm_seen_during_a_sync_is_not_saved(self):
        dm = {"destination": FRIEND_UUID, "destinationUuid": FRIEND_UUID,
              "destinationNumber": None, "timestamp": 7, "message": "private"}
        engine._save_notes_seen_during(envelope(dm))
        self.assertEqual(engine.read_notes(), [])

    def test_two_writers_at_once_never_lose_a_note(self):
        """The sync's worker thread and the window both read-modify-write this file.
        Unsynchronised, the last writer wins and silently drops the other's note."""
        import threading
        engine.write_notes([])
        barrier = threading.Barrier(8)

        def add(i):
            barrier.wait()          # maximise the overlap
            engine.store_notes([{"ts": 1000 + i, "text": f"note {i}", "photos": [],
                                 "missing_photos": 0, "expires": 0}])

        threads = [threading.Thread(target=add, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(engine.read_notes()), 8)

    def test_a_notes_failure_can_never_break_a_sync(self):
        engine.NOTES_FILE = Path(self.tmp.name) / "no-such-dir" / "notes.json"
        engine._save_notes_seen_during(envelope(note("x")))   # must not raise


if __name__ == "__main__":
    unittest.main()
