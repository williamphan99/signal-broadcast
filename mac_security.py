"""Mac vault storage. All destructive operations are scoped to an explicit vault.

Screen locking leaves the vault open for background jobs. This is not an OS
sandbox or a hardware retry counter; a modified program can bypass local policy.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import plistlib
import secrets
import shutil
import stat
import subprocess
import tempfile
import tomllib
from contextlib import contextmanager
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
ROOT = Path.home() / "Library/Application Support/Signal Broadcast"
HELPER = PROJECT / "vendor/mac-security"
SERVICE = "com.user.signal-broadcast.vault.v1"
LEGACY_NAMES = (
    "signal-cli-data", "config.toml", "groups.txt", "group-permissions.json",
    "message.txt", "attachments.txt", "notes.json", "notes.corrupt.json",
    "webui-uploads", "logs", "groups.lock", "notes.lock",
)
AAD = b"signal-broadcast-vault-v1"


@contextmanager
def dispatch_guard(root: Path):
    """Serialize the start of a dispatch against the service's erase marker."""
    with (root / "dispatch.lock").open("a") as lease:
        fcntl.flock(lease, fcntl.LOCK_SH)
        if (root / "erase.json").exists():
            raise SecurityError("Erasure has started. Dispatch is disabled.")
        yield


class SecurityError(Exception):
    pass


class WrongPassword(SecurityError):
    def __init__(self, remaining: int):
        self.remaining = remaining
        super().__init__(f"Incorrect password. {remaining} attempts remain.")


def atomic_json(path: Path, value) -> None:
    atomic_bytes(path, json.dumps(value).encode())


def atomic_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".pending")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Keychain:
    def __init__(self, service: str = SERVICE, helper: Path = HELPER):
        self.service, self.helper = service, helper

    def _call(self, operation: str, data: bytes | None = None):
        try:
            proc = subprocess.run([str(self.helper), operation, self.service], input=data,
                                  capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SecurityError("Mac Keychain is unavailable. Run Setup.command if needed.") from exc
        if proc.returncode == 44:
            return None
        if proc.returncode:
            raise SecurityError("Mac Keychain denied access. No password attempt was counted.")
        return proc.stdout

    def load(self) -> dict | None:
        data = self._call("get")
        if data is None:
            return None
        try:
            value = json.loads(data)
            if value["version"] != 1 or not 0 <= value["failures"] <= 3:
                raise ValueError()
            if value.get("phase") not in ("migrating", "ready", "erasing"):
                raise ValueError()
            return value
        except (ValueError, KeyError, TypeError) as exc:
            raise SecurityError("Security state is unreadable. Access remains locked.") from exc

    def save(self, record: dict) -> None:
        self._call("put", json.dumps(record).encode())

    def delete(self) -> None:
        self._call("delete")


def password_key(password: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
    return Argon2id(salt=salt, length=32, iterations=3, lanes=4,
                    memory_cost=64 * 1024).derive(password.encode("utf-8"))


def wrap_password(password: str, volume_password: bytes) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if len(password) < 12 or len(password.encode("utf-8")) > 4096:
        raise SecurityError("Use a password with at least 12 characters (maximum 4096 UTF-8 bytes).")
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    wrapped = AESGCM(password_key(password, salt)).encrypt(nonce, volume_password, AAD)
    return {"version": 1, "phase": "migrating", "failures": 0,
            "salt": base64.b64encode(salt).decode(), "nonce": base64.b64encode(nonce).decode(),
            "wrapped": base64.b64encode(wrapped).decode()}


def unwrap_password(password: str, record: dict) -> bytes:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        salt, nonce, wrapped = [base64.b64decode(record[k], validate=True)
                                for k in ("salt", "nonce", "wrapped")]
        if len(salt) != 16 or len(nonce) != 12 or len(wrapped) != 80:
            raise ValueError()
    except (KeyError, ValueError, TypeError) as exc:
        raise SecurityError("Security state is damaged. Access remains locked.") from exc
    try:
        return AESGCM(password_key(password, salt)).decrypt(nonce, wrapped, AAD)
    except InvalidTag as exc:
        raise WrongPassword(max(0, 2 - record["failures"])) from exc


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def regular_files(root: Path) -> list[Path]:
    if root.is_symlink():
        raise SecurityError("Symlinks cannot be migrated or erased automatically.")
    if not root.exists():
        return []
    if root.is_file():
        return [root]
    result = []
    for path in root.rglob("*"):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise SecurityError("Unsupported file in local data. Migration stopped safely.")
        if path.is_file():
            result.append(path)
    return result


def identity(path: Path) -> list[int]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or any(p.is_symlink() for p in path.parents):
        raise SecurityError("Choose a regular local file, not a symlink.")
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def copy_import(source: Path, destination_dir: Path) -> tuple[Path, dict]:
    source = source.absolute()
    before = identity(source)
    destination_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = destination_dir / (secrets.token_hex(8) + "-" + source.name[-180:])
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as src, destination.open("xb") as dest:
        info = os.fstat(src.fileno())
        if [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns] != before:
            raise SecurityError("Source changed. Import stopped.")
        shutil.copyfileobj(src, dest)
        dest.flush()
        os.fsync(dest.fileno())
    if identity(source) != before or digest(source) != digest(destination):
        raise SecurityError("Import verification failed. The original was retained.")
    return destination, {"source": str(source), "identity": before, "hash": digest(destination)}


def delete_original(receipt: dict) -> None:
    source = Path(receipt["source"])
    if not source.exists() and not source.is_symlink():
        return
    if identity(source) != receipt["identity"] or digest(source) != receipt["hash"]:
        raise SecurityError("Original changed after import. It was not deleted.")
    source.unlink()


class Vault:
    def __init__(self, root: Path = ROOT, project: Path = PROJECT, keychain=None):
        self.root, self.project = root, project
        self.image = root / "data.sparsebundle"
        self.mount = root / "mounted"
        self.data = self.mount / "store"
        self.marker = root / "erase.json"
        self.keychain = keychain or Keychain()

    def legacy_paths(self):
        paths = [self.project / name for name in LEGACY_NAMES]
        for pattern in ("groups.txt.*.tmp", "group-permissions.json.*.tmp", "notes.json*.tmp"):
            paths.extend(self.project.glob(pattern))
        return paths

    def clear_old_previews(self):
        if self.project != PROJECT:
            return  # Disposable migrations never touch the real user's old caches.
        directory = Path(tempfile.gettempdir())
        for path in [directory / "sb-link-qr.png", *directory.glob("signal-broadcast-thumbs-*")]:
            if not path.exists():
                continue
            if path.is_symlink() or path.stat().st_uid != os.getuid():
                raise SecurityError("Cannot safely remove an old preview cache.")
            regular_files(path)
            shutil.rmtree(path) if path.is_dir() else path.unlink()

    def queue_original(self, source: Path):
        receipt = {"source": str(source.absolute()), "identity": identity(source), "hash": digest(source)}
        record = self.keychain.load()
        pending = record.setdefault("pending_originals", [])
        if receipt not in pending:
            if len(pending) >= 100:
                raise SecurityError("Finish or erase incomplete imports before adding more images.")
            pending.append(receipt)
            self.keychain.save(record)
        return receipt

    def forget_original(self, receipt):
        record = self.keychain.load()
        record["pending_originals"] = [item for item in record.get("pending_originals", []) if item != receipt]
        self.keychain.save(record)

    def prepare(self):
        if self.root.is_symlink() or self.mount.is_symlink() or self.image.is_symlink():
            raise SecurityError("Unsafe vault path.")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    def _disk(self, args: list[str], password: bytes | None = None):
        try:
            proc = subprocess.run(["/usr/bin/hdiutil", *args], capture_output=True,
                                  input=password + b"\0" if password is not None else None, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SecurityError("Vault operation did not finish. Data remains protected.") from exc
        if proc.returncode:
            raise SecurityError("Vault operation failed. Check disk space and permissions.")
        return proc.stdout

    def attached_device(self) -> str | None:
        info = plistlib.loads(self._disk(["info", "-plist"]))
        for item in info.get("images", []):
            if Path(item.get("image-path", "")).resolve() == self.image.resolve():
                entities = item.get("system-entities", [])
                return entities[0]["dev-entry"] if entities else None
        return None

    def create_image(self, password: bytes):
        self.prepare()
        if not self.image.exists():
            self._disk(["create", "-size", "1t", "-type", "SPARSEBUNDLE", "-fs", "APFS",
                        "-volname", "Signal Broadcast", "-encryption", "AES-256", "-stdinpass",
                        "-nospotlight", str(self.image)], password)
        self.attach(password)

    def attach(self, password: bytes):
        self.prepare()
        if self.attached_device():
            self.detach()
        self.mount.mkdir(mode=0o700, exist_ok=True)
        if any(self.mount.iterdir()):
            raise SecurityError("Mount directory is not empty. Refusing to hide local files.")
        self._disk(["attach", str(self.image), "-stdinpass", "-mountpoint", str(self.mount),
                    "-nobrowse", "-owners", "on", "-plist"], password)
        if not self.mount.is_mount():
            raise SecurityError("Vault did not mount. Access denied.")
        os.chmod(self.mount, 0o700)
        self.data.mkdir(mode=0o700, exist_ok=True)
        for path in (self.root, self.data):
            proc = subprocess.run(["/usr/bin/tmutil", "addexclusion", str(path)], capture_output=True)
            if proc.returncode:
                self.detach()
                raise SecurityError("Could not exclude the vault from Time Machine. Access denied.")
        (self.mount / ".metadata_never_index").touch(mode=0o600)

    def detach(self):
        device = self.attached_device()
        if device:
            self._disk(["detach", device])
        if self.attached_device() or self.mount.is_mount():
            raise SecurityError("Vault is still mounted. Locking is incomplete.")

    def verify_tree(self, hashes: dict):
        for name, expected in hashes.items():
            path = self.data / name
            if not path.is_file() or path.is_symlink() or digest(path) != expected:
                raise SecurityError("Migrated data failed verification. Originals retained.")

    def migrate(self, password: bytes):
        receipt_file = self.data / ".migration.json"
        if receipt_file.exists():
            receipt = json.loads(receipt_file.read_text())
        else:
            receipt_file.with_name(receipt_file.name + ".pending").unlink(missing_ok=True)
            sources = []
            for legacy in self.legacy_paths():
                for source in regular_files(legacy):
                    if source == self.project / "logs/.gitkeep":
                        continue
                    relative = source.relative_to(self.project)
                    dest = self.data / relative
                    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    shutil.copyfile(source, dest)
                    sources.append({"source": str(source), "identity": identity(source), "hash": digest(dest)})
            # Rewrite actual note paths while preserving the user's message text.
            def remap_notes(value):
                if isinstance(value, list):
                    return [remap_notes(item) for item in value]
                if isinstance(value, dict):
                    result = {key: remap_notes(item) for key, item in value.items()}
                    raw = result.get("path")
                    if isinstance(raw, str) and Path(raw).is_relative_to(self.project):
                        result["path"] = str(self.data / Path(raw).relative_to(self.project))
                    return result
                return value
            for name in ("notes.json", "notes.corrupt.json"):
                path = self.data / name
                if path.exists():
                    try:
                        value = json.loads(path.read_text())
                    except ValueError:
                        continue  # Preserve corrupt legacy bytes for recovery.
                    atomic_json(path, remap_notes(value))
            attachments = self.data / "attachments.txt"
            if attachments.exists():
                paths = []
                for raw in attachments.read_text().splitlines():
                    if not raw.strip() or raw.lstrip().startswith("#"):
                        continue
                    path = Path(raw.strip())
                    if not path.is_absolute():
                        path = self.project / path
                    if path.is_relative_to(self.project):
                        migrated = self.data / path.relative_to(self.project)
                        if migrated.is_file():
                            path = migrated
                    if not path.is_relative_to(self.data):
                        queued = self.queue_original(path)
                        dest, original = copy_import(path, self.data / "imports")
                        if queued != original:
                            raise SecurityError("Source changed during migration. Originals retained.")
                        sources.append(original)
                        path = dest
                    if not path.is_file():
                        raise SecurityError("An attachment is missing. Restore it before migrating.")
                    paths.append(str(path))
                attachments.write_text("\n".join(paths) + "\n")
            config = self.data / "config.toml"
            if config.exists() and not (self.data / "schedule.json").exists():
                settings = tomllib.loads(config.read_text())
                atomic_json(self.data / "schedule.json", {
                    "enabled": bool(self.keychain.load().get("legacy_schedule_enabled")),
                    "times": settings.get("send_times", []), "last": ""})
            hashes = {str(p.relative_to(self.data)): digest(p) for p in regular_files(self.data)}
            receipt = {"sources": sources, "hashes": hashes}
            atomic_json(receipt_file, receipt)
        self.detach()
        self.attach(password)
        self.verify_tree(receipt["hashes"])
        pending = self.keychain.load().get("pending_originals", [])
        for item in receipt["sources"]:
            delete_original(item)
            if item in pending:
                self.forget_original(item)
        # Remove only empty legacy directories. A new/unrecognised file stops cleanup.
        for path in self.legacy_paths():
            if path.is_dir():
                for directory in sorted((p for p in path.rglob("*") if p.is_dir()), reverse=True):
                    directory.rmdir()
                if not any(path.iterdir()):
                    path.rmdir()
        self.clear_old_previews()
        record = self.keychain.load()
        record["phase"] = "ready"
        self.keychain.save(record)
        receipt_file.unlink(missing_ok=True)

    def import_image(self, source: Path) -> str:
        if source.absolute().is_relative_to(self.data):
            raise SecurityError("This file is already managed by the app.")
        queued = self.queue_original(source)
        dest, receipt = copy_import(source, self.data / "imports")
        if receipt != queued:
            raise SecurityError("Source changed during import. Both copies were retained.")
        journal = self.data / "imports" / (dest.name + ".import.json")
        atomic_json(journal, {"original": receipt, "destination": dest.name})
        self._register_import(dest)
        delete_original(receipt)
        self.forget_original(queued)
        journal.unlink()
        return str(dest)

    def _register_import(self, destination):
        attachments = self.data / "attachments.txt"
        paths = attachments.read_text().splitlines() if attachments.exists() else []
        if str(destination) not in paths:
            paths.append(str(destination))
            atomic_bytes(attachments, ("\n".join(paths) + "\n").encode())

    def recover_imports(self):
        for journal in (self.data / "imports").glob("*.import.json"):
            receipt = json.loads(journal.read_text())
            destination = self.data / "imports" / receipt["destination"]
            if destination.parent != self.data / "imports" or destination.is_symlink() or digest(destination) != receipt["original"]["hash"]:
                raise SecurityError("An interrupted import could not be verified. Originals retained.")
            self._register_import(destination)
            delete_original(receipt["original"])
            self.forget_original(receipt["original"])
            journal.unlink()

    def erase(self):
        self.prepare()
        atomic_json(self.marker, {"erasing": True})
        record = self.keychain.load()
        pending = record.get("pending_originals", []) if record else []
        legacy_imports = not record or record["phase"] == "migrating" or record.get("legacy_imports", False)
        # Replace the record first: the wrapped vault key is gone even if later
        # deletion of an original or an unmount fails. Keep only cleanup receipts.
        if record:
            self.keychain.save({"version": 1, "phase": "erasing", "failures": 3,
                                "pending_originals": pending, "legacy_imports": legacy_imports})
        self.detach()
        if self.image.exists():
            shutil.rmtree(self.image)
        for original in pending:
            delete_original(original)
        attachments = self.project / "attachments.txt"
        if legacy_imports and attachments.is_file() and not attachments.is_symlink():
            for line in attachments.read_text().splitlines():
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                source = Path(line.strip())
                if not source.is_absolute():
                    source = self.project / source
                if source.exists():
                    delete_original({"source": str(source), "identity": identity(source), "hash": digest(source)})
        for path in self.legacy_paths():
            for file in regular_files(path):
                if file != self.project / "logs/.gitkeep":
                    file.unlink()
            if path.is_dir():
                for directory in sorted((p for p in path.rglob("*") if p.is_dir()), reverse=True):
                    directory.rmdir()
                if not any(path.iterdir()):
                    path.rmdir()
        self.clear_old_previews()
        self.keychain.delete()
        self.marker.unlink()
