import asyncio
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import sau_cli


class BilibiliCliTests(unittest.TestCase):
    def test_build_parser_accepts_bilibili_login(self):
        parser = sau_cli.build_parser()
        args = parser.parse_args(["bilibili", "login", "--account", "creator"])
        self.assertEqual(args.platform, "bilibili")
        self.assertEqual(args.action, "login")

    def test_build_parser_requires_tid_for_upload_video(self):
        parser = sau_cli.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "bilibili",
                    "upload-video",
                    "--account",
                    "creator",
                    "--file",
                    "demo.mp4",
                    "--title",
                    "hello",
                    "--desc",
                    "hello",
                ]
            )

    def test_dispatch_bilibili_check_prints_valid(self):
        args = Namespace(platform="bilibili", action="check", account="creator")
        with patch("sau_cli.check_bilibili_account", new=AsyncMock(return_value=True)):
            code = asyncio.run(sau_cli.dispatch(args))
        self.assertEqual(code, 0)

    def test_login_bilibili_account_returns_friendly_message_without_terminal(self):
        with patch("sau_cli.has_interactive_terminal", return_value=False):
            result = asyncio.run(sau_cli.login_bilibili_account("creator"))
        self.assertFalse(result["success"])
        self.assertIn("local interactive terminal", result["message"].lower())
        self.assertIn("qrcode.png", result["message"].lower())

    def test_prepare_passes_materials_to_web_editor_without_native_posting(self):
        request = sau_cli.BilibiliVideoUploadRequest("creator", Path("demo.mp4"), "hello", "description", None, ["test"], 0, Path("cover.png"), category="科技数码", declaration="none", prepare_only=True)
        app = SimpleNamespace(main=AsyncMock(return_value="prepared"))
        with patch("sau_cli.resolve_account_file", return_value=Path("account.json")), \
             patch("uploader.bilibili_uploader.web_prepare.BilibiliWebPrepare", return_value=app) as factory, \
             patch("sau_cli.run_biliup_command") as native:
            self.assertEqual(asyncio.run(sau_cli.upload_bilibili_video(request)), Path("account.json"))
            factory.assert_called_once_with(request, Path("account.json"))
            native.assert_not_called()
            app.main.return_value = None
            with self.assertRaises(RuntimeError): asyncio.run(sau_cli.upload_bilibili_video(request))
            native.assert_not_called()

    def test_non_prepare_request_cannot_call_native_uploader(self):
        request = sau_cli.BilibiliVideoUploadRequest("creator", Path("demo.mp4"), "hello", "description", 21, ["test"], 0)
        with patch("sau_cli.run_biliup_command") as native:
            with self.assertRaises(RuntimeError): asyncio.run(sau_cli.upload_bilibili_video(request))
            native.assert_not_called()
