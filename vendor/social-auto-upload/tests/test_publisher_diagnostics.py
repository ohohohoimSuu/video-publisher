"""Bounded local checks of diagnostic labels; no browser or network is started."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import sau_cli
from utils import publisher_progress as progress
from uploader.xiaohongshu_uploader import main as xhs


class PublisherDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.result = self.root / "result.json"
        self.video = self.root / "fixture.mp4"
        self.video.write_bytes(b"never uploaded")
        env = {"PUBLISHER_PROGRESS_FILE": str(self.root / "progress.json"),
               "PUBLISHER_RESULT_FILE": str(self.result), "PUBLISHER_RESULT_NONCE": "local-nonce",
               "PUBLISHER_PROGRESS_PLATFORM": "xiaohongshu", "PUBLISHER_PROGRESS_ACTION": "publish",
               "SAU_PUBLISHER_SAFE_LOG": "1", "SAU_PUBLISHER_PREPARE_ONLY": "1"}
        self.environment = patch.dict(os.environ, env)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_allowlisted_types_and_counts_never_include_exception_text(self):
        SecretNamedError = type("SECRET_account_caption", (Exception,), {})
        progress.record_diagnostic("xiaohongshu", "topic", SecretNamedError("cookie=secret https://private"),
                                   topic_index=2, candidate_count=0)
        self.assertEqual(progress.get_diagnostic("xiaohongshu"),
                         {"step": "topic", "error_type": "OtherError", "topic_index": 2, "candidate_count": 0})
        for index, count in ((True, False), (11, 101), ("2", "0"), (-1, -1)):
            progress.record_diagnostic("xiaohongshu", "topic", RuntimeError("private"),
                                       topic_index=index, candidate_count=count)
            self.assertEqual(progress.get_diagnostic("xiaohongshu"), {"step": "topic", "error_type": "RuntimeError"})
        progress.record_diagnostic("xiaohongshu", "cover", TimeoutError("secret"), topic_index=2, candidate_count=1)
        self.assertEqual(progress.get_diagnostic("xiaohongshu"), {"step": "cover", "error_type": "TimeoutError"})
        progress.record_diagnostic("xiaohongshu", "upload", TimeoutError("cookie=secret https://private"))
        self.assertEqual(progress.get_diagnostic("xiaohongshu"), {"step": "upload", "error_type": "TimeoutError"})
        with self.assertRaises(ValueError):
            progress.record_diagnostic("xiaohongshu", "private caption", RuntimeError())

    def test_real_fill_meta_identifies_title_and_description_without_changing_order(self):
        for step in ("title", "description"):
            app = SimpleNamespace(fill_title=AsyncMock(), fill_desc=AsyncMock(), fill_tags=AsyncMock())
            getattr(app, "fill_title" if step == "title" else "fill_desc").side_effect = TimeoutError("private title")
            with self.assertRaises(TimeoutError):
                asyncio.run(xhs.XiaoHongShuBaseUploader.fill_meta(app, object()))
            self.assertEqual(progress.get_diagnostic("xiaohongshu"), {"step": step, "error_type": "TimeoutError"})
            app.fill_tags.assert_not_awaited()
            if step == "title":
                app.fill_desc.assert_not_awaited()
            else:
                app.fill_title.assert_awaited_once()

    def test_exact_topic_timeout_records_original_index_and_readonly_match_count(self):
        candidate = SimpleNamespace(wait_for=AsyncMock(side_effect=TimeoutError("private suggestion")),
                                    count=AsyncMock(return_value=0), click=AsyncMock())
        candidate.first = candidate
        candidate.filter = lambda **kwargs: candidate
        page = SimpleNamespace(keyboard=SimpleNamespace(type=AsyncMock()),
                               locator=lambda selector: candidate if selector.endswith(".item") else SimpleNamespace(wait_for=AsyncMock()),
                               get_by_text=lambda pattern: pattern)
        with self.assertRaises(RuntimeError):
            asyncio.run(xhs.XiaoHongShuBaseUploader.fill_tags(SimpleNamespace(tags=["private topic"], desc="private"), page))
        self.assertEqual(progress.get_diagnostic("xiaohongshu"),
                         {"step": "topic", "error_type": "TimeoutError", "topic_index": 1, "candidate_count": 0})
        candidate.click.assert_not_awaited()
        candidate.count.assert_awaited_once()

    def test_real_cover_failure_keeps_only_step_and_type(self):
        field = SimpleNamespace(scroll_into_view_if_needed=AsyncMock(), count=AsyncMock(return_value=1),
                                click=AsyncMock(side_effect=TimeoutError("cookie=private")))
        field.first = field
        page = SimpleNamespace(locator=lambda selector: field, wait_for_timeout=AsyncMock(),
                               get_by_role=MagicMock(side_effect=TimeoutError("cookie=private")))
        with self.assertRaises(RuntimeError):
            asyncio.run(xhs.XiaoHongShuVideo.set_thumbnail(None, page, "private-file.png"))
        self.assertEqual(progress.get_diagnostic("xiaohongshu"), {"step": "cover", "error_type": "TimeoutError"})

    def test_cli_failure_writes_atomic_paired_result_and_keeps_phase(self):
        async def fail(args):
            progress.emit_progress("xiaohongshu", "metadata")
            app = SimpleNamespace(fill_title=AsyncMock(side_effect=TimeoutError("private title cookie=secret")),
                                  fill_desc=AsyncMock(), fill_tags=AsyncMock())
            await xhs.XiaoHongShuBaseUploader.fill_meta(app, object())
        with patch.object(sau_cli, "dispatch", side_effect=fail), patch.object(sau_cli, "print"):
            code = sau_cli.main(["xiaohongshu", "upload-video", "--account", "fixture", "--file", str(self.video), "--title", "fixture"])
        self.assertEqual(code, 1)
        value = json.loads(self.result.read_text())
        self.assertEqual(value, {"nonce": "local-nonce", "platform": "xiaohongshu", "action": "publish",
                                 "status": "failed", "diagnostic": {"step": "title", "error_type": "TimeoutError"}})
        phase = json.loads((self.root / "progress.json").read_text())
        self.assertEqual((phase["phase"], phase["error_code"]), ("metadata", "metadata_failed"))
        self.assertEqual(self.result.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.root.glob(".publisher-*")), [])

    def test_diagnostics_cannot_leak_into_another_run_or_platform(self):
        progress.record_diagnostic("xiaohongshu", "topic", ValueError("private"), topic_index=3, candidate_count=2)
        with patch.dict(os.environ, {"PUBLISHER_RESULT_NONCE": "another-nonce"}):
            self.assertIsNone(progress.get_diagnostic("xiaohongshu"))
            progress.write_failure_result("xiaohongshu")
            self.assertFalse(self.result.exists())
        self.assertIsNone(progress.get_diagnostic("bilibili"))


if __name__ == "__main__":
    unittest.main()
