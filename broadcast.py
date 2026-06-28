#!/usr/bin/env python3
"""Command-line front end over engine.py — used for manual runs and the launchd
schedule. The GUI (gui.py) is the other front end; both share the same engine.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

import engine

log = logging.getLogger("broadcast")


def configure_logging() -> None:
    engine.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    handlers = [logging.StreamHandler(sys.stdout),
                logging.FileHandler(engine.LOGS_DIR / f"run-{stamp}.log", encoding="utf-8")]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S", handlers=handlers)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Broadcast a Signal message to many groups, slowly.")
    p.add_argument("--groups", default=str(engine.GROUPS_FILE),
                   help="group list file. Point at a failures-*.txt to resend just those.")
    p.add_argument("--message", default=str(engine.MESSAGE_FILE), help="message text file")
    p.add_argument("--attachments", default=str(engine.ATTACHMENTS_FILE), help="image-paths file")
    p.add_argument("--delay", type=float, default=None, help="override seconds between sends")
    p.add_argument("--limit", type=int, default=None, help="only send to the first N groups (testing)")
    p.add_argument("--dry-run", action="store_true", help="show what would send, send nothing")
    p.add_argument("--force", action="store_true", help="ignore the cooldown gate")
    return p.parse_args()


def _print_dry_run(cfg, message, attachments, groups, delay) -> None:
    est = len(groups) * delay / 60
    print("DRY RUN — nothing will be sent.\n")
    print(f"  account     : {cfg.account}")
    print(f"  groups      : {len(groups)}")
    print(f"  attachments : {len(attachments)}")
    for a in attachments:
        print(f"      - {a}")
    print(f"  pacing      : ~{delay:.0f}s +/-{cfg.jitter_seconds:.0f}s  (~{est:.0f} min total)\n")
    print("  message:")
    for line in message.splitlines() or [""]:
        print(f"    | {line}")
    print("\n  first groups:")
    for gid, name in groups[:5]:
        print(f"    - {name}  [{gid[:12]}...]")
    if len(groups) > 5:
        print(f"    ... and {len(groups) - 5} more")


def _log_progress(done: int, total: int, _name: str, status: str, secs: float) -> None:
    """CLI progress line. Must match engine.ProgressFn exactly (done, total, name,
    status, secs) — kept in lockstep with the GUI's handler and covered by a test,
    since a silent arity drift here only shows up in unattended scheduled runs."""
    if status == "sent":
        log.info("[%d/%d] sent in %.1fs", done, total, secs)
    elif status == "uncertain":
        log.warning("[%d/%d] timed out after %.0fs — MAY have sent", done, total, secs)
    elif status == "skipped":
        log.info("[%d/%d] skipped — admin-only", done, total)
    else:
        log.info("[%d/%d] FAILED after %.1fs", done, total, secs)


def run(args: argparse.Namespace) -> int:
    cfg = engine.load_config()
    message = engine.read_message(Path(args.message))
    attachments = engine.read_attachments(Path(args.attachments))
    groups = engine.read_groups(Path(args.groups))
    if args.limit is not None:
        groups = groups[: args.limit]
    delay = args.delay if args.delay is not None else cfg.base_delay_seconds

    # Resume an interrupted run instead of re-sending everything. Only when the
    # message+attachments are unchanged (same fingerprint) — otherwise the operator
    # clearly intends a new send, so start fresh.
    interrupted = engine.read_interrupted_run()
    if interrupted:
        if interrupted.fingerprint == engine.message_fingerprint(message, attachments):
            log.warning("Resuming interrupted run: %d of %d groups left (%d already sent, skipped).",
                        len(interrupted.remaining), interrupted.total, interrupted.done)
            groups = interrupted.remaining
        else:
            log.warning("A previous run was interrupted, but the message changed — starting fresh.")
            engine.clear_run_progress()

    if args.dry_run:
        _print_dry_run(cfg, message, attachments, groups, delay)
        return 0

    blocked = engine.cooldown_blocks_run(cfg.cooldown_hours)
    if blocked and not args.force:
        log.info("Skipping run — %s (use --force to override).", blocked)
        return 0

    results = engine.broadcast(
        config=cfg, groups=groups, message=message, attachments=attachments,
        base_delay=args.delay,
        on_log=log.info,
        on_progress=_log_progress,
    )
    engine.stamp_run()
    engine.write_run_summary(results)

    # Uncertain (timed out — may have sent) and skipped (admin-only) groups are NOT
    # written to the resend list: resending an uncertain one could duplicate a
    # message that already went out, and a skipped one would just fail again.
    failed = [r for r in results if not r.ok and not r.skipped and not r.uncertain]
    uncertain = [r for r in results if r.uncertain]
    skipped = [r for r in results if r.skipped]
    sent = len(results) - len(failed) - len(uncertain) - len(skipped)
    log.info("Done. Sent %d, failed %d, uncertain %d, skipped %d.",
             sent, len(failed), len(uncertain), len(skipped))
    if uncertain:
        log.warning("%d group(s) timed out and MAY have sent — left off the resend "
                    "list to avoid duplicates; check Signal before resending.", len(uncertain))
    out = engine.write_failures(failed)
    if out:
        log.warning("Resend the %d failed with: python3 broadcast.py --groups %s",
                    len(failed), out)
    return 1 if (failed or uncertain) else 0


def main() -> int:
    configure_logging()
    try:
        return run(parse_args())
    except engine.BroadcastError as exc:
        log.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        log.info("Stopped by user.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
