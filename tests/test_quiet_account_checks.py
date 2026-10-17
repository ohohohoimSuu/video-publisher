"""Quiet account-check contracts; synthetic state only, never starts Chromium."""
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import signal
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from utils import publisher_handoff as handoff
from uploader.xiaohongshu_uploader import main as xhs


class QuietLaunchTests(unittest.TestCase):
    def test_cold_quiet_launch_has_only_a_headless_startup_page(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            binary = root / 'browsers' / 'Google Chrome for Testing'
            binary.parent.mkdir(); binary.write_text('synthetic; never executed')
            process = SimpleNamespace(pid=4321, poll=lambda: None)
            with patch.dict(os.environ, PUBLISHER_DATA_DIR=str(root)), \
                 patch.object(handoff, 'BROWSER_ROOT', binary.parent), \
                 patch.object(handoff, '_endpoint_ready', return_value=True), \
                 patch.object(handoff.socket, 'socket') as reserve, \
                 patch.object(handoff.subprocess, 'Popen', return_value=process) as launch:
                reserve.return_value.__enter__.return_value.getsockname.return_value = ('127.0.0.1', 12345)
                handoff._start_detached(binary, 'a' * 64, 'xiaohongshu', quiet=True)
            arguments = launch.call_args.args[0]
            self.assertIn('--headless=new', arguments)
            self.assertIn('about:blank', arguments)
            self.assertNotIn('--no-startup-window', arguments)
            self.assertIs(json.loads((root / 'xiaohongshu-handoff/browser.json').read_text())['headless'], True)

    def test_explicit_editor_launch_still_has_a_startup_page(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            binary = root / 'browsers' / 'Google Chrome for Testing'
            binary.parent.mkdir(); binary.write_text('synthetic; never executed')
            with patch.dict(os.environ, PUBLISHER_DATA_DIR=str(root)), \
                 patch.object(handoff, 'BROWSER_ROOT', binary.parent), \
                 patch.object(handoff, '_endpoint_ready', return_value=True), \
                 patch.object(handoff.socket, 'socket') as reserve, \
                 patch.object(handoff.subprocess, 'Popen', return_value=SimpleNamespace(pid=4321, poll=lambda: None)) as launch:
                reserve.return_value.__enter__.return_value.getsockname.return_value = ('127.0.0.1', 12345)
                handoff._start_detached(binary, 'a' * 64, 'xiaohongshu')
            self.assertNotIn('--no-startup-window', launch.call_args.args[0])
            self.assertNotIn('--headless=new', launch.call_args.args[0])
            self.assertIn('about:blank', launch.call_args.args[0])


class OwnedBrowserModeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.binary = self.root / 'browsers' / 'Google Chrome for Testing'
        self.binary.parent.mkdir(); self.binary.write_text('synthetic; never executed')
        self.binding = 'a' * 64
        self.directory = self.root / 'xiaohongshu-handoff'; self.directory.mkdir()
        self.profile = self.directory / 'profiles' / self.binding
        self.profile.mkdir(parents=True)
        (self.profile / 'publisher-profile.json').write_text(json.dumps({
            'platform': 'xiaohongshu', 'binding_sha256': self.binding, 'session_sha256': self.binding}))
        self.state = {'pid': 4321, 'port': 12345, 'profile': str(self.profile), 'platform': 'xiaohongshu',
            'binding_sha256': self.binding, 'session_sha256': self.binding,
            'proxy_hash': hashlib.sha256(b'').hexdigest(), 'headless': True}
        self.state_file = self.directory / 'browser.json'
        self.state_file.write_text(json.dumps(self.state))

    def start_patches(self):
        patches = [patch.dict(os.environ, PUBLISHER_DATA_DIR=str(self.root)),
            patch.object(handoff, 'BROWSER_ROOT', self.binary.parent),
            patch.object(handoff, '_pid_alive', return_value=True),
            patch.object(handoff, '_owned_process', return_value=True),
            patch.object(handoff, '_endpoint_ready', return_value=True)]
        for item in patches:
            item.start(); self.addCleanup(item.stop)

    def test_explicit_editor_switches_only_headless_process_and_reuses_profile(self):
        self.start_patches()
        with patch.object(handoff, '_stop_owned_headless_browser') as stop, \
             patch.object(handoff.socket, 'socket') as reserve, \
             patch.object(handoff.subprocess, 'Popen', return_value=SimpleNamespace(pid=9876, poll=lambda: None)) as launch:
            reserve.return_value.__enter__.return_value.getsockname.return_value = ('127.0.0.1', 23456)
            handoff._start_detached(self.binary, self.binding, 'xiaohongshu', quiet=False)
        stop.assert_called_once_with(self.state, self.profile)
        arguments = launch.call_args.args[0]
        self.assertIn('--user-data-dir=' + str(self.profile), arguments)
        self.assertNotIn('--headless=new', arguments)
        saved = json.loads(self.state_file.read_text())
        self.assertEqual(saved['profile'], str(self.profile)); self.assertIs(saved['headless'], False)

    def test_quiet_check_reuses_ordinary_browser_without_closing_or_relaunching(self):
        self.state['headless'] = False; self.state_file.write_text(json.dumps(self.state))
        self.start_patches()
        with patch.object(handoff, '_stop_owned_headless_browser') as stop, patch.object(handoff.subprocess, 'Popen') as launch:
            endpoint = handoff._start_detached(self.binary, self.binding, 'xiaohongshu', quiet=True)
        self.assertEqual(endpoint, 'http://127.0.0.1:12345')
        stop.assert_not_called(); launch.assert_not_called()

    def test_resume_does_not_treat_headless_probe_browser_as_existing_editor(self):
        self.start_patches()
        with patch.object(handoff, '_stop_owned_headless_browser') as stop, patch.object(handoff.subprocess, 'Popen') as launch:
            with self.assertRaises(handoff.HandoffError) as caught:
                handoff._start_detached(self.binary, self.binding, 'xiaohongshu', require_existing=True)
        self.assertEqual(caught.exception.reason, 'browser_closed')
        stop.assert_not_called(); launch.assert_not_called()

    def test_stop_requires_saved_headless_marker_and_actual_owned_headless_command(self):
        normal = f'{self.binary} --user-data-dir={self.profile} --remote-debugging-port=12345'
        cases = [(False, normal + ' --headless=new'), (True, normal),
            (True, normal + ' --headless=newish'), (True, '--user-data-dir=/different/profile --headless=new')]
        for marker, command in cases:
            with self.subTest(marker=marker, command=command), \
                 patch.object(handoff.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=command)), \
                 patch.object(handoff.os, 'kill') as kill:
                with self.assertRaises(handoff.HandoffError):
                    handoff._stop_owned_headless_browser({**self.state, 'headless': marker}, self.profile)
                kill.assert_not_called()

    def test_verified_headless_stop_targets_only_its_pid_and_waits_for_exit(self):
        command = f'{self.binary} --user-data-dir={self.profile} --headless=new --remote-debugging-port=12345'
        with patch.object(handoff.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=command)), \
             patch.object(handoff.os, 'kill') as kill, \
             patch.object(handoff.os, 'waitpid', side_effect=ChildProcessError), \
             patch.object(handoff, '_pid_alive', return_value=False), \
             patch.object(handoff, '_endpoint_ready', return_value=False):
            handoff._stop_owned_headless_browser(self.state, self.profile)
        kill.assert_called_once_with(4321, signal.SIGTERM)

    def test_failed_mode_switch_does_not_launch_or_replace_the_browser_record(self):
        self.start_patches(); original = self.state_file.read_bytes()
        with patch.object(handoff, '_stop_owned_headless_browser', side_effect=handoff.HandoffError('browser_busy', 'synthetic')), \
             patch.object(handoff.subprocess, 'Popen') as launch:
            with self.assertRaises(handoff.HandoffError):
                handoff._start_detached(self.binary, self.binding, 'xiaohongshu', quiet=False)
        launch.assert_not_called(); self.assertEqual(self.state_file.read_bytes(), original)


class QuietDriverTests(unittest.IsolatedAsyncioTestCase):
    def driver_manager(self, *, fail=False):
        driver = SimpleNamespace(stop=AsyncMock())
        observed = []
        async def start():
            observed.append(os.environ.get('PW_CHROMIUM_ATTACH_TO_OTHER'))
            if fail:
                raise RuntimeError('synthetic driver failure')
            return driver
        manager = SimpleNamespace(start=AsyncMock(side_effect=start))
        return driver, manager, observed

    async def test_driver_attachment_setting_is_local_and_original_value_restored(self):
        driver, manager, observed = self.driver_manager()
        with patch.dict(os.environ, PW_CHROMIUM_ATTACH_TO_OTHER='original'), \
             patch('patchright.async_api.async_playwright', return_value=manager), \
             patch.object(handoff, 'async_playwright', return_value=manager, create=True):
            async with handoff.quiet_publisher_playwright() as active:
                self.assertIs(active, driver)
                self.assertEqual(os.environ.get('PW_CHROMIUM_ATTACH_TO_OTHER'), 'original')
            self.assertEqual(os.environ.get('PW_CHROMIUM_ATTACH_TO_OTHER'), 'original')
        self.assertEqual(observed, ['1'])
        driver.stop.assert_awaited_once()

    async def test_failed_driver_start_restores_absent_environment_setting(self):
        _, manager, observed = self.driver_manager(fail=True)
        with patch.dict(os.environ):
            os.environ.pop('PW_CHROMIUM_ATTACH_TO_OTHER', None)
            with patch('patchright.async_api.async_playwright', return_value=manager), \
                 patch.object(handoff, 'async_playwright', return_value=manager, create=True):
                with self.assertRaises(RuntimeError):
                    async with handoff.quiet_publisher_playwright():
                        self.fail('failed driver must not yield')
            self.assertNotIn('PW_CHROMIUM_ATTACH_TO_OTHER', os.environ)
        self.assertEqual(observed, ['1'])


class HiddenPageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        class Page:
            def __init__(self, url):
                self.url = url
                self.close = AsyncMock()
        self.editor = Page('https://creator.xiaohongshu.com/publish/editor')
        self.concurrent_page = Page('https://creator.xiaohongshu.com/another-editor')
        self.probe = Page('about:blank')
        self.context = SimpleNamespace(pages=[self.editor], new_page=AsyncMock(), close=AsyncMock())
        self.page_sessions = []
        async def page_cdp(page):
            target = 'probe-owned' if page is self.probe else 'user-editor'
            session = SimpleNamespace(send=AsyncMock(return_value={'targetInfo': {'targetId': target}}), detach=AsyncMock())
            self.page_sessions.append(session)
            return session
        self.context.new_cdp_session = AsyncMock(side_effect=page_cdp)
        async def root_send(method, params=None):
            if method == 'Target.createTarget':
                self.context.pages.extend([self.concurrent_page, self.probe])
                return {'targetId': 'probe-owned'}
            if method == 'Browser.getWindowForTarget':
                raise RuntimeError('No window found')
            if method == 'Target.closeTarget':
                return {'success': True}
            raise AssertionError('unexpected browser CDP method: ' + method)
        self.cdp = SimpleNamespace(send=AsyncMock(side_effect=root_send), detach=AsyncMock())
        self.browser = SimpleNamespace(new_browser_cdp_session=AsyncMock(return_value=self.cdp), close=AsyncMock())

    def assert_only_owned_target_closed(self):
        closed = [call.args[1]['targetId'] for call in self.cdp.send.call_args_list if call.args[0] == 'Target.closeTarget']
        self.assertEqual(closed, ['probe-owned'])
        self.context.new_page.assert_not_awaited()
        self.editor.close.assert_not_awaited()
        self.concurrent_page.close.assert_not_awaited()
        self.context.close.assert_not_awaited()
        self.browser.close.assert_not_awaited()

    async def test_hidden_target_is_matched_by_identity_not_page_order(self):
        async with handoff.hidden_auth_page(self.browser, self.context) as page:
            self.assertIs(page, self.probe)
        created = next(call for call in self.cdp.send.call_args_list if call.args[0] == 'Target.createTarget')
        self.assertEqual(created.args[1]['url'], 'about:blank')
        self.assertIs(created.args[1]['hidden'], True)
        self.assertIs(created.args[1]['background'], True)
        self.assert_only_owned_target_closed()

    async def test_failure_inside_probe_still_preserves_existing_editor(self):
        with self.assertRaisesRegex(RuntimeError, 'probe body failed'):
            async with handoff.hidden_auth_page(self.browser, self.context):
                raise RuntimeError('probe body failed')
        self.assert_only_owned_target_closed()

    async def test_hidden_target_rejection_never_falls_back_to_visible_tab(self):
        self.cdp.send.side_effect = RuntimeError('synthetic unsupported hidden target')
        with self.assertRaises(RuntimeError):
            async with handoff.hidden_auth_page(self.browser, self.context):
                self.fail('a rejected hidden target cannot yield a page')
        self.context.new_page.assert_not_awaited()
        self.editor.close.assert_not_awaited()
        self.browser.close.assert_not_awaited()

    async def test_unexpected_visible_target_is_rejected_and_only_that_target_is_closed(self):
        original_send = self.cdp.send.side_effect
        async def send(method, params=None):
            if method == 'Browser.getWindowForTarget':
                return {'windowId': 123}
            return await original_send(method, params)
        self.cdp.send.side_effect = send
        with self.assertRaises(handoff.AuthVerificationUnavailable):
            async with handoff.hidden_auth_page(self.browser, self.context):
                self.fail('a target with a window must not be used as a quiet probe')
        self.assert_only_owned_target_closed()

    async def test_actual_bundled_chromium_windowless_response_is_accepted(self):
        original_send = self.cdp.send.side_effect
        async def send(method, params=None):
            if method == 'Browser.getWindowForTarget':
                raise RuntimeError('CDPSession.send: Protocol error (Browser.getWindowForTarget): Browser window not found')
            return await original_send(method, params)
        self.cdp.send.side_effect = send
        async with handoff.hidden_auth_page(self.browser, self.context) as page:
            self.assertIs(page, self.probe)
        self.assert_only_owned_target_closed()

    async def test_unknown_window_check_failure_is_not_treated_as_windowless(self):
        original_send = self.cdp.send.side_effect
        async def send(method, params=None):
            if method == 'Browser.getWindowForTarget':
                raise RuntimeError('synthetic CDP connection broken')
            return await original_send(method, params)
        self.cdp.send.side_effect = send
        with self.assertRaises(handoff.AuthVerificationUnavailable):
            async with handoff.hidden_auth_page(self.browser, self.context):
                self.fail('unknown CDP failure cannot prove a target is windowless')
        self.assert_only_owned_target_closed()


class QuietStorageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.account = Path(self.temp.name) / 'synthetic.json'
        self.creator = 'https://creator.xiaohongshu.com'
        self.www = 'https://www.xiaohongshu.com'
        self.original_cookies = [{'name': 'web_session', 'value': 'synthetic-before', 'domain': '.xiaohongshu.com', 'path': '/'}]
        self.original_origins = [
            {'origin': self.creator, 'localStorage': [{'name': 'token', 'value': 'old'}]},
            {'origin': self.www, 'localStorage': [{'name': 'offline-origin', 'value': 'preserve-me'}]},
        ]
        self.account.write_text(json.dumps({'cookies': self.original_cookies, 'origins': self.original_origins}))
        cookies, storage, binding = handoff._load_session(self.account, 'xiaohongshu')
        snapshot = handoff._session_snapshot(cookies, storage)
        self.probe = SimpleNamespace(url=self.creator + '/publish/publish')
        self.unrelated = SimpleNamespace(url='https://private.example/sensitive')
        self.refreshed_cookies = [{**self.original_cookies[0], 'value': 'synthetic-refreshed'}]
        self.context = SimpleNamespace(
            pages=[self.unrelated, self.probe],
            cookies=AsyncMock(return_value=self.refreshed_cookies + [{'name': 'private', 'value': 'do-not-save', 'domain': '.private.example'}]),
            storage_state=AsyncMock(side_effect=AssertionError('storage_state may open visible pages')),
            new_page=AsyncMock(side_effect=AssertionError('a save must not create any page')),
            _publisher_session_owner={'platform': 'xiaohongshu', 'account_file': str(self.account.resolve()),
                'binding': binding, 'snapshot': snapshot, 'source_binding': binding, 'source_snapshot': snapshot},
        )
        self.cdp_sessions = []
        async def page_cdp(page):
            self.assertIsNot(page, self.unrelated, 'unrelated page contents must stay untouched')
            session = SimpleNamespace(send=AsyncMock(return_value={'entries': [['token', 'renewed']]}), detach=AsyncMock())
            self.cdp_sessions.append(session)
            return session
        self.context.new_cdp_session = AsyncMock(side_effect=page_cdp)

    async def test_probe_save_reads_existing_pages_and_preserves_absent_origin(self):
        await handoff.save_publisher_session(self.context, self.account, 'xiaohongshu', probe_page=self.probe)
        saved = json.loads(self.account.read_text())
        self.assertEqual(saved['cookies'], self.refreshed_cookies)
        origins = {origin['origin']: origin['localStorage'] for origin in saved['origins']}
        self.assertEqual(origins[self.creator], [{'name': 'token', 'value': 'renewed'}])
        self.assertEqual(origins[self.www], self.original_origins[1]['localStorage'])
        self.assertNotIn('private.example', self.account.read_text())
        self.context.storage_state.assert_not_awaited(); self.context.new_page.assert_not_awaited()
        calls = [call for session in self.cdp_sessions for call in session.send.call_args_list]
        reads = [call for call in calls if call.args[0] == 'DOMStorage.getDOMStorageItems']
        self.assertTrue(reads)
        self.assertTrue(all(call.args[1]['storageId']['isLocalStorage'] for call in reads))

    async def test_probe_read_failure_leaves_original_credentials_and_link_untouched(self):
        original = self.account.read_bytes()
        link = handoff._profile_link_path(self.account)
        link.write_text('{"synthetic":"original-link"}')
        original_link = link.read_bytes()
        self.context.new_cdp_session.side_effect = RuntimeError('synthetic unreadable probe')
        with self.assertRaises(handoff.AuthVerificationUnavailable):
            await handoff.save_publisher_session(self.context, self.account, 'xiaohongshu', probe_page=self.probe)
        self.assertEqual(self.account.read_bytes(), original)
        self.assertEqual(link.read_bytes(), original_link)
        self.context.storage_state.assert_not_awaited(); self.context.new_page.assert_not_awaited()

    async def test_existing_other_origin_read_failure_preserves_its_saved_storage(self):
        existing = SimpleNamespace(url=self.www + '/explore')
        self.context.pages.insert(0, existing)
        original_cdp = self.context.new_cdp_session.side_effect
        async def page_cdp(page):
            if page is existing:
                raise RuntimeError('synthetic already-closed editor')
            return await original_cdp(page)
        self.context.new_cdp_session.side_effect = page_cdp
        await handoff.save_publisher_session(self.context, self.account, 'xiaohongshu', probe_page=self.probe)
        saved = json.loads(self.account.read_text())
        stored = next(origin for origin in saved['origins'] if origin['origin'] == self.www)
        self.assertEqual(stored, self.original_origins[1])

    async def test_concurrent_explicit_import_is_not_overwritten_by_quiet_probe(self):
        replacement = {'cookies': [{**self.original_cookies[0], 'value': 'explicit-import'}], 'origins': []}
        self.account.write_text(json.dumps(replacement)); original = self.account.read_bytes()
        with self.assertRaises(handoff.HandoffError):
            await handoff.save_publisher_session(self.context, self.account, 'xiaohongshu', probe_page=self.probe)
        self.assertEqual(self.account.read_bytes(), original)

    async def test_successfully_cleared_storage_is_not_replaced_with_old_values(self):
        session = SimpleNamespace(send=AsyncMock(return_value={'entries': []}), detach=AsyncMock())
        self.context.new_cdp_session = AsyncMock(return_value=session)
        await handoff.save_publisher_session(self.context, self.account, 'xiaohongshu', probe_page=self.probe)
        stored = next(o for o in json.loads(self.account.read_text())['origins'] if o['origin'] == self.creator)
        self.assertEqual(stored['localStorage'], [])

    async def test_redirect_to_unapproved_probe_origin_preserves_all_saved_state(self):
        original = self.account.read_bytes()
        self.probe.url = 'https://unrelated.example/login'
        with self.assertRaises(handoff.AuthVerificationUnavailable):
            await handoff.save_publisher_session(self.context, self.account, 'xiaohongshu', probe_page=self.probe)
        self.context.new_cdp_session.assert_not_awaited()
        self.assertEqual(self.account.read_bytes(), original)


class QuietXhsChecks(unittest.IsolatedAsyncioTestCase):
    async def test_xhs_check_uses_quiet_connection_and_probe_save(self):
        with tempfile.TemporaryDirectory() as folder:
            account = Path(folder) / 'synthetic.json'; account.write_text('{}')
            driver = object(); browser = object()
            page = SimpleNamespace(goto=AsyncMock(return_value=SimpleNamespace(status=200)))
            context = SimpleNamespace(new_page=AsyncMock(side_effect=AssertionError('no visible fallback')))
            @asynccontextmanager
            async def quiet_driver():
                yield driver
            @asynccontextmanager
            async def quiet_page(actual_browser, actual_context):
                self.assertIs(actual_browser, browser); self.assertIs(actual_context, context)
                yield page
            with patch.object(handoff, 'quiet_publisher_playwright', quiet_driver), \
                 patch.object(handoff, 'hidden_auth_page', quiet_page), \
                 patch.object(handoff, 'connect_publisher_handoff', new=AsyncMock(return_value=(browser, context))) as connect, \
                 patch.object(handoff, 'save_publisher_session', new=AsyncMock()) as save, \
                 patch.object(xhs, '_xhs_auth_state', new=AsyncMock(return_value=True)):
                self.assertTrue(await xhs.cookie_auth(account))
            self.assertIs(connect.call_args.kwargs['quiet'], True)
            save.assert_awaited_once_with(context, account, 'xiaohongshu', probe_page=page)
            context.new_page.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
