"""Opt-in real-browser regression: no real account or external page is used.

Run PUBLISHER_TEST_BROWSER=1 python -m unittest tests.test_handoff_browser
after installing this project's pinned browser runtime.
"""
import asyncio
import json
import os
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import patch

from tests.test_handoff_sessions import ROOT, handoff


@unittest.skipUnless(os.environ.get('PUBLISHER_TEST_BROWSER') == '1', 'opt-in local browser integration')
class HandoffBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_local_files_and_editor_survival_on_every_prepare_platform(self):
        from patchright.async_api import async_playwright
        with tempfile.TemporaryDirectory(prefix='publisher-browser-test-') as folder:
            root = Path(folder).resolve()
            large_file = root/'synthetic.mp4'
            with large_file.open('wb') as handle: handle.truncate(64*1024*1024)
            with patch.dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=str(ROOT/'.browsers'), PUBLISHER_DATA_DIR=str(root)):
                for platform, spec in handoff.SPECS.items():
                    with self.subTest(platform=platform):
                        account = root/f'{platform}.json'
                        account.write_text(json.dumps({'cookies':[{'name':spec[3][0], 'value':'synthetic-local-only',
                            'domain':'.'+spec[1], 'path':'/', 'secure':True, 'httpOnly':True}], 'origins':[]}))
                        state_file = root/f'{platform}-handoff/browser.json'
                        try:
                            async with async_playwright() as p:
                                browser, context = await handoff.connect_publisher_handoff(p, account, platform)
                                page = await context.new_page()
                                await page.set_content('<input type="file" id="fixture" accept="video/*">')
                                await page.locator('#fixture').set_input_files(str(large_file))
                                self.assertEqual(await page.locator('#fixture').evaluate('(e) => e.files[0].size'), 64*1024*1024)
                                self.assertEqual(await page.locator('#fixture').evaluate('async (e) => Array.from(new Uint8Array(await e.files[0].slice(0,8).arrayBuffer()))'), [0]*8)
                                pages = len(context.pages)
                            state = json.loads(state_file.read_text())
                            self.assertTrue(handoff._pid_alive(state['pid']) and handoff._endpoint_ready(state['port']))
                            async with async_playwright() as p:
                                browser, context = await handoff.connect_publisher_handoff(p, account, platform)
                                self.assertEqual(len(context.pages), pages)
                                await context.new_page()
                                self.assertEqual(len(context.pages), pages+1)
                                self.assertTrue(all(page.url == 'about:blank' for page in context.pages))
                            self.assertEqual(json.loads(state_file.read_text())['pid'], state['pid'])
                        finally:
                            if state_file.exists():
                                pid = json.loads(state_file.read_text()).get('pid')
                                if pid and handoff._pid_alive(pid):
                                    os.kill(pid, signal.SIGTERM)
                                    for _ in range(30):
                                        if not handoff._pid_alive(pid): break
                                        await asyncio.sleep(.1)


if __name__ == '__main__': unittest.main()
