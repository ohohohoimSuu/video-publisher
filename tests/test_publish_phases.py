"""Local child processes and DOM-shaped mocks; never contact a platform."""
import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import adapter

sys.path.insert(0, str(adapter.VENDOR))
import sau_cli
from utils import publisher_progress as progress
from uploader.xiaohongshu_uploader import main as xhs


class Field:
    def __init__(self, count=1):
        self.first = self.last = self
        self.count = AsyncMock(return_value=count)
        self.fill = AsyncMock()
        self.click = AsyncMock()
        self.wait_for = AsyncMock()
        self.set_input_files = AsyncMock()
        self.scroll_into_view_if_needed = AsyncMock()
        self.is_visible = AsyncMock(return_value=True)
        self.is_enabled = AsyncMock(return_value=True)

    def filter(self, **kwargs):
        return self

    def get_by_text(self, *args, **kwargs):
        return self


class CoverEditor:
    """The modern, visible cover modal; publishing is a separate control."""
    def __init__(self, mode):
        self.mode = mode
        self.opened = self.hovered = False
        self.file = None
        self.preview_reads = 0
        self.actions = []

    def control(self, kind):
        return CoverControl(self, kind)


class CoverControl:
    def __init__(self, editor, kind, visible=False):
        self.editor, self.kind, self.visible_filter = editor, kind, visible

    def filter(self, **kwargs):
        return CoverControl(self.editor, self.kind, kwargs.get("visible", self.visible_filter))

    async def count(self):
        if self.kind == "legacy":
            return 0
        if self.kind == "hidden_old_slot":
            return 0 if self.visible_filter else 1
        if self.kind in {"heading", "modal", "file", "save"}:
            return int(self.editor.opened)
        if self.kind == "edit":
            return int(self.editor.hovered and not self.editor.opened)
        return 1

    async def is_visible(self):
        return bool(await self.count()) and self.kind != "hidden_old_slot"

    async def is_enabled(self):
        return True

    def locator(self, selector):
        assert self.kind == "heading" and selector.startswith("xpath=ancestor::")
        assert "not(self::body)" in selector and "完成" in selector
        return self.editor.control("modal")

    def get_by_label(self, label, *, exact):
        assert self.kind == "modal" and (label, exact) == ("上传封面图片", True)
        return self.editor.control("file")

    def get_by_role(self, role, *, name, exact):
        assert self.kind == "modal" and (role, name, exact) == ("button", "完成", True)
        return self.editor.control("save")

    async def evaluate(self, script):
        assert self.kind == "modal" and "signature" in script
        if self.editor.file is not None:
            self.editor.preview_reads += 1
        return {"signature": "new-cover" if self.editor.file is not None else "old-cover", "ready": True}

    async def hover(self):
        assert self.kind == "preview" and self.visible_filter
        self.editor.hovered = True
        self.editor.actions.append("hover_preview")

    async def wait_for(self, *, state, **kwargs):
        if state == "hidden":
            assert self.kind == "modal" and not self.editor.opened
        elif not await self.is_visible():
            raise TimeoutError("synthetic invisible cover control")

    async def click(self, **kwargs):
        if self.kind == "edit":
            assert self.visible_filter and self.editor.hovered
            if self.editor.mode == "cover_failure":
                raise TimeoutError("synthetic cover selector failure")
            self.editor.opened = True
            self.editor.actions.append("edit_cover")
        elif self.kind == "save":
            assert self.visible_filter and self.editor.file == "fixture.png"
            assert self.editor.preview_reads >= 2, "must wait for a changed, stable cover preview before saving"
            self.editor.actions.append("save_cover")
            self.editor.opened = False
        else:
            raise AssertionError("hidden, unrelated, or publish controls must not be clicked by set_thumbnail")

    async def set_input_files(self, path):
        assert self.kind == "file" and self.editor.opened
        self.editor.file = path
        self.editor.actions.append(("cover_file", path))


def xhs_fixture(mode):
    """Exercise the real video method, metadata methods, and cover method."""
    page_field, topic, publish = Field(), Field(0 if mode == "tag_failure" else 1), Field()
    cover = CoverEditor(mode)
    preview = SimpleNamespace(inner_text=AsyncMock(return_value="上传成功"))
    upload_input = SimpleNamespace(query_selector=AsyncMock(return_value=preview))
    def locate(selector):
        cover_controls = {"div.upload-cover": "hidden_old_slot", "div.d-modal": "legacy", "div.cover-plugin-preview": "preview"}
        if selector in cover_controls:
            return cover.control(cover_controls[selector])
        if selector.startswith("#creator-editor-topic-container"):
            return topic
        return page_field
    async def wait_url(url, **kwargs):
        if url == xhs.XHS_PUBLISH_SUCCESS_URL_PATTERN and mode == "after_click_timeout":
            raise TimeoutError("synthetic confirmation timeout")
    async def click_publish(**kwargs):
        current = json.loads(Path(os.environ["PUBLISHER_PROGRESS_FILE"]).read_text())
        if current["phase"] != "submitting":
            raise AssertionError("submission boundary must be durable before click")
        assert not cover.opened and cover.actions[-1] == "save_cover", "cover must be confirmed before submission"
    def find_text(value, **kwargs):
        if value == "编辑封面":
            assert kwargs.get("exact") is True
            return cover.control("edit")
        if hasattr(value, "search") and (value.search("上传中 18%") or value.search("上传失败")):
            return Field(count=0)
        return page_field
    def find_role(role, *, name, exact):
        if (role, name, exact) == ("heading", "设置封面", True):
            return cover.control("heading")
        assert (role, name, exact) == ("button", "发布", True)
        return publish
    publish.click.side_effect = click_publish
    page = SimpleNamespace(
        goto=AsyncMock(), wait_for_url=AsyncMock(side_effect=wait_url),
        wait_for_selector=AsyncMock(return_value=upload_input), locator=locate,
        get_by_role=find_role,
        get_by_text=find_text,
        cover_fixture=cover,
        wait_for_timeout=AsyncMock(),
        keyboard=SimpleNamespace(type=AsyncMock(), press=AsyncMock()),
    )
    app = xhs.XiaoHongShuVideo(title="synthetic", file_path="fixture.mp4", tags=["exact-topic"],
                              publish_date=0, account_file="synthetic-unused.json", thumbnail_path="fixture.png")
    app.check_original_declaration = AsyncMock()
    return app, page, publish


def run_xhs_fixture(mode, video):
    app, page, publish = xhs_fixture(mode)
    async def dispatch(args):
        await app.upload_video_content(page)
        return 0
    # Historical uploader protocol regression only. The public CLI now rejects
    # automatic posting; never route this synthetic legacy test through it.
    try:
        progress.emit_progress("xiaohongshu", "preparation")
        asyncio.run(dispatch(None))
        result = {"nonce": os.environ["PUBLISHER_RESULT_NONCE"], "platform": "xiaohongshu",
                  "action": "publish", "status": "submitted"}
        Path(os.environ["PUBLISHER_RESULT_FILE"]).write_text(json.dumps(result))
        code = 0
    except Exception:
        progress.record_failure("xiaohongshu")
        progress.write_failure_result("xiaohongshu")
        code = 1
    if mode in {"cover_failure", "tag_failure"}:
        publish.click.assert_not_awaited()
    else:
        publish.click.assert_awaited_once()
        assert page.cover_fixture.actions == ["hover_preview", "edit_cover", ("cover_file", "fixture.png"), "save_cover"]
    return code


class Events:
    def __init__(self):
        self.logs = []
        self.events = []

    def __call__(self, message):
        self.logs.append(message)

    def event(self, event):
        self.events.append(event)


class PublishPhaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.file = self.root / "progress.json"
        self.result_file = self.root / "result.json"
        self.video = self.root / "synthetic.mp4"
        self.video.write_bytes(b"local fixture, never uploaded")
        self.env = {
            "PUBLISHER_PROGRESS_FILE": str(self.file), "PUBLISHER_RESULT_NONCE": "synthetic-nonce",
            "PUBLISHER_PROGRESS_PLATFORM": "xiaohongshu", "PUBLISHER_PROGRESS_ACTION": "publish",
            "PUBLISHER_RESULT_FILE": str(self.result_file),
        }

    def read(self, platform="xiaohongshu"):
        return adapter._read_progress(self.file, "synthetic-nonce", platform, "publish")

    def local_run(self, platform, source, events=None, cancel=None, timeout=5):
        # This calls the real process supervisor; only the CLI command is a fixture.
        prefix = f"import sys; sys.path[:0] = {[str(adapter.BASE), str(adapter.VENDOR), str(Path(__file__).parent)]!r}; "
        return adapter._execute([str(adapter.PYTHON), "-u", "-c", prefix + source], platform, "publish",
                                events or Events(), cancel, timeout_seconds=timeout)

    def local_xhs_fixture(self, mode):
        # Importing the real CLI and all its uploader dependencies can consume
        # nearly five seconds on this Mac. Keep the real preview-stability
        # wait; only these full-fixture runs receive a startup allowance.
        source = f"from test_publish_phases import run_xhs_fixture; sys.exit(run_xhs_fixture({mode!r}, {str(self.video)!r}))"
        return self.local_run("xiaohongshu", source, timeout=15)

    def final_result(self, observed, platform="xiaohongshu"):
        cookies = self.root / "cookies"
        cookies.mkdir(exist_ok=True)
        (cookies / f"{platform}_default.json").write_text("{}")
        metadata = {"title": "synthetic", "video_path": str(self.video), "tags": [],
                    "description": "", "ai_generated": False, "bilibili": {"tid": 21}}
        with patch.object(adapter, "COOKIE_DIR", cookies), \
             patch.object(adapter, "runtime_status", return_value={"ready": True}), \
             patch.object(adapter, "_execute", return_value=observed):
            return adapter.run(platform, "publish", metadata)

    def test_atomic_private_schema_and_irreversible_submit_boundary(self):
        with patch.dict(os.environ, self.env):
            progress.emit_progress("xiaohongshu", "cover")
            progress.emit_progress("xiaohongshu", "submitting")
            with self.assertRaises(ValueError):
                progress.emit_progress("xiaohongshu", "metadata")
            progress.record_failure("xiaohongshu")
        payload = json.loads(self.file.read_text())
        self.assertEqual(set(payload), {"version", "nonce", "platform", "action", "phase", "updated_at", "error_code"})
        self.assertEqual(self.read()["phase"], "submitting")
        self.assertEqual(self.read()["error_code"], "submission_unconfirmed")
        self.assertEqual(self.file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.root.glob(".progress-*")), [])

    def test_unknown_native_boundary_cannot_regress_to_preparation(self):
        with patch.dict(os.environ, {**self.env, "PUBLISHER_PROGRESS_PLATFORM": "bilibili"}):
            progress.emit_progress("bilibili", "preparation")
            progress.emit_progress("bilibili", None, "submission_boundary_unknown")
            with self.assertRaises(ValueError):
                progress.emit_progress("bilibili", "preparation")
            progress.record_failure("bilibili")
        self.assertIsNone(self.read("bilibili")["phase"])

    def test_progress_rejects_stale_cross_platform_and_arbitrary_content(self):
        with patch.dict(os.environ, self.env):
            progress.emit_progress("xiaohongshu", "metadata")
        good = json.loads(self.file.read_text())
        for values in ({"nonce": "stale"}, {"platform": "bilibili"}, {"action": "login"},
                       {"phase": "published"}, {"error_code": "cookie=private"}, {"error_code": []},
                       {"caption": "private"}, {"updated_at": "not-a-time"}, {"version": True}):
            with self.subTest(values=values):
                self.file.write_text(json.dumps({**good, **values}))
                self.assertIsNone(self.read())

    def test_real_mocked_xhs_tag_and_cover_failures_are_safe_to_retry(self):
        for mode, expected in (("tag_failure", "metadata"), ("cover_failure", "cover")):
            with self.subTest(mode=mode):
                observed = self.local_xhs_fixture(mode)
                result = self.final_result(observed)
                self.assertEqual((result["status"], result["phase"], result["retry_safe"]), ("failed", expected, True))
                self.assertEqual(result["error_code"], expected + "_failed")
                self.assertFalse(observed["submitted"])

    def test_real_mocked_xhs_after_click_timeout_is_never_safe(self):
        observed = self.local_xhs_fixture("after_click_timeout")
        result = self.final_result(observed)
        self.assertEqual((result["status"], result["phase"], result["retry_safe"]), ("needs_attention", "submitting", False))
        self.assertEqual(result["error_code"], "submission_unconfirmed")

    def test_real_mocked_xhs_success_still_needs_exit_zero_and_private_result(self):
        observed = self.local_xhs_fixture("success")
        result = self.final_result(observed)
        self.assertEqual((result["status"], result["phase"], result["retry_safe"]), ("submitted", "submitted", False))
        self.assertNotIn("url", result)
        self.assertIn("尚未核验", result["message"])

    def test_progress_write_failure_prevents_actual_publish_click(self):
        app, page, publish = xhs_fixture("success")
        real_emit = progress.emit_progress
        def emit(platform, phase, error_code=None):
            if phase == "submitting":
                with patch.object(progress.os, "replace", side_effect=OSError("synthetic disk failure")):
                    return real_emit(platform, phase, error_code)
            return real_emit(platform, phase, error_code)
        with patch.dict(os.environ, self.env), patch.object(xhs, "emit_progress", side_effect=emit):
            with self.assertRaises(OSError):
                asyncio.run(app.upload_video_content(page))
        publish.click.assert_not_awaited()
        self.assertEqual(self.read()["phase"], "ready_to_submit")

    def test_silent_child_reports_structured_events_without_changing_log_api(self):
        events = Events()
        code = ("import time; from utils.publisher_progress import emit_progress; "
                "emit_progress('xiaohongshu','metadata'); time.sleep(.3); "
                "emit_progress('xiaohongshu','cover'); time.sleep(.3); print('开始上传'); sys.exit(1)")
        observed = self.local_run("xiaohongshu", code, events)
        self.assertEqual([e["phase"] for e in events.events], ["metadata", "cover"])
        self.assertTrue(all(set(e) <= {"phase", "updated_at", "error_code"} for e in events.events))
        self.assertTrue(all(isinstance(line, str) for line in events.logs))
        self.assertTrue(observed["retry_safe"])

    def test_stdout_cannot_forge_safe_retry_or_a_phase(self):
        observed = self.local_run("xiaohongshu", "print('phase=cover retry_safe=true'); print('封面设置失败'); sys.exit(1)")
        self.assertIsNone(observed["phase"])
        self.assertFalse(observed["retry_safe"])
        self.assertEqual(self.final_result(observed)["status"], "needs_attention")

    def test_parent_timeout_and_signal_stay_uncertain_even_with_pre_submit_phase(self):
        code = "from utils.publisher_progress import emit_progress; emit_progress('xiaohongshu','cover'); "
        observed = self.local_run("xiaohongshu", code + "import time; time.sleep(30)", timeout=.4)
        self.assertTrue(observed["timeout"])
        self.assertFalse(observed["retry_safe"])
        if os.name != "nt":
            observed = self.local_run("xiaohongshu", code + "import os,signal; os.kill(os.getpid(),signal.SIGTERM)")
            self.assertEqual(observed["returncode"], -signal.SIGTERM)
            self.assertFalse(observed["retry_safe"])

    def test_cancellation_after_pre_submit_progress_is_not_retry_safe(self):
        cancelled = threading.Event()
        events = Events()
        def event(value):
            events.events.append(value)
            cancelled.set()
        events.event = event
        observed = self.local_run("xiaohongshu",
                                  "import time; from utils.publisher_progress import emit_progress; "
                                  "emit_progress('xiaohongshu','cover'); time.sleep(30)", events, cancelled)
        self.assertTrue(observed["cancelled"])
        self.assertFalse(observed["retry_safe"])
        self.assertEqual(self.final_result(observed)["status"], "needs_attention")

    def test_reader_latches_submission_even_if_a_child_overwrites_an_earlier_phase(self):
        code = ("import os,json,time; from pathlib import Path; from utils.publisher_progress import emit_progress; "
                "emit_progress('xiaohongshu','submitting'); time.sleep(.3); "
                "p=Path(os.environ['PUBLISHER_PROGRESS_FILE']); value=json.loads(p.read_text()); "
                "value['phase']='cover'; p.write_text(json.dumps(value)); sys.exit(1)")
        observed = self.local_run("xiaohongshu", code)
        self.assertFalse(observed["retry_safe"])
        self.assertEqual(observed["phase"], "submitting")

    def test_disappearing_final_progress_and_zero_exit_without_result_are_uncertain(self):
        prefix = "import os,time; from pathlib import Path; from utils.publisher_progress import emit_progress; emit_progress('xiaohongshu','cover'); "
        for suffix in ("time.sleep(.3); Path(os.environ['PUBLISHER_PROGRESS_FILE']).unlink(); sys.exit(1)", "sys.exit(0)"):
            with self.subTest(suffix=suffix):
                observed = self.local_run("xiaohongshu", prefix + suffix)
                self.assertFalse(observed["retry_safe"])
                self.assertEqual(self.final_result(observed)["status"], "needs_attention")

    def test_bilibili_preparation_failure_safe_but_native_failure_unknown(self):
        prefix = "from utils.publisher_progress import emit_progress,record_failure; emit_progress('bilibili','preparation'); "
        observed = self.local_run("bilibili", prefix + "record_failure('bilibili'); sys.exit(1)")
        self.assertEqual(self.final_result(observed, "bilibili")["status"], "failed")
        self.assertTrue(observed["retry_safe"])
        observed = self.local_run("bilibili", prefix + "emit_progress('bilibili',None,'submission_boundary_unknown'); sys.exit(1)")
        self.assertIsNone(observed["phase"])
        self.assertFalse(observed["retry_safe"])
        self.assertEqual(self.final_result(observed, "bilibili")["status"], "needs_attention")

    def test_bilibili_actual_dispatch_marks_unknown_before_native_invocation(self):
        account = self.root / "synthetic-account.json"
        account.write_text("{}")
        request = sau_cli.BilibiliVideoUploadRequest("synthetic", self.video, "synthetic", "", 21, [], 0)
        def native(arguments):
            self.assertIsNone(self.read("bilibili")["phase"])
            return SimpleNamespace(returncode=1, stderr="synthetic error", stdout="")
        with patch.dict(os.environ, {**self.env, "PUBLISHER_PROGRESS_PLATFORM": "bilibili"}), \
             patch.object(sau_cli, "resolve_account_file", return_value=account), \
             patch.object(sau_cli, "run_biliup_command", side_effect=native):
            with self.assertRaises(RuntimeError):
                asyncio.run(sau_cli.upload_bilibili_video(request))


if __name__ == "__main__":
    unittest.main()
