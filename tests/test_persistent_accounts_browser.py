"""Opt-in real Chromium restart test with intercepted pages and synthetic cookies.

No real account is read; all page requests are fulfilled from this fixture.
"""
import asyncio
import json
import os
from pathlib import Path
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import tempfile
import unittest
from unittest.mock import patch
from utils import publisher_handoff as handoff
from uploader.xiaohongshu_uploader import main as xhs

ROOT = Path(__file__).resolve().parents[1]

@unittest.skipUnless(os.environ.get('PUBLISHER_TEST_BROWSER') == '1', 'opt-in isolated Chromium test')
class PersistentBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_imported_storage_does_not_overwrite_fresh_values_on_navigation(self):
        from patchright.async_api import async_playwright
        class FixtureHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200); self.send_header('Content-Type', 'text/html'); self.end_headers()
                self.wfile.write(b'<html>Local fixture only</html>')
            def log_message(self, *args): pass
        fixture = ThreadingHTTPServer(('127.0.0.1', 0), FixtureHandler)
        threading.Thread(target=fixture.serve_forever, daemon=True).start()
        self.addCleanup(fixture.server_close); self.addCleanup(fixture.shutdown)
        origin = 'http://127.0.0.1:' + str(fixture.server_port)
        with tempfile.TemporaryDirectory(prefix='publisher-storage-test-') as folder:
            root = Path(folder).resolve(); account = root/'synthetic.json'; state_file = root/'xiaohongshu-handoff/browser.json'
            account.write_text(json.dumps({'cookies': [{'name': 'web_session', 'value': 'synthetic', 'domain': '127.0.0.1', 'path': '/'}],
                'origins': [{'origin': origin, 'localStorage': [{'name': 'imported-state', 'value': 'initial'}]}]}))
            try:
                with patch.dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=str(ROOT/'.browsers'), PUBLISHER_DATA_DIR=str(root)), patch.dict(handoff.SPECS, xiaohongshu=('Test', '127.0.0.1', (origin,), ('web_session',))):
                    async with async_playwright() as p:
                        browser, context = await handoff.connect_publisher_handoff(p, account, 'xiaohongshu')
                        page = await context.new_page(); await page.goto(origin)
                        self.assertEqual(await page.evaluate("localStorage.getItem('imported-state')"), 'initial')
                        await page.evaluate("localStorage.setItem('imported-state','platform-refreshed')")
                        await page.reload()
                        self.assertEqual(await page.evaluate("localStorage.getItem('imported-state')"), 'platform-refreshed')
                        await browser.close()
            finally:
                if state_file.exists():
                    state = json.loads(state_file.read_text())
                    if handoff._pid_alive(state['pid']): os.kill(state['pid'], signal.SIGTERM)
                    for _ in range(50):
                        if not handoff._endpoint_ready(state['port']): break
                        await asyncio.sleep(.1)

    async def test_three_browser_restarts_reuse_profile_and_saved_session(self):
        from patchright.async_api import async_playwright
        class FixtureHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200); self.send_header('Content-Type', 'text/html'); self.end_headers()
                self.wfile.write(b'<html><input type="file" accept="video/mp4"></html>')
            def log_message(self, *args): pass
        fixture = ThreadingHTTPServer(('127.0.0.1', 0), FixtureHandler)
        threading.Thread(target=fixture.serve_forever, daemon=True).start()
        self.addCleanup(fixture.server_close); self.addCleanup(fixture.shutdown)
        origin = 'http://127.0.0.1:' + str(fixture.server_port)
        with tempfile.TemporaryDirectory(prefix='publisher-account-reuse-test-') as folder:
            root = Path(folder).resolve(); account = root/'synthetic-xhs.json'; state_file = root/'xiaohongshu-handoff/browser.json'
            account.write_text(json.dumps({'cookies': [{'name': 'web_session', 'value': 'synthetic-first-login',
                'domain': '127.0.0.1', 'path': '/', 'secure': False, 'httpOnly': True}], 'origins': []}))
            original_profile = None
            try:
                with patch.dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=str(ROOT/'.browsers'), PUBLISHER_DATA_DIR=str(root), SAU_XHS_CREATOR_BASE_URL=origin), patch.dict(handoff.SPECS, xiaohongshu=('Test', '127.0.0.1', (origin,), ('web_session',))):
                    for attempt in range(3):
                        async with async_playwright() as p:
                            browser, context = await handoff.connect_publisher_handoff(p, account, 'xiaohongshu')
                            state = json.loads(state_file.read_text())
                            if original_profile is None: original_profile = state['profile']
                            self.assertEqual(state['profile'], original_profile)
                            await context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body='<html><input type="file" accept="video/mp4"></html>'))
                            page = await context.new_page()
                            await page.goto(origin + '/publish/publish')
                            self.assertTrue(await xhs._xhs_auth_state(page))
                            if attempt:
                                self.assertEqual(await page.evaluate("localStorage.getItem('synthetic-persistence')"), 'kept-locally')
                            await page.evaluate("localStorage.setItem('synthetic-persistence','kept-locally')")
                            await context.add_cookies([{'name': 'web_session', 'value': 'synthetic-refresh-'+str(attempt),
                                'domain': '127.0.0.1', 'path': '/', 'secure': False, 'httpOnly': True}])
                            await handoff.save_publisher_session(context, account, 'xiaohongshu')
                            # Disconnecting the service keeps the editor alive.
                        self.assertTrue(handoff._pid_alive(state['pid']))
                        async with async_playwright() as p:
                            browser, context = await handoff.connect_publisher_handoff(p, account, 'xiaohongshu')
                            self.assertEqual(json.loads(state_file.read_text())['pid'], state['pid'])
                            self.assertTrue(any((origin + '/publish') in pg.url for pg in context.pages))
                            await browser.close()
                        if handoff._pid_alive(state['pid']): os.kill(state['pid'], signal.SIGTERM)
                        for _ in range(50):
                            if not handoff._endpoint_ready(state['port']): break
                            await asyncio.sleep(.1)
                        self.assertFalse(handoff._endpoint_ready(state['port']))
                    self.assertEqual(next(c['value'] for c in json.loads(account.read_text())['cookies'] if c['name']=='web_session'), 'synthetic-refresh-2')
            finally:
                if state_file.exists():
                    state = json.loads(state_file.read_text())
                    if handoff._pid_alive(state['pid']):
                        os.kill(state['pid'], signal.SIGTERM)
                        for _ in range(50):
                            if not handoff._endpoint_ready(state['port']): break
                            await asyncio.sleep(.1)

if __name__ == '__main__': unittest.main()
