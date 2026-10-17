"""One shared CLI contract for the three explicit manual-submit platforms."""
import asyncio
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import sau_cli


class PrepareCliContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.video = self.root / "fixture.mp4"
        self.video.write_bytes(b"never uploaded")
        self.result = self.root / "result.json"
        self.env = {"SAU_PUBLISHER_PREPARE_ONLY": "1", "SAU_PUBLISHER_SAFE_LOG": "1",
                    "PUBLISHER_RESULT_FILE": str(self.result), "PUBLISHER_RESULT_NONCE": "fixture"}

    def test_only_three_video_actions_return_prepared_instead_of_submitted(self):
        for platform, method in (("douyin", "upload_video"), ("xiaohongshu", "upload_xiaohongshu_video"), ("youtube", "upload_youtube_video")):
            with self.subTest(platform=platform):
                args = [platform, "upload-video", "--account", "fixture", "--file", str(self.video), "--title", "fixture", "--headed"]
                if platform == "youtube":
                    args += ["--made-for-kids", "false", "--altered-synthetic", "false", "--visibility", "private"]
                output = io.StringIO()
                progress_file = self.root / (platform + "-progress.json")
                env = {**self.env, "PUBLISHER_PROGRESS_FILE": str(progress_file),
                       "PUBLISHER_PROGRESS_PLATFORM": platform, "PUBLISHER_PROGRESS_ACTION": "publish"}
                with patch.dict(os.environ, env), patch.object(sau_cli, method, new=AsyncMock()) as upload, patch("sys.stdout", output):
                    self.assertEqual(sau_cli.main(args), 0)
                self.assertTrue(upload.await_args.args[0].prepare_only)
                self.assertNotIn("submitted", output.getvalue())
                value = json.loads(self.result.read_text())
                self.assertEqual((value["platform"], value["status"], value["phase"], value["retry_safe"]),
                                 (platform, "prepared", "ready_to_submit", False))
                self.assertEqual(json.loads(progress_file.read_text())["phase"], "ready_to_submit")
        with patch.dict(os.environ, self.env), patch.object(sau_cli, "dispatch", new=AsyncMock()) as dispatch, patch("sys.stderr", io.StringIO()):
            for args in (["bilibili", "check", "--account", "fixture"], ["youtube", "check", "--account", "fixture"]):
                with self.assertRaises(SystemExit):
                    sau_cli.main(args)
            dispatch.assert_not_awaited()

    def test_youtube_metadata_and_account_file_survive_prepare_routing(self):
        request = sau_cli.YouTubeVideoUploadRequest("fixture", self.video, "title", "description", ["tag"],
                                                   thumbnail_file=self.video, playlist="playlist", visibility="private",
                                                   made_for_kids=True, altered_synthetic=True, prepare_only=True)
        app = SimpleNamespace(main=AsyncMock(return_value="prepared"))
        account = self.root / "synthetic-account.json"
        with patch.object(sau_cli, "resolve_account_file", return_value=account), \
             patch.object(sau_cli, "youtube_setup", new=AsyncMock(return_value=True)), \
             patch.object(sau_cli, "YouTubeVideo", return_value=app) as cls:
            self.assertEqual(asyncio.run(sau_cli.upload_youtube_video(request)), account)
            self.assertEqual(cls.call_args.args, ("title", str(self.video), ["tag"], str(account)))
            for key, value in {"description": "description", "thumbnail_path": str(self.video), "playlist": "playlist",
                               "visibility": "private", "made_for_kids": True, "altered_synthetic": True, "prepare_only": True}.items():
                self.assertEqual(cls.call_args.kwargs[key], value)
            app.main.return_value = None
            with self.assertRaises(RuntimeError):
                asyncio.run(sau_cli.upload_youtube_video(request))


if __name__ == "__main__":
    unittest.main()
