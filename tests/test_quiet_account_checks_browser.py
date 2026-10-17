"""Opt-in quiet Chromium tests: temporary profile, local HTTP, synthetic cookies.

Cold browsers use headless mode; each probe target is additionally hidden and
checked for the absence of a browser window. These tests never read an installed
account, visit a platform, or create a visible tab.
"""
import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from utils import publisher_handoff as handoff
from uploader.xiaohongshu_uploader import main as xhs

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get('PUBLISHER_TEST_BROWSER') == '1', 'opt-in isolated windowless Chromium test')
class QuietBrowserTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        class FixtureHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.end_headers()
                self.wfile.write(b'<html><input type="file" accept="video/mp4"><input id="draft" value="untouched"></html>')
            def log_message(self, *args):
                pass
        self.fixture = ThreadingHTTPServer(('127.0.0.1', 0), FixtureHandler)
        threading.Thread(target=self.fixture.serve_forever, daemon=True).start()
        self.addCleanup(self.fixture.server_close)
        self.addCleanup(self.fixture.shutdown)
        self.origin = 'http://127.0.0.1:' + str(self.fixture.server_port)
        self.temp = tempfile.TemporaryDirectory(prefix='publisher-quiet-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.account = self.root / 'synthetic.json'
        self.state_file = self.root / 'xiaohongshu-handoff' / 'browser.json'
        self.account.write_text(json.dumps({
            'cookies': [{'name': 'web_session', 'value': 'synthetic-before', 'domain': '127.0.0.1', 'path': '/', 'httpOnly': True, 'secure': False}],
            'origins': [{'origin': self.origin, 'localStorage': [{'name': 'imported', 'value': 'saved-before'}]}],
        }))
        self.environment = patch.dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=str(ROOT / '.browsers'),
            PUBLISHER_DATA_DIR=str(self.root), SAU_XHS_CREATOR_BASE_URL=self.origin)
        self.scope = patch.dict(handoff.SPECS, xiaohongshu=('Local fixture', '127.0.0.1', (self.origin,), ('web_session',)))
        self.environment.start(); self.scope.start()
        self.addCleanup(self.environment.stop); self.addCleanup(self.scope.stop)

    async def asyncTearDown(self):
        await self.stop_test_browser()

    async def stop_test_browser(self):
        if not self.state_file.exists():
            return
        state = json.loads(self.state_file.read_text())
        profile = Path(state['profile']).resolve()
        self.assertTrue(profile.is_relative_to(self.root), 'test may only stop its temporary browser')
        if handoff._pid_alive(state['pid']):
            self.assertTrue(handoff._owned_process(state['pid'], profile), 'test browser PID ownership changed')
            os.kill(state['pid'], signal.SIGTERM)
        for _ in range(100):
            try:
                exited, _ = os.waitpid(state['pid'], os.WNOHANG)
            except ChildProcessError:
                exited = 0 if handoff._pid_alive(state['pid']) else state['pid']
            if exited or not handoff._pid_alive(state['pid']):
                return
            await asyncio.sleep(.05)
        self.fail('temporary test browser did not stop')

    async def assert_windowless(self, browser, context, page):
        page_session = await context.new_cdp_session(page)
        try:
            target_id = (await page_session.send('Target.getTargetInfo'))['targetInfo']['targetId']
        finally:
            await page_session.detach()
        session = await browser.new_browser_cdp_session()
        try:
            with self.assertRaisesRegex(Exception, 'No window found|Browser window not found'):
                await session.send('Browser.getWindowForTarget', {'targetId': target_id})
        finally:
            await session.detach()

    def assert_headless_baseline(self, context, *, allow_restored_blank=False):
        state = json.loads(self.state_file.read_text())
        self.assertIs(state['headless'], True)
        command = subprocess.run(['ps', '-p', str(state['pid']), '-o', 'command='], capture_output=True, text=True, check=True).stdout
        self.assertIn('--headless=new', command.split())
        self.assertIn('--user-data-dir=' + state['profile'], command.split())
        urls = [page.url for page in context.pages]
        self.assertTrue(urls)
        if allow_restored_blank:
            # Chrome can restore its previous empty startup tab in addition
            # to the explicit command-line blank. Both remain headless.
            self.assertTrue(all(url == 'about:blank' for url in urls))
        else:
            self.assertEqual(urls, ['about:blank'])
        return list(context.pages)

    async def test_hidden_probe_preserves_existing_editor_and_saves_without_implicit_pages(self):
        async with handoff.quiet_publisher_playwright() as driver:
            browser, context = await handoff.connect_publisher_handoff(driver, self.account, 'xiaohongshu', quiet=True)
            baseline = self.assert_headless_baseline(context)
            with patch.object(context, 'new_page', new=AsyncMock(side_effect=AssertionError('no visible new_page'))), \
                 patch.object(context, 'storage_state', new=AsyncMock(side_effect=AssertionError('no implicit storage page'))):
                async with handoff.hidden_auth_page(browser, context) as editor:
                    await editor.goto(self.origin + '/existing-editor')
                    await editor.locator('#draft').fill('keep this draft')
                    await self.assert_windowless(browser, context, editor)
                    async with handoff.hidden_auth_page(browser, context) as probe:
                        await probe.goto(self.origin + '/publish/publish')
                        await self.assert_windowless(browser, context, probe)
                        self.assertTrue(await xhs._xhs_auth_state(probe))
                        self.assertEqual(await probe.evaluate("localStorage.getItem('imported')"), 'saved-before')
                        await probe.evaluate("localStorage.setItem('imported', 'refreshed-locally')")
                        await context.add_cookies([{'name': 'web_session', 'value': 'synthetic-refreshed', 'domain': '127.0.0.1', 'path': '/', 'httpOnly': True, 'secure': False}])
                        await handoff.save_publisher_session(context, self.account, 'xiaohongshu', probe_page=probe)
                        self.assertEqual(len(context.pages), len(baseline) + 2)
                    self.assertEqual(context.pages, [*baseline, editor])
                    self.assertFalse(editor.is_closed())
                    self.assertEqual(editor.url, self.origin + '/existing-editor')
                    self.assertEqual(await editor.locator('#draft').input_value(), 'keep this draft')
                self.assertEqual(context.pages, baseline)
            state = json.loads(self.state_file.read_text())
        self.assertTrue(handoff._pid_alive(state['pid']), 'driver disconnect must not close the owned browser')
        saved = json.loads(self.account.read_text())
        self.assertEqual(next(c['value'] for c in saved['cookies'] if c['name'] == 'web_session'), 'synthetic-refreshed')
        self.assertEqual(saved['origins'][0]['localStorage'], [{'name': 'imported', 'value': 'refreshed-locally'}])
        async with handoff.quiet_publisher_playwright() as driver:
            browser, context = await handoff.connect_publisher_handoff(driver, self.account, 'xiaohongshu', quiet=True)
            self.assertEqual(json.loads(self.state_file.read_text())['pid'], state['pid'])
            self.assert_headless_baseline(context)
            async with handoff.hidden_auth_page(browser, context) as probe:
                await probe.goto(self.origin + '/publish/publish')
                self.assertTrue(await xhs._xhs_auth_state(probe))
                self.assertEqual(await probe.evaluate("localStorage.getItem('imported')"), 'refreshed-locally')

    async def test_saved_session_survives_windowless_browser_restart_in_same_profile(self):
        async with handoff.quiet_publisher_playwright() as driver:
            browser, context = await handoff.connect_publisher_handoff(driver, self.account, 'xiaohongshu', quiet=True)
            self.assert_headless_baseline(context)
            async with handoff.hidden_auth_page(browser, context) as probe:
                await probe.goto(self.origin + '/publish/publish')
                await probe.evaluate("localStorage.setItem('roundtrip', 'retained')")
                await context.add_cookies([{'name': 'web_session', 'value': 'synthetic-after-renewal', 'domain': '127.0.0.1', 'path': '/', 'httpOnly': True, 'secure': False}])
                await handoff.save_publisher_session(context, self.account, 'xiaohongshu', probe_page=probe)
            initial = json.loads(self.state_file.read_text())
        await self.stop_test_browser()
        async with handoff.quiet_publisher_playwright() as driver:
            browser, context = await handoff.connect_publisher_handoff(driver, self.account, 'xiaohongshu', quiet=True)
            current = json.loads(self.state_file.read_text())
            self.assertEqual(current['profile'], initial['profile'])
            self.assertNotEqual(current['pid'], initial['pid'])
            baseline = self.assert_headless_baseline(context, allow_restored_blank=True)
            self.assertEqual(next(c['value'] for c in await context.cookies() if c['name'] == 'web_session'), 'synthetic-after-renewal')
            async with handoff.hidden_auth_page(browser, context) as probe:
                self.assertEqual(len(context.pages), len(baseline) + 1)
                await probe.goto(self.origin + '/publish/publish')
                await self.assert_windowless(browser, context, probe)
                self.assertTrue(await xhs._xhs_auth_state(probe))
                self.assertEqual(await probe.evaluate("localStorage.getItem('roundtrip')"), 'retained')
            self.assertEqual(context.pages, baseline)


if __name__ == '__main__':
    unittest.main()
