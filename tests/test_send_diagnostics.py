"""Always-on send diagnostics with disposable storage and no Signal traffic."""
import ast
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import engine
from runtime import isolated_engine


class SendDiagnosticTests(unittest.TestCase):
    def setUp(self):
        scope = isolated_engine()
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)

    def test_retry_and_recovery_are_logged_without_private_inputs(self):
        for debug in (False, True):
            with self.subTest(debug=debug):
                events = []
                transport = mock.Mock(side_effect=[
                    (False, False, 'connection reset +19999999999 Secret Group private-message /private/photo.jpg'),
                    (True, False, '')])
                with mock.patch.object(engine, '_interruptible_sleep'):
                    result = engine._deliver_to_group(transport, 'private-group-id', 'private-message',
                        ['/private/photo.jpg'], 0, lambda _: None, lambda: False, debug,
                        run='test-run', position=17, on_diagnostic=events.append)
                self.assertEqual(result, ('sent', ''))
                self.assertEqual([e['status'] for e in events], ['error', 'sent'])
                self.assertEqual([e['attempt'] for e in events], [1, 2])
                self.assertEqual(events[0]['reason'], 'network or connection problem')
                text = (engine.LOGS_DIR / 'send-diagnostics.jsonl').read_text()
                for private in ('+19999999999', 'Secret Group', 'private-message', 'private-group-id', '/private/photo.jpg'):
                    self.assertNotIn(private, text)
                self.assertFalse(list(engine.LOGS_DIR.glob('debug-*')))

    def test_uncertain_send_is_logged_without_retry(self):
        send = mock.Mock(return_value=(False, False, 'daemon timed out after 900s private-id'))
        result = engine._deliver_to_group(send, 'private-id', 'secret', [], 4,
                                         lambda _: None, lambda: False)
        self.assertEqual(result[0], 'uncertain')
        send.assert_called_once()
        entry = json.loads((engine.LOGS_DIR / 'send-diagnostics.jsonl').read_text())
        self.assertEqual(entry['status'], 'uncertain')

    def test_parallel_records_and_rotation(self):
        path = engine.LOGS_DIR / 'send-diagnostics.jsonl'
        path.write_text('x' * (256 * 1024))
        def log(position):
            engine._send_diagnostic(run='test', position=position, attempt=1, seconds=1,
                                    ok=False, err='429 private-id', on_diagnostic=lambda _: None)
        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(log, range(50)))
        entries = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(len(entries), 50)
        self.assertEqual({e['position'] for e in entries}, set(range(50)))
        self.assertEqual({e['reason'] for e in entries}, {'rate limited'})
        self.assertTrue((engine.LOGS_DIR / 'send-diagnostics.previous.jsonl').exists())

    def test_disk_error_does_not_fail_send_and_is_reported(self):
        events = []
        with mock.patch('pathlib.Path.open', side_effect=OSError('private-path')):
            result = engine._deliver_to_group(lambda *args: (True, False, ''), 'private-id',
                'secret', [], 0, lambda _: None, lambda: False, on_diagnostic=events.append)
        self.assertEqual(result, ('sent', ''))
        self.assertTrue(events[0]['log_write_failed'])
        self.assertNotIn('private-path', json.dumps(events))

    def test_daemon_fallback_does_not_expose_raw_error(self):
        events = []
        with mock.patch.object(engine, 'SignalCliDaemon', side_effect=engine.BroadcastError('connection private-id')), \
             mock.patch.object(engine, '_reap_orphan_signal_cli', return_value=False):
            self.assertIsNone(engine._start_daemon('private-account', events.append, True))
        self.assertIn('network or connection problem', events[-1])
        self.assertNotIn('private-id', str(events))

    def test_mac_activity_shows_diagnostics_but_still_ignores_raw_log_events(self):
        source = ast.parse((Path(__file__).resolve().parents[1] / 'mac_app.py').read_text())
        loop = next(node for node in ast.walk(source) if isinstance(node, ast.For)
                    and ast.unparse(node.iter) == "data['events']")
        module = ast.fix_missing_locations(ast.Module(body=[loop], type_ignores=[]))
        shown = []
        ui = SimpleNamespace(sequence=0, screen='main', add_activity=shown.append)
        events = [{'id': 1, 'kind': 'log', 'value': 'raw private-message'},
                  {'id': 2, 'kind': 'send_diagnostic', 'value': {
                      'position': 17, 'attempt': 3, 'seconds': 5.0,
                      'status': 'error', 'reason': 'rate limited'}}]
        exec(compile(module, 'mac_app.py event handler', 'exec'),
             {'self': ui, 'data': {'events': events}})
        self.assertEqual(shown, ['Group position 17, attempt 3: error after 5.0s. rate limited.'])
