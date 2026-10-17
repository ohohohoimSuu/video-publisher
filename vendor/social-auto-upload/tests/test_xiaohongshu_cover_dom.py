"""Opt-in L2 coverage: real sandboxed Chromium, offline controlled HTML only.

PUBLISHER_TEST_XHS_DOM=1 [PUBLISHER_TEST_CHROMIUM_PATH=/path/to/chromium] \\
  python -m unittest discover -s vendor/social-auto-upload/tests -p 'test_xiaohongshu_cover_dom.py' -v

No accounts, platform requests, video transfers, or final publishing are used.
Chromium sandbox must remain enabled. A failed browser launch is a failure,
not a skipped/passing test; environments without browser support omit opt-in.
"""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from patchright.async_api import async_playwright
from uploader.xiaohongshu_uploader import main as xhs


@unittest.skipUnless(os.environ.get("PUBLISHER_TEST_XHS_DOM") == "1", "opt-in sandboxed offline DOM integration")
class CoverDOMTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.driver = await async_playwright().start()
        self.addAsyncCleanup(self.driver.stop)
        options = {"headless": True, "chromium_sandbox": True}
        if os.environ.get("PUBLISHER_TEST_CHROMIUM_PATH"):
            options["executable_path"] = os.environ["PUBLISHER_TEST_CHROMIUM_PATH"]
        self.browser = await self.driver.chromium.launch(**options)
        self.addAsyncCleanup(self.browser.close)
        self.context = await self.browser.new_context(service_workers="block")
        self.addAsyncCleanup(self.context.close)
        self.folder = tempfile.TemporaryDirectory(prefix="xhs-offline-dom-")
        self.addCleanup(self.folder.cleanup)
        self.cover = Path(self.folder.name) / "cover.png"
        Image.new("RGB", (640, 360), "green").save(self.cover)
        html = (Path(__file__).parent / "fixtures" / "xiaohongshu_cover.html").read_text()
        # All HTTP(S) is intercepted: this synthetic secure origin is never
        # resolved or contacted. It makes WebCrypto available to the real code.
        async def offline(route):
            if route.request.url == "https://cover-fixture.invalid/":
                await route.fulfill(status=200, content_type="text/html", body=html)
            elif route.request.url == "https://tainted-fixture.invalid/cover.png":
                # Deliberately omit CORS headers for a genuine browser-tainted
                # canvas. This other synthetic origin is also intercepted.
                await route.fulfill(status=200, content_type="image/png", body=self.cover.read_bytes())
            else:
                await route.abort()
        await self.context.route("**/*", offline)
        self.page = await self.context.new_page()
        await self.page.goto("https://cover-fixture.invalid/")

    async def configure(self, **options):
        await self.page.evaluate("options => window.configure(options)", options, isolated_context=False)

    async def test_hidden_old_editor_and_ai_controls_are_not_used(self):
        await self.configure(delay=650)
        await xhs.XiaoHongShuVideo.set_thumbnail(None, self.page, str(self.cover))
        self.assertEqual(await self.page.evaluate("window.events", isolated_context=False), ["cover-file", "preview-loaded", "save"])
        self.assertTrue(await self.page.locator("#editor").is_hidden())

    async def test_legacy_editor_also_waits_for_decoded_changed_preview(self):
        await self.configure(legacy=True, delay=650)
        await xhs.XiaoHongShuVideo.set_thumbnail(None, self.page, str(self.cover))
        self.assertEqual(await self.page.evaluate("window.events", isolated_context=False), ["cover-file", "preview-loaded", "save"])

    async def test_unchanged_large_preview_rejects_enabled_save_and_changed_small_reference(self):
        await self.configure(unchanged=True, smallReference=True)
        real_wait = xhs._wait_for_cover_preview
        async def bounded(*args): return await real_wait(*args, timeout_seconds=1.8)
        with patch.object(xhs, "_wait_for_cover_preview", side_effect=bounded), self.assertRaises(RuntimeError):
            await xhs.XiaoHongShuVideo.set_thumbnail(None, self.page, str(self.cover))
        self.assertEqual(await self.page.evaluate("window.events", isolated_context=False), ["cover-file", "preview-loaded"])
        self.assertTrue(await self.page.locator("#save").is_enabled())

    async def test_unreadable_canvas_never_selects_cover_or_saves(self):
        await self.configure()
        await self.page.evaluate("""async () => {
            const image = new Image();
            image.src = 'https://tainted-fixture.invalid/cover.png';
            await image.decode();
            document.querySelector('#preview').getContext('2d').drawImage(image, 0, 0);
        }""")
        real_wait = xhs._wait_for_initial_cover_preview
        async def bounded(*args): return await real_wait(*args, timeout_seconds=0.8)
        with patch.object(xhs, "_wait_for_initial_cover_preview", side_effect=bounded), self.assertRaises(RuntimeError):
            await xhs.XiaoHongShuVideo.set_thumbnail(None, self.page, str(self.cover))
        self.assertEqual(await self.page.evaluate("window.events", isolated_context=False), [])

    async def test_closing_editor_during_decode_never_saves(self):
        await self.configure(delay=1500)
        await self.page.evaluate("document.querySelector('#cover-input').addEventListener('change', () => setTimeout(() => document.querySelector('#editor').hidden = true, 50))")
        real_wait = xhs._wait_for_cover_preview
        async def bounded(*args): return await real_wait(*args, timeout_seconds=0.8)
        with patch.object(xhs, "_wait_for_cover_preview", side_effect=bounded), self.assertRaises(RuntimeError):
            await xhs.XiaoHongShuVideo.set_thumbnail(None, self.page, str(self.cover))
        self.assertNotIn("save", await self.page.evaluate("window.events", isolated_context=False))


if __name__ == "__main__":
    unittest.main()
