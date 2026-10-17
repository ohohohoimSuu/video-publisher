"""L1 state/DOM-shaped fixtures, without a browser, network, or legacy uploader."""
from dataclasses import replace
import asyncio
import os
from unittest.mock import AsyncMock
from pathlib import Path
import tempfile
import unittest

from uploader.bilibili_uploader.prepare import BilibiliFixtureAdapter
from uploader.tencent_uploader.prepare import WeChatChannelsFixtureAdapter
from utils.experimental_prepare import FIXTURE_CSP, FixturePrepareDriver, PrepareFailure, PrepareMaterials


class Element:
    def __init__(self, page, name):
        self.page, self.name = page, name
        self.visible = True
        self.value = ""
        self.attributes = {}

    def locator(self, selector): return self.page.locator(selector)
    def filter(self, **kwargs): return self
    async def count(self): return self.page.counts.get(self.name, 1)
    async def get_attribute(self, name): return self.attributes.get(name)
    async def input_value(self): return self.value
    async def is_enabled(self): return self.page.final_enabled if self.name == "publish" else True

    async def fill(self, value):
        self.page.actions.append(("fill", self.name, value))
        self.value = value[:2] if self.page.truncate and self.name == "title" else value

    async def set_input_files(self, path):
        self.page.actions.append(("file", self.name, Path(path).name))
        if self.name == "video-file":
            self.page.transfer_polls = 0
            self.page.nodes["transfer"].attributes.update({"data-state": "uploading", "data-file-name": Path(path).name,
                                                          "data-file-size": str(Path(path).stat().st_size)})
        elif self.name == "cover-file":
            self.page.nodes["editor"].attributes["data-cover-saved"] = "false"
            if not self.page.freeze_cover: self.page.signature = "changed-cover"
        else: raise AssertionError("Unexpected file target")

    async def click(self):
        self.page.actions.append(("click", self.name))
        if self.name == "save-cover":
            if not self.page.ignore_save: self.page.nodes["editor"].attributes["data-cover-saved"] = "true"
        else: raise AssertionError("Final submission must never be clicked")

    async def evaluate(self, script, *args):
        if self.name == "editor":
            assert "data-prepare-owner" in script
            self.attributes["data-prepare-owner"] = args[0]
            return None
        assert self.name == "cover-preview" and "canvas.toDataURL" in script
        self.page.signature_polls += 1
        return self.page.signature if self.page.preview_readable else None


class Transfer(Element):
    async def get_attribute(self, name):
        if name == "data-state":
            self.page.transfer_polls += 1
            if not self.page.stall_video and self.page.transfer_polls > 2:
                self.attributes["data-state"] = "complete"
        return await super().get_attribute(name)


class FixturePage:
    def __init__(self, platform="tencent"):
        self.url = "about:blank"
        self.closed = False
        self.actions, self.counts = [], {}
        self.stall_video = self.freeze_cover = self.ignore_save = self.truncate = False
        self.final_enabled = self.preview_readable = True
        self.signature = "old-cover"
        self.signature_polls = self.transfer_polls = 0
        self.nodes = {name: Element(self, name) for name in ("marker", "csp", "editor", "video-file", "cover-file", "cover-preview",
                                                            "save-cover", "publish", "title", "description", "tags")}
        self.nodes["transfer"] = Transfer(self, "transfer")
        self.nodes["marker"].attributes["content"] = f"{platform}-v1"
        self.nodes["csp"].attributes["content"] = FIXTURE_CSP
        self.nodes["editor"].attributes.update({"data-account-id": "42", "data-cover-saved": "false"})

    def is_closed(self): return self.closed
    def locator(self, selector):
        name = { 'meta[name="publisher-fixture"]': "marker", 'meta[http-equiv="Content-Security-Policy"]': "csp" }.get(selector)
        if name is None:
            assert selector.startswith('[data-publisher="') and selector.endswith('"]')
            name = selector[len('[data-publisher="'):-2]
        return self.nodes[name]

    async def goto(self, *args, **kwargs): raise AssertionError("Must not navigate")
    async def close(self): raise AssertionError("Must retain the editor")


class ExperimentalPrepareTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.video, self.cover = root / "synthetic.mp4", root / "cover.png"
        self.video.write_bytes(b"synthetic video bytes, never transmitted")
        self.cover.write_bytes(b"synthetic image bytes; decoded only by the opt-in browser test")
        self.materials = PrepareMaterials(self.video, "原始 标题 ✨", "原始\n正文", ("标签一", "tag 2"), self.cover)

    def driver(self, platform="tencent"):
        return FixturePrepareDriver(platform, timeout_seconds=.04, poll_seconds=.001)

    async def test_pipeline_preserves_materials_waits_for_transfer_and_never_publishes(self):
        for platform in ("bilibili", "tencent"):
            with self.subTest(platform=platform):
                page, driver = FixturePage(platform), self.driver(platform)
                result = await driver.prepare_fixture(page, self.materials, expected_account="42")
                self.assertEqual((result.status, result.phase, result.submitted, result.production_enabled),
                                 ("fixture_prepared", "fixture_ready_to_submit", False, False))
                self.assertEqual(result.cover_status, "verified_fixture")
                self.assertEqual(page.nodes["description"].value, self.materials.description)
                self.assertEqual(page.nodes["tags"].value, "标签一\ntag 2")
                self.assertGreaterEqual(page.transfer_polls, 3)
                self.assertGreaterEqual(page.signature_polls, 4)
                self.assertEqual([a for a in page.actions if a[0] == "click"], [("click", "save-cover")])
                self.assertFalse(page.closed)

    async def test_production_entry_is_unconditionally_disabled(self):
        for driver in (BilibiliFixtureAdapter(), WeChatChannelsFixtureAdapter()):
            with self.subTest(platform=driver.platform), self.assertRaises(PrepareFailure) as raised:
                await driver.prepare()
            self.assertEqual(raised.exception.reason, "production_disabled")

    async def test_live_urls_missing_markers_and_foreign_accounts_fail_before_files(self):
        for change in (lambda p: setattr(p, "url", "https://channels.weixin.qq.com/platform/post/create"),
                       lambda p: setattr(p, "url", "https://member.bilibili.com/platform/upload/video/frame"),
                       lambda p: p.counts.update(marker=0), lambda p: p.counts.update(editor=2),
                       lambda p: p.nodes["editor"].attributes.update({"data-account-id": "43"}),
                       lambda p: p.nodes["csp"].attributes.update(content="default-src *")):
            page = FixturePage(); change(page)
            with self.assertRaises(PrepareFailure): await self.driver().prepare_fixture(page, self.materials, expected_account="42")
            self.assertEqual(page.actions, [])

    async def test_enabled_final_button_does_not_prove_upload_complete(self):
        page = FixturePage(); page.stall_video = True
        with self.assertRaises(PrepareFailure) as raised:
            await self.driver().prepare_fixture(page, self.materials, expected_account="42")
        self.assertEqual((raised.exception.stage, raised.exception.reason, raised.exception.retry_safe), ("uploading", "video_not_ready", False))
        self.assertFalse(any(a[1] == "cover-file" or a[0] == "click" for a in page.actions))

    async def test_unchanged_and_unreadable_cover_never_saves(self):
        for change in (lambda p: setattr(p, "freeze_cover", True), lambda p: setattr(p, "preview_readable", False)):
            page = FixturePage(); change(page)
            with self.assertRaises(PrepareFailure) as raised:
                await self.driver().prepare_fixture(page, self.materials, expected_account="42")
            self.assertEqual(raised.exception.stage, "cover")
            self.assertFalse(any(a[0] == "click" for a in page.actions))

    async def test_cover_confirmation_must_be_observed(self):
        page = FixturePage(); page.ignore_save = True
        with self.assertRaises(PrepareFailure) as raised:
            await self.driver().prepare_fixture(page, self.materials, expected_account="42")
        self.assertEqual(raised.exception.reason, "cover_save_unconfirmed")

    async def test_title_truncation_is_failure_not_silent_success(self):
        page = FixturePage(); page.truncate = True
        with self.assertRaises(PrepareFailure) as raised:
            await self.driver().prepare_fixture(page, self.materials, expected_account="42")
        self.assertEqual(raised.exception.reason, "metadata_not_preserved")
        self.assertFalse(any(a[1] == "cover-file" for a in page.actions))

    async def test_resume_cover_keeps_video_and_exact_metadata(self):
        page, driver = FixturePage(), self.driver(); page.freeze_cover = True
        with self.assertRaises(PrepareFailure): await driver.prepare_fixture(page, self.materials, expected_account="42")
        old_actions = list(page.actions); page.freeze_cover = False
        result = await self.driver().resume_cover_fixture(page, self.materials, driver.checkpoint, expected_account="42")
        self.assertEqual(result.status, "fixture_prepared")
        self.assertEqual(page.actions[len(old_actions):], [("file", "cover-file", "cover.png"), ("click", "save-cover")])
        self.assertEqual(sum(a[:2] == ("file", "video-file") for a in page.actions), 1)

    async def test_resume_mismatch_closed_editor_or_changed_assets_never_reuploads(self):
        for mode in ("closed", "token", "title", "file", "platform"):
            with self.subTest(mode=mode):
                page, driver = FixturePage(), self.driver(); page.freeze_cover = True
                with self.assertRaises(PrepareFailure): await driver.prepare_fixture(page, self.materials, expected_account="42")
                checkpoint = driver.checkpoint
                if mode == "closed": page.closed = True
                if mode == "token": page.nodes["editor"].attributes["data-prepare-owner"] = "other"
                if mode == "title": page.nodes["title"].value = "other"
                if mode == "file": self.cover.write_bytes(b"changed synthetic asset")
                if mode == "platform": checkpoint = replace(checkpoint, platform="bilibili")
                old_actions = list(page.actions)
                with self.assertRaises(PrepareFailure): await self.driver().resume_cover_fixture(page, self.materials, checkpoint, expected_account="42")
                self.assertEqual(page.actions, old_actions)

    async def test_second_prepare_on_owned_editor_does_not_repeat_video(self):
        page, driver = FixturePage(), self.driver()
        await driver.prepare_fixture(page, self.materials, expected_account="42")
        old_actions = list(page.actions)
        with self.assertRaises(PrepareFailure) as raised: await driver.prepare_fixture(page, self.materials, expected_account="42")
        self.assertEqual(raised.exception.reason, "editor_already_owned")
        self.assertEqual(page.actions, old_actions)

    async def test_browser_exception_is_sanitized_and_retains_checkpoint(self):
        page, driver = FixturePage(), self.driver()
        page.nodes["video-file"].set_input_files = AsyncMock(side_effect=RuntimeError("private-cookie-value"))
        with self.assertRaises(PrepareFailure) as raised:
            await driver.prepare_fixture(page, self.materials, expected_account="42")
        self.assertEqual(raised.exception.reason, "browser_operation_failed")
        self.assertNotIn("private-cookie-value", str(raised.exception))
        self.assertFalse(raised.exception.retry_safe)
        self.assertIsNotNone(driver.checkpoint)
        self.assertFalse(page.closed)

    async def test_cancellation_retains_editor_and_never_claims_completion(self):
        page, driver = FixturePage(), self.driver()
        page.nodes["video-file"].set_input_files = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await driver.prepare_fixture(page, self.materials, expected_account="42")
        self.assertIsNotNone(driver.checkpoint)
        self.assertTrue(driver.upload_started)
        self.assertFalse(page.closed)
        self.assertFalse(any(a[0] == "click" for a in page.actions))

    async def test_same_size_timestamp_preserving_asset_change_is_detected_on_resume(self):
        page, driver = FixturePage(), self.driver(); page.freeze_cover = True
        with self.assertRaises(PrepareFailure): await driver.prepare_fixture(page, self.materials, expected_account="42")
        stat = self.cover.stat()
        self.cover.write_bytes(b"x" * stat.st_size)
        os.utime(self.cover, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        actions = list(page.actions)
        with self.assertRaises(PrepareFailure) as raised:
            await self.driver().resume_cover_fixture(page, self.materials, driver.checkpoint, expected_account="42")
        self.assertEqual(raised.exception.reason, "editor_mismatch")
        self.assertEqual(page.actions, actions)

    async def test_no_cover_requested_is_explicit(self):
        page = FixturePage()
        result = await self.driver().prepare_fixture(page, replace(self.materials, cover=None), expected_account="42")
        self.assertEqual(result.cover_status, "not_requested")
        self.assertFalse(any(a[1] == "cover-file" or a[0] == "click" for a in page.actions))

    async def test_missing_assets_and_invalid_metadata_fail_without_interactions(self):
        for materials in (replace(self.materials, title=""), replace(self.materials, tags=("a\nb",)),
                          replace(self.materials, video=self.video.with_name("missing.mp4")), replace(self.materials, title="x" * 10001)):
            page = FixturePage()
            with self.assertRaises(PrepareFailure): await self.driver().prepare_fixture(page, materials, expected_account="42")
            self.assertEqual(page.actions, [])


if __name__ == "__main__": unittest.main()
