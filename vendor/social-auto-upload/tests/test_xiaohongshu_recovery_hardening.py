"""Fail-closed cover/recovery regressions; synthetic objects, no platform I/O."""
import asyncio
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from uploader.xiaohongshu_uploader import main as xhs


def uploader(description="original description", tags=None):
    return xhs.XiaoHongShuVideo("title", "fixture.mp4", ["one", "two"] if tags is None else tags,
                               0, "unused.json", thumbnail_path="cover.png", desc=description,
                               headless=False, prepare_only=True, resume_cover=True)


class ReadField:
    def __init__(self, values):
        self.values = values
    def filter(self, **kwargs): return self
    async def count(self): return len(self.values)
    def nth(self, index): return ReadField([self.values[index]])
    async def input_value(self, **kwargs): return self.values[0]
    async def inner_text(self, **kwargs): return self.values[0]


class Editor:
    def __init__(self, text="original description #one #two", title="title", filename="fixture.mp4"):
        self.url = "https://creator.xiaohongshu.com/publish/publish?target=video"
        self.texts, self.title, self.filename = [text], title, filename
        self.goto = AsyncMock(side_effect=AssertionError("Never navigate during recovery"))
    def locator(self, selector):
        if selector == 'input[placeholder*="填写标题"]': return ReadField([self.title])
        if selector == '[contenteditable="true"]': return ReadField(self.texts)
        raise AssertionError("Recovery must not fill fields or upload video")
    def get_by_text(self, pattern):
        return ReadField([self.filename] if pattern.search(self.filename) else [])


class RecoveryMatchingTests(unittest.IsolatedAsyncioTestCase):
    async def assert_blocked(self, editors, reason="editor_missing"):
        app = uploader()
        app.wait_for_video_material_ready = AsyncMock()
        app.set_thumbnail = AsyncMock()
        with self.assertRaises(xhs.publisher_handoff.HandoffError) as error:
            await app.resume_cover_from_editor(SimpleNamespace(pages=editors))
        self.assertEqual(error.exception.reason, reason)
        app.set_thumbnail.assert_not_awaited()
        return app

    def test_body_and_topics_are_exact_after_layout_normalization(self):
        accepted = ["original\ndescription\n#one #two", "original description#one#two",
                    " original  description  #one\t#two "]
        rejected = ["original description CHANGED #one #two", "original description #ones #two",
                    "original description #one #twos", "original description #one #two #extra",
                    "original description #two #one", "original description #one #two CHANGED",
                    "original description #one", "original description #one #one #two"]
        for text in accepted:
            self.assertTrue(xhs._resume_text_matches(text, "original description", ["one", "two"]), text)
        for text in rejected:
            self.assertFalse(xhs._resume_text_matches(text, "original description", ["one", "two"]), text)
        self.assertTrue(xhs._resume_text_matches("", "", []))
        self.assertFalse(xhs._resume_text_matches("any content", "", []))
        self.assertTrue(xhs._resume_text_matches("#cat", "", ["cat"]))
        self.assertFalse(xhs._resume_text_matches("#cats", "", ["cat"]))
        self.assertFalse(xhs._resume_text_matches("hello CHANGED", "hello", []))

    async def test_mismatched_prefixes_extra_text_and_topics_do_not_resume(self):
        for body in ("original description CHANGED #one #two", "original description #ones #two",
                     "original description #one #two #extra", "original description #two #one"):
            with self.subTest(body=body):
                app = await self.assert_blocked([Editor(body)])
                app.wait_for_video_material_ready.assert_not_awaited()

    async def test_duplicates_include_two_editors_on_the_same_page(self):
        duplicate = Editor()
        duplicate.texts *= 2
        for pages in ([Editor(), Editor()], [duplicate]):
            await self.assert_blocked(pages, "editor_ambiguous")

    async def test_closed_or_unreadable_candidate_is_not_silently_ignored(self):
        closed = Editor()
        closed.locator = lambda _: SimpleNamespace(count=AsyncMock(side_effect=RuntimeError("Target closed")), filter=lambda **kwargs: None)
        # A disappearing candidate prevents proving that the other candidate is
        # unique; do not select the readable page as a fallback.
        await self.assert_blocked([Editor(), closed])
        await self.assert_blocked([])

    async def test_filename_boundary_title_and_origin_stay_strict(self):
        pages = [Editor(title="changed"), Editor(filename="other-fixture.mp4"), Editor(filename="fixture.mp4.bak")]
        for origin in ("http://creator.xiaohongshu.com/publish/publish", "https://creator.xiaohongshu.com.evil.invalid/publish/publish",
                       "https://creator.xiaohongshu.com/publish/other"):
            page = Editor()
            page.url = origin
            pages.append(page)
        for page in pages:
            await self.assert_blocked([page])

    async def test_readiness_wait_cannot_adopt_changed_replaced_or_duplicated_editor(self):
        for change in ("body", "close", "replace", "duplicate"):
            with self.subTest(change=change):
                app, page = uploader(), Editor()
                context = SimpleNamespace(pages=[page])
                async def change_while_waiting(*args, **kwargs):
                    if change == "body": page.texts = ["original description CHANGED #one #two"]
                    if change == "close": context.pages.clear()
                    if change == "replace": context.pages = [Editor()]
                    if change == "duplicate": context.pages.append(Editor())
                app.wait_for_video_material_ready = AsyncMock(side_effect=change_while_waiting)
                app.set_thumbnail = AsyncMock()
                with patch.object(xhs, "emit_progress") as progress, self.assertRaises(xhs.publisher_handoff.HandoffError):
                    await app.resume_cover_from_editor(context)
                app.set_thumbnail.assert_not_awaited()
                progress.assert_not_called()
                page.goto.assert_not_awaited()

    async def test_unchanged_unique_editor_still_reaches_prepared_without_navigation(self):
        app, page = uploader(), Editor()
        app.wait_for_video_material_ready = AsyncMock()
        app.set_thumbnail = AsyncMock()
        with patch.object(xhs, "emit_progress") as progress:
            self.assertEqual(await app.resume_cover_from_editor(SimpleNamespace(pages=[page])), "prepared")
        self.assertEqual([call.args[1] for call in progress.call_args_list], ["cover", "ready_to_submit"])
        app.set_thumbnail.assert_awaited_once_with(page, "cover.png")
        page.goto.assert_not_awaited()


class MetadataPreflightTests(unittest.IsolatedAsyncioTestCase):
    async def test_oversized_title_and_topics_fail_before_account_check_or_transfer(self):
        for title, tags in (("字" * 21, []), ("title", ["topic"] * 11)):
            app = uploader(tags=tags)
            app.title = title
            app.validate_base_args = AsyncMock()
            with self.assertRaises(ValueError):
                await app.validate_upload_args()
            app.validate_base_args.assert_not_awaited()

    async def test_title_is_written_completely_or_rejected_without_mutation(self):
        field = SimpleNamespace(fill=AsyncMock())
        page = SimpleNamespace(locator=lambda _: field)
        app = uploader()
        app.title = "字" * 20
        await app.fill_title(page)
        field.fill.assert_awaited_once_with(app.title)
        field.fill.reset_mock()
        app.title += "字"
        with self.assertRaises(ValueError):
            await app.fill_title(page)
        field.fill.assert_not_awaited()


class PreviewEvidenceTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for preview-script object tests")
    async def test_actual_preview_javascript_on_controlled_objects(self):
        modal = SimpleNamespace(evaluate=AsyncMock())
        await xhs._cover_preview_state(modal)
        script = modal.evaluate.await_args.args[0]
        harness = Path(__file__).parent / "fixtures" / "xiaohongshu_preview_checks.cjs"
        result = subprocess.run([shutil.which("node"), str(harness)], input=json.dumps({"script": script}),
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Controlled preview JavaScript checks passed (L1)", result.stdout)

    async def test_initial_preview_must_finish_loading_and_stabilize(self):
        sequence = [{"signature": None, "ready": False}, {"signature": "old1", "ready": True},
                    {"signature": "old2", "ready": True}, {"signature": "old2", "ready": True}]
        with patch.object(xhs, "_cover_preview_state", new=AsyncMock(side_effect=sequence)) as reads, \
             patch.object(xhs.asyncio, "sleep", new=AsyncMock()):
            self.assertEqual(await xhs._wait_for_initial_cover_preview(object()), sequence[-1])
        self.assertEqual(reads.await_count, 4)

    async def test_new_preview_stability_resets_while_processing_or_button_disabled(self):
        confirm = SimpleNamespace(count=AsyncMock(return_value=1), is_enabled=AsyncMock(return_value=True))
        sequence = [{"signature": "old", "ready": True}, {"signature": "new", "ready": True},
                    {"signature": "new", "ready": False}, {"signature": "new2", "ready": True},
                    {"signature": "new2", "ready": True}, {"signature": "new2", "ready": True}]
        with patch.object(xhs, "_cover_preview_state", new=AsyncMock(side_effect=sequence)) as reads, \
             patch.object(xhs.asyncio, "sleep", new=AsyncMock()):
            await xhs._wait_for_cover_preview(object(), confirm, {"signature": "old", "ready": True})
        self.assertEqual(reads.await_count, 6)


class LegacyPage:
    """The old cover tab is exercised through the real set_thumbnail method."""
    def __init__(self, changed=True):
        self.changed, self.selected, self.saved = changed, False, False
        self.actions = []
    def get_by_role(self, *args, **kwargs): return LegacyControl(self, "heading")
    def locator(self, selector):
        assert selector == "div.d-modal"
        return LegacyControl(self, "modal")


class LegacyControl:
    def __init__(self, page, kind): self.page, self.kind = page, kind
    def filter(self, **kwargs): return self
    async def count(self): return 0 if self.kind == "heading" else 1
    async def is_enabled(self): return True  # Default-enabled save is not evidence.
    async def is_visible(self): return True
    def get_by_text(self, text, **kwargs):
        assert (self.kind, text) == ("modal", "上传封面")
        return LegacyControl(self.page, "tab")
    def get_by_role(self, role, **kwargs):
        assert (self.kind, role, kwargs["name"]) == ("modal", "button", "确定")
        return LegacyControl(self.page, "save")
    def locator(self, selector):
        assert (self.kind, selector) == ("modal", "input[type='file'][accept*='image']")
        return LegacyControl(self.page, "file")
    async def evaluate(self, script):
        assert self.kind == "modal"
        self.page.actions.append("preview")
        return {"signature": "new" if self.page.selected and self.page.changed else "old", "ready": True}
    async def wait_for(self, *, state, **kwargs):
        if state == "hidden": assert self.page.saved
    async def click(self, **kwargs):
        assert self.kind in {"tab", "save"}
        self.page.actions.append(self.kind)
        if self.kind == "save": self.page.saved = True
    async def set_input_files(self, path):
        assert self.kind == "file" and path == "cover.png"
        self.page.selected = True
        self.page.actions.append("cover_file")


class LegacyCoverTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_default_enabled_save_requires_changed_stable_preview(self):
        for changed in (False, True):
            with self.subTest(changed=changed):
                page = LegacyPage(changed)
                real_wait = xhs._wait_for_cover_preview
                async def bounded(*args): return await real_wait(*args, timeout_seconds=0.02)
                with patch.object(xhs, "_wait_for_cover_preview", side_effect=bounded), \
                     patch.object(xhs.asyncio, "sleep", new=AsyncMock()):
                    if changed:
                        await xhs.XiaoHongShuVideo.set_thumbnail(None, page, "cover.png")
                    else:
                        with self.assertRaises(RuntimeError):
                            await xhs.XiaoHongShuVideo.set_thumbnail(None, page, "cover.png")
                self.assertEqual(page.saved, changed)
                self.assertEqual(page.actions.count("cover_file"), 1)
                if changed:
                    self.assertEqual(page.actions[-4:], ["preview", "preview", "preview", "save"])

    async def test_unreadable_initial_preview_never_selects_cover_or_saves(self):
        page = LegacyPage()
        with patch.object(xhs, "_wait_for_initial_cover_preview", new=AsyncMock(side_effect=TimeoutError())), \
             self.assertRaises(RuntimeError):
            await xhs.XiaoHongShuVideo.set_thumbnail(None, page, "cover.png")
        self.assertEqual(page.actions, ["tab"])
        self.assertFalse(page.selected or page.saved)


if __name__ == "__main__":
    unittest.main()
