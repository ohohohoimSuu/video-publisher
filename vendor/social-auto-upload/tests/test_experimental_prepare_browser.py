"""Opt-in L2 DOM checks. Only controlled about:blank documents and local assets.

PUBLISHER_TEST_EXPERIMENTAL_BROWSER=1 PUBLISHER_TEST_CHROMIUM_PATH=/usr/bin/chromium
Never disable Chromium's sandbox to make these tests pass. They do not test a
platform's live DOM, account login, or detached browser-process lifetime.
"""
import os
from pathlib import Path
import tempfile
import unittest

from uploader.bilibili_uploader.prepare import BilibiliFixtureAdapter, convert_native_session
from uploader.tencent_uploader.prepare import WeChatChannelsFixtureAdapter
from utils.experimental_prepare import PrepareFailure, PrepareMaterials

FIXTURE = Path(__file__).parent / "fixtures" / "experimental_editor.html"
NATIVE = b'{"cookie_info":{"cookies":[{"name":"SESSDATA","value":"synthetic"},{"name":"bili_jct","value":"synthetic"},{"name":"DedeUserID","value":"42"}]}}'


@unittest.skipUnless(os.environ.get("PUBLISHER_TEST_EXPERIMENTAL_BROWSER") == "1", "Opt-in controlled browser fixture; no live platform")
class ExperimentalBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from patchright.async_api import async_playwright
        from PIL import Image
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.video, self.cover = root / "synthetic-64mb.mp4", root / "synthetic-cover.png"
        with self.video.open("wb") as file: file.truncate(64 * 1024 * 1024)
        Image.new("RGB", (64, 64), "#ed4928").save(self.cover)
        self.materials = PrepareMaterials(self.video, "原始标题", "第一行\n第二行", ("原始话题", "tag two"), self.cover)
        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        options = {"headless": True, "chromium_sandbox": True}
        if os.environ.get("PUBLISHER_TEST_CHROMIUM_PATH"):
            options["executable_path"] = os.environ["PUBLISHER_TEST_CHROMIUM_PATH"]
        self.browser = await self.playwright.chromium.launch(**options)
        self.addAsyncCleanup(self.browser.close)
        print("Controlled experimental fixture Chromium:", self.browser.version)
        self.context = await self.browser.new_context()
        self.addAsyncCleanup(self.context.close)
        self.network_requests = []
        async def deny_network(route):
            self.network_requests.append(route.request.url)
            await route.abort()
        await self.context.route("**/*", deny_network)

    async def page_and_driver(self, platform, *, timeout=3):
        page = await self.context.new_page()
        await page.set_content(FIXTURE.read_text().replace("PLATFORM-v1", platform + "-v1"))
        await page.locator('[data-publisher="cover-preview"]').evaluate("img => img.decode()")
        if platform == "bilibili":
            driver = BilibiliFixtureAdapter(timeout_seconds=timeout)
            arguments = {"session": convert_native_session(NATIVE, expected_account_id="42"),
                         "identity_payload": {"code": 0, "data": {"isLogin": True, "mid": 42}}, "expected_account_id": "42"}
        else:
            driver, arguments = WeChatChannelsFixtureAdapter(timeout_seconds=timeout), {"expected_account": "42"}
        return page, driver, arguments

    async def test_both_platform_fixtures_select_64mb_fill_cover_and_retain_editor(self):
        for platform in ("bilibili", "tencent"):
            page, driver, arguments = await self.page_and_driver(platform)
            result = await driver.prepare_fixture(page, self.materials, **arguments)
            self.assertEqual(result.status, "fixture_prepared")
            self.assertFalse(page.is_closed())
            self.assertEqual(await page.locator('[data-publisher="description"]').input_value(), self.materials.description)
            self.assertEqual(await page.locator('[data-publisher="video-file"]').evaluate("node => node.files[0].size"), 64 * 1024 * 1024)
            self.assertEqual(await page.evaluate("fixtureControls.publishClicks", isolated_context=False), 0)
        self.assertEqual(self.network_requests, [])

    async def test_enabled_final_control_does_not_short_circuit_stalled_video(self):
        page, driver, arguments = await self.page_and_driver("tencent", timeout=.25)
        await page.evaluate("fixtureControls.stallVideo = true", isolated_context=False)
        with self.assertRaises(PrepareFailure) as raised: await driver.prepare_fixture(page, self.materials, **arguments)
        self.assertEqual(raised.exception.reason, "video_not_ready")
        self.assertEqual(await page.evaluate("fixtureControls.publishClicks", isolated_context=False), 0)

    async def test_cover_failure_continues_same_page_without_second_video_selection(self):
        page, driver, arguments = await self.page_and_driver("bilibili", timeout=.3)
        await page.evaluate("fixtureControls.freezeCover = true", isolated_context=False)
        with self.assertRaises(PrepareFailure): await driver.prepare_fixture(page, self.materials, **arguments)
        await page.evaluate("fixtureControls.freezeCover = false", isolated_context=False)
        result = await driver.resume_cover_fixture(page, self.materials, driver.checkpoint, **arguments)
        self.assertEqual(result.status, "fixture_prepared")
        self.assertEqual(await page.evaluate("fixtureControls.videoSelections", isolated_context=False), 1)
        self.assertEqual(await page.evaluate("fixtureControls.publishClicks", isolated_context=False), 0)
        self.assertFalse(page.is_closed())

    async def test_hidden_duplicate_text_field_is_not_filled(self):
        page, driver, arguments = await self.page_and_driver("tencent")
        await page.evaluate("""() => {
            const input = document.createElement('input'); input.dataset.publisher = 'title';
            input.style.display = 'none'; input.value = 'old hidden value';
            document.querySelector('[data-publisher="editor"]').prepend(input);
        }""")
        await driver.prepare_fixture(page, self.materials, **arguments)
        values = await page.locator('[data-publisher="title"]').evaluate_all("nodes => nodes.map(node => node.value)")
        self.assertEqual(values, ["old hidden value", self.materials.title])
        self.assertEqual(await page.evaluate("fixtureControls.publishClicks", isolated_context=False), 0)


if __name__ == "__main__": unittest.main()
