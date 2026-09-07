"""Recovery regressions using sanitised provider errors and disposable storage."""
import json
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import engine
import mac_retry
from runtime import isolated_engine

UPLOAD_RETRY = ('org.signal.libsignal.net.RetryLaterException: Retry after 4 seconds '
                '(AttachmentInvalidException) (UnexpectedErrorException)')
NOT_MEMBER = 'User is not a member in group: null (fixture-group)'
NO_RESPONSE = ('Failed to send message: java.net.SocketException: '
               'Failed to get response for request (IOException) (UnexpectedErrorException)')


class ErrorClassificationTests(unittest.TestCase):
    def setUp(self):
        scope = isolated_engine()
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)

    def test_upload_retry_later_is_throttling_not_an_invalid_photo(self):
        self.assertTrue(engine.THROTTLE_PATTERN.search(UPLOAD_RETRY))
        self.assertEqual(engine.classify_error(UPLOAD_RETRY), 'rate limited')
        self.assertEqual(engine._throttle_wait(1, UPLOAD_RETRY), 30)
        self.assertEqual(engine._throttle_wait(1, 'Retry after 75 seconds'), 75)

    def test_membership_failure_is_permanent_and_attempted_once(self):
        send = mock.Mock(return_value=(False, False, NOT_MEMBER))
        with mock.patch.object(engine, '_interruptible_sleep'):
            status, reason = engine._deliver_to_group(send, 'fixture-group', 'fixture-message',
                [], 4, lambda _: None, lambda: False)
        self.assertEqual(status, 'permanent')
        self.assertEqual(reason, 'not a member of this group')
        send.assert_called_once()

    def test_lost_response_stays_uncertain_even_with_throttle_text(self):
        send = mock.Mock(return_value=(False, True, NO_RESPONSE + ' ' + UPLOAD_RETRY))
        status, _ = engine._deliver_to_group(send, 'fixture-group', 'fixture-message', [], 4,
                                            lambda _: None, lambda: False)
        self.assertEqual(status, 'uncertain')
        send.assert_called_once()

    def test_permanent_group_is_excluded_from_retry_and_resume(self):
        result = engine.GroupSendResult('fixture-group', 'Fixture', False, permanent=True,
                                        reason='not a member of this group')
        self.assertFalse(result.retryable)
        mac_retry.save([result], 'fixture-message', [], 'none')
        self.assertEqual(json.loads((engine.RUNTIME_DIR / 'retry.json').read_text())['groups'], [])
        engine.begin_run_progress([('fixture-group', 'Fixture'), ('pending', 'Pending')], 'fixture')
        engine.record_group_progress('fixture-group', 'permanent')
        self.assertEqual(engine.read_interrupted_run().remaining, [('pending', '')])


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class RecoveryGateTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.events = []
        self.stopped = False
        self.recovery = engine.SendRecovery(lambda: self.stopped, self.events.append, clock=self.clock)

    def finish(self, permit, ok=False, err=UPLOAD_RETRY):
        self.recovery.finish(permit, ok=ok, err=err)

    def automatic_time(self):
        return mock.patch.object(self.recovery.condition, 'wait', side_effect=self.clock.advance)

    def test_throttled_send_waits_then_only_one_probe_is_admitted(self):
        permits = [self.recovery.acquire() for _ in range(5)]
        self.finish(permits[0])
        self.assertEqual(self.events[-1]['retry_after'], 30)
        for permit in permits[1:]:
            self.finish(permit, ok=True, err='')
        with self.automatic_time():
            probe = self.recovery.acquire()
        self.assertTrue(probe.probe)
        self.assertEqual(self.clock(), 30)
        self.assertEqual(self.recovery.inflight, 1)
        self.finish(probe, ok=True, err='')
        self.assertEqual(self.events[-1]['event'], 'recovered')
        permits = [self.recovery.acquire() for _ in range(5)]
        self.assertTrue(all(not permit.probe for permit in permits))

    def test_persistent_throttling_pauses_after_fifteen_minutes(self):
        with self.automatic_time():
            while (permit := self.recovery.acquire()) is not None:
                self.finish(permit)
        self.assertEqual(self.clock(), 900)
        self.assertTrue(self.recovery.paused)
        self.assertEqual(self.events[-1]['event'], 'paused')
        self.assertEqual([event['retry_after'] for event in self.events if event['event'] == 'throttled'],
                         [30, 60, 120, 240, 300, 150])

    def test_long_server_delay_pauses_without_dispatching_early(self):
        self.finish(self.recovery.acquire(), err='RetryLaterException: Retry after 3600 seconds')
        with self.automatic_time():
            self.assertIsNone(self.recovery.acquire())
        self.assertEqual(self.clock(), 900)
        self.assertTrue(self.recovery.paused)

    def test_inflight_success_resets_window_but_does_not_bypass_cooldown(self):
        first, second = self.recovery.acquire(), self.recovery.acquire()
        self.finish(first)
        self.clock.advance(20)
        self.finish(second, ok=True, err='')
        with self.automatic_time():
            while (permit := self.recovery.acquire()) is not None:
                self.finish(permit)
        self.assertEqual(self.clock(), 920)

    def test_stop_aborts_wait_without_new_admission(self):
        self.finish(self.recovery.acquire())
        self.stopped = True
        self.assertIsNone(self.recovery.acquire())
        self.assertEqual(self.clock(), 0)
        self.assertFalse(self.recovery.paused)

    def test_late_success_does_not_unpause_a_timed_out_run(self):
        first, second = self.recovery.acquire(), self.recovery.acquire()
        self.finish(first)
        with self.automatic_time():
            self.assertIsNone(self.recovery.acquire())
        self.finish(second, ok=True, err='')
        self.assertTrue(self.recovery.paused)
        self.assertIsNone(self.recovery.acquire())

    def test_success_does_not_shorten_a_long_server_delay(self):
        first, second = self.recovery.acquire(), self.recovery.acquire()
        self.finish(first, err='RetryLaterException: Retry after 3600 seconds')
        self.clock.advance(20)
        self.finish(second, ok=True, err='')
        with self.automatic_time():
            self.assertIsNone(self.recovery.acquire())
        self.assertEqual(self.clock(), 920)

    def test_waiting_workers_share_cooldown_and_admit_one_probe(self):
        initial = [self.recovery.acquire() for _ in range(5)]
        self.finish(initial[0])
        waiters_ready = threading.Event()
        wait_count = 0
        original_wait = self.recovery.condition.wait
        def wait(seconds):
            nonlocal wait_count
            wait_count += 1
            if wait_count >= 4:
                waiters_ready.set()
            return original_wait(seconds)
        with mock.patch.object(self.recovery.condition, 'wait', side_effect=wait):
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(self.recovery.acquire) for _ in range(4)]
                try:
                    self.assertTrue(waiters_ready.wait(2))
                    self.assertFalse(any(f.done() for f in futures))
                    for permit in initial[1:]:
                        self.finish(permit, ok=True, err='')
                    self.assertFalse(any(f.done() for f in futures))
                    with self.recovery.condition:
                        self.clock.advance(30)
                        self.recovery.condition.notify_all()
                    from concurrent.futures import wait as wait_futures, FIRST_COMPLETED
                    done, _ = wait_futures(futures, timeout=2, return_when=FIRST_COMPLETED)
                    self.assertEqual(len(done), 1)
                    probe = next(iter(done)).result()
                    self.assertTrue(probe.probe)
                    self.assertEqual(self.recovery.inflight, 1)
                    self.finish(probe, ok=True, err='')
                    self.assertEqual(len(wait_futures(futures, timeout=2).done), 4)
                finally:
                    self.stopped = True
                    with self.recovery.condition:
                        self.recovery.condition.notify_all()

    def test_recovery_does_not_release_a_burst_of_queued_sends(self):
        self.recovery.pace = lambda: 10.0
        first = self.recovery.acquire()
        self.finish(first)
        with self.automatic_time():
            probe = self.recovery.acquire()
            self.assertEqual(self.clock(), 30)
            self.finish(probe, ok=True, err='')
            self.recovery.acquire()
            self.assertEqual(self.clock(), 40)
            self.recovery.acquire()
            self.assertEqual(self.clock(), 50)


class BroadcastRecoveryTests(unittest.TestCase):
    def setUp(self):
        scope = isolated_engine()
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        self.clock = FakeClock()
        self.calls = []
        self.plan = {}
        self.transport = mock.Mock()
        self.transport.send.side_effect = self.send
        self.transport.is_running.return_value = True
        self.cfg = engine.Config('fixture-account', 0, 0, 0, 4, [], concurrent_sends=1)
        original = engine.SendRecovery
        self.recoveries = []
        self.auto_time = True
        def recovery(*args, **kwargs):
            value = original(*args, **kwargs, clock=self.clock)
            self.recoveries.append(value)
            if self.auto_time:
                value.condition.wait = lambda seconds: self.clock.advance(seconds)
            return value
        patches = [mock.patch.object(engine, 'signal_cli_bin', return_value='/fixture/signal-cli'),
                   mock.patch.object(engine, '_unsendable_groups_unlocked', return_value=set()),
                   mock.patch.object(engine, 'check_signal_reachable', return_value=None),
                   mock.patch.object(engine, '_start_daemon', return_value=self.transport),
                   mock.patch.object(engine, '_send_one', side_effect=lambda binary, account, gid, msg, atts, styles: self.send(gid, msg, atts, styles)),
                   mock.patch.object(engine, 'MIN_DELAY_S', 0),
                   mock.patch.object(engine, 'NON_THROTTLE_WAIT_S', 0),
                   mock.patch.object(engine, 'SendRecovery', side_effect=recovery)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def send(self, gid, message, attachments, styles=None):
        self.calls.append(gid)
        return self.plan.get(gid, (True, False, ''))

    def broadcast(self, resume=False, groups=None):
        return engine.broadcast(config=self.cfg, groups=groups or [('g1', 'First'), ('g2', 'Second'),
            ('g3', 'Third'), ('g4', 'Fourth'), ('g5', 'Fifth')], message='fixture-message',
            attachments=[], resume=resume)

    def test_repeated_pause_resume_preserves_all_prior_outcomes(self):
        self.plan = {'g2': (False, False, NOT_MEMBER), 'g3': (False, False, NO_RESPONSE),
                     'g4': (False, True, UPLOAD_RETRY)}
        with self.assertRaises(engine.BroadcastPaused) as first:
            self.broadcast()
        results = first.exception.results
        self.assertEqual(len(results), 5)
        self.assertEqual([r.group_id for r in results if r.waiting], ['g4', 'g5'])
        self.assertEqual(self.calls.count('g1'), 1)
        self.assertEqual(self.calls.count('g2'), 1)
        self.assertEqual(self.calls.count('g3'), 1)
        snapshot = json.loads(engine.RUN_PROGRESS_FILE.read_text())
        self.assertTrue(snapshot['paused'])
        self.assertEqual(snapshot['done']['g4'], 'waiting')
        engine.write_run_summary(results, paused=True)
        self.assertEqual(engine.read_run_summary().pending, 2)
        self.assertEqual(engine.read_run_summary().failed, 1)
        self.assertTrue(engine.read_run_summary().paused)

        self.plan = {'g5': (False, True, UPLOAD_RETRY)}
        self.calls.clear()
        with self.assertRaises(engine.BroadcastPaused):
            self.broadcast(resume=True, groups=[('unrelated', 'Changed selected groups')])
        self.assertEqual(self.calls.count('g4'), 1)
        self.assertEqual(set(self.calls), {'g4', 'g5'})
        second = json.loads(engine.RUN_PROGRESS_FILE.read_text())
        self.assertEqual(second['groups'], snapshot['groups'])
        self.assertEqual(second['run'], snapshot['run'])
        self.assertEqual(second['done']['g2'], 'permanent')
        self.assertEqual(second['done']['g3'], 'uncertain')

        self.plan = {}
        self.calls.clear()
        results = self.broadcast(resume=True)
        self.assertEqual(self.calls, ['g5'])
        self.assertEqual(len(results), 5)
        self.assertEqual(sum(r.ok for r in results), 3)
        self.assertEqual(sum(r.permanent for r in results), 1)
        self.assertEqual(sum(r.uncertain for r in results), 1)
        remaining = engine.read_interrupted_run()
        self.assertEqual(remaining.remaining, [])
        self.assertEqual(remaining.uncertain, [('g3', '')])

    def test_paused_ledger_cannot_be_overwritten_by_a_new_broadcast(self):
        self.plan = {'g1': (False, True, UPLOAD_RETRY)}
        with self.assertRaises(engine.BroadcastPaused):
            self.broadcast()
        before = engine.RUN_PROGRESS_FILE.read_bytes()
        self.calls.clear()
        with self.assertRaisesRegex(engine.BroadcastError, 'paused'):
            self.broadcast()
        self.assertEqual(self.calls, [])
        self.assertEqual(engine.RUN_PROGRESS_FILE.read_bytes(), before)

    def test_changed_style_cannot_resume_and_legacy_attempting_remains_uncertain(self):
        engine.begin_run_progress([('old', 'Old'), ('g1', 'Next')], engine.message_fingerprint('fixture-message', []))
        engine.record_group_progress('old', 'attempting')
        self.broadcast(resume=True)
        self.assertEqual(self.calls, ['g1'])
        self.assertEqual(engine.read_interrupted_run().uncertain, [('old', '')])
        self.cfg.message_style = 'bold'
        before = engine.RUN_PROGRESS_FILE.read_bytes()
        with self.assertRaisesRegex(engine.BroadcastError, 'formatting changed'):
            self.broadcast(resume=True)
        self.assertEqual(engine.RUN_PROGRESS_FILE.read_bytes(), before)

    def test_error_after_known_rejection_preserves_waiting_for_restart(self):
        self.plan = {'g1': (False, True, UPLOAD_RETRY)}
        original = engine.record_group_progress
        def crash_after_waiting(gid, status):
            original(gid, status)
            if status == 'waiting':
                raise RuntimeError('simulated shutdown after rejected send')
        with mock.patch.object(engine, 'record_group_progress', side_effect=crash_after_waiting):
            with self.assertRaises(RuntimeError):
                self.broadcast()
        self.assertEqual(engine.read_interrupted_run().remaining[0], ('g1', ''))
        self.assertNotIn(('g1', ''), engine.read_interrupted_run().uncertain)

    def test_real_workers_drain_before_single_probe_and_resume_without_duplicates(self):
        for concurrency in (1, 5):
            with self.subTest(concurrency=concurrency):
                self.cfg.concurrent_sends = concurrency
                self.auto_time = False
                self.calls.clear()
                self.recoveries.clear()
                first_ready, release_initial = threading.Event(), threading.Event()
                probe_ready, release_probe = threading.Event(), threading.Event()
                calls_lock = threading.Lock()
                def send(gid, message, attachments, styles=None):
                    with calls_lock:
                        self.calls.append(gid)
                        count = len(self.calls)
                    if count <= concurrency:
                        if count == concurrency:
                            first_ready.set()
                        if not release_initial.wait(3):
                            raise RuntimeError('test initial release timed out')
                        return False, True, UPLOAD_RETRY
                    if count == concurrency + 1:
                        probe_ready.set()
                        if not release_probe.wait(3):
                            raise RuntimeError('test probe release timed out')
                    return True, False, ''
                self.transport.send.side_effect = send
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(self.broadcast, groups=[(f'g{i}', f'Fixture {i}') for i in range(15)])
                    try:
                        self.assertTrue(first_ready.wait(2))
                        release_initial.set()
                        recovery = self.recoveries[0]
                        with recovery.condition:
                            self.assertTrue(recovery.condition.wait_for(lambda: recovery.strikes == concurrency, 2))
                            self.assertEqual(len(self.calls), concurrency)
                            self.clock.advance(recovery.until - self.clock())
                            recovery.condition.notify_all()
                        self.assertTrue(probe_ready.wait(2))
                        with recovery.condition:
                            self.assertEqual(recovery.inflight, 1)
                            self.assertEqual(len(self.calls), concurrency + 1)
                        release_probe.set()
                        results = future.result(timeout=3)
                        self.assertEqual(len(results), 15)
                        self.assertTrue(all(r.ok for r in results))
                        self.assertEqual(len(self.calls), 15 + concurrency)
                    finally:
                        release_initial.set()
                        release_probe.set()

    def test_large_payload_and_hundreds_of_groups_have_one_successful_dispatch_each(self):
        photos = []
        for n in range(12):
            path = engine.RUNTIME_DIR / f'photo-{n}.jpg'
            path.write_bytes(b'fixture')
            photos.append(str(path))
        payload = 'large fixture message ' * 6000
        groups = [(f'g{i}', f'Fixture {i}') for i in range(300)]
        for concurrency in (1, 5):
            self.cfg.concurrent_sends = concurrency
            self.calls.clear()
            result = engine.broadcast(config=self.cfg, groups=groups, message=payload, attachments=photos)
            self.assertEqual(len(result), 300)
            self.assertEqual(len(self.calls), 300)
            self.assertEqual(len(set(self.calls)), 300)
            self.assertTrue(all(r.ok for r in result))
