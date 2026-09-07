"""Recovery regressions using sanitised provider errors and disposable storage."""
import json
import unittest
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
