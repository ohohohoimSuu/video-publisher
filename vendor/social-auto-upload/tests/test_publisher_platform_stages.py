"""Structured stages stay before side effects and never infer success from logs."""
import asyncio
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import sau_cli
from utils import publisher_progress as progress
from uploader.douyin_uploader import main as douyin


class PlatformStageTests(unittest.TestCase):
    def test_cli_failure_records_preparation_and_preserves_the_last_stage_for_enabled_prepare_platforms(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "fixture.mp4"
            video.write_bytes(b"never uploaded")
            for platform in ("douyin", "xiaohongshu", "youtube"):
                with self.subTest(platform=platform):
                    path = root / (platform + "-progress.json")
                    env = {"SAU_PUBLISHER_PREPARE_ONLY": "1", "PUBLISHER_PROGRESS_FILE": str(path),
                           "PUBLISHER_RESULT_NONCE": platform, "PUBLISHER_PROGRESS_PLATFORM": platform,
                           "PUBLISHER_PROGRESS_ACTION": "publish"}
                    args = [platform, "upload-video", "--account", "fixture", "--file", str(video), "--title", "fixture"]
                    if platform == "bilibili":
                        args += ["--desc", "", "--tid", "21"]
                    elif platform == "youtube":
                        args += ["--made-for-kids", "false", "--altered-synthetic", "false", "--visibility", "private"]
                    async def fail(parsed):
                        self.assertEqual(json.loads(path.read_text())["phase"], "preparation")
                        if platform == "bilibili":
                            progress.emit_progress(platform, None, "submission_boundary_unknown")
                        else:
                            progress.emit_progress(platform, "metadata")
                        raise RuntimeError("fixture_secret")
                    with patch.dict(os.environ, env), patch.object(sau_cli, "dispatch", side_effect=fail), patch("sys.stderr", io.StringIO()):
                        self.assertEqual(sau_cli.main(args), 1)
                    value = json.loads(path.read_text())
                    self.assertEqual(value["phase"], None if platform == "bilibili" else "metadata")
                    self.assertEqual(value["error_code"], "submission_boundary_unknown" if platform == "bilibili" else "metadata_failed")
                    self.assertNotIn("fixture_secret", path.read_text())


class DouyinStageTests(unittest.IsolatedAsyncioTestCase):
    def app(self, prepare_only):
        return douyin.DouYinVideo("fixture", "fixture.mp4", [], 0, "unused.json", headless=False, prepare_only=prepare_only)

    async def test_prepare_stages_precede_file_metadata_cover_and_stop_at_ready(self):
        app, phases = self.app(True), []
        def require(phase):
            self.assertEqual(phases[-1], phase)
        field = SimpleNamespace(wait_for=AsyncMock(), set_input_files=AsyncMock(side_effect=lambda _: require("uploading")),
                                count=AsyncMock(return_value=1))
        field.first = field
        empty = SimpleNamespace(count=AsyncMock(return_value=0))
        button = SimpleNamespace(count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=True),
                                 is_enabled=AsyncMock(return_value=True), get_attribute=AsyncMock(return_value=None),
                                 click=AsyncMock(side_effect=AssertionError("must not click final publish")))
        button.filter = lambda **kwargs: button
        page = SimpleNamespace(goto=AsyncMock(), wait_for_url=AsyncMock(), wait_for_timeout=AsyncMock(),
                               url="https://creator.douyin.com/creator-micro/content/post/video", is_closed=lambda: False,
                               evaluate=AsyncMock(return_value={"completed": True, "busy": False, "error": False}),
                               locator=lambda selector: field if "accept" in selector else empty,
                               get_by_role=MagicMock(return_value=button))
        context = SimpleNamespace(new_page=AsyncMock(return_value=page), close=AsyncMock(), storage_state=AsyncMock())
        browser = SimpleNamespace(close=AsyncMock())
        playwright = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock()))
        app.validate_upload_args = AsyncMock()
        app.fill_title_and_description = AsyncMock(side_effect=lambda *args: require("metadata"))
        app.apply_self_declaration = AsyncMock()
        app.apply_collection = AsyncMock()
        app.set_thumbnail = AsyncMock(side_effect=lambda *args: require("cover"))
        with patch.object(douyin, "connect_douyin_handoff", new=AsyncMock(return_value=(browser, context))), \
             patch.object(douyin, "set_init_script", new=AsyncMock(return_value=context)), \
             patch.object(douyin.asyncio, "sleep", new=AsyncMock()), \
             patch.object(douyin, "emit_progress", side_effect=lambda platform, phase: phases.append(phase)):
            self.assertEqual(await app.upload(playwright), "prepared")
        # Percentage/heartbeat updates may repeat the current phase. The order
        # of distinct stages and the final manual boundary must stay intact.
        distinct = [phase for index, phase in enumerate(phases) if index == 0 or phases[index-1] != phase]
        self.assertEqual(distinct, ["uploading", "metadata", "cover", "ready_to_submit"])
        button.click.assert_not_awaited()
        page.get_by_role.assert_called_once_with("button", name="发布", exact=True)
        context.close.assert_not_awaited()
        browser.close.assert_not_awaited()

    async def test_existing_auto_click_is_marked_submitting_before_it_can_fail(self):
        app, phases = self.app(False), []
        field = SimpleNamespace(count=AsyncMock(return_value=0))
        field.first = field
        def click(**kwargs):
            self.assertEqual(phases[-1], "submitting")
        button = SimpleNamespace(click=AsyncMock(side_effect=click))
        page = SimpleNamespace(locator=lambda _: field, get_by_role=lambda *args, **kwargs: button,
                               wait_for_url=AsyncMock(side_effect=TimeoutError("fixture")))
        with patch.object(douyin, "emit_progress", side_effect=lambda platform, phase: phases.append(phase)), self.assertRaises(RuntimeError):
            await app.finish_upload(page, object(), object())
        self.assertEqual(phases, ["ready_to_submit", "submitting"])
        button.click.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
