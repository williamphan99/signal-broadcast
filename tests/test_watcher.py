#!/usr/bin/env python3
"""Station-mode wipe retry tests with every side effect stubbed."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import engine  # noqa: E402
import watcher  # noqa: E402


class StationWipeTests(unittest.TestCase):
    def test_busy_safe_boundary_is_retried_without_dropping_awake(self):
        attempts = []
        awake = []
        logs = []

        def wipe():
            attempts.append(True)
            if len(attempts) == 1:
                raise engine.BroadcastError("Signal is busy")

        guard = watcher.PowerWatcher(
            on_ac=lambda: False,
            is_linked=lambda: True,
            wipe=wipe,
            set_awake=awake.append,
            sleep=lambda _seconds: None,
            clock=lambda: 10.0,
            log=logs.append,
            poll=0,
            debounce=1,
            grace=0,
        )

        guard.tick()  # arms the zero-second deadline
        guard.tick()  # busy: retain deadline and stay awake
        self.assertEqual(len(attempts), 1)
        self.assertTrue(guard._grace_deadline is not None)
        self.assertTrue(awake[-1])
        self.assertTrue(any("Wipe delayed" in line for line in logs))

        guard.tick()  # safe boundary free: wipe succeeds and watcher stands down
        self.assertEqual(len(attempts), 2)
        self.assertIsNone(guard._grace_deadline)
        self.assertFalse(awake[-1])


if __name__ == "__main__":
    unittest.main()
