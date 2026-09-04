"""Authenticated terminal client. Never runs signal-cli or opens the vault itself."""
import argparse
import getpass
import resource
import sys

from mac_security import SecurityError
from mac_service import Client


def main():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    parser = argparse.ArgumentParser(description="Signal Broadcast protected Mac client")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--link", action="store_true")
    group.add_argument("--sync", action="store_true")
    group.add_argument("--notes", action="store_true")
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if not sys.stdin.isatty():
        raise SecurityError("Use the app's saved schedule for unattended sending.")
    client = Client()
    if client.call("status")["setup_required"]:
        raise SecurityError("Open the Mac app to set your password before linking.")
    client.call("unlock", password=getpass.getpass("Signal Broadcast password: "))
    try:
        if args.dry_run:
            data = client.call("snapshot")
            print(f"Selected groups: {sum(g['enabled'] for g in data['groups'])}")
            print(f"Attachments: {len(data['attachments'])}")
            print(data["message"])
            return
        kind = "link" if args.link else "sync" if args.sync else "notes" if args.notes else "resume" if args.resume else "send"
        if kind in ("send", "resume") and input("Send the saved draft to the selected groups? Type SEND: ") != "SEND":
            return
        client.call("job", kind=kind)
        print("Started in the local service. Open the app to view the QR or progress.")
    finally:
        client.call("lock")


if __name__ == "__main__":
    try:
        main()
    except SecurityError as exc:
        sys.exit(str(exc))
