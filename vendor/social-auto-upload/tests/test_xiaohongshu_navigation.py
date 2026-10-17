"""The one navigation retry must end before the first upload side effect."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from uploader.xiaohongshu_uploader import main as xhs


class NavigationTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        field = SimpleNamespace(wait_for=AsyncMock(), is_enabled=AsyncMock(return_value=True), set_input_files=AsyncMock(), count=AsyncMock(return_value=1))
        field.first = field
        page = SimpleNamespace(goto=AsyncMock(), wait_for_url=AsyncMock(), locator=lambda _: field)
        app = xhs.XiaoHongShuVideo(title="fixture", file_path="fixture.mp4", tags=[], publish_date=0, account_file="unused.json")
        app.fill_meta = AsyncMock(side_effect=RuntimeError("stop fixture after upload"))
        return app, page, field

    async def test_first_navigation_failure_retries_once_then_uploads_once(self):
        app, page, field = self.fixture()
        page.goto.side_effect = [TimeoutError("external resource"), None]
        with patch.object(xhs, "record_diagnostic") as diagnostic, self.assertRaises(RuntimeError):
            await app.upload_video_content(page)
        self.assertEqual(page.goto.await_count, 2)
        for call in page.goto.await_args_list:
            self.assertEqual(call.kwargs, {"wait_until": "domcontentloaded", "timeout": 60000})
        self.assertEqual(page.wait_for_url.await_args.kwargs, {"wait_until": "domcontentloaded", "timeout": 60000})
        field.set_input_files.assert_awaited_once_with("fixture.mp4")
        diagnostic.assert_not_called()

    async def test_unavailable_input_stops_after_one_renavigation_without_upload(self):
        app, page, field = self.fixture()
        field.wait_for.side_effect = TimeoutError("missing upload input")
        with patch.object(xhs, "record_diagnostic") as diagnostic, self.assertRaises(TimeoutError):
            await app.upload_video_content(page)
        self.assertEqual(page.goto.await_count, 2)
        field.set_input_files.assert_not_awaited()
        app.fill_meta.assert_not_awaited()
        diagnostic.assert_called_once()
        self.assertEqual(diagnostic.call_args.args[:2], ("xiaohongshu", "navigation"))
        self.assertIsInstance(diagnostic.call_args.args[2], TimeoutError)

    async def test_file_transfer_failure_never_reopens_page_or_retransfers(self):
        app, page, field = self.fixture()
        field.set_input_files.side_effect = TimeoutError("file transfer uncertain")
        with patch.object(xhs, "record_diagnostic") as diagnostic, self.assertRaises(TimeoutError):
            await app.upload_video_content(page)
        page.goto.assert_awaited_once()
        field.set_input_files.assert_awaited_once()
        diagnostic.assert_called_once()
        self.assertEqual(diagnostic.call_args.args[:2], ("xiaohongshu", "upload"))
        self.assertIsInstance(diagnostic.call_args.args[2], TimeoutError)

    async def test_video_selection_excludes_images_and_rejects_ambiguous_matches(self):
        # Synthetic file-input metadata models an image input beside a hydrated
        # video input with new classes; no browser or file transfer is started.
        image = {"id": "cover", "type": "file", "accept": "image/png", "class": "upload-input"}
        video = {"id": "video", "type": "file", "accept": "video/mp4", "class": "hydrated-new-class"}
        extension_video = {"id": "extension-video", "type": "file", "accept": ".mp4,.mov", "class": "another-class"}
        for inputs, selected_id in (([image, video], "video"), ([image, extension_video], "extension-video"),
                                    ([image], None), ([image, video, extension_video], None)):
            with self.subTest(inputs=[item["id"] for item in inputs]):
                app, page, field = self.fixture()
                selected = []
                def locate(selector):
                    if selector.startswith("input[type='file']"):
                        self.assertEqual(selector, "input[type='file'][accept*='video'], input[type='file'][accept*='.mp4']")
                        matches = [item for item in inputs if item["type"] == "file" and
                                   ("video" in item["accept"] or ".mp4" in item["accept"])]
                        field.count.return_value = len(matches)
                        field.wait_for.side_effect = None if matches else TimeoutError("no video input")
                        field.set_input_files.side_effect = lambda _: selected.append(matches[0]["id"])
                        return field
                    self.assertIn("填写标题", selector)
                    return SimpleNamespace(wait_for=AsyncMock())
                page.locator = locate
                with patch.object(xhs, "record_diagnostic"), self.assertRaises((RuntimeError, TimeoutError)):
                    await app.upload_video_content(page)
                self.assertEqual(selected, [selected_id] if selected_id else [])
                if not selected_id:
                    field.set_input_files.assert_not_awaited()
                    app.fill_meta.assert_not_awaited()
                if len(inputs) == 3:
                    page.goto.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
