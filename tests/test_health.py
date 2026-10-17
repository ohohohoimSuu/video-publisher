"""Synthetic local liveness: no account, dependency or browser probes."""
import tempfile
import unittest
from unittest.mock import patch
import server


class HealthTests(unittest.TestCase):
    def test_health_is_lightweight_and_has_no_session_secrets(self):
        with tempfile.TemporaryDirectory() as folder:
            client = server.create_app(folder, start_worker=False).test_client()
            with patch('adapter.runtime_status', side_effect=AssertionError('no dependency probe')), patch('browser_sessions.read_metadata', side_effect=AssertionError('no account probe')):
                result = client.get('/api/health')
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json, {'application': 'local-video-publisher', 'version': '0.5.1', 'status': 'ok'})
                self.assertEqual(client.get('/api/health', headers={'Host': 'evil.example'}).status_code, 403)
                self.assertEqual(client.get('/api/health', headers={'Origin': 'https://evil.example'}).status_code, 403)
                self.assertEqual(client.post('/api/health').status_code, 403)
