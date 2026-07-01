#!/usr/bin/env python3
"""Web-UI tests. Drive webui.py through Flask's test client with the engine monkeypatched,
so the whole HTTP surface + the background send lifecycle are exercised with NO network and
NO real signal-cli. Run:  python3 -m unittest discover -s tests

Covers: state (linked/unlinked), message save, groups list/save, the send lifecycle
(start → progress → summary), the double-send 409 guard, the cooldown gate + force,
resend-failed, schedule validation, link start/flow, and unlink.
"""
import dataclasses
import io
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import engine  # noqa: E402

try:
    import webui  # noqa: E402
    HAVE_FLASK = True
except ImportError:
    HAVE_FLASK = False


def _cfg(**over):
    base = engine.Config(account="+61400000000", base_delay_seconds=10.0, jitter_seconds=3.0,
                         cooldown_hours=0.0, max_retries=4, send_times=["09:00"])
    return dataclasses.replace(base, **over) if over else base


@unittest.skipUnless(HAVE_FLASK, "Flask not installed (installed in the Debian guest)")
class WebUITests(unittest.TestCase):
    def setUp(self):
        self.state = webui._State()
        self.app = webui.create_app(self.state)
        self.c = self.app.test_client()
        # Patch the engine surface the web layer touches. Defaults = "linked, ready".
        self.p = {}
        def patch(name, val):
            m = mock.patch.object(engine, name, val); m.start(); self.addCleanup(m.stop)
        patch("is_linked", lambda: True)
        patch("detect_account", lambda: "+61400000000")
        patch("load_config", lambda: _cfg())
        patch("read_message", lambda *a, **k: "hello world")
        patch("read_attachments", lambda *a, **k: [])
        patch("write_message", lambda *a, **k: None)
        patch("write_attachments", lambda *a, **k: None)
        patch("read_groups", lambda *a, **k: [("g1", "One"), ("g2", "Two"), ("g3", "Three")])
        patch("read_group_entries", lambda *a, **k: [
            engine.GroupEntry("g1", "One", True), engine.GroupEntry("g2", "Two", True),
            engine.GroupEntry("g3", "Three", False)])
        patch("write_group_selection", lambda ids: None)
        patch("cooldown_blocks_run", lambda h: None)
        patch("stamp_run", lambda: None)
        patch("write_run_summary", lambda r: None)
        patch("failure_breakdown", lambda r: "")
        patch("save_send_times", lambda t: None)
        patch("read_run_summary", lambda: None)  # Schedule tab's "last send"; deterministic default

    # ---- state ----
    def test_index_serves_html(self):
        r = self.c.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Signal Broadcast", r.data)

    def test_state_linked(self):
        j = self.c.get("/api/state").get_json()
        self.assertTrue(j["linked"])
        self.assertEqual(j["account"], "+61400000000")
        self.assertEqual(j["groups_enabled"], 2)
        self.assertEqual(j["groups_total"], 3)

    def test_state_unlinked(self):
        with mock.patch.object(engine, "is_linked", lambda: False), \
             mock.patch.object(engine, "detect_account", lambda: None):
            j = self.c.get("/api/state").get_json()
        self.assertFalse(j["linked"])

    def test_state_half_linked_not_linked(self):
        # Keys exist on disk (link was started) but no real account saved yet: load_config
        # raises on the placeholder, so the app must still report NOT linked.
        def raise_placeholder():
            raise engine.BroadcastError("account is still the placeholder")
        with mock.patch.object(engine, "is_linked", lambda: True), \
             mock.patch.object(engine, "load_config", raise_placeholder):
            j = self.c.get("/api/state").get_json()
        self.assertFalse(j["linked"])
        self.assertIsNone(j["account"])

    # ---- message + groups ----
    def test_message_save(self):
        r = self.c.post("/api/message", json={"message": "hi"})
        self.assertTrue(r.get_json()["ok"])

    def test_groups_list_and_save(self):
        j = self.c.get("/api/groups").get_json()
        self.assertEqual(len(j["groups"]), 3)
        self.assertFalse(j["groups"][2]["enabled"])
        r = self.c.post("/api/groups", json={"enabled": ["g1", "g2"]})
        self.assertTrue(r.get_json()["ok"])

    # ---- send lifecycle ----
    def _fake_broadcast(self, results, delay=0.0):
        def fake(*, config, groups, message, attachments, on_log, on_progress, should_stop, **k):
            for i, (gid, name) in enumerate(groups, 1):
                if should_stop():
                    break
                if delay:
                    time.sleep(delay)
                on_progress(i, len(groups), name, "sent", 0.1)
                on_log(f"[{i}/{len(groups)}] sent")
            return results
        return fake

    def _drain(self, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            p = self.c.get("/api/progress").get_json()
            if not p["running"]:
                return p
            time.sleep(0.05)
        self.fail("send did not finish in time")

    def test_send_success(self):
        results = [engine.GroupSendResult("g1", "One", ok=True),
                   engine.GroupSendResult("g2", "Two", ok=True),
                   engine.GroupSendResult("g3", "Three", ok=True)]
        with mock.patch.object(engine, "broadcast", self._fake_broadcast(results)):
            r = self.c.post("/api/send", json={})
            self.assertTrue(r.get_json()["started"])
            p = self._drain()
        self.assertEqual(p["summary"]["sent"], 3)
        self.assertEqual(p["summary"]["failed"], 0)
        self.assertEqual(p["failed_count"], 0)

    def test_send_with_failures_enables_resend(self):
        results = [engine.GroupSendResult("g1", "One", ok=True),
                   engine.GroupSendResult("g2", "Two", ok=False, reason="network")]
        with mock.patch.object(engine, "broadcast", self._fake_broadcast(results)):
            self.c.post("/api/send", json={})
            p = self._drain()
        self.assertEqual(p["summary"]["failed"], 1)
        self.assertEqual(p["failed_count"], 1)
        # resend-failed should now target just the 1 failed group
        seen = {}
        def capture(*, groups, **k):
            seen["n"] = len(groups)
            return [engine.GroupSendResult(g, n, ok=True) for g, n in groups]
        with mock.patch.object(engine, "broadcast", capture):
            self.c.post("/api/send", json={"only_failed": True})
            self._drain()
        self.assertEqual(seen["n"], 1)

    def test_double_send_returns_409(self):
        results = [engine.GroupSendResult("g1", "One", ok=True)]
        with mock.patch.object(engine, "broadcast", self._fake_broadcast(results, delay=0.3)):
            r1 = self.c.post("/api/send", json={})
            self.assertTrue(r1.get_json()["started"])
            r2 = self.c.post("/api/send", json={})
            self.assertEqual(r2.status_code, 409)
            self._drain()

    def test_cooldown_gate_then_force(self):
        with mock.patch.object(engine, "cooldown_blocks_run", lambda h: "too soon since last run"):
            r = self.c.post("/api/send", json={})
            self.assertIn("cooldown", r.get_json())
            self.assertIsNone(r.get_json().get("started"))
            results = [engine.GroupSendResult("g1", "One", ok=True)]
            with mock.patch.object(engine, "broadcast", self._fake_broadcast(results)):
                r2 = self.c.post("/api/send", json={"force": True})
                self.assertTrue(r2.get_json()["started"])
                self._drain()

    def test_send_empty_message_rejected(self):
        with mock.patch.object(engine, "read_message", lambda *a, **k: "   "):
            r = self.c.post("/api/send", json={})
            self.assertEqual(r.status_code, 400)

    def test_stop_halts_send(self):
        results = [engine.GroupSendResult(f"g{i}", str(i), ok=True) for i in range(1, 21)]
        many = [(f"g{i}", str(i)) for i in range(1, 21)]
        with mock.patch.object(engine, "read_groups", lambda *a, **k: many), \
             mock.patch.object(engine, "broadcast", self._fake_broadcast(results, delay=0.05)):
            self.c.post("/api/send", json={})
            time.sleep(0.12)
            self.c.post("/api/stop")
            p = self._drain()
        self.assertLess(p["done"], 20)  # stopped before finishing all 20

    # ---- schedule ----
    def test_schedule_valid(self):
        with mock.patch.object(webui, "_cron_write", lambda t: True), \
             mock.patch.object(webui, "_cron_clear", lambda: True):
            r = self.c.post("/api/schedule", json={"times": ["09:00", "16:30"], "enabled": True})
            self.assertTrue(r.get_json()["ok"])

    def test_schedule_invalid_time_rejected(self):
        r = self.c.post("/api/schedule", json={"times": ["25:00"], "enabled": True})
        self.assertEqual(r.status_code, 400)

    def test_schedule_get_shape(self):
        # GET mirrors the Mac Schedule tab: times + enabled + next_send + last_send.
        summ = engine.RunSummary(at="2026-07-01T09:00:00", total=5, sent=5,
                                 failed=0, skipped=0, uncertain=0)
        with mock.patch.object(webui, "_cron_installed", lambda: True), \
             mock.patch.object(engine, "read_run_summary", lambda: summ):
            j = self.c.get("/api/schedule").get_json()
        self.assertTrue(j["enabled"])
        self.assertEqual(j["times"], ["09:00"])
        self.assertIsNotNone(j["next_send"])          # computed while enabled
        self.assertEqual(j["last_send"]["sent"], 5)
        self.assertIn("Jul", j["last_send"]["at"])     # ISO reformatted to "Jul 01, 09:00"

    def test_schedule_get_next_send_none_when_off(self):
        with mock.patch.object(webui, "_cron_installed", lambda: False):
            j = self.c.get("/api/schedule").get_json()
        self.assertFalse(j["enabled"])
        self.assertIsNone(j["next_send"])

    def test_schedule_enable_surfaces_cron_failure(self):
        # crontab missing / write failed → must NOT report "on" (else a schedule that
        # never fires looks enabled).
        with mock.patch.object(webui, "_cron_write", lambda t: False):
            r = self.c.post("/api/schedule", json={"times": ["09:00"], "enabled": True})
            self.assertEqual(r.status_code, 500)
            self.assertIn("error", r.get_json())

    # ---- link / unlink ----
    def test_link_flow(self):
        with mock.patch.object(engine, "is_linked", lambda: False), \
             mock.patch.object(engine, "signal_cli_command",
                               lambda *a: (["printf", "sgnl://linkdevice?uuid=x\n"], None)), \
             mock.patch.object(webui, "_qr_png_b64", lambda uri: "QRB64"), \
             mock.patch.object(engine, "detect_account", lambda: "+61400000000"), \
             mock.patch.object(engine, "save_account", lambda n: None), \
             mock.patch.object(engine, "sync_groups", lambda a, on_log=None: 3):
            self.assertTrue(self.c.post("/api/link/start").get_json()["started"])
            end = time.time() + 5
            s = {}
            while time.time() < end:
                s = self.c.get("/api/link").get_json()
                if s.get("linked"):
                    break
                time.sleep(0.05)
        self.assertTrue(s.get("linked"))
        self.assertTrue(s.get("uri", "").startswith("sgnl://linkdevice"))

    def test_link_status_not_linked_without_account(self):
        # The bug: link *started* (keys on disk, URI shown) must NOT report "linked" until a
        # real account is saved — otherwise the page shows "Linked!" but never advances.
        self.state.link_uri = "sgnl://linkdevice?uuid=x"
        self.state.link_linked = False
        def raise_placeholder():
            raise engine.BroadcastError("placeholder account")
        with mock.patch.object(engine, "is_linked", lambda: True), \
             mock.patch.object(engine, "load_config", raise_placeholder):
            j = self.c.get("/api/link").get_json()
        self.assertFalse(j["linked"])
        self.assertTrue(j["uri"].startswith("sgnl://"))

    def test_groups_refresh(self):
        with mock.patch.object(engine, "sync_groups", lambda a, on_log=None: 7):
            self.assertTrue(self.c.post("/api/groups/refresh").get_json()["started"])
            end = time.time() + 5
            s = {}
            while time.time() < end:
                s = self.c.get("/api/groups/refresh").get_json()
                if not s["running"]:
                    break
                time.sleep(0.05)
        self.assertFalse(s["running"])
        self.assertEqual(s["count"], 7)

    def test_unlink(self):
        # Mock _cron_clear too — otherwise the real one rewrites the dev's actual crontab.
        with mock.patch.object(engine, "unlink") as u, \
             mock.patch.object(webui, "_cron_clear") as cc:
            r = self.c.post("/api/unlink")
            self.assertTrue(r.get_json()["ok"])
            u.assert_called_once()
            cc.assert_called_once()  # the port's scheduled cron is torn down too

    # ---- upload hardening ----
    def test_upload_keeps_images_and_drops_nonimages(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(webui, "UPLOAD_DIR", Path(d)):
            data = {"images": [
                (io.BytesIO(b"\x89PNG\r\n\x1a\n"), "photo.png", "image/png"),
                (io.BytesIO(b"#!/bin/sh\nrm -rf /"), "evil.sh", "application/x-sh"),
            ]}
            r = self.c.post("/api/upload", data=data, content_type="multipart/form-data")
            self.assertEqual(r.get_json()["attachments"], ["photo.png"])  # .sh ignored
            self.assertTrue((Path(d) / "photo.png").exists())
            self.assertFalse((Path(d) / "evil.sh").exists())

    def test_upload_strips_path_traversal(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(webui, "UPLOAD_DIR", Path(d)):
            data = {"images": [(io.BytesIO(b"\x89PNG\r\n\x1a\n"),
                                "../../etc/passwd.png", "image/png")]}
            r = self.c.post("/api/upload", data=data, content_type="multipart/form-data")
            names = r.get_json()["attachments"]
            self.assertEqual(len(names), 1)
            self.assertNotIn("/", names[0])
            self.assertNotIn("..", names[0])

    # ---- local-only guard (CSRF / DNS-rebinding) ----
    def test_rejects_foreign_host(self):
        r = self.c.post("/api/unlink", headers={"Host": "evil.example.com"})
        self.assertEqual(r.status_code, 403)

    def test_rejects_cross_site_origin(self):
        r = self.c.post("/api/message", json={"message": "x"},
                        headers={"Origin": "http://evil.example.com"})
        self.assertEqual(r.status_code, 403)

    def test_allows_loopback_origin(self):
        r = self.c.post("/api/message", json={"message": "x"},
                        headers={"Origin": "http://127.0.0.1:8787"})
        self.assertTrue(r.get_json()["ok"])


if __name__ == "__main__":
    unittest.main()
