import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('handoff_sessions_test', ROOT/'vendor/social-auto-upload/utils/publisher_handoff.py')
handoff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(handoff)


class HandoffSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.account = self.root/'synthetic.json'
        self.binary = self.root/'browsers/Google Chrome for Testing'
        self.binary.parent.mkdir(); self.binary.write_text('synthetic; never launched')
        self.binding = hashlib.sha256(b'synthetic').hexdigest()

    def session(self, platform):
        spec = handoff.SPECS[platform]
        return {'cookies': [{'name': spec[3][0], 'value': 'synthetic-local-only', 'domain': '.'+spec[1], 'path': '/'}],
                'origins': [{'origin': spec[2][0], 'localStorage': [{'name': 'synthetic', 'value': 'local-only'}]}]}

    def test_each_platform_accepts_only_its_own_authorized_scope(self):
        for platform in (p for p in handoff.SPECS if p != "bilibili"):
            saved = self.session(platform); self.account.write_text(json.dumps(saved))
            cookies, storage, binding = handoff._load_session(self.account, platform)
            self.assertEqual(cookies, saved['cookies']); self.assertEqual(len(binding), 64)
            self.assertEqual(storage[0]['origin'], saved['origins'][0]['origin'])
            saved['cookies'].append({'name': 'sessionid', 'value': 'never-export', 'domain': '.unrelated.example'})
            self.account.write_text(json.dumps(saved))
            with self.assertRaisesRegex(RuntimeError, '会话无法'): handoff._load_session(self.account, platform)

    def test_bilibili_converts_native_cookies_without_exporting_tokens(self):
        native = {"cookie_info": {"cookies": [
            {"name": "DedeUserID", "value": "42"},
            {"name": "SESSDATA", "value": "synthetic-session"},
            {"name": "bili_jct", "value": "synthetic-csrf"}]},
            "token_info": {"mid": 42, "access_token": "must-not-export"}}
        self.account.write_text(json.dumps(native))
        cookies, storage, binding = handoff._load_session(self.account, "bilibili")
        self.assertEqual({c["name"] for c in cookies}, {"DedeUserID", "SESSDATA", "bili_jct"})
        self.assertTrue(all(c["domain"] == ".bilibili.com" for c in cookies))
        self.assertEqual(storage, [])
        self.assertEqual(len(binding), 64)
        self.assertNotIn("must-not-export", json.dumps(cookies))
        native["cookie_info"]["cookies"][0]["domain"] = ".unrelated.example"
        self.account.write_text(json.dumps(native))
        with self.assertRaises(RuntimeError): handoff._load_session(self.account, "bilibili")

    def test_youtube_does_not_expand_to_google_credentials(self):
        saved = self.session('youtube')
        saved['origins'].append({'origin': 'https://accounts.google.com', 'localStorage': []})
        self.account.write_text(json.dumps(saved))
        with self.assertRaises(RuntimeError): handoff._load_session(self.account, 'youtube')

    def prepare_state(self, platform='xiaohongshu'):
        directory = self.root/f'{platform}-handoff'; directory.mkdir()
        profile = directory/'profiles'/self.binding
        data = {'binding_sha256': self.binding, 'pid': 4321, 'port': 32123,
                'profile': str(profile), 'proxy_hash': hashlib.sha256(b'').hexdigest()}
        (directory/'browser.json').write_text(json.dumps(data))
        return directory, profile

    def test_owned_live_browser_is_reused_without_closing_existing_drafts(self):
        self.prepare_state()
        with patch.dict(os.environ, PUBLISHER_DATA_DIR=str(self.root)), patch.object(handoff, 'BROWSER_ROOT', self.binary.parent), \
             patch.object(handoff, '_pid_alive', return_value=True), patch.object(handoff, '_owned_process', return_value=True), \
             patch.object(handoff, '_endpoint_ready', return_value=True), patch.object(handoff.subprocess, 'Popen') as launch:
            endpoint = handoff._start_detached(self.binary, self.binding, 'xiaohongshu')
        self.assertEqual(endpoint, 'http://127.0.0.1:32123'); launch.assert_not_called()

    def test_foreign_pid_or_changed_account_never_reuses_or_overwrites_window(self):
        directory, _ = self.prepare_state(); original = (directory/'browser.json').read_bytes()
        with patch.dict(os.environ, PUBLISHER_DATA_DIR=str(self.root)), patch.object(handoff, 'BROWSER_ROOT', self.binary.parent), \
             patch.object(handoff, '_pid_alive', return_value=True), patch.object(handoff, '_owned_process', return_value=False), \
             patch.object(handoff.subprocess, 'Popen') as launch:
            with self.assertRaises(RuntimeError): handoff._start_detached(self.binary, self.binding, 'xiaohongshu')
            with self.assertRaises(RuntimeError): handoff._start_detached(self.binary, 'a'*64, 'xiaohongshu')
        launch.assert_not_called(); self.assertEqual((directory/'browser.json').read_bytes(), original)

    def test_authenticated_proxy_and_external_browser_are_rejected(self):
        with patch.dict(os.environ, PUBLISHER_DATA_DIR=str(self.root)), patch.object(handoff, 'BROWSER_ROOT', self.binary.parent), \
             patch.object(handoff.subprocess, 'Popen') as launch:
            with self.assertRaises(RuntimeError): handoff._start_detached(self.binary, self.binding, 'youtube', 'http://u:p@proxy.example:80')
            with self.assertRaises(RuntimeError): handoff._start_detached('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', self.binding)
            with self.assertRaises(RuntimeError): handoff._start_detached(self.binary, '../bad')
        launch.assert_not_called()

    def test_new_snapshot_with_same_identity_cannot_silently_reuse_old_browser(self):
        directory, _ = self.prepare_state()
        original = (directory/'browser.json').read_bytes()
        with patch.dict(os.environ, PUBLISHER_DATA_DIR=str(self.root)), patch.object(handoff, 'BROWSER_ROOT', self.binary.parent), \
             patch.object(handoff, '_pid_alive', return_value=True), patch.object(handoff.subprocess, 'Popen') as launch:
            with self.assertRaisesRegex(RuntimeError, '重新同步'):
                handoff._start_detached(self.binary, self.binding, 'xiaohongshu', session_sha256='b'*64)
        launch.assert_not_called(); self.assertEqual((directory/'browser.json').read_bytes(), original)

    def test_resume_requires_existing_browser_and_never_launches_a_new_one(self):
        with patch.dict(os.environ, PUBLISHER_DATA_DIR=str(self.root)), patch.object(handoff, 'BROWSER_ROOT', self.binary.parent), \
             patch.object(handoff.subprocess, 'Popen') as launch:
            with self.assertRaisesRegex(RuntimeError, '原编辑窗口已关闭'):
                handoff._start_detached(self.binary,self.binding,'xiaohongshu',require_existing=True)
        launch.assert_not_called()


if __name__ == '__main__': unittest.main()
