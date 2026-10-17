"""Account reuse and truthful transient states; synthetic credentials only."""
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import adapter
import sau_cli
import server
from utils import publisher_handoff as handoff
from uploader.xiaohongshu_uploader import main as xhs

class AuthCheckTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.account = self.root / 'account.json'
        self.account.write_text('{"synthetic":"preserve-on-failure"}')

    def browser(self):
        self.page = SimpleNamespace(goto=AsyncMock(return_value=SimpleNamespace(status=200)), close=AsyncMock())
        self.context = SimpleNamespace(new_page=AsyncMock(return_value=self.page), close=AsyncMock())
        self.browser_handle = SimpleNamespace(close=AsyncMock())
        @asynccontextmanager
        async def probe(browser, context):
            try:
                yield self.page
            finally:
                await self.page.close()
        self.enterContext(patch.object(handoff, 'hidden_auth_page', side_effect=probe))
        return patch.object(handoff, 'connect_publisher_handoff', new=AsyncMock(return_value=(self.browser_handle, self.context)))

    async def test_xhs_check_uses_owned_context_and_closes_only_probe_tab(self):
        with self.browser() as connect, patch.object(xhs, '_xhs_auth_state', new=AsyncMock(return_value=True)), patch.object(handoff, 'save_publisher_session', new=AsyncMock()) as save:
            self.assertTrue(await xhs.cookie_auth(self.account))
        self.assertEqual(connect.call_args.args[1:], (self.account, 'xiaohongshu'))
        self.assertEqual(connect.call_args.kwargs, {'quiet': True})
        save.assert_awaited_once_with(self.context, self.account, 'xiaohongshu', probe_page=self.page)
        self.context.new_page.assert_not_awaited()
        self.page.close.assert_awaited_once()
        self.context.close.assert_not_awaited(); self.browser_handle.close.assert_not_awaited()

    async def test_xhs_timeout_keeps_credentials_and_does_not_start_login(self):
        original = self.account.read_bytes()
        with self.browser(), patch.object(handoff, 'save_publisher_session', new=AsyncMock()) as save, patch.object(xhs, 'xiaohongshu_cookie_gen', new=AsyncMock()) as login:
            self.page.goto.side_effect = TimeoutError('private-page-content')
            with self.assertRaises(handoff.AuthVerificationUnavailable) as caught:
                await xhs.xiaohongshu_setup(self.account, handle=True)
        self.assertNotIn('private-page-content', str(caught.exception))
        save.assert_not_awaited(); login.assert_not_awaited()
        self.assertEqual(self.account.read_bytes(), original); self.page.close.assert_awaited_once()

    async def test_xhs_blank_page_is_unknown_and_explicit_login_page_is_invalid(self):
        page = SimpleNamespace(url='https://creator.xiaohongshu.com/publish/publish')
        with self.assertRaises(handoff.AuthVerificationUnavailable): await xhs._xhs_auth_state(page, timeout_seconds=0)
        page.url = 'https://creator.xiaohongshu.com/login'
        self.assertFalse(await xhs._xhs_auth_state(page))

    async def test_xhs_late_scan_in_retained_browser_does_not_request_another_qr(self):
        with self.browser(), patch.object(xhs, '_xhs_auth_state', new=AsyncMock(return_value=True)), patch.object(handoff, 'save_publisher_session', new=AsyncMock()) as save, patch.object(xhs, '_save_xhs_qrcode', new=AsyncMock()) as qr:
            result = await xhs.xiaohongshu_cookie_gen(self.account, headless=False)
        self.assertTrue(result['success']); qr.assert_not_awaited(); save.assert_awaited_once()
        self.browser_handle.close.assert_not_awaited()

    async def test_bilibili_only_explicit_logged_out_response_requires_login(self):
        self.account.write_text(json.dumps({'cookie_info': {'cookies': [{'name': 'DedeUserID', 'value': '42'}, {'name': 'SESSDATA', 'value': 'synthetic'}]}}))
        original = self.account.read_bytes()
        cases = [({'code': 0, 'data': {'isLogin': True, 'mid': 42}}, True), ({'code': -101}, False), ({'code': 0, 'data': {'isLogin': False}}, False), ({'code': -412}, None), ({'code': 0, 'data': {}}, None), ({'code': 0, 'data': {'isLogin': True, 'mid': 7}}, None)]
        for payload, expected in cases:
            response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)
            with self.subTest(payload=payload), patch.object(sau_cli, 'resolve_account_file', return_value=self.account), patch('requests.get', return_value=response):
                if expected is None:
                    with self.assertRaises(handoff.AuthVerificationUnavailable): await sau_cli.check_bilibili_account('default')
                else: self.assertIs(await sau_cli.check_bilibili_account('default'), expected)
        self.assertEqual(self.account.read_bytes(), original)

    async def test_bilibili_network_error_preserves_session_and_never_opens_qr(self):
        self.account.write_text(json.dumps({'cookie_info': {'cookies': []}})); original = self.account.read_bytes()
        with patch.object(sau_cli, 'resolve_account_file', return_value=self.account), patch('requests.get', side_effect=TimeoutError()), patch.object(sau_cli, 'run_biliup_command') as login:
            with self.assertRaises(handoff.AuthVerificationUnavailable): await sau_cli.login_bilibili_account('default')
        login.assert_not_called(); self.assertEqual(self.account.read_bytes(), original)

    async def test_bilibili_existing_valid_session_needs_no_qr_or_terminal(self):
        with patch.object(sau_cli, 'resolve_account_file', return_value=self.account), patch.object(sau_cli, 'check_bilibili_account', new=AsyncMock(return_value=True)), patch.object(sau_cli, 'run_biliup_command') as login, patch.object(sau_cli, 'has_interactive_terminal') as terminal:
            self.assertTrue((await sau_cli.login_bilibili_account('default'))['success'])
        login.assert_not_called(); terminal.assert_not_called()

class ProfileReuseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.account = self.root / 'xhs.json'
        self.cookies = [{'name': 'web_session', 'value': 'synthetic-original', 'domain': '.xiaohongshu.com', 'path': '/'}]
        self.account.write_text(json.dumps({'cookies': self.cookies, 'origins': []}))
        self.context = SimpleNamespace(cookies=AsyncMock(return_value=self.cookies), add_cookies=AsyncMock(), add_init_script=AsyncMock(), storage_state=AsyncMock())
        self.browser = SimpleNamespace(contexts=[self.context])
        self.playwright = SimpleNamespace(chromium=SimpleNamespace(executable_path='/synthetic/chrome', connect_over_cdp=AsyncMock(return_value=self.browser)))

    async def test_rotated_cookie_keeps_same_profile_and_imported_account_does_not(self):
        with patch.object(handoff, '_start_detached', return_value='http://127.0.0.1:9999') as start:
            await handoff.connect_publisher_handoff(self.playwright, self.account, 'xiaohongshu'); first = start.call_args.args[1:]
            refreshed = [{**self.cookies[0], 'value': 'synthetic-renewed'}]
            self.context.storage_state.return_value = {'cookies': refreshed, 'origins': []}; self.context.cookies.return_value = refreshed
            await handoff.save_publisher_session(self.context, self.account, 'xiaohongshu')
            await handoff.connect_publisher_handoff(self.playwright, self.account, 'xiaohongshu')
            self.assertEqual(start.call_args.args[1:], first)
            self.account.write_text(json.dumps({'cookies': [{**self.cookies[0], 'value': 'different-import'}], 'origins': []}))
            self.context.cookies.return_value = []
            await handoff.connect_publisher_handoff(self.playwright, self.account, 'xiaohongshu')
            self.assertNotEqual(start.call_args.args[1:], first)

    async def test_unrecognized_foreign_context_is_not_adopted(self):
        self.context.cookies.return_value = [{**self.cookies[0], 'value': 'wrong-account'}]
        with patch.object(handoff, '_start_detached', return_value='http://127.0.0.1:9999'):
            with self.assertRaises(handoff.HandoffError): await handoff.connect_publisher_handoff(self.playwright, self.account, 'xiaohongshu')
        self.context.add_cookies.assert_not_awaited(); self.assertFalse(handoff._profile_link_path(self.account).exists())

    async def test_only_platform_scoped_state_saved_and_missing_auth_never_overwrites(self):
        with patch.object(handoff, '_start_detached', return_value='http://127.0.0.1:9999'):
            await handoff.connect_publisher_handoff(self.playwright, self.account, 'xiaohongshu')
        self.context.storage_state.return_value = {'cookies': self.cookies + [{'name': 'secret', 'value': 'private-other-site', 'domain': '.other.example'}], 'origins': [{'origin': 'https://creator.xiaohongshu.com', 'localStorage': []}, {'origin': 'https://other.example', 'localStorage': [{'name': 'secret', 'value': 'private-other-site'}]}]}
        await handoff.save_publisher_session(self.context, self.account, 'xiaohongshu')
        original = self.account.read_bytes(); self.assertNotIn(b'private-other-site', original)
        self.context.storage_state.return_value = {'cookies': [], 'origins': []}
        with self.assertRaises(RuntimeError): await handoff.save_publisher_session(self.context, self.account, 'xiaohongshu')
        self.assertEqual(self.account.read_bytes(), original)

    async def test_pending_first_login_reuses_its_browser_after_scan_completes(self):
        self.account.unlink()
        with patch.object(handoff, '_start_detached', return_value='http://127.0.0.1:9999') as start:
            await handoff.connect_publisher_handoff(self.playwright, self.account, 'xiaohongshu', allow_login=True)
        self.assertTrue(start.called); self.assertEqual(self.context._publisher_session_owner['account_file'], str(self.account.resolve()))

    async def test_failed_link_write_does_not_replace_saved_session(self):
        with patch.object(handoff, '_start_detached', return_value='http://127.0.0.1:9999'):
            await handoff.connect_publisher_handoff(self.playwright, self.account, 'xiaohongshu')
        original = self.account.read_bytes()
        self.context.storage_state.return_value = {'cookies': [{**self.cookies[0], 'value': 'renewed'}], 'origins': []}
        with patch.object(handoff, '_write_state', side_effect=OSError('synthetic disk failure')):
            with self.assertRaises(OSError): await handoff.save_publisher_session(self.context, self.account, 'xiaohongshu')
        self.assertEqual(self.account.read_bytes(), original)

    async def test_interrupted_session_write_recovers_old_source_in_same_profile(self):
        with patch.object(handoff, '_start_detached', return_value='http://127.0.0.1:9999') as start:
            await handoff.connect_publisher_handoff(self.playwright, self.account, 'xiaohongshu')
            original_profile = start.call_args.args[1:]
        original = self.account.read_bytes()
        self.context.storage_state.return_value = {'cookies': [{**self.cookies[0], 'value': 'renewed'}], 'origins': []}
        actual_write = handoff._write_state
        def interrupted_write(path, value):
            if path == self.account: raise OSError('synthetic session write interrupted')
            return actual_write(path, value)
        with patch.object(handoff, '_write_state', side_effect=interrupted_write):
            with self.assertRaises(OSError): await handoff.save_publisher_session(self.context, self.account, 'xiaohongshu')
        self.assertEqual(self.account.read_bytes(), original)
        with patch.object(handoff, '_start_detached', return_value='http://127.0.0.1:9999') as start:
            await handoff.connect_publisher_handoff(self.playwright, self.account, 'xiaohongshu')
            self.assertEqual(start.call_args.args[1:], original_profile)
            await handoff.save_publisher_session(self.context, self.account, 'xiaohongshu')
            self.context.cookies.return_value = self.context.storage_state.return_value['cookies']
            await handoff.connect_publisher_handoff(self.playwright, self.account, 'xiaohongshu')
            self.assertEqual(start.call_args.args[1:], original_profile)

    async def test_probe_does_not_overwrite_an_imported_replacement(self):
        with patch.object(handoff, '_start_detached', return_value='http://127.0.0.1:9999'):
            await handoff.connect_publisher_handoff(self.playwright, self.account, 'xiaohongshu')
        replacement = {'cookies': [{**self.cookies[0], 'value': 'explicit-different-import'}], 'origins': []}
        self.account.write_text(json.dumps(replacement)); original = self.account.read_bytes()
        self.context.storage_state.return_value = {'cookies': self.cookies, 'origins': []}
        with patch.object(handoff, '_write_state') as write:
            with self.assertRaises(handoff.HandoffError): await handoff.save_publisher_session(self.context, self.account, 'xiaohongshu')
        write.assert_not_called(); self.assertEqual(self.account.read_bytes(), original)

class AccountStateTests(unittest.TestCase):
    def test_unavailable_check_keeps_last_verified_date_and_session(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); account = root / 'xiaohongshu_default.json'; account.write_text('synthetic')
            app = server.create_app(root / 'app', start_worker=False); store = app.extensions['store']; manager = app.extensions['manager']
            store.put('account', 'xiaohongshu', {'status': 'verified', 'checked_at': '2026-10-14T01:00:00'})
            job = manager.create('check', ['xiaohongshu'])
            with patch.object(adapter, 'COOKIE_DIR', root), patch.object(adapter, 'runtime_status', return_value={'ready': True}), patch.object(adapter, 'run', return_value={'platform': 'xiaohongshu', 'status': 'verification_unavailable', 'message': '重试检查'}):
                manager.execute(job['id'])
                platform = next(p for p in app.test_client().get('/api/state').get_json()['platforms'] if p['id'] == 'xiaohongshu')
            self.assertEqual(platform['account_status'], 'verification_unavailable'); self.assertEqual(platform['last_verified_at'], '2026-10-14T01:00:00')
            self.assertTrue(platform['session_saved']); self.assertEqual(platform['connection_method'], 'persistent_browser'); self.assertEqual(account.read_text(), 'synthetic')

    def test_adapter_bilibili_connect_reuses_valid_session_before_qr_install_check(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root/'bilibili_default.json').write_text('synthetic')
            observed = dict(returncode=0, valid=True, invalid=False, cancelled=False, timeout=False)
            with patch.object(adapter, 'COOKIE_DIR', root), patch.object(adapter, 'runtime_status', return_value={'ready': True}), patch.object(adapter, '_execute', return_value=observed) as execute, patch('install_biliup.installation_valid') as installed:
                result = adapter.run('bilibili', 'login')
            self.assertEqual(result['status'], 'verified'); self.assertEqual(execute.call_args.args[2], 'check'); self.assertEqual(execute.call_count, 1); installed.assert_not_called()

    def test_adapter_bilibili_unknown_state_does_not_start_new_login(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root/'bilibili_default.json').write_text('synthetic')
            observed = dict(returncode=1, valid=False, invalid=False, cancelled=False, timeout=False)
            with patch.object(adapter, 'COOKIE_DIR', root), patch.object(adapter, 'runtime_status', return_value={'ready': True}), patch.object(adapter, '_execute', return_value=observed) as execute:
                result = adapter.run('bilibili', 'login')
            self.assertEqual(result['status'], 'verification_unavailable'); self.assertEqual(execute.call_count, 1)

    def test_xhs_cookie_refresh_preserves_imported_account_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); account = root/'xiaohongshu_default.json'; account.write_text('synthetic-before')
            metadata = root/'xiaohongshu_default.json.publisher.json'; metadata.write_text('{"source":"chrome"}')
            observed = dict(returncode=0, valid=False, invalid=False, login=False, cancelled=False, timeout=False)
            def execute(*args):
                if args[2] == 'login':
                    account.write_text('synthetic-refreshed')
                    return {**observed, 'login': True, 'result': {'session_reused': True}}
                return {**observed, 'valid': True}
            with patch.object(adapter, 'COOKIE_DIR', root), patch.object(adapter, 'runtime_status', return_value={'ready': True}), patch.object(adapter, '_execute', side_effect=execute):
                result = adapter.run('xiaohongshu', 'login')
            self.assertEqual(result['status'], 'verified'); self.assertTrue(metadata.exists())

if __name__ == '__main__': unittest.main()
