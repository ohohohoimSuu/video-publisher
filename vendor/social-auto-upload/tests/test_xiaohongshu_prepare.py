"""Prepare-only boundaries using synthetic files and browser-shaped doubles."""
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
from uploader.xiaohongshu_uploader import main as xhs


def uploader():
    return xhs.XiaoHongShuVideo(title="fixture", file_path="fixture.mp4", tags=[], publish_date=0,
                               account_file="unused.json", thumbnail_path="fixture.png", headless=False,
                               prepare_only=True)


class PrepareBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepared_waits_for_material_and_cover_but_never_reaches_submit(self):
        app = uploader()
        order, phases = [], []
        def step(name):
            order.append(name)
        app.fill_meta = AsyncMock(side_effect=lambda *args: step("metadata"))
        app.wait_for_video_material_ready = AsyncMock(side_effect=lambda *args: step("material_ready"))
        app.set_thumbnail = AsyncMock(side_effect=lambda *args: step("cover"))
        app.check_original_declaration = AsyncMock()
        field = SimpleNamespace(wait_for=AsyncMock(), is_enabled=AsyncMock(return_value=True), set_input_files=AsyncMock(), count=AsyncMock(return_value=1))
        field.first = field
        page = SimpleNamespace(goto=AsyncMock(), wait_for_url=AsyncMock(), locator=lambda _: field,
                               get_by_role=MagicMock(side_effect=AssertionError("submit branch must not be reached")))
        with patch.object(xhs, "emit_progress", side_effect=lambda platform, phase: phases.append(phase)):
            result = await app.upload_video_content(page)
        self.assertEqual(result, "prepared")
        self.assertEqual(order, ["metadata", "material_ready", "cover"])
        self.assertEqual(phases, ["uploading", "metadata", "cover", "ready_to_submit"])
        app.check_original_declaration.assert_not_awaited()
        page.get_by_role.assert_not_called()

    async def test_driver_disconnect_keeps_persistent_context_on_success_and_error(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                app = uploader()
                app.validate_upload_args = AsyncMock()
                app.upload_video_content = AsyncMock(return_value="prepared", side_effect=RuntimeError("fixture") if failure else None)
                context = SimpleNamespace(new_page=AsyncMock(return_value=object()), close=AsyncMock(), storage_state=AsyncMock())
                browser = SimpleNamespace(close=AsyncMock(), new_context=AsyncMock())
                playwright = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock()))
                driver = MagicMock()
                driver.__aenter__ = AsyncMock(return_value=playwright)
                driver.__aexit__ = AsyncMock(return_value=False)
                with patch.object(xhs.publisher_handoff, "connect_publisher_handoff", new=AsyncMock(return_value=(browser, context)), create=True) as connect, \
                     patch.object(xhs, "set_init_script", new=AsyncMock(return_value=context)), \
                     patch.object(xhs, "async_playwright", return_value=driver):
                    if failure:
                        with self.assertRaises(RuntimeError):
                            await app.main()
                    else:
                        self.assertEqual(await app.main(), "prepared")
                connect.assert_awaited_once_with(playwright, "unused.json", "xiaohongshu")
                driver.__aexit__.assert_awaited_once()
                context.close.assert_not_awaited()
                context.storage_state.assert_not_awaited()
                browser.close.assert_not_awaited()
                browser.new_context.assert_not_awaited()
                playwright.chromium.launch.assert_not_awaited()

    async def test_prepare_headless_is_rejected_before_account_validation(self):
        app = uploader()
        app.headless = True
        app.validate_upload_args = AsyncMock()
        with self.assertRaises(RuntimeError):
            await app.upload(object())
        app.validate_upload_args.assert_not_awaited()


class PrepareCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.video = self.root / "fixture.mp4"
        self.video.write_bytes(b"never uploaded")
        self.result = self.root / "result.json"
        self.progress = self.root / "progress.json"
        self.env = {"SAU_PUBLISHER_PREPARE_ONLY": "1", "SAU_PUBLISHER_SAFE_LOG": "1",
                    "PUBLISHER_RESULT_FILE": str(self.result), "PUBLISHER_RESULT_NONCE": "fixture-nonce",
                    "PUBLISHER_PROGRESS_FILE": str(self.progress), "PUBLISHER_PROGRESS_PLATFORM": "xiaohongshu",
                    "PUBLISHER_PROGRESS_ACTION": "publish"}
        self.args = ["xiaohongshu", "upload-video", "--account", "fixture", "--file", str(self.video),
                     "--title", "fixture", "--headed"]

    def test_cli_propagates_prepare_and_never_emits_submitted(self):
        output = io.StringIO()
        with patch.dict(os.environ, self.env), \
             patch.object(sau_cli, "upload_xiaohongshu_video", new=AsyncMock(return_value=Path("unused.json"))) as upload, \
             patch("sys.stdout", output):
            code = sau_cli.main(self.args)
        self.assertEqual(code, 0)
        self.assertTrue(upload.await_args.args[0].prepare_only)
        self.assertNotIn("submitted", output.getvalue())
        result = json.loads(self.result.read_text())
        self.assertEqual((result["status"], result["phase"], result["retry_safe"]), ("prepared", "ready_to_submit", False))
        self.assertEqual(result["cover_status"], "not_requested")
        self.assertEqual(json.loads(self.progress.read_text())["phase"], "ready_to_submit")

    def test_cli_error_and_unsupported_action_cannot_report_prepared(self):
        with patch.dict(os.environ, self.env), \
             patch.object(sau_cli, "upload_xiaohongshu_video", new=AsyncMock(side_effect=RuntimeError("fixture"))), \
             patch("sys.stderr", io.StringIO()):
            self.assertEqual(sau_cli.main(self.args), 1)
            with self.assertRaises(SystemExit):
                sau_cli.main(["xiaohongshu", "check", "--account", "fixture"])
        # CLI failures now retain a sanitized diagnostic, but can never report
        # prepared/submitted or include the raw exception text.
        result = json.loads(self.result.read_text())
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['diagnostic']['error_type'], 'RuntimeError')
        self.assertNotIn('prepared', result.values())

    def test_request_wrapper_rejects_an_unconfirmed_prepare_return(self):
        request = sau_cli.XiaohongshuVideoUploadRequest("fixture", self.video, "fixture", "", [], 0, prepare_only=True)
        for outcome in ("prepared", None):
            with self.subTest(outcome=outcome):
                app = SimpleNamespace(main=AsyncMock(return_value=outcome))
                with patch.object(sau_cli, "resolve_account_file", return_value=Path("unused.json")), \
                     patch.object(sau_cli, "xiaohongshu_setup", new=AsyncMock(return_value=True)), \
                     patch.object(sau_cli, "XiaoHongShuVideo", return_value=app) as cls:
                    if outcome == "prepared":
                        self.assertEqual(asyncio.run(sau_cli.upload_xiaohongshu_video(request)), Path("unused.json"))
                    else:
                        with self.assertRaises(RuntimeError):
                            asyncio.run(sau_cli.upload_xiaohongshu_video(request))
                self.assertTrue(cls.call_args.kwargs["prepare_only"])


if __name__ == "__main__":
    unittest.main()
