"""Synthetic cover-editor and resume safety checks; no browser is launched."""
import io
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import sau_cli
from uploader.xiaohongshu_uploader import main as xhs


class CoverPage:
    def __init__(self, opened):
        self.opened, self.hovered = opened, False
        self.freeze_preview, self.preview_ready = False, True
        self.actions = []
        self.wait_for_timeout = AsyncMock()

    def get_by_role(self, role, *, name, exact):
        assert (role, name, exact) == ("heading", "设置封面", True)
        return CoverLocator(self, "heading")

    def get_by_text(self, text, *, exact):
        assert (text, exact) == ("编辑封面", True)
        return CoverLocator(self, "edit")

    def locator(self, selector):
        kinds = {"div.d-modal": "legacy", "div.cover-plugin-preview": "preview", "div.upload-cover": "hidden_old_slot"}
        return CoverLocator(self, kinds[selector])


class CoverLocator:
    def __init__(self, page, kind, visible=False):
        self.page, self.kind, self.visible_filter = page, kind, visible

    def filter(self, **kwargs):
        return CoverLocator(self.page, self.kind, kwargs.get("visible", self.visible_filter))

    async def count(self):
        if self.kind == "legacy":
            return 0
        if self.kind == "hidden_old_slot":
            return 0 if self.visible_filter else 1
        if self.kind in {"heading", "modal", "file", "save"}:
            return int(self.page.opened)
        if self.kind == "edit":
            return int(self.page.hovered and not self.page.opened)
        return 1

    async def is_visible(self):
        return bool(await self.count()) and self.kind != "hidden_old_slot"

    async def is_enabled(self):
        return True

    async def evaluate(self, script):
        assert self.kind == "modal"
        changed = any(isinstance(action, tuple) and action[0] == "cover_file" for action in self.page.actions)
        return {"signature": "new" if changed and not self.page.freeze_preview else "old", "ready": self.page.preview_ready if changed else True}

    def locator(self, selector):
        assert self.kind == "heading" and selector.startswith("xpath=ancestor::")
        assert "not(self::body)" in selector and "完成" in selector
        return CoverLocator(self.page, "modal")

    def get_by_label(self, label, *, exact):
        assert self.kind == "modal" and (label, exact) == ("上传封面图片", True)
        return CoverLocator(self.page, "file")

    def get_by_role(self, role, *, name, exact):
        assert self.kind == "modal" and (role, name, exact) == ("button", "完成", True)
        return CoverLocator(self.page, "save")

    async def hover(self):
        assert self.kind == "preview" and self.visible_filter
        self.page.hovered = True
        self.page.actions.append("hover_visible_preview")

    async def wait_for(self, *, state, **kwargs):
        if state == "hidden":
            assert self.kind == "modal" and not self.page.opened
        elif not await self.is_visible():
            raise TimeoutError("synthetic invisible element")

    async def click(self, **kwargs):
        if self.kind == "edit":
            assert self.visible_filter and self.page.hovered
            self.page.actions.append("edit_cover")
            self.page.opened = True
        elif self.kind == "save":
            assert self.visible_filter and self.page.actions[-1] == ("cover_file", "cover.png")
            self.page.actions.append("save_cover")
            self.page.opened = False
        else:
            raise AssertionError("hidden slots, generation and publishing must not be clicked")

    async def set_input_files(self, path):
        assert self.kind == "file" and self.page.opened
        self.page.actions.append(("cover_file", path))


class ReadLocator:
    def __init__(self, value="", count=1):
        self.value, self.number = value, count
    def filter(self, **kwargs): return self
    async def count(self): return self.number
    async def input_value(self, **kwargs): return self.value
    def nth(self, index): return self
    async def inner_text(self, **kwargs): return self.value


class ReadEditor:
    def __init__(self, title="title", filename="fixture.mp4", text="original description #one #two", url=None):
        self.url = url or "https://creator.xiaohongshu.com/publish/publish?target=video"
        self.title, self.filename, self.text = title, filename, text
        self.goto = AsyncMock(side_effect=AssertionError("must not navigate"))
        self.get_by_role = MagicMock(side_effect=AssertionError("must not publish"))
    def locator(self, selector):
        if selector == 'input[placeholder*="填写标题"]': return ReadLocator(self.title)
        if selector == '[contenteditable="true"]': return ReadLocator(self.text)
        raise AssertionError("must not select a video input or edit fields")
    def get_by_text(self, pattern):
        return ReadLocator(count=int(bool(pattern.search(self.filename))))


class CoverResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_unchanged_or_unloaded_preview_never_saves_an_enabled_button(self):
        real_wait = xhs._wait_for_cover_preview
        async def bounded(modal, confirm, previous):
            return await real_wait(modal, confirm, previous, timeout_seconds=0.01)
        for unchanged in (True, False):
            with self.subTest(unchanged=unchanged):
                page = CoverPage(True)
                page.freeze_preview = unchanged
                page.preview_ready = unchanged
                with patch.object(xhs, "_wait_for_cover_preview", side_effect=bounded), \
                     patch.object(xhs.asyncio, "sleep", new=AsyncMock()), self.assertRaises(RuntimeError):
                    await xhs.XiaoHongShuVideo.set_thumbnail(None, page, "cover.png")
                self.assertEqual(page.actions, [("cover_file", "cover.png")])
                self.assertTrue(page.opened)

    async def test_modern_editor_reuses_existing_modal_or_opens_only_visible_edit_control(self):
        for opened in (True, False):
            with self.subTest(opened=opened):
                page = CoverPage(opened)
                with patch.object(xhs.asyncio, "sleep", new=AsyncMock()):
                    await xhs.XiaoHongShuVideo.set_thumbnail(None, page, "cover.png")
                expected = [("cover_file", "cover.png"), "save_cover"]
                if not opened:
                    expected = ["hover_visible_preview", "edit_cover", *expected]
                self.assertEqual(page.actions, expected)

    async def test_resume_uses_existing_context_and_never_navigates_uploads_video_or_publishes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            video, cover = root / "fixture.mp4", root / "cover.png"
            video.write_bytes(b"local fixture")
            cover.write_bytes(b"local fixture")
            for editors, matched in (([ReadEditor()], True), ([ReadEditor(title="other")], False),
                                     ([ReadEditor(filename="other.mp4")], False), ([ReadEditor(text="other #one #two")], False),
                                     ([ReadEditor(text="original description #one")], False),
                                     ([ReadEditor(url="https://example.invalid/publish/publish")], False),
                                     ([ReadEditor(), ReadEditor()], False)):
                with self.subTest(matched=matched, editors=len(editors)):
                    app = xhs.XiaoHongShuVideo("title", str(video), ["one", "two"], 0, "unused.json",
                                             thumbnail_path=str(cover), desc="original\ndescription", headless=False,
                                             prepare_only=True, resume_cover=True)
                    app.validate_upload_args = AsyncMock(side_effect=AssertionError("must not launch login check"))
                    app.upload_video_content = AsyncMock(side_effect=AssertionError("must not restart upload"))
                    app.wait_for_video_material_ready = AsyncMock()
                    app.set_thumbnail = AsyncMock()
                    context = SimpleNamespace(pages=editors, new_page=AsyncMock(side_effect=AssertionError("must not create page")),
                                              close=AsyncMock(), storage_state=AsyncMock())
                    browser = SimpleNamespace(close=AsyncMock())
                    phases = []
                    with patch.object(xhs.publisher_handoff, "connect_publisher_handoff", new=AsyncMock(return_value=(browser, context))) as connect, \
                         patch.object(xhs, "set_init_script", new=AsyncMock(side_effect=AssertionError("must not modify before matching"))), \
                         patch.object(xhs, "emit_progress", side_effect=lambda platform, phase: phases.append(phase)):
                        if matched:
                            self.assertEqual(await app.upload(object()), "prepared")
                            app.wait_for_video_material_ready.assert_awaited_once_with(editors[0], timeout_seconds=30)
                            app.set_thumbnail.assert_awaited_once_with(editors[0], str(cover))
                            self.assertEqual(phases, ["cover", "ready_to_submit"])
                        else:
                            with self.assertRaises(RuntimeError):
                                await app.upload(object())
                            app.wait_for_video_material_ready.assert_not_awaited()
                            app.set_thumbnail.assert_not_awaited()
                            self.assertEqual(phases, [])
                    self.assertEqual(connect.await_args.kwargs, {"require_existing": True})
                    context.new_page.assert_not_awaited()
                    context.close.assert_not_awaited()
                    context.storage_state.assert_not_awaited()
                    browser.close.assert_not_awaited()
                    app.validate_upload_args.assert_not_awaited()
                    app.upload_video_content.assert_not_awaited()
                    for page in editors:
                        page.goto.assert_not_awaited()
                        page.get_by_role.assert_not_called()

    async def test_resume_request_skips_separate_login_browser_and_passes_flag(self):
        request = sau_cli.XiaohongshuVideoUploadRequest("fixture", Path("fixture.mp4"), "title", "", [], 0,
                                                       prepare_only=True, resume_cover=True)
        with patch.object(sau_cli, "resolve_account_file", return_value=Path("unused.json")), \
             patch.object(sau_cli, "xiaohongshu_setup", new=AsyncMock()) as setup, \
             patch.object(sau_cli, "XiaoHongShuVideo", return_value=SimpleNamespace(main=AsyncMock(return_value="prepared"))) as cls:
            await sau_cli.upload_xiaohongshu_video(request)
        setup.assert_not_awaited()
        self.assertTrue(cls.call_args.kwargs["resume_cover"])


class ResumeCliTests(unittest.TestCase):
    def test_resume_environment_rejects_other_actions_and_automatic_mode(self):
        with patch.dict(os.environ, {"SAU_PUBLISHER_RESUME_COVER": "1", "SAU_PUBLISHER_PREPARE_ONLY": "1"}), \
             patch("sys.stderr", io.StringIO()), patch.object(sau_cli, "dispatch", new=AsyncMock()) as dispatch:
            for platform in ("douyin", "youtube", "xiaohongshu"):
                with self.assertRaises(SystemExit):
                    sau_cli.main([platform, "check", "--account", "fixture"])
            dispatch.assert_not_awaited()
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "fixture.mp4"
            video.write_bytes(b"fixture")
            with patch.dict(os.environ, {"SAU_PUBLISHER_RESUME_COVER": "1", "SAU_PUBLISHER_PREPARE_ONLY": "0"}), \
                 patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
                sau_cli.main(["xiaohongshu", "upload-video", "--account", "fixture", "--file", str(video), "--title", "fixture"])
            with patch.dict(os.environ, {"SAU_PUBLISHER_RESUME_COVER": "1", "SAU_PUBLISHER_PREPARE_ONLY": "1"}), \
                 patch.object(sau_cli, "upload_xiaohongshu_video", new=AsyncMock()) as upload:
                self.assertEqual(sau_cli.main(["xiaohongshu", "upload-video", "--account", "fixture", "--file", str(video), "--title", "fixture"]), 0)
            self.assertTrue(upload.await_args.args[0].resume_cover)
            self.assertTrue(upload.await_args.args[0].prepare_only)


if __name__ == "__main__":
    unittest.main()
