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

# Some supported test interpreters have no Tk framework. These state methods do not
# create widgets, so a tiny import shim loads gui.py without opening a window.
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
    def test_busy_link_health_check_is_retried(self):
        app = gui.App.__new__(gui.App)
        app.events = queue.Queue()
        with mock.patch.object(engine, "link_is_broken", return_value=None):
            app._verify_link()
        self.assertEqual(app.events.get_nowait(), ("verify_link_retry", None))

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
        result = engine.UpdateResult(False, "Already current")

        app._handle("update_done", result)

        app._finish_update.assert_called_once_with(result)


class UpdateSafetyTests(unittest.TestCase):
    def test_unplugged_station_prompt_still_offers_update(self):
        app = gui.App.__new__(gui.App)
        app.container = mock.Mock()
        app.destroy = mock.Mock()
        app._disarm_from_prompt = mock.Mock()
        app._check_update = mock.Mock()
        app.after = mock.Mock()
        buttons = [mock.Mock(), mock.Mock(), mock.Mock()]

        with mock.patch.object(gui.ttk, "Frame", return_value=mock.Mock(), create=True), \
             mock.patch.object(gui.ttk, "Label", return_value=mock.Mock(), create=True), \
             mock.patch.object(gui.ttk, "Button", side_effect=buttons, create=True) as button:
            app._show_plug_in_prompt()

        self.assertTrue(any(call.kwargs.get("text") == "Update" for call in button.call_args_list))
        self.assertIs(app.update_btn, buttons[2])
        app.update_btn.pack.assert_called_once()

    def test_active_broadcast_blocks_update_before_git_pull(self):
        app = gui.App.__new__(gui.App)
        app._update_ready = False
        app._sending = True
        app._linking = app._refreshing = app._checking_notes = False
        app.update_btn = mock.Mock()
        app.events = queue.Queue()

        with mock.patch.object(gui.messagebox, "showinfo", create=True) as showinfo, \
             mock.patch.object(engine, "git_pull") as git_pull:
            app._check_update()

        git_pull.assert_not_called()
        app.update_btn.configure.assert_not_called()
        self.assertIn("broadcast", showinfo.call_args.args[1])

    def test_restart_waits_for_background_signal_activity(self):
        app = gui.App.__new__(gui.App)
        app._linking = app._sending = app._refreshing = app._checking_notes = False
        app.update_btn = mock.Mock()

        with mock.patch.object(engine, "signal_cli_operation",
                               side_effect=engine.BroadcastError("busy")), \
             mock.patch.object(gui.messagebox, "showinfo", create=True), \
             mock.patch.object(gui.os, "execv") as execv:
            app._restart()

        execv.assert_not_called()
        app.update_btn.configure.assert_called_once_with(state="normal", text="Restart update")

    def test_update_holds_signal_lease_while_pulling_code(self):
        app = gui.App.__new__(gui.App)
        app._update_ready = False
        app._linking = app._sending = app._refreshing = app._checking_notes = False
        app.update_btn = mock.Mock()
        app.events = queue.Queue()
        operations = []

        @contextmanager
        def lease(operation):
            operations.append(operation)
            yield

        def immediate_thread(*, target, daemon):
            return types.SimpleNamespace(start=target)

        result = engine.UpdateResult(False, "current")
        with mock.patch.object(engine, "signal_cli_operation", lease), \
             mock.patch.object(engine, "git_pull", return_value=result), \
             mock.patch.object(gui.threading, "Thread", side_effect=immediate_thread):
            app._check_update()

        self.assertEqual(operations, ["updating the app"])
        self.assertEqual(app.events.get_nowait(), ("update_done", result))

    def test_dependency_update_requires_setup_instead_of_restart(self):
        app = gui.App.__new__(gui.App)
        app._updating = True
        app._update_ready = False
        app.update_btn = mock.Mock()
        result = engine.UpdateResult(True, "pulled", needs_setup=True)

        with mock.patch.object(gui.messagebox, "showinfo", create=True) as showinfo, \
             mock.patch.object(gui.messagebox, "askyesno", create=True) as askyesno:
            app._finish_update(result)

        askyesno.assert_not_called()
        self.assertFalse(app._update_ready)
        self.assertIn("Setup.command", showinfo.call_args.args[1])


class WipeOnQuitTests(unittest.TestCase):
    def test_busy_wipe_keeps_the_mac_app_open(self):
        app = gui.App.__new__(gui.App)
        app.destroy = mock.Mock()
        config = mock.Mock(wipe_on_close=True)

        with mock.patch.object(engine, "load_config", return_value=config), \
             mock.patch.object(engine, "unlink", side_effect=engine.BroadcastError("busy")), \
             mock.patch.object(gui.messagebox, "askyesno", return_value=True, create=True), \
             mock.patch.object(gui.messagebox, "showerror", create=True) as show_error, \
             mock.patch.object(gui.thumbs, "clear") as clear_thumbs:
            app._quit()

        show_error.assert_called_once()
        app.destroy.assert_not_called()
        clear_thumbs.assert_not_called()


class GroupRefreshTests(unittest.TestCase):
    def test_screen_clear_cancels_a_pending_group_filter(self):
        app = gui.App.__new__(gui.App)
        app._group_render_job = "job-1"
        app.after_cancel = mock.Mock()
        app.container = mock.Mock()
        app.container.winfo_children.return_value = []

        app._clear()

        app.after_cancel.assert_called_once_with("job-1")
        self.assertIsNone(app._group_render_job)

    def test_refresh_can_reuse_snapshot_permissions_without_another_probe(self):
        app = gui.App.__new__(gui.App)
        app._render_groups = mock.Mock()
        app._check_group_perms = mock.Mock()
        entries = [engine.GroupEntry("g1", "One", True)]

        with mock.patch.object(engine, "read_group_entries", return_value=entries):
            app._populate_groups(check_permissions=False)

        app._render_groups.assert_called_once_with()
        app._check_group_perms.assert_not_called()

    def test_startup_uses_stored_permission_labels_without_signal_cli(self):
        app = gui.App.__new__(gui.App)
        app.group_entries = [engine.GroupEntry("g1", "One", True)]
        app.events = queue.Queue()

        def immediate_thread(*, target, daemon):
            return types.SimpleNamespace(start=target)

        with mock.patch.object(engine, "load_config", return_value=mock.Mock(account="+1")), \
             mock.patch.object(engine, "stored_unsendable_groups", return_value={"g1"}), \
             mock.patch.object(engine, "unsendable_groups") as live_probe, \
             mock.patch.object(gui.threading, "Thread", side_effect=immediate_thread):
            app._check_group_perms()

        live_probe.assert_not_called()
        self.assertEqual(app.events.get_nowait(), ("group_perms", {"g1"}))


class _FakeListbox:
    def __init__(self):
        self.items = []
        self.selected = set()
        self.insert_calls = 0
        self.selection_calls = 0

    def delete(self, _first, _last):
        self.items.clear()
        self.selected.clear()

    def insert(self, _where, *labels):
        self.insert_calls += 1
        self.items.extend(labels)

    def selection_set(self, first, last=None):
        self.selection_calls += 1
        last = first if last is None else last
        self.selected.update(range(first, last + 1))

    def selection_clear(self, first, last):
        end = len(self.items) - 1 if last == "end" else last
        self.selected.difference_update(range(first, end + 1))

    def curselection(self):
        return tuple(sorted(self.selected))


class _Value:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value


class _Label:
    def configure(self, **_kwargs):
        pass


class LargeGroupRenderingTests(unittest.TestCase):
    def test_five_thousand_groups_use_one_list_and_keep_selection_across_filters(self):
        app = gui.App.__new__(gui.App)
        app.groups_list = _FakeListbox()
        app.group_search = _Value()
        app.group_count_label = _Label()
        app.group_entries = [engine.GroupEntry(f"g{i}", f"Group {i}", True)
                             for i in range(5000)]
        app._enabled_group_ids = {"g0", "g4999"}
        app._visible_ids = []
        app._group_render_job = None
        app._unsendable_ids = set()

        app._render_groups()
        self.assertEqual(len(app.groups_list.items), 5000)
        self.assertEqual(len(app.groups_list.selected), 2)
        self.assertEqual(app.groups_list.insert_calls, 1)
        self.assertEqual(app.groups_list.selection_calls, 2)

        app.group_search.value = "4999"
        app._render_groups()
        self.assertEqual(app._visible_ids, ["g4999"])
        self.assertEqual(app.groups_list.selected, {0})

        app.group_search.value = ""
        app._render_groups()
        self.assertEqual(app._enabled_group_ids, {"g0", "g4999"})
        self.assertEqual(len(app.groups_list.items), 5000)


if __name__ == "__main__":
    unittest.main()
