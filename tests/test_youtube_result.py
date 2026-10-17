import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import adapter


class YoutubeResultTests(unittest.TestCase):
    def test_network_error_is_not_reported_as_logged_out(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'youtube_default.json').write_text('{}')
            observed = {'returncode': 1, 'valid': False, 'invalid': True, 'cancelled': False, 'timeout': False,
                        'result': {'diagnostic': {'reason': 'navigation_failed', 'net_error': 'net::ERR_CONNECTION_CLOSED'}}}
            with patch.object(adapter, 'COOKIE_DIR', Path(folder)), patch.object(adapter, 'runtime_status', return_value={'ready': True}), patch.object(adapter, '_execute', return_value=observed):
                result = adapter.run('youtube', 'check')
            self.assertEqual(result['status'], 'failed')
            self.assertIn('ERR_CONNECTION_CLOSED', result['message'])
            self.assertNotIn('重新登录', result['message'])

    def test_channel_mismatch_is_manual_attention_and_does_not_echo_unsafe_fields(self):
        result = adapter._youtube_check_result({'reason': 'channel_mismatch', 'actual_channel': 'UC' + 'a' * 22,
                                               'expected_channel': 'UC' + 'b' * 22, 'url': 'https://example.invalid/?secret=value',
                                               'error_type': 'SECRET', 'net_error': 'token=SECRET'})
        self.assertEqual(result['status'], 'needs_attention')
        self.assertNotIn('SECRET', str(result))
        self.assertNotIn('secret=value', str(result))

    def test_only_explicit_signin_redirect_asks_for_session_resync(self):
        self.assertEqual(adapter._youtube_check_result({'reason': 'login_redirect'})['status'], 'login_required')
        self.assertEqual(adapter._youtube_check_result({'reason': 'studio_not_ready'})['status'], 'needs_attention')
        self.assertIsNone(adapter._youtube_check_result({'reason': 'unknown_private_error'}))


if __name__ == '__main__':
    unittest.main()
