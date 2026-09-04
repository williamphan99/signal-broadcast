"""Protected command arguments, with all Signal subprocesses replaced locally."""
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import engine
import mac_worker
from runtime import isolated_engine


class PrivateCommandTests(unittest.TestCase):
    def setUp(self):
        scope = isolated_engine()
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)

    def test_sync_notes_and_permission_commands_keep_account_out_of_private_argv(self):
        account = "+19999999999"
        for private in (True, False):
            with self.subTest(private=private):
                argv = []
                def run(command, **kwargs):
                    argv.append(command)
                    return mock.Mock(returncode=0, stdout="[]", stderr="")
                def catalog(command, *args):
                    argv.append(command)
                    return mock.Mock(returncode=0, stdout='[{"id":"fixture","name":"Disposable group"}]', stderr="")
                def popen(command, **kwargs):
                    argv.append(command)
                    return mock.Mock(returncode=0, stdout=io.StringIO(""), stderr=io.StringIO(""))
                with mock.patch.object(engine, "PRIVATE_TRANSPORT", private), \
                     mock.patch.object(engine, "signal_cli_bin", return_value="/disposable/signal-cli"), \
                     mock.patch.object(engine, "_signal_env", return_value={}), \
                     mock.patch.object(engine, "_run_group_catalog_read", side_effect=catalog), \
                     mock.patch("engine.subprocess.run", side_effect=run), \
                     mock.patch("engine.subprocess.Popen", side_effect=popen):
                    self.assertEqual(engine.sync_groups(account), 1)
                    self.assertEqual(engine.unsendable_groups(account), set())
                    self.assertEqual(engine.fetch_notes(account)["new"], 0)
                self.assertTrue(all(account not in command if private else account in command for command in argv))
                for operation in ("sendSyncRequest", "receive", "listGroups"):
                    self.assertTrue(any(operation in command for command in argv))

    def test_private_worker_rejects_mismatched_account_before_sync_or_notes(self):
        with tempfile.TemporaryDirectory(prefix="sb-private-worker-") as temporary:
            root = Path(temporary) / "mounted" / "store"
            root.mkdir(parents=True)
            for kind in ("sync", "notes"):
                with self.subTest(kind=kind), mock.patch("pathlib.Path.is_mount", return_value=True), \
                     mock.patch.dict(mac_worker.os.environ), \
                     mock.patch.object(engine, "load_config", return_value=mock.Mock(account="+19999999999")), \
                     mock.patch.object(engine, "detect_account", return_value="+18888888888"), \
                     mock.patch.object(engine, "sync_groups") as sync, \
                     mock.patch.object(engine, "fetch_notes") as notes:
                    with self.assertRaisesRegex(engine.BroadcastError, "does not match"):
                        mac_worker.run({"root": str(root), "job": kind})
                    sync.assert_not_called()
                    notes.assert_not_called()

    def test_private_account_detection_rejects_ambiguous_legacy_store(self):
        accounts = '[{"number":"+19999999999"},{"number":"+18888888888"}]'
        with mock.patch.object(engine, "signal_cli_bin", return_value="/disposable/signal-cli"), \
             mock.patch.object(engine, "_signal_env", return_value={}), \
             mock.patch("engine.subprocess.run", return_value=mock.Mock(returncode=0, stdout=accounts)):
            self.assertEqual(engine.detect_account(), "+19999999999")
            with self.assertRaisesRegex(engine.BroadcastError, "Multiple local Signal accounts"):
                engine.detect_account(require_single=True)
