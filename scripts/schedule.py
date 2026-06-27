#!/usr/bin/env python3
"""Generate the launchd schedule from config.toml send_times (CLI equivalent of
the app's Schedule tab). The app calls engine.enable_schedule directly; this
prints the launchctl commands for anyone who prefers the terminal.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # so 'engine' imports
import engine  # noqa: E402


def main() -> None:
    cfg = engine.load_config()
    dest = engine.write_plist(cfg.send_times, dest=engine.LOCAL_PLIST)
    times = ", ".join(cfg.send_times)
    uid = "$(id -u)"
    print(f"Wrote {dest.name} — fires daily at: {times}\n")
    print("Enable it:")
    print(f"  cp {dest} ~/Library/LaunchAgents/")
    print(f"  launchctl bootstrap gui/{uid} ~/Library/LaunchAgents/{engine.SCHEDULE_LABEL}.plist\n")
    print("Disable it:")
    print(f"  launchctl bootout gui/{uid}/{engine.SCHEDULE_LABEL}")
    print("\nOr just use the Schedule tab in the app.")


if __name__ == "__main__":
    main()
