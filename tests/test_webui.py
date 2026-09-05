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
import threading
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
        platform = mock.patch.object(engine, "IS_DARWIN", False)
        platform.start()
        self.addCleanup(platform.stop)
        self.state = webui._State()
        self.app = webui.create_app(self.state)
        self.c = self.app.test_client()
        # Patch the engine surface the web layer touches. Defaults = "linked, ready".
        self.p = {}
        def patch(name, val):
            m = mock.patch.object(engine, name, val); m.start(); self.addCleanup(m.stop)
        patch("is_linked", lambda: True)
        # Healthy by default. MUST be patched: unpatched it spawns a real signal-cli.
        patch("link_is_broken", lambda: False)
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
        patch("read_notes", lambda: [])
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

    def test_group_checkbox_updates_use_the_id_index(self):
        page = self.c.get("/").get_data(as_text=True)
        self.assertIn("groupsById=new Map", page)
        self.assertIn("groupsById.get", page)
        self.assertNotIn("allGroups.find", page)

    def test_group_selection_save_waits_for_active_sync(self):
        with self.state.lock:
            self.state.refresh_running = True
        with mock.patch.object(engine, "write_group_selection") as write:
            response = self.c.post("/api/groups", json={"enabled": ["g1"]})
        self.assertEqual(response.status_code, 409)
        self.assertIn("sync", response.get_json()["error"].lower())
        write.assert_not_called()

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

    # ---- link health ----
    def _settle(self, timeout=2.0):
        """Wait for the background link-health worker to publish its verdict."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self.state.lock:
                if not self.state.link_checking and self.state.link_checked_at:
                    return
            time.sleep(0.01)
        self.fail("link health check did not finish")

    def test_dead_link_reads_as_not_linked(self):
        # On-disk keys survive the phone removing this device, so is_linked() stays
        # True. Without the health check the page shows a normal app whose every send
        # fails; it must route to the link screen instead.
        with mock.patch.object(engine, "link_is_broken", lambda: True):
            self.c.get("/api/state")          # first poll kicks off the worker
            self._settle()
            j = self.c.get("/api/state").get_json()
        self.assertFalse(j["linked"])
        self.assertIsNone(j["account"])

    def test_health_check_is_cached_not_run_per_poll(self):
        # /api/state is polled every couple of seconds and the real check spawns
        # signal-cli, so repeated polls must reuse the cached verdict.
        calls = []
        with mock.patch.object(engine, "link_is_broken", lambda: calls.append(1) or False):
            for _ in range(12):
                self.c.get("/api/state")
                self._settle()
        self.assertEqual(len(calls), 1, "should check once, then serve from cache")

    def test_stale_verdict_is_rechecked_after_ttl(self):
        calls = []
        with mock.patch.object(engine, "link_is_broken", lambda: calls.append(1) or False):
            self.c.get("/api/state")
            self._settle()
            with self.state.lock:      # pretend the TTL elapsed
                self.state.link_checked_at -= (webui.LINK_CHECK_TTL_S + 1)
            self.c.get("/api/state")
            self._settle()
        self.assertEqual(len(calls), 2)

    def test_a_healthy_link_stays_linked(self):
        self.c.get("/api/state")
        self._settle()
        j = self.c.get("/api/state").get_json()
        self.assertTrue(j["linked"])
        self.assertEqual(j["account"], "+61400000000")

    def test_transient_check_failure_does_not_bounce_a_healthy_install(self):
        # link_is_broken() raising must never read as "broken" — that would throw a
        # working install back to the link screen on a blip.
        def boom():
            raise engine.BroadcastError("signal-cli busy")
        with mock.patch.object(engine, "link_is_broken", boom):
            self.c.get("/api/state")
            self._settle()
            j = self.c.get("/api/state").get_json()
        self.assertTrue(j["linked"])

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

    def test_link_reports_success_while_initial_group_sync_is_still_running(self):
        sync_started = threading.Event()
        release_sync = threading.Event()
        sync_calls = []

        def slow_sync(_account, on_log=None):
            sync_calls.append(_account)
            sync_started.set()
            release_sync.wait(timeout=5)
            return 3

        self.addCleanup(release_sync.set)
        with mock.patch.object(engine, "is_linked", lambda: False), \
             mock.patch.object(engine, "signal_cli_command",
                               lambda *a: (["printf", "sgnl://linkdevice?uuid=x\n"], None)), \
             mock.patch.object(webui, "_qr_png_b64", lambda uri: "QRB64"), \
             mock.patch.object(engine, "detect_account", lambda: "+61400000000"), \
             mock.patch.object(engine, "save_account", lambda n: None), \
             mock.patch.object(engine, "load_config", return_value=_cfg()), \
            mock.patch.object(engine, "sync_groups", slow_sync):
            self.assertTrue(self.c.post("/api/link/start").get_json()["started"])
            end = time.time() + 5
            while True:
                s = self.c.get("/api/link").get_json()
                if s["linked"] or time.time() >= end:
                    break
                time.sleep(0.01)
            self.assertTrue(sync_started.wait(timeout=5))

        self.assertTrue(s["linked"])
        self.assertTrue(sync_started.is_set())
        self.assertFalse(release_sync.is_set(), "link status must not wait for group sync")
        self.assertTrue(self.c.get("/api/groups/refresh").get_json()["running"])
        self.assertTrue(self.c.post("/api/groups/refresh").get_json()["running"])
        self.assertEqual(len(sync_calls), 1, "manual refresh must reuse the tracked run")

    def test_link_fresh_starts_when_idle(self):
        # Tapping "Open Signal" calls /api/link/fresh; with no loop running it should kick
        # one off (so the user gets a code) rather than erroring.
        with mock.patch.object(engine, "is_linked", lambda: False), \
             mock.patch.object(engine, "detect_account", lambda: None), \
             mock.patch.object(engine, "signal_cli_command",
                               lambda *a: (["sleep", "30"], None)):
            j = self.c.post("/api/link/fresh").get_json()
            end = time.time() + 5
            while self.state.link_proc is None and time.time() < end:
                time.sleep(0.01)
            with self.state.lock:
                self.state.link_linked = True
                proc = self.state.link_proc
            if proc is not None:
                proc.terminate()
            while self.state.link_running and time.time() < end:
                time.sleep(0.01)
        self.assertTrue(j.get("started") or j.get("ok"))
        self.assertFalse(self.state.link_running, "test must release the shared Signal lease")

    def test_link_status_reports_code_age(self):
        # The page opens Signal only while a code is young (Chrome drops the sgnl:// launch
        # outside a fresh user gesture), so /api/link must say how old the code is.
        self.state.link_uri = "sgnl://linkdevice?uuid=x"
        self.state.link_uri_ts = time.time() - 3
        with mock.patch.object(engine, "is_linked", lambda: False), \
             mock.patch.object(engine, "detect_account", lambda: None):
            j = self.c.get("/api/link").get_json()
        self.assertGreaterEqual(j["age"], 3)
        self.assertLess(j["age"], 10)
        self.state.link_uri = None
        with mock.patch.object(engine, "is_linked", lambda: False), \
             mock.patch.object(engine, "detect_account", lambda: None):
            j = self.c.get("/api/link").get_json()
        self.assertIsNone(j["age"])

    def test_link_status_supplies_explicit_signal_android_intent(self):
        self.state.link_uri = "sgnl://linkdevice?uuid=abc&pub_key=def%2Bghi"
        self.state.link_uri_ts = time.time()
        with mock.patch.object(engine, "is_linked", lambda: False), \
             mock.patch.object(engine, "detect_account", lambda: None):
            j = self.c.get("/api/link").get_json()
        self.assertEqual(
            j["open_uri"],
            "intent://linkdevice?uuid=abc&pub_key=def%2Bghi"
            "#Intent;scheme=sgnl;package=org.thoughtcrime.securesms;end",
        )

    def test_link_connection_closed_stops_with_actionable_error(self):
        output = ("sgnl://linkdevice?uuid=x&pub_key=y\\n"
                  "Link request error: Connection closed!\\n")
        with mock.patch.object(engine, "is_linked", lambda: False), \
             mock.patch.object(engine, "signal_cli_command",
                               lambda *a: (["printf", output], None)), \
             mock.patch.object(webui, "_qr_png_b64", lambda uri: "QRB64"), \
             mock.patch.object(engine, "detect_account", lambda: None), \
             mock.patch.object(webui, "LINK_TOTAL_S", 0.1):
            self.assertTrue(self.c.post("/api/link/start").get_json()["started"])
            end = time.time() + 2
            while self.state.link_running and time.time() < end:
                time.sleep(0.01)
            j = self.c.get("/api/link").get_json()
        self.assertFalse(j["running"])
        self.assertIn("did not confirm", j["error"])
        self.assertIn("Try again", j["error"])

    def test_link_http_403_stops_with_update_and_network_advice(self):
        output = ("sgnl://linkdevice?uuid=x&pub_key=y\\n"
                  "Link request error: HTTP 403 Forbidden\\n")
        with mock.patch.object(engine, "is_linked", lambda: False), \
             mock.patch.object(engine, "signal_cli_command",
                               lambda *a: (["printf", output], None)), \
             mock.patch.object(webui, "_qr_png_b64", lambda uri: "QRB64"), \
             mock.patch.object(engine, "detect_account", lambda: None), \
             mock.patch.object(webui, "LINK_TOTAL_S", 0.1):
            self.assertTrue(self.c.post("/api/link/start").get_json()["started"])
            end = time.time() + 2
            while self.state.link_running and time.time() < end:
                time.sleep(0.01)
            j = self.c.get("/api/link").get_json()
        self.assertFalse(j["running"])
        self.assertIn("Update", j["error"])
        self.assertIn("mobile data", j["error"])

    def test_link_fresh_clears_stale_code(self):
        # Asking for a fresh code abandons the current one — the dead code must vanish
        # immediately, so a tap can't open Signal with a code whose provisioning socket
        # is already gone (the JVM restart takes tens of seconds under proot).
        import subprocess
        proc = subprocess.Popen(["sleep", "30"])
        self.addCleanup(proc.kill)
        self.state.link_running = True
        self.state.link_proc = proc
        self.state.link_uri = "sgnl://linkdevice?uuid=stale"
        self.state.link_qr = "QRB64"
        with mock.patch.object(engine, "is_linked", lambda: False), \
             mock.patch.object(engine, "detect_account", lambda: None):
            j = self.c.post("/api/link/fresh").get_json()
        self.assertTrue(j.get("ok"))
        self.assertIsNone(self.state.link_uri)
        self.assertIsNone(self.state.link_qr)
        proc.wait(timeout=5)  # terminate() was delivered → the attempt actually ended

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

    def test_groups_refresh_publishes_customer_safe_progress(self):
        def sync(_account, on_log=None):
            on_log("Reading your group list… Large accounts can take several minutes.")
            return 7

        with mock.patch.object(engine, "sync_groups", sync):
            self.assertTrue(self.c.post("/api/groups/refresh").get_json()["started"])
            end = time.time() + 5
            s = {}
            while time.time() < end:
                s = self.c.get("/api/groups/refresh").get_json()
                if not s["running"]:
                    break
                time.sleep(0.01)
        self.assertEqual(s["status"],
                         "Reading your group list… Large accounts can take several minutes.")

    def test_groups_refresh_waits_for_notes_check(self):
        with self.state.lock:
            self.state.notes_running = True
        with mock.patch.object(engine, "sync_groups") as sync:
            response = self.c.post("/api/groups/refresh")
        self.assertEqual(response.status_code, 409)
        self.assertIn("notes", response.get_json()["error"].lower())
        sync.assert_not_called()

    # ---- notes ----
    def test_notes_list_projects_only_browser_safe_fields(self):
        notes = [{"ts": 123, "text": "buy milk", "photos": [
            {"path": "/private/secret/photo.jpg", "name": "photo.jpg"}],
            "missing_photos": 2, "view_once_photos": 1, "missing_body": True,
            "unexpected": "private"}]
        with mock.patch.object(engine, "read_notes", return_value=notes):
            response = self.c.get("/api/notes")
            j = response.get_json()
        self.assertEqual(j["notes"], [{"ts": 123, "text": "buy milk", "photos": 1,
                                        "missing_photos": 2, "view_once_photos": 1,
                                        "missing_body": True}])
        self.assertNotIn("/private/secret", response.get_data(as_text=True))

    def test_notes_refresh_reports_plain_completion_summary(self):
        report = {"envelopes": 8, "transcripts": 3, "notes": 2, "new": 1,
                  "seconds": 4.2}
        with mock.patch.object(engine, "fetch_notes", return_value=report):
            self.assertTrue(self.c.post("/api/notes/refresh").get_json()["started"])
            end = time.time() + 5
            s = {}
            while time.time() < end:
                s = self.c.get("/api/notes/refresh").get_json()
                if not s["running"]:
                    break
                time.sleep(0.01)
        self.assertFalse(s["running"])
        self.assertEqual(s["result"], {"transcripts": 3, "notes": 2, "new": 1,
                                         "complete": True, "warning": ""})

    def test_notes_refresh_exposes_incomplete_status_and_progress(self):
        def receive(_account, on_log):
            on_log("Receiving: 8 messages processed, 1 note saved.")
            return {"new": 1, "complete": False, "warning": "Receiving timed out. Try again."}
        with mock.patch.object(engine, "fetch_notes", side_effect=receive):
            self.c.post("/api/notes/refresh")
            end = time.time() + 5
            while time.time() < end:
                status = self.c.get("/api/notes/refresh").get_json()
                if not status["running"]:
                    break
                time.sleep(0.01)
        self.assertFalse(status["result"]["complete"])
        self.assertEqual(status["result"]["warning"], "Receiving timed out. Try again.")
        self.assertIn("8 messages", status["status"])

    def test_notes_refresh_waits_for_group_sync(self):
        with self.state.lock:
            self.state.refresh_running = True
        with mock.patch.object(engine, "fetch_notes") as fetch:
            response = self.c.post("/api/notes/refresh")
        self.assertEqual(response.status_code, 409)
        self.assertIn("groups", response.get_json()["error"].lower())
        fetch.assert_not_called()

    def test_note_can_be_loaded_into_message_with_existing_photos(self):
        writes = {}
        with tempfile.TemporaryDirectory() as directory:
            one = Path(directory) / "one.jpg"
            one.write_bytes(b"photo")
            two = Path(directory) / "two.jpg"
            two.write_bytes(b"photo")
            note = {"ts": 123, "text": "send this", "photos": [
                {"path": str(one), "name": "one.jpg"},
                {"path": str(two), "name": "two.jpg"}]}
            with mock.patch.object(engine, "read_notes", return_value=[note]), \
                 mock.patch.object(engine, "write_message",
                                   side_effect=lambda value: writes.update(message=value)), \
                 mock.patch.object(engine, "write_attachments",
                                   side_effect=lambda value: writes.update(attachments=value)):
                response = self.c.post("/api/notes/use", json={"ts": 123})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(writes["message"], "send this")
        self.assertEqual(writes["attachments"], [str(one), str(two)])

    def test_note_delete_uses_timestamp(self):
        with mock.patch.object(engine, "delete_note") as delete:
            response = self.c.delete("/api/notes/123")
        self.assertTrue(response.get_json()["ok"])
        delete.assert_called_once_with(123)

    def test_incomplete_note_is_refused_without_changing_the_draft(self):
        note = {"ts": 123, "text": "text only", "photos": [
            {"path": "/missing/photo.jpg", "name": "photo.jpg"}],
            "missing_photos": 1}
        with mock.patch.object(engine, "read_notes", return_value=[note]), \
             mock.patch.object(engine, "write_message") as write_message, \
             mock.patch.object(engine, "write_attachments") as write:
            response = self.c.post("/api/notes/use", json={"ts": 123})
        self.assertEqual(response.status_code, 409)
        self.assertIn("missing", response.get_json()["error"])
        write_message.assert_not_called()
        write.assert_not_called()

    def test_text_only_note_clears_old_draft_photos(self):
        note = {"ts": 123, "text": "text only", "photos": []}
        with mock.patch.object(engine, "read_notes", return_value=[note]), \
             mock.patch.object(engine, "write_attachments") as write:
            response = self.c.post("/api/notes/use", json={"ts": 123})
        self.assertEqual(response.status_code, 200)
        write.assert_called_once_with([])

    def test_note_with_disappeared_photo_file_is_refused(self):
        note = {"ts": 123, "text": "caption", "photos": [
            {"path": "/missing/photo.jpg", "name": "photo.jpg"}]}
        with mock.patch.object(engine, "read_notes", return_value=[note]), \
             mock.patch.object(engine, "write_message") as write_message:
            response = self.c.post("/api/notes/use", json={"ts": 123})
        self.assertEqual(response.status_code, 409)
        self.assertIn("disappeared", response.get_json()["error"])
        write_message.assert_not_called()

    def test_pixel_page_has_install_offline_accessibility_and_polling_guards(self):
        page = self.c.get("/").get_data(as_text=True)
        self.assertIn('rel="manifest"', page)
        self.assertIn("serviceWorker.register", page)
        self.assertIn('aria-current="page"', page)
        self.assertIn("document.visibilityState==='visible'", page)
        self.assertIn("offlineDisabled", page)
        self.assertIn("min-height:var(--tap)", page)

    def test_pwa_assets_have_expected_shape(self):
        manifest = self.c.get("/manifest.webmanifest")
        self.assertEqual(manifest.status_code, 200)
        self.assertEqual(manifest.get_json()["display"], "standalone")
        self.assertEqual(self.c.get("/sw.js").mimetype, "application/javascript")
        for size in (192, 512):
            icon = self.c.get(f"/icon-{size}.png")
            self.assertEqual(icon.mimetype, "image/png")
            self.assertEqual(icon.data[16:24], size.to_bytes(4, "big") * 2)

    def test_pixel_launcher_waits_for_server_readiness(self):
        root = Path(__file__).resolve().parent.parent
        script = (root / "scripts" / "refresh-pixel-widget.sh").read_text()
        self.assertNotIn("sleep 4; termux-open-url", script)
        self.assertIn("until curl -fsS", script)
        self.assertIn('kill -0 "\\$server_pid"', script)
        self.assertIn("refresh-pixel-widget.sh", (root / "scripts" / "setup-termux.sh").read_text())

    def test_both_setups_install_current_signal_provisioning_client(self):
        root = Path(__file__).resolve().parent.parent
        self.assertIn('SIGNAL_CLI_VERSION="0.14.7"', (root / "Setup.command").read_text())
        self.assertIn('SIGNAL_CLI_VERSION="${SIGNAL_CLI_VERSION:-0.14.7}"',
                      (root / "scripts" / "setup-termux.sh").read_text())

    def test_unlink(self):
        # Mock _cron_clear too — otherwise the real one rewrites the dev's actual crontab.
        with mock.patch.object(engine, "unlink") as u, \
             mock.patch.object(webui, "_cron_clear") as cc:
            r = self.c.post("/api/unlink")
            self.assertTrue(r.get_json()["ok"])
            u.assert_called_once()
            cc.assert_called_once()  # the port's scheduled cron is torn down too

    def test_unlink_reports_busy_without_resetting_state(self):
        self.state.link_linked = True
        with mock.patch.object(engine, "unlink",
                               side_effect=engine.BroadcastError("Signal is busy")), \
             mock.patch.object(webui, "_cron_clear") as clear_cron:
            r = self.c.post("/api/unlink")
        self.assertEqual(r.status_code, 409)
        self.assertIn("busy", r.get_json()["error"])
        self.assertTrue(self.state.link_linked)
        clear_cron.assert_not_called()

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
