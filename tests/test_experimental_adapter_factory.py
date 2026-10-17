"""Production-shaped adapter boundary, exercised with synthetic retained ports."""
from dataclasses import replace
import asyncio
from pathlib import Path
import tempfile
import unittest

from prepare_adapters import create_experimental_prepare_adapter
from utils.experimental_prepare import PrepareFailure, PrepareMaterials
from utils.manual_prepare_contract import (BilibiliSettings, CoverEvidence, EditorBinding, ManualPrepareRequest,
                                           OwnedEditorLease, TencentSettings, VideoEvidence, file_sha256)


class FixturePort:
    fixture_only = True
    capabilities = frozenset({"exact_metadata", "transfer_complete", "processing_complete", "retained_editor", "account_identity",
                              "category", "copyright", "source", "short_title", "declaration", "verified_cover", "collection"})
    def __init__(self, binding):
        self.binding, self.calls = binding, []
        self.metadata = None
        self.settings = None
        self.video = None
        self.account = binding.account_id
        self.truncate = self.processing = self.cover_bad = self.settings_bad = self.raise_private = False
        self.ready = True
    async def assert_controlled_fixture(self): self.calls.append("guard")
    async def verify_account(self): self.calls.append("account"); return self.account
    async def select_video(self, path):
        self.calls.append("video"); self.video = path
        if self.raise_private: raise RuntimeError("private-cookie-value")
    async def fill_metadata(self, materials):
        self.calls.append("metadata")
        self.metadata = (materials.title[:2] if self.truncate else materials.title, materials.description, materials.tags)
    async def read_metadata(self): self.calls.append("read_metadata"); return self.metadata
    async def wait_video_ready(self):
        self.calls.append("video_ready")
        return VideoEvidence(self.video.name, self.video.stat().st_size, True, not self.processing)
    async def apply_settings(self, settings): self.calls.append("settings"); self.settings = settings
    async def read_settings(self): self.calls.append("read_settings"); return None if self.settings_bad else self.settings
    async def set_cover(self, slot, path):
        self.calls.append(("cover", slot))
        return CoverEvidence(slot, file_sha256(path), True, not self.cover_bad, True)
    async def editor_ready(self): self.calls.append("editor_ready"); return self.ready
    async def close(self): raise AssertionError("Retained editor must not be closed")
    async def submit(self): raise AssertionError("Final submit must not be called")


class FixtureFactory:
    fixture_only = True
    def __init__(self, port): self.port, self.calls, self.used = port, [], False
    async def acquire(self, binding, *, require_existing):
        self.calls.append((binding, require_existing))
        lease = OwnedEditorLease(self.port.binding, self.port.binding.profile_key, "synthetic-editor", self.port, True, True, not self.used)
        self.used = True
        return lease


class ExperimentalAdapterFactoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.video, self.cover = root / "synthetic.mp4", root / "synthetic.png"
        self.video.write_bytes(b"no real upload"); self.cover.write_bytes(b"synthetic cover")

    def request(self, platform):
        binding = EditorBinding(platform, "42", "a" * 64)
        settings = (BilibiliSettings(249, "repost", "https://example.invalid/original") if platform == "bilibili"
                    else TencentSettings("这是原始短标题", "ai_generated", self.cover, self.cover, "原始合集"))
        materials = PrepareMaterials(self.video, "完整原始标题", "原始\n正文", ("原始话题",), self.cover if platform == "bilibili" else None)
        return ManualPrepareRequest(binding, materials, settings)

    async def test_factory_production_gate_never_acquires_editor(self):
        for platform in ("bilibili", "tencent"):
            adapter = create_experimental_prepare_adapter(platform)
            request = self.request(platform); factory = FixtureFactory(FixturePort(request.binding))
            with self.subTest(platform=platform), self.assertRaises(PrepareFailure) as raised:
                await adapter.prepare(request, factory)
            self.assertEqual(raised.exception.reason, "production_disabled")
            self.assertEqual(factory.calls, [])
            self.assertFalse(adapter.capability_report()["prepare_supported"])
            self.assertTrue(adapter.capability_report()["missing_capabilities"])
            self.assertFalse(adapter.capability_report()["final_submit_supported"])

    async def test_both_platform_requests_follow_prepare_stages_and_retain_owned_lease(self):
        for platform in ("bilibili", "tencent"):
            request = self.request(platform); port = FixturePort(request.binding); factory = FixtureFactory(port)
            adapter = create_experimental_prepare_adapter(platform)
            result = await adapter.prepare_fixture(request, factory)
            self.assertEqual(result.status, "fixture_prepared")
            self.assertFalse(result.submitted)
            self.assertEqual(result.profile_key, request.binding.profile_key)
            self.assertEqual(adapter.lease.editor, port)
            self.assertLess(port.calls.index("account"), port.calls.index("video"))
            self.assertLess(port.calls.index("video_ready"), port.calls.index("settings"))
            self.assertEqual(port.settings, request.settings)
            self.assertEqual(result.cover_slots, ("main",) if platform == "bilibili" else ("landscape", "portrait"))
            self.assertEqual(factory.calls, [(request.binding, False)])

    async def test_factory_rejects_nonfixture_or_wrong_snapshot_before_upload(self):
        request = self.request("bilibili")
        for mode in ("live_factory", "wrong_snapshot", "missing_capability", "wrong_account"):
            port = FixturePort(request.binding); factory = FixtureFactory(port)
            if mode == "live_factory": factory.fixture_only = False
            if mode == "wrong_snapshot": port.binding = replace(request.binding, session_sha256="b" * 64)
            if mode == "missing_capability": port.capabilities = port.capabilities - {"copyright"}
            if mode == "wrong_account": port.account = "43"
            with self.subTest(mode=mode), self.assertRaises(PrepareFailure):
                await create_experimental_prepare_adapter("bilibili").prepare_fixture(request, factory)
            self.assertNotIn("video", port.calls)

    async def test_second_acquisition_keeps_old_editor_without_reupload(self):
        request = self.request("tencent"); port = FixturePort(request.binding); factory = FixtureFactory(port)
        await create_experimental_prepare_adapter("tencent").prepare_fixture(request, factory)
        with self.assertRaises(PrepareFailure) as raised:
            await create_experimental_prepare_adapter("tencent").prepare_fixture(request, factory)
        self.assertEqual(raised.exception.reason, "editor_already_in_use")
        self.assertFalse(raised.exception.retry_safe)
        self.assertEqual(port.calls.count("video"), 1)

    async def test_each_stage_requires_exact_evidence(self):
        expected = {"truncate": "metadata_not_preserved", "processing": "video_not_ready", "settings_bad": "settings_not_preserved",
                    "cover_bad": "cover_unverified", "raise_private": "adapter_stage_failed", "ready": "handoff_unverified"}
        for mode, reason in expected.items():
            request = self.request("tencent"); port = FixturePort(request.binding)
            setattr(port, mode, mode != "ready")
            adapter = create_experimental_prepare_adapter("tencent")
            with self.subTest(mode=mode), self.assertRaises(PrepareFailure) as raised:
                await adapter.prepare_fixture(request, FixtureFactory(port))
            self.assertEqual(raised.exception.reason, reason)
            self.assertFalse(raised.exception.retry_safe)
            self.assertIs(adapter.lease.editor, port)
            self.assertNotIn("private-cookie-value", str(raised.exception))

    async def test_invalid_platform_specific_fields_rejected_before_factory(self):
        cases = [replace(self.request("bilibili"), settings=BilibiliSettings(0, "original")),
                 replace(self.request("bilibili"), settings=BilibiliSettings(249, "repost")),
                 replace(self.request("bilibili"), settings=BilibiliSettings(249, "original", "https://example.invalid")),
                 replace(self.request("bilibili"), settings=None),
                 replace(self.request("tencent"), settings=TencentSettings("短", "ai_generated")),
                 replace(self.request("tencent"), settings=TencentSettings("这是原始短标题", None)),
                 replace(self.request("tencent"), materials=replace(self.request("tencent").materials, cover=self.cover)),
                 replace(self.request("tencent"), binding=None)]
        for request in cases:
            platform = "bilibili" if isinstance(request.settings, BilibiliSettings) or request.settings is None else "tencent"
            factory = FixtureFactory(FixturePort(EditorBinding(platform, "42", "a" * 64)))
            with self.subTest(request=request), self.assertRaises(PrepareFailure):
                await create_experimental_prepare_adapter(platform).prepare_fixture(request, factory)
            self.assertEqual(factory.calls, [])

    def test_profile_keys_are_platform_account_and_snapshot_isolated(self):
        binding = EditorBinding("bilibili", "42", "a" * 64)
        keys = {binding.profile_key, replace(binding, account_id="43").profile_key,
                replace(binding, platform="tencent").profile_key, replace(binding, session_sha256="b" * 64).profile_key}
        self.assertEqual(len(keys), 4)

    def test_unknown_platform_has_no_native_fallback(self):
        with self.assertRaises(ValueError): create_experimental_prepare_adapter("unknown")


if __name__ == "__main__": unittest.main()
