"""Scheduling against disposable vaults; no real Signal subprocesses."""
import json
import unittest
from datetime import datetime, timedelta
from unittest import mock

import engine
from mac_service import Service
import test_mac_security


class ScheduleTests(unittest.TestCase):
    setUp = test_mac_security.SecurityTests.setUp
    setup = test_mac_security.SecurityTests.setup
    request = test_mac_security.SecurityTests.request

    def prepare(self, times=('12:00',)):
        self.setup()
        engine.set_config_value('account', '+19999999999')
        engine.set_config_value('cooldown_hours', 0)
        engine.GROUPS_FILE.write_text('fixture\tFixture\n')
        engine.write_message('Saved scheduled draft')
        engine.write_attachments([])
        self.request('schedule', enabled=True, times=list(times))

    def test_busy_slot_waits_then_runs_once(self):
        self.prepare()
        with mock.patch.object(self.service, '_start_job') as start:
            self.service.job = {'kind': 'notes'}
            self.service.tick(datetime(2026, 9, 5, 12))
            self.assertIsNotNone(self.service.schedule().get('pending'))
            self.service.job = None
            self.service.tick(datetime(2026, 9, 5, 12, 10))
            self.service.tick(datetime(2026, 9, 5, 12, 11))
            start.assert_called_once_with('send')

    def test_missed_slots_coalesce_and_expire(self):
        self.prepare(('12:00', '12:10', '12:20'))
        with mock.patch.object(self.service, '_start_job') as start:
            self.service.tick(datetime(2026, 9, 5, 11, 59))
            self.service.job = {'kind': 'send'}
            self.service.tick(datetime(2026, 9, 5, 12, 25))
            self.assertEqual(self.service.schedule()['pending'], '2026-09-05 12:20')
            self.service.job = None
            self.service.tick(datetime(2026, 9, 5, 13, 21))
            start.assert_not_called()
            self.assertIsNone(self.service.schedule()['pending'])
            self.assertEqual(self.service.schedule()['history'][-1]['state'], 'expired')

    def test_cooldown_retries_without_consuming_pending(self):
        self.prepare()
        with mock.patch.object(engine, 'cooldown_blocks_run', return_value='cooldown'), \
             mock.patch.object(self.service, '_start_job') as start:
            self.service.tick(datetime(2026, 9, 5, 12))
            start.assert_not_called()
            self.assertIsNotNone(self.service.schedule()['pending'])
        with mock.patch.object(self.service, '_start_job') as start:
            self.service.tick(datetime(2026, 9, 5, 12, 1))
            start.assert_called_once_with('send')

    def test_pending_survives_lock_and_service_recreation(self):
        self.prepare()
        self.service.job = {'kind': 'send'}
        self.service.tick(datetime(2026, 9, 5, 12))
        self.service.job = None
        restarted = Service(self.vault, retire=lambda _: None)
        with mock.patch.object(restarted, '_start_job') as start:
            restarted.tick(datetime(2026, 9, 5, 12, 1))
            start.assert_not_called()
            restarted.authenticate(test_mac_security.PASSWORD)
            restarted.lock()
            restarted.tick(datetime(2026, 9, 5, 12, 2))
            start.assert_called_once_with('send')

    def test_disabling_cancels_pending(self):
        self.prepare()
        self.service.job = {'kind': 'notes'}
        self.service.tick(datetime(2026, 9, 5, 12))
        self.request('schedule', enabled=False, times=['12:00'])
        self.service.job = None
        with mock.patch.object(self.service, '_start_job') as start:
            self.service.tick(datetime(2026, 9, 5, 12, 1))
            start.assert_not_called()
        self.assertIsNone(self.service.schedule().get('pending'))

    def test_spawn_failure_is_contained_and_retried(self):
        self.prepare()
        with mock.patch.object(self.service, 'spawn', side_effect=OSError('fixture')):
            self.service.tick(datetime(2026, 9, 5, 12))
        self.assertTrue(self.service.open)
        with mock.patch.object(self.service, '_start_job') as start:
            self.service.tick(datetime(2026, 9, 5, 12, 0, 10))
            start.assert_not_called()
            self.service.tick(datetime(2026, 9, 5, 12, 1))
            start.assert_called_once_with('send')

    def test_corrupt_schedule_does_not_crash_service_or_overwrite_data(self):
        self.prepare()
        path = self.vault.data / 'schedule.json'
        path.write_text('{bad')
        self.service.tick(datetime(2026, 9, 5, 12))
        self.assertEqual(path.read_text(), '{bad')
        self.assertTrue(self.service.open)
        self.assertTrue(self.service.snapshot()['schedule'].get('error'))

    def test_schedule_log_is_bounded_and_has_no_message_or_recipient(self):
        self.prepare()
        self.service.job = {'kind': 'notes'}
        for second in range(50):
            self.service.tick(datetime(2026, 9, 5, 12, 0, second))
        self.service.job = None
        log = (engine.LOGS_DIR / 'schedule.jsonl').read_text()
        self.assertNotIn('Saved scheduled draft', log)
        self.assertNotIn('fixture', log)
        self.assertLess(len(log.splitlines()), 5)

    def test_sleep_across_midnight_catches_up_once(self):
        self.prepare(('23:55', '00:05'))
        with mock.patch.object(self.service, '_start_job') as start:
            self.service.tick(datetime(2026, 9, 5, 23, 50))
            self.service.tick(datetime(2026, 9, 6, 0, 10))
            self.service.tick(datetime(2026, 9, 6, 0, 11))
            start.assert_called_once_with('send')

    def test_reserved_dispatch_is_never_replayed_after_crash(self):
        self.prepare()
        with mock.patch.object(self.service, '_start_job') as start:
            self.service.tick(datetime(2026, 9, 5, 12))
            start.assert_called_once()
        restarted = Service(self.vault, retire=lambda _: None)
        restarted.authenticate(test_mac_security.PASSWORD)
        with mock.patch.object(restarted, '_start_job') as start:
            restarted.tick(datetime(2026, 9, 5, 12, 1))
            start.assert_not_called()
        self.assertEqual(restarted.schedule()['history'][-1]['state'], 'interrupted')

    def test_failed_durable_write_never_starts_a_worker(self):
        self.prepare()
        with mock.patch('mac_schedule.atomic_json', side_effect=OSError('disk full')), \
             mock.patch.object(self.service, '_start_job') as start:
            self.service.tick(datetime(2026, 9, 5, 12))
            start.assert_not_called()
        self.assertTrue(self.service.open)
        self.assertTrue(self.service.schedule_error)

    def test_signed_out_schedule_never_runs(self):
        self.prepare()
        self.service.job = {'kind': 'notes'}
        self.service.tick(datetime(2026, 9, 5, 12))
        self.service.job = None
        self.request('erase', confirmed=True)
        with mock.patch.object(self.service, '_start_job') as start:
            self.service.tick(datetime(2026, 9, 5, 12, 1))
            start.assert_not_called()
        self.assertFalse(self.service.open)
        self.assertFalse(self.vault.image.exists())

    def test_log_rotation_and_missing_attachment_do_not_expose_content(self):
        self.prepare()
        engine.write_attachments(['/private/missing-private-fixture.png'])
        self.service.tick(datetime(2026, 9, 5, 12))
        log = engine.LOGS_DIR / 'schedule.jsonl'
        self.assertNotIn('missing-private-fixture', log.read_text())
        log.write_text('x' * (256 * 1024))
        self.request('schedule', enabled=False, times=['12:00'])
        self.assertLess(log.stat().st_size, 1024)
        self.assertTrue((engine.LOGS_DIR / 'schedule.previous.jsonl').exists())

    def test_send_wrapper_prevents_idle_sleep_and_recipients_are_frozen(self):
        self.prepare()
        process = mock.Mock(pid=999999)
        with mock.patch.object(self.service, 'spawn', return_value=process) as spawn, \
             mock.patch('mac_service.threading.Thread'):
            self.request('job', kind='send')
        command = spawn.call_args.args[0]
        self.assertEqual(command[:2], ['/usr/bin/caffeinate', '-i'])
        with self.assertRaisesRegex(Exception, 'recipients'):
            self.request('groups', enabled=[])
        self.service.job = None
        (self.vault.root / 'worker.json').unlink()

    def test_completed_scheduled_run_has_its_own_result(self):
        self.prepare()
        import io
        process = mock.Mock(stdout=io.StringIO(''), wait=mock.Mock(return_value=0))
        job = {'kind': 'send', 'proc': process, 'scheduled': True}
        data = self.service.schedule()
        data['running'] = '2026-09-05 12:00'
        from mac_schedule import save
        save(self.vault.data / 'schedule.json', data)
        self.service.job = job
        engine.write_run_summary([engine.GroupSendResult('fixture', 'Fixture', True)])
        with mock.patch('mac_service.terminate_group'):
            self.service._read_job(job)
        history = self.service.schedule()['history']
        self.assertEqual(history[-1]['state'], 'completed')
        self.assertIn('1 sent', history[-1]['message'])
