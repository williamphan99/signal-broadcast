"""Response revocation races with disposable stores and in-memory socket streams."""
import io
import json
import os
import queue
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from mac_service import Service, serve
from mac_security import SecurityError
from runtime import isolated_engine
from test_mac_security import DirectoryVault, MemoryKeychain, PASSWORD


class ResponseRevocationTests(unittest.TestCase):
    def setUp(self):
        scope = isolated_engine()
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        temporary = tempfile.TemporaryDirectory(prefix="sb-ipc-review-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        project = root / "project"
        project.mkdir()
        (project / "message.txt").write_text("PRIVATE RESPONSE SENTINEL")
        self.service = Service(DirectoryVault(root / "vault", project, MemoryKeychain()), retire=lambda _: None)
        self.token = self.service.handle({"op": "setup", "password": PASSWORD})["token"]
        with mock.patch("mac_service.socketserver.ThreadingUnixStreamServer.__init__", return_value=None) as create, \
             mock.patch("mac_service.os.chmod"):
            serve(self.service, root / "unused.sock")
        self.handler = create.call_args.args[1]

    def response_during_lock(self, request, match):
        handler = object.__new__(self.handler)
        handler.connection = mock.Mock()
        handler.connection.getpeereid.return_value = (os.getuid(), 0)
        handler.rfile = io.BytesIO(json.dumps(request).encode() + b"\n")
        handler.wfile = io.BytesIO()
        serialized = threading.Event()
        release = threading.Event()
        dumps = json.dumps

        def pause(value, *args, **kwargs):
            result = dumps(value, *args, **kwargs)
            if isinstance(value, dict) and match(value.get("result", {})):
                serialized.set()
                release.wait(5)
            return result

        with mock.patch("mac_service.json.dumps", side_effect=pause):
            worker = threading.Thread(target=handler.handle)
            worker.start()
            try:
                self.assertTrue(serialized.wait(5))
                with self.service.mutex:
                    self.service.lock()
                self.assertEqual(self.service.status()["state"], "screen_locked")
            finally:
                release.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
        response = handler.wfile.getvalue().decode()
        self.assertNotIn("PRIVATE RESPONSE SENTINEL", response)
        self.assertNotIn(self.token, response)
        self.assertEqual(json.loads(response)["error_code"], "locked")

    def test_pending_snapshot_is_rejected_after_lock(self):
        self.response_during_lock({"op": "snapshot", "token": self.token}, lambda result: "message" in result)

    def test_pending_authentication_token_is_rejected_after_lock(self):
        self.response_during_lock({"op": "unlock", "password": PASSWORD}, lambda result: "token" in result)

    @unittest.skipUnless(os.environ.get("SB_RUN_MAC_IPC") == "1", "Opt-in local socket backpressure test")
    def test_stalled_response_reader_cannot_indefinitely_delay_lock(self):
        writing, sent = threading.Event(), threading.Event()
        sender, reader = socket.socketpair()
        with sender, reader, mock.patch.object(self.service, "snapshot", return_value={"message": "x" * (2 * 1024 * 1024)}):
            sender.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            handler = object.__new__(self.handler)
            handler.connection = sender
            handler.rfile = io.BytesIO(json.dumps({"op": "snapshot", "token": self.token}).encode() + b"\n")
            def write(payload):
                writing.set()
                sender.sendall(payload)
                sent.set()
            handler.wfile = mock.Mock(write=write)
            worker = threading.Thread(target=handler.handle)
            worker.start()
            try:
                self.assertTrue(writing.wait(5))
                started = time.monotonic()
                with self.service.mutex:
                    self.service.lock()
                self.assertLess(time.monotonic() - started, 3)
                self.assertFalse(sent.is_set())
            finally:
                reader.close()
                worker.join(5)
            self.assertFalse(worker.is_alive())

    def test_ui_clears_access_using_error_category(self):
        from mac_app import App
        for code in ("locked", "unavailable"):
            with self.subTest(code=code):
                app = mock.Mock(generation=0, screen="main", responses=queue.Queue())
                app.responses.put((0, None, None, SecurityError("Reworded explanation", code=code)))
                App.drain(app)
                app.show_login.assert_called_once_with("Reworded explanation")

    def test_close_revokes_authentication_that_finishes_after_window_is_destroyed(self):
        from mac_app import App
        started, release, locked = threading.Event(), threading.Event(), threading.Event()
        app = mock.Mock(generation=0, responses=queue.Queue(), closing=threading.Event(), authentication=None)
        app.client.token = None

        class PendingClient:
            def __init__(self, *args, **kwargs):
                self.token = None

            def call(self, operation, **values):
                if operation == "unlock":
                    started.set()
                    release.wait(5)
                    self.token = "late authentication"
                    return {"token": self.token}
                if operation == "lock":
                    self.token = None
                    locked.set()
                    return {"locked": True}
                raise AssertionError(operation)

        with mock.patch("mac_app.Client", PendingClient), \
             mock.patch("mac_app.threading.Thread", wraps=threading.Thread) as thread:
            App.request(app, "unlock", password=PASSWORD)
            try:
                self.assertTrue(started.wait(5))
                App.close(app)
                app.destroy.assert_called_once()
                self.assertFalse(thread.call_args.kwargs["daemon"])
            finally:
                release.set()
            self.assertTrue(locked.wait(5))
        self.assertIsNone(app.authentication.token)
        self.assertTrue(app.responses.empty())

    def test_close_revokes_queued_authentication_before_ui_consumes_result(self):
        from mac_app import App
        app = mock.Mock(generation=0, closing=threading.Event())
        app.client.token = None
        app.authentication.token = "queued authentication"
        with mock.patch("mac_app.Client") as client:
            App.close(app)
        self.assertEqual(client.return_value.token, "queued authentication")
        client.return_value.call.assert_called_once_with("lock")
