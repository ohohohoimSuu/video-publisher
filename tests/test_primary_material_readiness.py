"""L1 only: execute actual readiness helpers with synthetic DOM-shaped objects."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from utils import publisher_materials as materials
from uploader.youtube_uploader import main as yt
from uploader.douyin_uploader import main as dy


def state(signature="old", *, ready=True, busy=False, error=False):
    return dict(signature=signature, ready=ready, busy=busy, error=error)


class Control:
    def __init__(self, *, count=1, visible=True, enabled=True, attributes=None, text=""):
        self.first = self
        self.count = AsyncMock(return_value=count)
        self.is_visible = AsyncMock(return_value=visible)
        self.is_enabled = AsyncMock(return_value=enabled)
        self.attributes = attributes or {}
        self.get_attribute = AsyncMock(side_effect=lambda key: self.attributes.get(key))
        self.click = AsyncMock(side_effect=AssertionError("this fixture must never submit"))
        self.set_input_files = AsyncMock()
        self.evaluate = AsyncMock(return_value=state())
        self.inner_text = AsyncMock(return_value=text)
        self.filter = Mock(return_value=self if visible else Control(count=0))
        self.nth = Mock(return_value=self)
        self.wait_for = AsyncMock()


class PreviewReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_requires_three_stable_loaded_changed_samples(self):
        root = Control()
        root.evaluate.side_effect = [state("new", ready=False), state("new"), state("new", busy=True, ready=False),
                                    state("new"), state("new"), state("new")]
        self.assertEqual(await materials.wait_changed_preview(root, state(), sample_interval=0), state("new"))
        self.assertEqual(root.evaluate.await_count, 6)

    async def test_rejects_unchanged_missing_unreadable_and_busy_preview(self):
        for evidence in (state(), state(None, ready=False), state("new", ready=False), state("new", ready=False, busy=True)):
            with self.subTest(evidence=evidence):
                root = Control()
                root.evaluate.return_value = evidence
                with self.assertRaisesRegex(RuntimeError, "未完成更新"):
                    await materials.wait_changed_preview(root, state(), timeout_seconds=.005, sample_interval=0)

    async def test_stability_resets_on_an_intermediate_different_preview(self):
        root = Control()
        root.evaluate.side_effect = [state("a"), state("a"), state("b"), state("a"), state("a"), state("a")]
        await materials.wait_changed_preview(root, state(), sample_interval=0)
        self.assertEqual(root.evaluate.await_count, 6)

    async def test_visible_error_stops_immediately(self):
        root = Control()
        root.evaluate.return_value = state("new", error=True)
        with self.assertRaisesRegex(RuntimeError, "处理失败"):
            await materials.wait_changed_preview(root, state(), sample_interval=0)
        root.evaluate.assert_awaited_once()

    async def test_missing_or_tainted_initial_preview_never_becomes_a_baseline(self):
        root = Control()
        root.evaluate.return_value = state(None, ready=False)
        with self.assertRaisesRegex(RuntimeError, "原封面预览"):
            await materials.wait_initial_preview(root, timeout_seconds=.005, sample_interval=0)

    async def test_unverifiable_state_and_closed_editor_are_not_success(self):
        root = Control()
        for evidence in (None, {}, {"signature": "new", "ready": True}):
            root.evaluate.return_value = evidence
            with self.assertRaisesRegex(RuntimeError, "无法核对"):
                await materials.preview_state(root)
        root.evaluate.side_effect = RuntimeError("closed")
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await materials.wait_changed_preview(root, state(), sample_interval=0)


class YouTubeMaterialTests(unittest.IsolatedAsyncioTestCase):
    async def test_thumbnail_uses_unique_visible_editor_and_waits_for_preview(self):
        root, file_input = Control(), Control()
        root.evaluate.side_effect = [state(), state(), state(), state("new", ready=False), state("new"), state("new"), state("new")]
        root.locator = Mock(return_value=file_input)
        page = SimpleNamespace(locator=Mock(return_value=root))
        with patch.object(materials.asyncio, "sleep", new=AsyncMock()):
            await yt._set_verified_thumbnail(page, "synthetic.png")
        page.locator.assert_called_once_with("ytcp-thumbnail-uploader")
        root.locator.assert_called_once_with("input[type='file']")
        file_input.set_input_files.assert_awaited_once_with("synthetic.png")
        self.assertEqual(root.evaluate.await_count, 7)
        file_input.click.assert_not_awaited()

    async def test_ambiguous_or_hidden_thumbnail_editor_never_selects_file(self):
        for root in (Control(count=2), Control(visible=False)):
            file_input = Control()
            root.locator = Mock(return_value=file_input)
            with self.assertRaisesRegex(RuntimeError, "缺失或不唯一"):
                await yt._set_verified_thumbnail(SimpleNamespace(locator=lambda _: root), "synthetic.png")
            file_input.set_input_files.assert_not_awaited()

    async def test_ambiguous_thumbnail_input_never_selects_file(self):
        root, file_input = Control(), Control(count=2)
        root.locator = Mock(return_value=file_input)
        with self.assertRaisesRegex(RuntimeError, "缺失或不唯一"):
            await yt._set_verified_thumbnail(SimpleNamespace(locator=lambda _: root), "synthetic.png")
        file_input.set_input_files.assert_not_awaited()

    async def test_upload_failure_is_not_completion_substring(self):
        for text in ("Upload incomplete", "Processing failed", "检查失败", "Error: upload complete"):
            with self.subTest(text=text):
                control = Control(text=text)
                page = SimpleNamespace(locator=lambda _: control, wait_for_timeout=AsyncMock())
                with self.assertRaisesRegex(RuntimeError, "报错"):
                    await yt._wait_upload_complete(page, max_polls=2)

    async def test_upload_hidden_complete_and_visible_processing_cannot_pass(self):
        for control in (Control(text="Upload complete", visible=False), Control(text="Processing"), Control(text="Uploading 90%")):
            page = SimpleNamespace(locator=lambda _: control, wait_for_timeout=AsyncMock())
            self.assertFalse(await yt._wait_upload_complete(page, max_polls=2))

    async def test_negated_completion_and_incomplete_percentages_cannot_pass(self):
        for text in ("Video not uploaded", "0% uploaded", "Upload complete: 12%", "Not uploaded",
                     "Upload complete: 99.9%", "Upload complete: 101%", "尚未上传完成"):
            with self.subTest(text=text):
                control = Control(text=text)
                page = SimpleNamespace(locator=lambda _: control, wait_for_timeout=AsyncMock())
                self.assertFalse(await yt._wait_upload_complete(page, max_polls=2))

    async def test_separate_incomplete_progress_blocks_a_complete_label(self):
        complete, incomplete = Control(text="Upload complete"), Control(text="Uploading 12%")
        page = SimpleNamespace(locator=lambda selector: complete if selector == ".progress-label" else incomplete,
                               wait_for_timeout=AsyncMock())
        self.assertFalse(await yt._wait_upload_complete(page, max_polls=2))

    async def test_visible_explicit_upload_completion_requires_two_samples(self):
        for text in ("Upload complete", "Checks complete", "Finished processing", "已上传", "Upload complete: 100%"):
            control = Control(text=text)
            page = SimpleNamespace(locator=lambda _: control, wait_for_timeout=AsyncMock())
            self.assertTrue(await yt._wait_upload_complete(page, max_polls=2))
            page.wait_for_timeout.assert_awaited_once()

    async def test_long_tags_fail_before_browser_or_truncation(self):
        app = yt.YouTubeVideo("synthetic", "unused.mp4", ["x" * 501], "unused.json",
                              visibility="private", made_for_kids=False, altered_synthetic=False, prepare_only=True)
        with self.assertRaisesRegex(ValueError, "未截断"):
            await app.upload(object())

    async def test_missing_declaration_and_invalid_visibility_fail_early(self):
        with self.assertRaisesRegex(ValueError, "可见范围"):
            yt.YouTubeVideo("synthetic", "unused.mp4", [], "unused.json", visibility="unknown")
        app = yt.YouTubeVideo("synthetic", "unused.mp4", [], "unused.json", prepare_only=True)
        with self.assertRaisesRegex(ValueError, "必须明确"):
            await app.upload(object())


class DouyinHandoffTests(unittest.IsolatedAsyncioTestCase):
    def page(self):
        button, empty = Control(), Control(count=0)
        page = SimpleNamespace(url="https://creator.douyin.com/creator-micro/content/post/video?enter_from=publish_page",
                               is_closed=Mock(return_value=False), wait_for_timeout=AsyncMock(),
                               evaluate=AsyncMock(return_value=dict(completed=True, busy=False, error=False)),
                               locator=Mock(return_value=empty), get_by_role=Mock(return_value=button))
        return page, button

    async def test_prepare_checks_page_upload_and_button_without_final_click(self):
        page, button = self.page()
        app = object.__new__(dy.DouYinVideo)
        app.prepare_only = True
        with patch.object(dy, "emit_progress") as emit:
            self.assertEqual(await app.finish_upload(page, object(), object()), "prepared")
        emit.assert_called_once_with("douyin", "ready_to_submit")
        button.click.assert_not_awaited()

    async def test_closed_or_redirected_editor_never_reports_ready(self):
        for closed, url in ((True, "https://creator.douyin.com/creator-micro/content/post/video"),
                            (False, "https://creator.douyin.com/login"),
                            (False, "https://evil.invalid/creator-micro/content/post/video")):
            page, button = self.page()
            page.is_closed.return_value, page.url = closed, url
            with self.assertRaisesRegex(RuntimeError, "未完成准备"):
                await dy._require_douyin_handoff(page)
            button.click.assert_not_awaited()

    async def test_disabled_ambiguous_final_button_or_blocking_dialog_fails(self):
        for case in ("disabled", "aria-disabled", "class-disabled", "duplicate", "dialog"):
            with self.subTest(case=case):
                page, button = self.page()
                if case == "disabled": button.is_enabled.return_value = False
                if case == "aria-disabled": button.attributes["aria-disabled"] = "true"
                if case == "class-disabled": button.attributes["class"] = "semi-button-disabled"
                if case == "duplicate": button.count.return_value = 2
                if case == "dialog": page.locator.return_value = Control()
                with self.assertRaises(RuntimeError):
                    await dy._require_douyin_handoff(page)
                button.click.assert_not_awaited()

    async def test_upload_completion_must_be_visible_and_not_busy(self):
        page, _ = self.page()
        page.evaluate.side_effect = [dict(completed=False, busy=False, error=False),
                                     dict(completed=True, busy=True, error=False),
                                     dict(completed=True, busy=False, error=False),
                                     dict(completed=True, busy=False, error=False)]
        await dy._wait_douyin_upload(page, max_polls=4)
        self.assertEqual(page.evaluate.await_count, 4)

    async def test_failed_or_missing_upload_stops_without_reattaching_file(self):
        for evidence in (dict(completed=True, busy=False, error=True), dict(completed=False, busy=False, error=False)):
            page, _ = self.page()
            page.evaluate.return_value = evidence
            with self.assertRaises(RuntimeError):
                await dy._wait_douyin_upload(page, max_polls=2)
            page.locator.assert_not_called()

    async def test_cover_tab_not_confirmed_stops_before_upload(self):
        tab, file_input = Control(), Control()
        tab.click = AsyncMock()
        tab.evaluate.return_value = False
        dialog = SimpleNamespace(locator=lambda *a, **kw: tab)
        with self.assertRaisesRegex(RuntimeError, "无法确认切换"):
            await dy._upload_cover_slot(object(), dialog, file_input, "设置横封面", "synthetic.png")
        file_input.set_input_files.assert_not_awaited()

    async def test_cover_slot_waits_for_stable_changed_preview(self):
        tab, dialog, file_input = Control(), Control(), Control()
        tab.click = AsyncMock()
        tab.evaluate.return_value = True
        dialog.locator = Mock(side_effect=lambda selector: tab if 'steps-' in selector else dialog)
        dialog.evaluate.side_effect = [state(), state(), state(), state("new", ready=False), state("new"), state("new"), state("new")]
        with patch.object(materials.asyncio, "sleep", new=AsyncMock()):
            await dy._upload_cover_slot(object(), dialog, file_input, "设置横封面", "synthetic.png")
        self.assertEqual(dialog.evaluate.await_count, 7)
        file_input.set_input_files.assert_awaited_once_with("synthetic.png")


class DouyinDeclarationEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_declaration_requires_checked_choice_closed_dialog_and_saved_label(self):
        declaration = "内容由AI生成"
        for mode in ("success", "unchecked", "dialog_still_open", "missing_saved_label"):
            with self.subTest(mode=mode):
                entry, dialog, option, confirm, saved = [Control() for _ in range(5)]
                entry.click, option.click, confirm.click = AsyncMock(), AsyncMock(), AsyncMock()
                option.evaluate.return_value = mode != "unchecked"
                if mode == "dialog_still_open":
                    async def wait_for(**kwargs):
                        if kwargs.get("state") == "hidden":
                            raise TimeoutError("synthetic dialog did not close")
                    dialog.wait_for.side_effect = wait_for
                if mode == "missing_saved_label":
                    saved.count.return_value = 0
                dialog.locator = lambda selector: option if "radio" in selector else confirm
                page = SimpleNamespace(get_by_text=lambda text, **kwargs: saved if text == declaration else entry,
                                       locator=lambda selector: option if "radio-addon" in selector else dialog,
                                       wait_for_timeout=AsyncMock())
                app = dy.DouYinVideo("fixture", "unused.mp4", [], 0, "unused.json", declaration=declaration)
                app._clear_blocking_overlays = AsyncMock()
                self.assertEqual(await app.set_self_declaration(page, declaration), mode == "success")
                if mode == "unchecked":
                    confirm.click.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
