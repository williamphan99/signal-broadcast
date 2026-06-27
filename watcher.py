#!/usr/bin/env python3
"""Station-mode watcher: if the Mac is unplugged while linked, erase everything.

Runs as a background launchd agent (see engine.enable_watcher). It polls the power
source; once the Mac has been on battery for DEBOUNCE_POLLS consecutive checks it
starts a GRACE_SECONDS countdown, and if power hasn't returned by the end it runs the
full engine.unlink() wipe. While it needs the Mac awake (on AC, or counting down) it
holds a caffeinate assertion. Output goes to logs/watcher.out and contains no
personal data — only power-state transitions.

The decision logic lives in PowerWatcher with every side effect injected, so it can
be driven with stubs in a test without touching real power, files, or sleep.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))  # so 'engine' imports
import engine  # noqa: E402

POLL_SECONDS = 2.0
DEBOUNCE_POLLS = 3       # consecutive on-battery reads before we trust it's unplugged
GRACE_SECONDS = 10.0     # plug back in within this window to cancel the wipe


class PowerWatcher:
    def __init__(self, *, on_ac: Callable[[], bool], is_linked: Callable[[], bool],
                 wipe: Callable[[], None], set_awake: Callable[[bool], None],
                 sleep: Callable[[float], None], clock: Callable[[], float],
                 log: Callable[[str], None], stop: Callable[[], bool] = lambda: False,
                 poll: float = POLL_SECONDS, debounce: int = DEBOUNCE_POLLS,
                 grace: float = GRACE_SECONDS) -> None:
        self._on_ac, self._is_linked, self._wipe = on_ac, is_linked, wipe
        self._set_awake, self._sleep, self._clock = set_awake, sleep, clock
        self._log, self._stop = log, stop
        self._poll, self._debounce, self._grace = poll, debounce, grace
        self._battery_polls = 0
        self._grace_deadline: float | None = None

    def run(self) -> None:
        self._log("Station watcher started.")
        while not self._stop():
            self.tick()
            self._sleep(self._poll)

    def tick(self) -> None:
        if self._on_ac():
            # Stay awake to notice an unplug only while there's a link worth protecting.
            self._stand_down(awake=self._is_linked())
            return
        if not self._is_linked():
            self._stand_down(awake=False)          # nothing to protect; allow sleep
            return
        self._set_awake(True)                      # on battery with data — stay awake to wipe
        self._battery_polls += 1
        if self._battery_polls < self._debounce:
            return                                 # not yet sure it's really unplugged
        if self._grace_deadline is None:
            self._grace_deadline = self._clock() + self._grace
            self._log(f"Unplugged — wiping in {self._grace:.0f}s unless power returns.")
        elif self._clock() >= self._grace_deadline:
            self._log("Grace elapsed — wiping now.")
            self._wipe()
            self._battery_polls = 0                # reset silently; the wipe wasn't cancelled
            self._grace_deadline = None
            self._set_awake(False)

    def _stand_down(self, *, awake: bool) -> None:
        """Power's back or there's nothing to protect — cancel any pending wipe."""
        if self._grace_deadline is not None:
            self._log("Power restored — wipe cancelled.")
        self._battery_polls = 0
        self._grace_deadline = None
        self._set_awake(awake)


def _make_caffeinate_controller() -> Callable[[bool], None]:
    """Hold one `caffeinate -i` while awake is wanted; it also exits with us (-w)."""
    held: list[subprocess.Popen] = []

    def set_awake(on: bool) -> None:
        if on and not held:
            held.append(subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())]))
        elif not on and held:
            held.pop().terminate()

    return set_awake


def main() -> None:
    PowerWatcher(
        on_ac=engine.on_ac_power, is_linked=engine.is_linked, wipe=engine.unlink,
        set_awake=_make_caffeinate_controller(),
        sleep=time.sleep, clock=time.monotonic,
        log=lambda m: print(m, flush=True),
    ).run()


if __name__ == "__main__":
    main()
