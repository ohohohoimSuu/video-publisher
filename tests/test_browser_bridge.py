import json
from pathlib import Path
import tempfile
import unittest
import sqlite3
from unittest.mock import patch

import adapter
import browser_bridge
import server

ORIGIN = 'chrome-extension://' + 'a' * 32
OTHER_ORIGIN = 'chrome-extension://' + 'b' * 32
SYNTHETIC_VALUE = 'synthetic-only-not-a-real-session'


class BrowserBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cookie_dir = self.root / 'cookies'
        patcher = patch.object(adapter, 'COOKIE_DIR', self.cookie_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = server.create_app(self.root / 'data', start_worker=False)
        self.client = self.app.test_client()
        self.token_headers = {'X-Publisher-Token': self.app.extensions['publisher_token']}
        self.pairs = self.app.extensions['browser_pairings']
        self.store = self.app.extensions['store']
        self.manager = self.app.extensions['manager']

    def create_pair(self, pid='douyin'):
        r = self.client.post('/api/browser/pair', json={'platform': pid}, headers=self.token_headers)
        self.assertEqual(r.status_code, 201, r.get_json())
        return r.get_json()['code']

    def handshake(self, code, origin=ORIGIN):
        return self.client.post('/api/browser/handshake', json={'code': code}, headers={'Origin': origin})

    def payload(self):
        return {'platform': 'douyin', 'page_url': 'https://creator.douyin.com/creator-micro/home',
                'cookies': [{'name': 'sessionid', 'value': SYNTHETIC_VALUE, 'domain': '.douyin.com', 'path': '/',
                             'secure': True, 'httpOnly': True, 'session': True, 'hostOnly': False, 'sameSite': 'lax'}],
                'origins': [{'origin': 'https://creator.douyin.com', 'localStorage': []}]}

    def import_session(self, code, payload=None, origin=ORIGIN):
        return self.client.post('/api/browser/import', json=payload or self.payload(), headers={'Origin': origin, 'X-Bridge-Code': code})

    def test_pair_creation_requires_publisher_token(self):
        self.assertEqual(self.client.post('/api/browser/pair', json={'platform': 'douyin'}).status_code, 403)
        code = self.create_pair()
        self.assertEqual(len(code), 64)

    def test_arbitrary_web_origin_cannot_import_or_preflight(self):
        code = self.create_pair()
        for origin in ('https://evil.example', 'null', 'chrome-extension://invalid'):
            with self.subTest(origin=origin):
                self.assertEqual(self.handshake(code, origin).status_code, 403)
                r = self.client.options('/api/browser/import', headers={'Origin': origin})
                self.assertEqual(r.status_code, 403)
                self.assertNotIn('Access-Control-Allow-Origin', r.headers)

    def test_extension_cors_is_limited_to_import_endpoints(self):
        r = self.client.options('/api/browser/import', headers={'Origin': ORIGIN})
        self.assertEqual(r.status_code, 204)
        self.assertEqual(r.headers['Access-Control-Allow-Origin'], ORIGIN)
        self.assertNotIn('*', r.headers['Access-Control-Allow-Headers'])
        self.assertEqual(self.client.get('/api/state', headers={'Origin': ORIGIN}).status_code, 403)

    def test_expired_code_rejected(self):
        code = self.create_pair()
        deadline = self.pairs.items[code]['deadline']
        self.pairs.clock = lambda: deadline + 1
        self.assertEqual(self.handshake(code).status_code, 409)
        r = self.client.post('/api/browser/status', json={'code': code}, headers=self.token_headers)
        self.assertEqual(r.get_json()['status'], 'expired')

    def test_close_cancel_invalidates_code(self):
        code = self.create_pair()
        r = self.client.post('/api/browser/cancel', json={'code': code}, headers=self.token_headers)
        self.assertEqual(r.get_json()['status'], 'expired')
        self.assertEqual(self.handshake(code).status_code, 409)

    def test_new_code_revokes_previous_pending_code(self):
        first = self.create_pair()
        second = self.create_pair()
        self.assertEqual(self.handshake(first).status_code, 409)
        self.assertEqual(self.handshake(second).status_code, 200)

    def test_handshake_binds_extension_origin(self):
        code = self.create_pair()
        self.assertEqual(self.handshake(code).status_code, 200)
        self.assertEqual(self.import_session(code, origin=OTHER_ORIGIN).status_code, 403)
        self.assertFalse(self.cookie_dir.exists())

    def test_platform_mismatch_does_not_write_session(self):
        code = self.create_pair()
        self.handshake(code)
        payload = self.payload()
        payload['platform'] = 'youtube'
        self.assertEqual(self.import_session(code, payload).status_code, 400)
        self.assertFalse(self.cookie_dir.exists())

    def test_off_platform_cookie_never_replaces_existing_session(self):
        self.cookie_dir.mkdir()
        account = self.cookie_dir / 'douyin_default.json'
        account.write_text('original session')
        code = self.create_pair()
        self.handshake(code)
        payload = self.payload()
        payload['cookies'][0]['domain'] = '.google.com'
        r = self.import_session(code, payload)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(account.read_text(), 'original session')
        self.assertNotIn(SYNTHETIC_VALUE, r.get_data(as_text=True))

    def test_active_job_blocks_session_switch(self):
        self.manager.create('check', ['douyin'])
        code = self.create_pair()
        self.handshake(code)
        self.assertEqual(self.import_session(code).status_code, 409)
        self.assertFalse(self.cookie_dir.exists())

    def test_import_saves_matching_session_and_queues_real_check(self):
        code = self.create_pair()
        self.handshake(code)
        r = self.import_session(code)
        self.assertEqual(r.status_code, 201, r.get_json())
        job_id = r.get_json()['job']['id']
        account = self.cookie_dir / 'douyin_default.json'
        self.assertTrue(account.is_file())
        self.assertEqual(json.loads(account.read_text())['cookies'][0]['value'], SYNTHETIC_VALUE)
        self.assertEqual(self.store.get('account', 'douyin')['status'], 'imported')
        self.assertNotIn(SYNTHETIC_VALUE, r.get_data(as_text=True))
        # The check and later publish both use this exact adapter account path.
        def check(pid, action, metadata, log, event):
            self.assertEqual((pid, action), ('douyin', 'check'))
            self.assertEqual(json.loads((adapter.COOKIE_DIR / (pid + '_default.json')).read_text())['cookies'][0]['value'], SYNTHETIC_VALUE)
            return {'platform': pid, 'status': 'verified', 'message': 'synthetic verification only'}
        with patch.object(adapter, 'run', side_effect=check):
            self.manager.execute(job_id)
        self.assertEqual(self.store.get('account', 'douyin')['status'], 'verified')
        with patch.object(adapter, 'runtime_status', return_value={'ready': True}):
            state = self.client.get('/api/state').get_json()
        platform = next(p for p in state['platforms'] if p['id'] == 'douyin')
        self.assertEqual(platform['account_source'], 'chrome')
        self.assertNotIn(SYNTHETIC_VALUE, json.dumps(state))
        self.assertNotIn(code, json.dumps(self.store.all('job')))

    def test_import_replay_is_rejected(self):
        code = self.create_pair()
        self.handshake(code)
        self.assertEqual(self.import_session(code).status_code, 201)
        self.assertEqual(self.import_session(code).status_code, 409)

    def test_status_requires_token_and_is_not_a_secret_url(self):
        code = self.create_pair()
        self.assertEqual(self.client.post('/api/browser/status', json={'code': code}).status_code, 403)
        self.assertEqual(self.client.get('/api/browser/pair/' + code).status_code, 404)

    def test_oversized_import_is_rejected_before_processing(self):
        code = self.create_pair()
        self.handshake(code)
        payload = self.payload()
        payload['padding'] = 'x' * (browser_bridge.MAX_SESSION_BYTES + 1)
        self.assertEqual(self.import_session(code, payload).status_code, 413)
        self.assertFalse(self.cookie_dir.exists())

    def test_bilibili_and_restricted_platform_not_offered(self):
        for pid in ('bilibili', 'tencent'):
            r = self.client.post('/api/browser/pair', json={'platform': pid}, headers=self.token_headers)
            self.assertEqual(r.status_code, 400)

    def test_database_failure_before_import_preserves_credentials(self):
        self.cookie_dir.mkdir()
        target = self.cookie_dir / 'douyin_default.json'
        target.write_text('old credentials')
        self.store.put('account', 'douyin', {'status': 'verified'})
        code = self.create_pair()
        self.handshake(code)
        with patch.object(self.store, 'put', side_effect=sqlite3.OperationalError('synthetic disk failure')):
            r = self.import_session(code)
        self.assertEqual(r.status_code, 503)
        self.assertEqual(target.read_text(), 'old credentials')
        self.assertEqual(self.pairs.status(code)['status'], 'failed')

    def test_failure_after_credentials_never_inherits_old_verified_label(self):
        self.store.put('account', 'douyin', {'status': 'verified'})
        code = self.create_pair()
        self.handshake(code)
        original_put = self.store.put
        count = 0
        def broken_second_write(*args):
            nonlocal count
            count += 1
            if count > 1:
                raise sqlite3.OperationalError('synthetic disk failure')
            return original_put(*args)
        with patch.object(self.store, 'put', side_effect=broken_second_write):
            r = self.import_session(code)
        self.assertEqual(r.status_code, 503)
        self.assertTrue((self.cookie_dir / 'douyin_default.json').is_file())
        self.assertEqual(self.store.get('account', 'douyin')['status'], 'pending_verification')
        self.assertEqual(self.pairs.status(code)['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
