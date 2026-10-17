"""Regression for the observed editable-title / 18%-uploaded page state."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from uploader.xiaohongshu_uploader import main as xhs


class MaterialReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_metadata_can_fill_but_cover_waits_for_upload_and_enabled_publish(self):
        state = {"busy": True, "enabled": False}
        phases, checks = [], []
        field = SimpleNamespace(wait_for=AsyncMock(), set_input_files=AsyncMock(), is_enabled=AsyncMock(return_value=True), count=AsyncMock(return_value=1))
        field.first = field
        publish = SimpleNamespace(count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=True),
                                  is_enabled=AsyncMock(side_effect=lambda: state["enabled"]), click=AsyncMock())

        class StatusMatches:
            def __init__(self, pattern):
                self.busy_pattern = bool(pattern.search("上传中 18%"))
            async def count(self):
                return 1
            def nth(self, index):
                return self
            async def is_visible(self):
                return state["busy"] if self.busy_pattern else False

        page = SimpleNamespace(goto=AsyncMock(), wait_for_url=AsyncMock(), locator=lambda _: field,
                               get_by_role=lambda *args, **kwargs: publish,
                               get_by_text=lambda pattern: StatusMatches(pattern))
        app = xhs.XiaoHongShuVideo(title="fixture", file_path="fixture.mp4", tags=[], publish_date=0,
                                  account_file="unused.json", thumbnail_path="fixture.png")
        app.fill_meta = AsyncMock()
        app.set_thumbnail = AsyncMock()
        app.check_original_declaration = AsyncMock()

        async def upload_advances(_):
            app.fill_meta.assert_awaited_once()
            app.set_thumbnail.assert_not_awaited()
            publish.click.assert_not_awaited()
            self.assertEqual(phases[-1], "metadata")
            checks.append(dict(state))
            # Both gates are necessary: no progress label alone is insufficient,
            # and an enabled button while progress is visible is insufficient.
            if len(checks) == 1:
                state.update(busy=False, enabled=False)
            elif len(checks) == 2:
                state.update(busy=True, enabled=True)
            else:
                state.update(busy=False, enabled=True)

        with patch.object(xhs.asyncio, "sleep", new=AsyncMock(side_effect=upload_advances)), \
             patch.object(xhs, "emit_progress", side_effect=lambda platform, phase: phases.append(phase)), \
             patch("builtins.print") as heartbeat:
            await app.upload_video_content(page)
        self.assertEqual(len(checks), 3)
        app.set_thumbnail.assert_awaited_once_with(page, "fixture.png")
        self.assertEqual(phases, ["uploading", "metadata", "cover", "ready_to_submit", "submitting", "submitted"])
        publish.click.assert_awaited_once()
        heartbeat.assert_called_with("小红书视频仍在上传或处理中。", flush=True)


if __name__ == "__main__":
    unittest.main()
