"""Synthetic Studio controls only; no browser, account, or network access."""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from uploader.youtube_uploader import main as yt
from utils import publisher_progress


CHANNEL = "UC" + "a" * 22
OTHER_CHANNEL = "UC" + "b" * 22


class Element:
    def __init__(self, *, text="", checked=None, count=1):
        self.first = self
        self.attributes = {"aria-checked": checked}
        self.wait_for = AsyncMock()
        self.click = AsyncMock()
        self.fill = AsyncMock()
        self.type = AsyncMock()
        self.set_input_files = AsyncMock()
        self.count = AsyncMock(return_value=count)
        self.is_visible = AsyncMock(return_value=True)
        self.is_enabled = AsyncMock(return_value=True)
        self.inner_text = AsyncMock(return_value=text)
        self.filter = MagicMock(return_value=self)
        self.nth = MagicMock(return_value=self)
        self.get_attribute = AsyncMock(side_effect=lambda name: self.attributes.get(name))


class YouTubePrepareTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cover_path = self.root / "synthetic-cover.png"
        self.cover_path.write_bytes(b"local test fixture; never uploaded")
        self.video_input = Element()
        self.cover_input = Element()
        self.thumbnail = Element()
        self.thumbnail.locator = MagicMock(return_value=self.cover_input)
        self.thumbnail.evaluate = AsyncMock(side_effect=lambda _: {
            "signature": f"cover-{self.cover_input.set_input_files.await_count}",
            "ready": True, "busy": False, "error": False})
        self.title = Element()
        self.description = Element()
        self.tags = Element()
        self.audience = Element(checked="true")
        self.synthetic = Element(checked="true")
        self.visibility = Element(checked="true")
        self.done = Element()
        self.done.click.side_effect = AssertionError("prepare must never click the final submit control")
        self.generic = Element()
        self.control = Element()
        self.selectors = []
        self.page = MagicMock()
        self.page.url = f"{yt.STUDIO_URL}/channel/{CHANNEL}?authuser=2"
        self.page.goto = AsyncMock(return_value=SimpleNamespace(status=200))
        self.page.wait_for_timeout = AsyncMock()
        self.page.evaluate = AsyncMock()
        self.page.keyboard = SimpleNamespace(press=AsyncMock())
        self.page.get_by_role.return_value = self.control
        self.page.locator.side_effect = self.locate
        self.context = SimpleNamespace(new_page=AsyncMock(return_value=self.page),
                                       storage_state=AsyncMock(), close=AsyncMock())
        self.browser = SimpleNamespace(new_context=AsyncMock(), close=AsyncMock())
        self.playwright = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock()))
        self.manager = MagicMock()
        self.manager.__aenter__ = AsyncMock(return_value=self.playwright)
        self.manager.__aexit__ = AsyncMock(return_value=False)
        self.connect = AsyncMock(return_value=(self.browser, self.context))
        for owner, name, value in (
            (yt, "async_playwright", MagicMock(return_value=self.manager)),
            (yt, "_publisher_binding", MagicMock(return_value={"channel_id": CHANNEL, "authuser": 2})),
            (yt, "set_init_script", AsyncMock(return_value=self.context)),
            (yt.publisher_handoff, "connect_publisher_handoff", self.connect),
        ):
            patcher = patch.object(owner, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def locate(self, selector):
        self.selectors.append(selector)
        if selector == 'input[type="file"]':
            return self.video_input
        if selector == "ytcp-thumbnail-uploader":
            return self.thumbnail
        if selector == "#title-textarea #textbox":
            return self.title
        if selector == "#description-textarea #textbox":
            return self.description
        if "#tags-container" in selector:
            return self.tags
        if "VIDEO_MADE_FOR_KIDS_" in selector:
            return self.audience
        if "VIDEO_HAS_SYNTHETIC_" in selector:
            return self.synthetic
        if "tp-yt-paper-radio-button[name=" in selector:
            return self.visibility
        if selector == "#done-button":
            return self.done
        if selector in (".progress-label", "span.progress-label", "ytcp-video-upload-progress"):
            return Element(text="Upload complete")
        if selector == "tp-yt-iron-dropdown:visible":
            return Element(count=0)
        return self.generic

    def video(self, **kwargs):
        values = dict(description="Original description\nwith newlines", thumbnail_path=str(self.cover_path),
                      visibility="unlisted", made_for_kids=True, altered_synthetic=False, prepare_only=True)
        values.update(kwargs)
        return yt.YouTubeVideo("Original title", "/synthetic-video-not-uploaded.mp4", ["one", "two"],
                               "/synthetic-session-not-read.json", **values)

    def assert_retained_without_submission(self):
        self.done.click.assert_not_awaited()
        self.browser.close.assert_not_awaited()
        self.context.close.assert_not_awaited()
        self.context.storage_state.assert_not_awaited()
        self.browser.new_context.assert_not_awaited()
        self.playwright.chromium.launch.assert_not_awaited()

    async def test_full_prepare_returns_through_main_and_keeps_persistent_context(self):
        self.assertEqual(await self.video().main(), "prepared")
        self.connect.assert_awaited_once_with(self.playwright, "/synthetic-session-not-read.json", "youtube", proxy=yt.YT_PROXY)
        self.page.goto.assert_awaited_once_with(
            f"{yt.STUDIO_URL}/channel/{CHANNEL}?authuser=2", wait_until="domcontentloaded")
        self.video_input.set_input_files.assert_awaited_once_with("/synthetic-video-not-uploaded.mp4")
        self.cover_input.set_input_files.assert_awaited_once_with(str(self.cover_path))
        self.title.fill.assert_awaited_once_with("Original title")
        self.description.fill.assert_awaited_once_with("Original description\nwith newlines")
        self.tags.type.assert_awaited_once_with("one,two,", delay=4)
        self.audience.click.assert_awaited_once()
        self.synthetic.click.assert_awaited_once()
        self.visibility.click.assert_awaited_once()
        self.assertIn("tp-yt-paper-radio-button[name='VIDEO_MADE_FOR_KIDS_MFK']", self.selectors)
        self.assertIn("tp-yt-paper-radio-button[name='UNLISTED']", self.selectors)
        self.assertTrue(any("VIDEO_HAS_SYNTHETIC_CONTENT_NO" in s for s in self.selectors))
        self.done.wait_for.assert_awaited_once_with(state="visible", timeout=15000)
        self.assert_retained_without_submission()

    async def test_unfinished_upload_never_returns_prepared(self):
        with patch.object(yt, "_wait_upload_complete", AsyncMock(return_value=False)):
            with self.assertRaisesRegex(RuntimeError, "上传完成超时"):
                await self.video().main()
        self.done.wait_for.assert_not_awaited()
        self.assert_retained_without_submission()

    async def test_final_button_must_be_visible_and_enabled(self):
        for control, value in (("is_visible", False), ("is_enabled", False)):
            with self.subTest(control=control):
                getattr(self.done, control).return_value = value
                with self.assertRaisesRegex(RuntimeError, "最终提交按钮尚不可用"):
                    await self.video().main()
                getattr(self.done, control).return_value = True
        for name, value in (("aria-disabled", "true"), ("disabled", "")):
            with self.subTest(attribute=name):
                self.done.attributes[name] = value
                with self.assertRaisesRegex(RuntimeError, "最终提交按钮尚不可用"):
                    await self.video().main()
                del self.done.attributes[name]
        self.assert_retained_without_submission()

    async def test_channel_change_while_final_button_loads_blocks_handoff(self):
        async def switch(**kwargs):
            self.page.url = f"{yt.STUDIO_URL}/channel/{OTHER_CHANNEL}"
        self.done.wait_for.side_effect = switch
        with self.assertRaisesRegex(RuntimeError, "频道"):
            await self.video().main()
        self.assert_retained_without_submission()

    async def test_unconfirmed_audience_synthetic_or_visibility_blocks_handoff(self):
        for name in ("audience", "synthetic", "visibility"):
            with self.subTest(control=name):
                field = getattr(self, name)
                field.attributes["aria-checked"] = "false"
                with self.assertRaises(RuntimeError):
                    await self.video().main()
                field.attributes["aria-checked"] = "true"
        self.done.wait_for.assert_not_awaited()
        self.assert_retained_without_submission()

    async def test_requested_cover_must_exist_and_be_uploaded(self):
        with self.assertRaisesRegex(RuntimeError, "封面文件已不存在"):
            await self.video(thumbnail_path=str(self.root / "missing.png")).main()
        self.cover_input.set_input_files.side_effect = RuntimeError("synthetic cover failure")
        with self.assertRaisesRegex(RuntimeError, "封面上传失败"):
            await self.video().main()
        self.done.wait_for.assert_not_awaited()
        self.assert_retained_without_submission()

    async def test_playlist_done_controls_are_scoped_to_the_playlist_dialog(self):
        self.assertEqual(await self.video(playlist="Synthetic playlist").main(), "prepared")
        done_selectors = [s for s in self.selectors if "has-text('Done')" in s or "has-text('完成')" in s]
        self.assertTrue(done_selectors)
        self.assertTrue(all(part.strip().startswith("ytcp-playlist-dialog ")
                            for selector in done_selectors for part in selector.split(",")))
        self.assert_retained_without_submission()

    async def test_invalid_binding_stops_before_opening_handoff_browser(self):
        yt._publisher_binding.side_effect = RuntimeError("synthetic lock mismatch")
        with self.assertRaisesRegex(RuntimeError, "lock mismatch"):
            await self.video().main()
        self.connect.assert_not_awaited()
        self.video_input.set_input_files.assert_not_awaited()

    async def test_headless_request_still_uses_handoff_and_preserves_proxy(self):
        with patch.object(yt, "YT_PROXY", "http://127.0.0.1:7897"):
            self.assertEqual(await self.video(headless=True).main(), "prepared")
        self.connect.assert_awaited_once_with(self.playwright, "/synthetic-session-not-read.json", "youtube",
                                              proxy="http://127.0.0.1:7897")
        self.assert_retained_without_submission()

    async def test_handoff_failure_never_falls_back_to_regular_browser(self):
        self.connect.side_effect = RuntimeError("synthetic handoff unavailable")
        with self.assertRaisesRegex(RuntimeError, "handoff unavailable"):
            await self.video(headless=True).main()
        self.video_input.set_input_files.assert_not_awaited()
        self.assert_retained_without_submission()

    def progress_environment(self):
        return {"PUBLISHER_PROGRESS_FILE": str(self.root / "private-progress.json"),
                "PUBLISHER_RESULT_NONCE": "synthetic-youtube-stage-nonce",
                "PUBLISHER_PROGRESS_PLATFORM": "youtube", "PUBLISHER_PROGRESS_ACTION": "publish"}

    async def test_private_prepare_phases_are_durable_before_side_effects_and_stop_at_ready(self):
        progress_path = self.root / "private-progress.json"
        phases = []
        def emit(platform, phase):
            publisher_progress.emit_progress(platform, phase)
            phases.append(phase)
        def require_phase(expected):
            async def check(*args, **kwargs):
                self.assertEqual(json.loads(progress_path.read_text())["phase"], expected)
            return check
        self.video_input.set_input_files.side_effect = require_phase("uploading")
        self.title.fill.side_effect = require_phase("metadata")
        self.title.type.side_effect = require_phase("metadata")
        self.description.fill.side_effect = require_phase("metadata")
        self.description.type.side_effect = require_phase("metadata")
        self.cover_input.set_input_files.side_effect = require_phase("cover")
        with patch.dict(os.environ, self.progress_environment()), patch.object(yt, "emit_progress", side_effect=emit):
            self.assertEqual(await self.video().main(), "prepared")
        self.assertEqual(phases, ["uploading", "metadata", "cover", "ready_to_submit"])
        final = json.loads(progress_path.read_text())
        self.assertEqual(final["phase"], "ready_to_submit")
        self.assertEqual(set(final), {"version", "nonce", "platform", "action", "phase", "updated_at"})
        self.assert_retained_without_submission()

    async def test_ready_progress_write_failure_cannot_claim_prepared(self):
        phases = []
        def emit(platform, phase):
            phases.append(phase)
            if phase == "ready_to_submit":
                raise OSError("synthetic private evidence failure")
        with patch.object(yt, "emit_progress", side_effect=emit):
            with self.assertRaisesRegex(OSError, "private evidence failure"):
                await self.video(thumbnail_path=None).main()
        self.assertEqual(phases, ["uploading", "metadata", "ready_to_submit"])
        self.assert_retained_without_submission()

    async def test_automatic_path_records_submitting_before_final_click(self):
        phases = []
        def emit(platform, phase):
            phases.append(phase)
        async def final_click():
            self.assertEqual(phases[-1], "submitting")
        self.done.click.side_effect = final_click
        # Exercise the legacy branch with the same synthetic context. No
        # browser is launched and no actual platform click can occur.
        with patch.object(yt, "emit_progress", side_effect=emit):
            await self.video(prepare_only=False)._upload_with_browser(
                self.browser, {"channel_id": CHANNEL, "authuser": 2}, context=self.context)
        self.assertEqual(phases, ["uploading", "metadata", "cover", "ready_to_submit", "submitting", "submitted"])
        self.done.click.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
