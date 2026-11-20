"""Moving an earlier install to the neutral Sudoku layout, with disposable data only."""
import json
import os
import secrets
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import mac_migrate
import mac_security as security
from mac_migrate import MigrationError

PROJECT = Path(__file__).resolve().parent.parent

FAKE_HELPER = textwrap.dedent('''\
    #!{python}
    """Stands in for vendor/mac-security: same arguments, items kept in a JSON file."""
    import json, sys
    from pathlib import Path
    store = Path({store!r})
    items = json.loads(store.read_text()) if store.exists() else {{}}
    operation, service = sys.argv[1], sys.argv[2]
    account = sys.argv[3] if len(sys.argv) == 4 else "vault"
    key = service + "/" + account
    if operation == "get":
        if key not in items:
            sys.exit(44)
        sys.stdout.write(items[key])
    elif operation == "put":
        items[key] = sys.stdin.read()
        store.write_text(json.dumps(items))
    elif operation == "delete":
        if items.pop(key, None) is None:
            sys.exit(44)
        store.write_text(json.dumps(items))
''')


class MigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sb-migrate-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.store = self.base / "keychain.json"
        self.helper = self.base / "helper"
        self.helper.write_text(FAKE_HELPER.format(python=sys.executable, store=str(self.store)))
        self.helper.chmod(0o700)
        self.legacy_root, self.root = self.base / "Signal Broadcast", self.base / "Sudoku"
        self.old_project, self.new_project = self.base / "checkout", self.base / "Sudoku.app/Contents/Resources/app"
        (self.new_project / "logs").mkdir(parents=True)
        (self.new_project / "logs/.gitkeep").touch()
        self.old_keychain = security.Keychain(security.LEGACY_SERVICE, self.helper, account=None)
        self.new_keychain = security.Keychain(security.SERVICE, self.helper)

    def items(self):
        return json.loads(self.store.read_text()) if self.store.exists() else {}

    def migrate(self, old_project=None):
        return mac_migrate.migrate(old_project, new_project=self.new_project, home=self.base,
                                   legacy_root=self.legacy_root, root=self.root,
                                   old_keychain=self.old_keychain, new_keychain=self.new_keychain, system=False)

    def make_vault_install(self):
        (self.legacy_root / security.LEGACY_IMAGE_NAME / "bands").mkdir(parents=True)
        (self.legacy_root / security.LEGACY_IMAGE_NAME / "bands/0").write_bytes(b"ciphertext")
        (self.legacy_root / security.LEGACY_MOUNT_NAME).mkdir()
        (self.legacy_root / "service.lock").touch()
        (self.legacy_root / "erase.json").write_text('{"erasing": true}')
        self.old_keychain.save({"version": 1, "phase": "ready", "failures": 1})

    def test_vault_install_moves_storage_and_keychain_under_neutral_names(self):
        self.make_vault_install()
        self.root.mkdir()
        (self.root / "game.json").write_text("{}")  # A game played before migrating stays.
        done = self.migrate()
        self.assertEqual(done, ["Keychain entry renamed", "encrypted storage moved"])
        self.assertFalse(self.legacy_root.exists())
        self.assertEqual((self.root / security.IMAGE_NAME / "bands/0").read_bytes(), b"ciphertext")
        self.assertTrue((self.root / "game.json").exists())
        self.assertTrue((self.root / "erase.json").exists())  # An unfinished wipe resumes after migrating.
        self.assertFalse((self.root / "service.lock").exists())
        self.assertEqual(oct(self.root.stat().st_mode & 0o777), "0o700")
        self.assertEqual(list(self.items()), [f"{security.SERVICE}/{security.ACCOUNT}"])
        self.assertEqual(self.new_keychain.load()["failures"], 1)
        self.assertEqual(self.migrate(), [])  # Running Setup again changes nothing.

    def test_conflicting_storage_or_keychain_stops_without_overwriting(self):
        self.make_vault_install()
        (self.root / security.IMAGE_NAME).mkdir(parents=True)
        with self.assertRaisesRegex(MigrationError, "Nothing was overwritten"):
            self.migrate()
        self.assertTrue((self.legacy_root / security.LEGACY_IMAGE_NAME).exists())
        self.assertEqual(list(self.items()), [f"{security.LEGACY_SERVICE}/vault"])  # Keychain untouched too.
        self.new_keychain.save({"version": 1, "phase": "ready", "failures": 0})  # Differs from the old item.
        with self.assertRaisesRegex(MigrationError, "differ"):
            mac_migrate.move_keychain(self.old_keychain, self.new_keychain)
        self.assertIn(f"{security.LEGACY_SERVICE}/vault", self.items())

    def test_open_old_storage_is_never_moved(self):
        self.make_vault_install()
        (self.legacy_root / security.LEGACY_MOUNT_NAME / "store").mkdir()
        with self.assertRaisesRegex(MigrationError, "still open"):
            mac_migrate.move_storage(self.legacy_root, self.root)
        self.assertTrue((self.legacy_root / security.LEGACY_IMAGE_NAME).exists())

    def test_interrupted_keychain_copy_resumes(self):
        self.make_vault_install()
        data = self.old_keychain._call("get")
        self.new_keychain._call("put", data)  # Copied, but the old item was not yet deleted.
        self.assertTrue(mac_migrate.move_keychain(self.old_keychain, self.new_keychain))
        self.assertEqual(list(self.items()), [f"{security.SERVICE}/{security.ACCOUNT}"])

    def test_unencrypted_checkout_data_moves_into_the_installed_app(self):
        old = self.old_project
        (old / "signal-cli-data/data").mkdir(parents=True)
        (old / "signal-cli-data/data/account.db").write_bytes(b"link keys")
        (old / "webui-uploads").mkdir()
        (old / "webui-uploads/photo.png").write_bytes(b"photo")
        (old / "logs").mkdir()
        (old / "logs/.gitkeep").touch()
        (old / "logs/activity.txt").write_text("old activity")
        (old / "config.toml").write_text('account = "+19999999999"\n')
        outside = self.base / "elsewhere.png"
        outside.write_bytes(b"outside")
        (old / "attachments.txt").write_text(f"{old / 'webui-uploads/photo.png'}\n{outside}\n")
        (old / "Signal Broadcast.app/Contents").mkdir(parents=True)
        (old / "Signal Broadcast.app/Contents/Info.plist").write_text("<plist/>")
        (old / "README.md").write_text("source stays")
        done = self.migrate(old)
        self.assertIn("5 unprotected file(s) moved for encryption at the next password setup", done)
        new = self.new_project
        self.assertEqual((new / "signal-cli-data/data/account.db").read_bytes(), b"link keys")
        self.assertEqual((new / "logs/activity.txt").read_text(), "old activity")
        self.assertEqual((new / "attachments.txt").read_text().splitlines(),
                         [str(new / "webui-uploads/photo.png"), str(outside)])
        self.assertFalse((old / "signal-cli-data").exists())
        self.assertFalse((old / "config.toml").exists())
        self.assertFalse((old / "Signal Broadcast.app").exists())
        self.assertTrue((old / "README.md").exists())
        # The vault's own migration at password setup now finds everything in the app's folder.
        vault = security.Vault(self.base / "unused", new, keychain=object())
        found = {p.relative_to(new) for root in vault.legacy_paths() for p in security.regular_files(root)}
        self.assertTrue({Path("config.toml"), Path("signal-cli-data/data/account.db")} <= found)


class InstallAppTests(unittest.TestCase):
    def git(self, *args, cwd):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    def test_install_clones_with_the_real_remote_and_updates_in_place(self):
        with tempfile.TemporaryDirectory(prefix="sb-install-") as directory:
            base = Path(directory).resolve()
            source = base / "signal-broadcast"
            source.mkdir()
            self.git("init", "-q", "-b", "main", cwd=source)
            (source / "Setup.command").write_text("#!/bin/bash\n")
            (source / "vendor/signal-cli-9.9").mkdir(parents=True)
            (source / "vendor/signal-cli-9.9/marker").write_text("downloaded")
            self.git("add", "Setup.command", cwd=source)
            self.git("-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qm", "one", cwd=source)
            self.git("remote", "add", "origin", "https://example.invalid/project.git", cwd=source)
            bundle = base / "Applications/Sudoku.app"
            script = PROJECT / "scripts/install-app.sh"
            subprocess.run(["bash", str(script), str(source), str(bundle)], check=True, capture_output=True)
            app = bundle / "Contents/Resources/app"
            self.assertTrue((app / "Setup.command").exists())
            self.assertEqual(self.git("remote", "get-url", "origin", cwd=app), "https://example.invalid/project.git")
            self.assertEqual(self.git("config", "branch.main.merge", cwd=app), "refs/heads/main")
            self.assertEqual((app / "vendor/signal-cli-9.9/marker").read_text(), "downloaded")
            (source / "mac_app.py").write_text("# new\n")
            self.git("add", "mac_app.py", cwd=source)
            self.git("-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qm", "two", cwd=source)
            subprocess.run(["bash", str(script), str(source), str(bundle)], check=True, capture_output=True)
            self.assertTrue((app / "mac_app.py").exists())
            self.assertEqual(self.git("rev-parse", "HEAD", cwd=app), self.git("rev-parse", "HEAD", cwd=source))


@unittest.skipUnless(os.environ.get("SB_RUN_MAC_INTEGRATION") == "1", "Opt-in real encrypted image and Keychain test")
class RealVaultMigrationTests(unittest.TestCase):
    def test_old_encrypted_image_and_keychain_item_open_under_new_names(self):
        helper = PROJECT / "vendor/mac-security"
        tag = secrets.token_hex(4)
        old_keychain = security.Keychain(f"com.user.signal-broadcast.test.{tag}", helper, account=None)
        new_keychain = security.Keychain(f"local.sudoku.test.{tag}", helper)
        def forget():
            for keychain in (old_keychain, new_keychain):
                try:
                    keychain._call("delete")
                except security.SecurityError:
                    pass
        self.addCleanup(forget)
        with tempfile.TemporaryDirectory(prefix="sb-mac-integration-migrate-") as directory:
            base = Path(directory).resolve()
            legacy_root, root = base / "Signal Broadcast", base / "Sudoku"
            legacy_root.mkdir(mode=0o700)
            password = secrets.token_hex(32).encode()
            image = legacy_root / security.LEGACY_IMAGE_NAME
            subprocess.run(["/usr/bin/hdiutil", "create", "-size", "64m", "-type", "SPARSEBUNDLE", "-fs", "APFS",
                            "-volname", "Signal Broadcast", "-encryption", "AES-256", "-stdinpass", str(image)],
                           input=password + b"\0", check=True, capture_output=True)
            old_keychain.save({"version": 1, "phase": "ready", "failures": 0})
            mac_migrate.migrate(None, new_project=PROJECT, home=base, legacy_root=legacy_root, root=root,
                                old_keychain=old_keychain, new_keychain=new_keychain, system=False)
            self.assertIsNone(old_keychain._call("get"))
            vault = security.Vault(root, PROJECT, new_keychain)
            vault.attach(password)
            try:
                info = subprocess.run(["/usr/sbin/diskutil", "info", "-plist", str(vault.mount)],
                                      capture_output=True, check=True)
                import plistlib
                self.assertEqual(plistlib.loads(info.stdout)["VolumeName"], security.VOLUME_NAME)
            finally:
                vault.detach()


if __name__ == "__main__":
    unittest.main()
