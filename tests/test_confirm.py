#!/usr/bin/env python3
"""Tests for confirming a timed-out send from signal-cli's late reply (feature B).
No network, no real signal-cli — we build a bare SignalCliDaemon (no subprocess) and
drive its message routing directly. Run with:  python3 -m unittest discover -s tests

Behaviour under test: a send that times out keeps listening; if signal-cli's reply
arrives within CONFIRM_GRACE_S it yields a real sent/failed verdict; only a send that
never answers at all stays uncertain. No second send is issued during the wait.
"""
import queue
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import engine  # noqa: E402

D = engine.SignalCliDaemon


def bare_daemon():
    """A SignalCliDaemon with just the state the routing/confirm logic needs — no
    Popen, no threads. Lets us exercise the real methods deterministically."""
    d = D.__new__(D)
    d._lock = threading.Lock()
    d._pending = {}
    d._timed_out = set()
    d._late = {}
    return d


class ConfirmTests(unittest.TestCase):
    def test_route_keeps_a_late_reply_for_a_timed_out_id(self):
        d = bare_daemon()
        d._timed_out.add(7)
        d._route({"id": 7, "result": {"timestamp": 123}})
        self.assertIn(7, d._late, "a late reply to a timed-out send must be kept")
        self.assertNotIn(7, d._timed_out)

    def test_route_ignores_notifications_and_unknown_ids(self):
        d = bare_daemon()
        d._route({"method": "receive", "params": {}})   # no id — incoming notification
        d._route({"id": 999, "result": {}})             # nobody waiting on 999
        self.assertEqual(d._late, {})

    def test_wait_late_returns_then_parses_sent(self):
        d = bare_daemon()
        d._timed_out.add(7)
        d._route({"id": 7, "result": {}})               # no "error" => sent
        late = d._wait_late(7, grace=1.0)
        self.assertIsNotNone(late)
        assert late is not None  # narrow for type-checkers
        self.assertEqual(D._parse_send_response(late), (True, False, ""))

    def test_wait_late_parses_a_failed_reply(self):
        d = bare_daemon()
        d._timed_out.add(9)
        d._route({"id": 9, "error": {"message": "untrusted identity"}})
        late = d._wait_late(9, grace=1.0)
        assert late is not None  # narrow for type-checkers
        ok, throttled, err = D._parse_send_response(late)
        self.assertFalse(ok)
        self.assertIn("untrusted", err)

    def test_wait_late_returns_none_when_nothing_arrives(self):
        d = bare_daemon()
        d._timed_out.add(7)
        self.assertIsNone(d._wait_late(7, grace=0.3), "no reply in the window -> stuck")
        self.assertNotIn(7, d._timed_out, "give up listening after the grace window")

    def test_send_confirms_sent_after_a_timeout(self):
        # The real send(): the await times out, then a late SUCCESS reply arrives within
        # the grace window -> send() returns a confirmed (True, ...), not uncertain.
        d = bare_daemon()
        box: queue.Queue = queue.Queue(maxsize=1)
        d._dispatch = lambda method, params: (5, box)   # no real process/stdin
        orig = (engine.SEND_TIMEOUT_S, engine.CONFIRM_GRACE_S)
        engine.SEND_TIMEOUT_S, engine.CONFIRM_GRACE_S = 0.2, 3.0

        def deliver_late():
            time.sleep(0.4)                              # after the (tiny) send timeout
            d._route({"id": 5, "result": {"timestamp": 1}})
        try:
            threading.Thread(target=deliver_late, daemon=True).start()
            ok, throttled, err = d.send("g1", "hi", [])
        finally:
            engine.SEND_TIMEOUT_S, engine.CONFIRM_GRACE_S = orig
        self.assertTrue(ok, "a late reply after the timeout must confirm the send")

    def test_send_stays_uncertain_when_never_confirmed(self):
        d = bare_daemon()
        box: queue.Queue = queue.Queue(maxsize=1)
        d._dispatch = lambda method, params: (6, box)
        orig = (engine.SEND_TIMEOUT_S, engine.CONFIRM_GRACE_S)
        engine.SEND_TIMEOUT_S, engine.CONFIRM_GRACE_S = 0.2, 0.4  # no late reply ever
        try:
            ok, throttled, err = d.send("g1", "hi", [])
        finally:
            engine.SEND_TIMEOUT_S, engine.CONFIRM_GRACE_S = orig
        self.assertFalse(ok)
        self.assertTrue(engine.CLIENT_TIMEOUT_PATTERN.search(err),
                        "no confirmation -> reported as a client timeout (uncertain)")


if __name__ == "__main__":
    unittest.main()
