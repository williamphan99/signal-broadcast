#!/usr/bin/env python3
"""Focused GUI state tests. No Tk window and no real signal-cli are used."""

import queue
import sys
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import engine  # noqa: E402

# The project test interpreter intentionally has no Tk framework. The state methods
# below do not create widgets, so a tiny import shim is enough to load gui.py without
# opening a window or requiring a display.
try:
    import tkinter  # noqa: F401
except (ImportError, ModuleNotFoundError):
    class _TkBase:
        pass

    tkinter_stub = types.ModuleType("tkinter")
    tkinter_stub.Label = _TkBase
    tkinter_stub.Tk = _TkBase
    tkinter_stub.filedialog = types.SimpleNamespace()
    tkinter_stub.messagebox = types.SimpleNamespace()
    tkinter_stub.ttk = types.SimpleNamespace(Frame=_TkBase)
    sys.modules["tkinter"] = tkinter_stub
import gui  # noqa: E402


class _FinishedLinkProcess:
    def __init__(self):
        self.stdout = iter([
            "sgnl://linkdevice?uuid=test\n",
            "Provisioning message received\n",
        ])
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


class LinkStateTests(unittest.TestCase):
    def test_link_worker_uses_the_shared_signal_operation_lease(self):
        app = gui.App.__new__(gui.App)
        app.events = queue.Queue()
        app._link_worker = mock.Mock()
        operations = []

        @contextmanager
        def lease(operation):
            operations.append(operation)
            yield

        with mock.patch.object(engine, "signal_cli_operation", lease):
            app._link_worker_serialized()

        self.assertEqual(operations, ["linking"])
        app._link_worker.assert_called_once_with()

    def test_successful_link_is_not_held_hostage_by_group_sync(self):
        app = gui.App.__new__(gui.App)
        app.events = queue.Queue()
        app._linklog = mock.Mock()

        with mock.patch.object(engine, "qrencode_bin", return_value="qrencode"), \
             mock.patch.object(engine, "signal_cli_command", return_value=(["signal-cli"], {})), \
             mock.patch.object(engine, "wait_for_account", return_value="+61400000000"), \
             mock.patch.object(engine, "save_account") as save_account, \
             mock.patch.object(engine, "sync_groups") as sync_groups, \
             mock.patch.object(gui.subprocess, "Popen", return_value=_FinishedLinkProcess()), \
             mock.patch.object(gui.subprocess, "run", return_value=mock.Mock(returncode=0)):
            app._link_worker()

        save_account.assert_called_once_with("+61400000000")
        sync_groups.assert_not_called()
        events = list(app.events.queue)
        self.assertIn(("linked_done", None), events)
        self.assertFalse(any(kind == "link_error" for kind, _ in events))

    def test_link_completion_enters_main_screen_then_starts_retryable_refresh(self):
        app = gui.App.__new__(gui.App)
        app._screen = "link"
        app._stop_link_progress = mock.Mock()
        app.show_main = mock.Mock()
        app._refresh_groups = mock.Mock()

        app._handle("linked_done", None)

        app._stop_link_progress.assert_called_once_with()
        app.show_main.assert_called_once_with()
        app._refresh_groups.assert_called_once_with()

    def test_update_completion_is_handled_while_unlinked(self):
        app = gui.App.__new__(gui.App)
        app._screen = "link"
        app._finish_update = mock.Mock()
        result = (False, "Already current")

        app._handle("update_done", result)

        app._finish_update.assert_called_once_with(result)


if __name__ == "__main__":
    unittest.main()
