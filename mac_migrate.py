"""Move an earlier Mac installation to the neutral Sudoku layout. Run by Setup.

Every step checks what is already done, so an interrupted Setup can simply run
again. Nothing is overwritten: if the old and new locations both hold data, the
migration stops and leaves both in place. All of it stays on this Mac.

    old                                                 new
    ~/Library/Application Support/Signal Broadcast/     ~/Library/Application Support/Sudoku/
        data.sparsebundle, mounted/                         library.sparsebundle, library/
    Keychain com.user.signal-broadcast.vault.v1/vault   local.sudoku.library.v1/library
    launchd com.user.signal-broadcast.service           local.sudoku.helper (Setup installs it)
    runtime files inside the downloaded checkout        the installed app's folder
"""
from __future__ import annotations

import argparse
import filecmp
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import mac_security as security

LEGACY_LABEL = "com.user.signal-broadcast.service"
OLD_LAUNCHERS = ("Signal Broadcast.app", "Sudoku.app")


class MigrationError(Exception):
    pass


def stop_legacy_service(home: Path = Path.home()) -> None:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LEGACY_LABEL}"], capture_output=True)
    (home / "Library/LaunchAgents" / (LEGACY_LABEL + ".plist")).unlink(missing_ok=True)


def detach_legacy_images(legacy_root: Path) -> None:
    """The old service detaches on exit; make sure no old image is still attached."""
    proc = subprocess.run(["/usr/bin/hdiutil", "info", "-plist"], capture_output=True)
    if proc.returncode:
        raise MigrationError("Could not check attached disk images.")
    for image in plistlib.loads(proc.stdout).get("images", []):
        path = Path(image.get("image-path", ""))
        if not path.resolve().is_relative_to(legacy_root.resolve()):
            continue
        for entity in image.get("system-entities", [])[:1]:
            if subprocess.run(["/usr/bin/hdiutil", "detach", entity["dev-entry"]], capture_output=True).returncode:
                raise MigrationError("The old encrypted storage is still in use. "
                                     "Close any Finder window showing it and run Setup again.")


def move_keychain(old: security.Keychain, new: security.Keychain) -> bool:
    """Copy the wrapped key to the neutral item, verify it, then delete the old item."""
    data = old._call("get")
    if data is None:
        return False
    existing = new._call("get")
    if existing is None:
        new._call("put", data)
        if new._call("get") != data:
            raise MigrationError("The Keychain copy could not be verified. The old entry was kept.")
    elif existing != data:
        raise MigrationError("Both the old and the new Keychain entries exist and differ. Nothing was changed.")
    old._call("delete")
    return True


NAMES = {security.LEGACY_IMAGE_NAME: security.IMAGE_NAME, security.LEGACY_MOUNT_NAME: security.MOUNT_NAME}


def check_storage(legacy_root: Path, root: Path) -> list[Path]:
    """Refuse before anything changes if the move could overwrite or catch it open."""
    if not legacy_root.exists():
        return []
    if legacy_root.is_symlink() or root.is_symlink():
        raise MigrationError("Unsafe storage path.")
    entries = list(legacy_root.iterdir())
    for entry in entries:
        if entry.name == security.LEGACY_MOUNT_NAME and (entry.is_mount() or (entry.is_dir() and any(entry.iterdir()))):
            raise MigrationError("The old encrypted storage is still open. Run Setup again in a moment.")
        target = root / NAMES.get(entry.name, entry.name)
        if target.exists() and not (entry.is_dir() and not any(entry.iterdir())) and entry.suffix != ".lock":
            raise MigrationError(f"Both the old and the new storage contain {target.name}. Nothing was overwritten.")
    return entries


def move_storage(legacy_root: Path, root: Path) -> bool:
    """Rename the old storage folder's contents into the neutral folder."""
    entries = check_storage(legacy_root, root)
    if not legacy_root.exists():
        return False
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    for entry in entries:
        target = root / NAMES.get(entry.name, entry.name)
        if entry.name == security.LEGACY_MOUNT_NAME or entry.suffix == ".lock":
            if entry.is_dir():
                entry.rmdir()  # The empty mount point; the vault recreates its own.
            else:
                entry.unlink()  # Lock files hold no data.
            continue
        os.rename(entry, target)
    legacy_root.rmdir()
    return True


def move_checkout_data(old_project: Path, new_project: Path) -> int:
    """Bring runtime files an unmigrated install kept in its checkout. Returns files moved."""
    moved = 0
    for source_root in security.legacy_paths(old_project):
        for source in security.regular_files(source_root):
            relative = source.relative_to(old_project)
            target = new_project / relative
            if relative == Path("logs/.gitkeep"):
                continue  # Tracked file; the installed app has its own.
            if target.exists():
                if filecmp.cmp(source, target, shallow=False):
                    source.unlink()
                    continue
                raise MigrationError(f"{relative} exists in both places. Nothing was overwritten.")
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.move(str(source), str(target))
            moved += 1
        if source_root.is_dir():
            for directory in sorted((p for p in source_root.rglob("*") if p.is_dir()), reverse=True):
                if not any(directory.iterdir()):
                    directory.rmdir()
            if not any(source_root.iterdir()) and source_root != old_project / "logs":
                source_root.rmdir()
    # Draft photo paths that pointed inside the old checkout now point at the moved copies.
    attachments = new_project / "attachments.txt"
    if attachments.is_file():
        lines = []
        for line in attachments.read_text().splitlines():
            path = Path(line.strip())
            if line.strip() and path.is_absolute() and path.is_relative_to(old_project):
                line = str(new_project / path.relative_to(old_project))
            lines.append(line)
        attachments.write_text("\n".join(lines) + "\n")
    return moved


def remove_old_launchers(old_project: Path) -> None:
    for name in OLD_LAUNCHERS:
        bundle = old_project / name
        if (bundle / "Contents/Info.plist").is_file() and not bundle.is_symlink():
            shutil.rmtree(bundle)


def migrate(old_project: Path | None, *, new_project: Path = security.PROJECT, home: Path = Path.home(),
            legacy_root: Path = security.LEGACY_ROOT, root: Path = security.ROOT,
            old_keychain: security.Keychain | None = None, new_keychain: security.Keychain | None = None,
            system: bool = True) -> list[str]:
    done = []
    if system:
        stop_legacy_service(home)
        detach_legacy_images(legacy_root)
    check_storage(legacy_root, root)  # Before the Keychain step, so a conflict changes nothing.
    if old_keychain is None:
        old_helper = old_project / "vendor/mac-security" if old_project else None
        # The old helper created the old item, so reading it there needs no new Keychain approval.
        old_keychain = (security.Keychain(security.LEGACY_SERVICE, old_helper, account=None)
                        if old_helper and old_helper.is_file()
                        else security.Keychain(security.LEGACY_SERVICE, account=security.LEGACY_ACCOUNT))
    if move_keychain(old_keychain, new_keychain or security.Keychain()):
        done.append("Keychain entry renamed")
    if move_storage(legacy_root, root):
        done.append("encrypted storage moved")
    if old_project and old_project.resolve() != new_project.resolve():
        moved = move_checkout_data(old_project, new_project)
        if moved:
            done.append(f"{moved} unprotected file(s) moved for encryption at the next password setup")
        remove_old_launchers(old_project)
    return done


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from", dest="source", type=Path, help="the folder Setup was first run from")
    args = parser.parse_args(argv)
    try:
        done = migrate(args.source.resolve() if args.source else None)
    except (MigrationError, security.SecurityError, OSError) as exc:
        print(f"Migration stopped safely: {exc}", file=sys.stderr)
        return 1
    print("Migration: " + ("; ".join(done) if done else "nothing to move") + ".")
    return 0


if __name__ == "__main__":
    sys.exit(main())
