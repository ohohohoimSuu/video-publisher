"""Production-shaped prepare adapter boundary; all current production gates shut.

This module owns stage ordering and structured evidence, independently of page
selectors. An editor port has no submit/save-draft/close capability. The same
orchestration is exercised through controlled fixture factories, while live
browser acquisition is rejected before touching any factory or material.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Protocol
from urllib.parse import urlsplit

from utils.experimental_prepare import PrepareFailure, PrepareMaterials


@dataclass(frozen=True)
class EditorBinding:
    platform: str
    account_id: str
    session_sha256: str

    def validate(self):
        if (self.platform not in {"bilibili", "tencent"} or not isinstance(self.account_id, str)
                or not self.account_id or len(self.account_id) > 128
                or not isinstance(self.session_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", self.session_sha256)):
            raise PrepareFailure("account", "invalid_editor_binding", "Verify the selected account and session snapshot.")

    @property
    def profile_key(self):
        self.validate()
        value = [self.platform, self.account_id, self.session_sha256]
        return hashlib.sha256(json.dumps(value).encode()).hexdigest()


@dataclass(frozen=True)
class BilibiliSettings:
    category_id: int
    copyright: str
    source_url: str | None = None

    def validate(self):
        if type(self.category_id) is not int or self.category_id <= 0 or not isinstance(self.copyright, str) or self.copyright not in {"original", "repost"}:
            raise PrepareFailure("validation", "bilibili_settings_required", "Choose the category and explicitly choose original or repost.")
        if self.copyright == "repost":
            if not isinstance(self.source_url, str) or not self.source_url:
                raise PrepareFailure("validation", "repost_source_required", "Supply the explicit source URL for reposted material.")
            try:
                source = urlsplit(self.source_url)
            except (TypeError, ValueError):
                raise PrepareFailure("validation", "repost_source_required", "Supply a valid source URL.") from None
            if source.scheme not in {"http", "https"} or not source.hostname or source.username or source.password:
                raise PrepareFailure("validation", "repost_source_required", "Supply the explicit source URL for reposted material.")
        elif self.source_url is not None:
            raise PrepareFailure("validation", "bilibili_settings_conflict", "Check the source and original/repost choices.")


@dataclass(frozen=True)
class TencentSettings:
    short_title: str
    declaration: str
    landscape_cover: Path | None = None
    portrait_cover: Path | None = None
    collection: str | None = None

    def validate(self):
        # Sourced from existing Tencent short-title validation. Do not strip,
        # truncate, or derive a replacement from the user's longer main title.
        if not isinstance(self.short_title, str) or not 6 <= len(self.short_title) <= 16:
            raise PrepareFailure("validation", "short_title_required", "Supply the exact 6–16 character short title.")
        if not isinstance(self.declaration, str) or self.declaration not in {"none", "ai_generated"}:
            raise PrepareFailure("validation", "declaration_required", "Explicitly choose the content declaration; it is never inferred.")
        for path in (self.landscape_cover, self.portrait_cover):
            if path is not None and (not isinstance(path, Path) or not path.is_file() or not path.stat().st_size):
                raise PrepareFailure("validation", "cover_missing", "Restore the specified cover for that slot.")
        if self.collection is not None and (not isinstance(self.collection, str) or not self.collection):
            raise PrepareFailure("validation", "collection_invalid", "Supply the exact requested collection or leave it unrequested.")


@dataclass(frozen=True)
class ManualPrepareRequest:
    binding: EditorBinding
    materials: PrepareMaterials
    settings: BilibiliSettings | TencentSettings

    def validate(self, platform):
        settings_type = BilibiliSettings if platform == "bilibili" else TencentSettings
        if (not isinstance(self.binding, EditorBinding) or not isinstance(self.materials, PrepareMaterials)
                or not isinstance(self.settings, settings_type) or self.binding.platform != platform):
            raise PrepareFailure("validation", "platform_request_mismatch", "Use the selected platform's explicit request fields.")
        self.binding.validate()
        self.materials.validate()
        self.settings.validate()
        if platform == "tencent" and self.materials.cover is not None:
            raise PrepareFailure("validation", "cover_slot_required", "Specify portrait and/or landscape cover explicitly; no cover slot is guessed.")

    def covers(self):
        if isinstance(self.settings, TencentSettings):
            return tuple((slot, path) for slot, path in (("landscape", self.settings.landscape_cover), ("portrait", self.settings.portrait_cover)) if path is not None)
        return (("main", self.materials.cover),) if self.materials.cover is not None else ()


@dataclass(frozen=True)
class VideoEvidence:
    filename: str
    size: int
    transfer_complete: bool
    processing_complete: bool


@dataclass(frozen=True)
class CoverEvidence:
    slot: str
    input_sha256: str
    preview_changed: bool
    preview_stable: bool
    save_confirmed: bool


@dataclass(frozen=True)
class OwnedEditorLease:
    """Acquisition result from the owned-editor factory, never a generic browser."""
    binding: EditorBinding
    profile_key: str
    editor_id: str
    editor: "PrepareEditorPort"
    fixture_only: bool
    retained: bool
    fresh_editor: bool = False


class PrepareEditorPort(Protocol):
    """Minimum interface. Deliberately no final-submit or browser-close method."""
    fixture_only: bool
    capabilities: frozenset[str]
    async def assert_controlled_fixture(self) -> None: ...
    async def verify_account(self) -> str: ...
    async def select_video(self, path: Path) -> None: ...
    async def fill_metadata(self, materials: PrepareMaterials) -> None: ...
    async def read_metadata(self) -> tuple[str, str, tuple[str, ...]]: ...
    async def wait_video_ready(self) -> VideoEvidence: ...
    async def apply_settings(self, settings: BilibiliSettings | TencentSettings) -> None: ...
    async def read_settings(self) -> BilibiliSettings | TencentSettings: ...
    async def set_cover(self, slot: str, path: Path) -> CoverEvidence: ...
    async def editor_ready(self) -> bool: ...


class OwnedEditorFactory(Protocol):
    fixture_only: bool
    async def acquire(self, binding: EditorBinding, *, require_existing: bool) -> OwnedEditorLease: ...


@dataclass(frozen=True)
class AdapterFixtureResult:
    platform: str
    editor_id: str
    profile_key: str
    cover_slots: tuple[str, ...]
    status: str = "fixture_prepared"
    phase: str = "fixture_ready_to_submit"
    production_enabled: bool = False
    submitted: bool = False


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ManualPrepareAdapter:
    production_enabled = False
    platform = ""
    missing_capabilities = ()
    required_capabilities = frozenset({"exact_metadata", "transfer_complete", "processing_complete", "retained_editor", "account_identity"})

    def __init__(self):
        self.stage = "validation"
        self.upload_started = False
        self.lease = None

    def _fail(self, reason, next_action):
        raise PrepareFailure(self.stage, reason, next_action, upload_started=self.upload_started) from None

    def capability_report(self):
        return {"platform": self.platform, "prepare_supported": False, "implementation_scope": "fixture_only",
                "missing_capabilities": list(self.missing_capabilities), "final_submit_supported": False}

    async def prepare(self, request, factory):
        # No environment flag, login result, request attribute or optional
        # argument can convert a fixture adapter into a production executor.
        self.stage = "validation"
        self._fail("production_disabled", "Complete the listed source integration and real-account acceptance first.")

    async def prepare_fixture(self, request: ManualPrepareRequest, factory: OwnedEditorFactory):
        self.stage, self.upload_started, self.lease = "validation", False, None
        try:
            if not isinstance(request, ManualPrepareRequest):
                self._fail("request_invalid", "Supply the selected platform's typed prepare request.")
            request.validate(self.platform)
            if getattr(factory, "fixture_only", False) is not True:
                self._fail("production_disabled", "Only a controlled fixture factory may exercise this adapter.")
            # Compute expected evidence before selecting a file or changing fields.
            original_materials = request.materials.fingerprint()
            covers = {slot: file_sha256(path) for slot, path in request.covers()}
            self.stage = "account"
            self.lease = await factory.acquire(request.binding, require_existing=False)
            if not isinstance(self.lease, OwnedEditorLease):
                self._fail("owned_editor_missing", "Use a retained editor lease from the selected platform's owned factory.")
            lease, editor = self.lease, self.lease.editor
            if lease.fresh_editor is not True:
                self.upload_started = True
                self._fail("editor_already_in_use", "Keep the existing editor; do not automatically reselect the video.")
            if (lease.binding != request.binding or lease.profile_key != request.binding.profile_key
                    or lease.fixture_only is not True or lease.retained is not True or not lease.editor_id
                    or getattr(editor, "fixture_only", False) is not True):
                self._fail("owned_editor_mismatch", "Retain the old editor; do not import a new session or overwrite its profile.")
            await editor.assert_controlled_fixture()
            required = set(self.required_capabilities)
            if covers: required.add("verified_cover")
            if self.platform == "bilibili": required.update({"category", "copyright", "source"})
            else:
                required.update({"short_title", "declaration"})
                if request.settings.collection is not None: required.add("collection")
            if not required <= editor.capabilities:
                self._fail("editor_capability_missing", "Implement and verify every requested field before uploading.")
            if await editor.verify_account() != request.binding.account_id:
                self._fail("account_mismatch", "Verify the intended platform account before selecting materials.")
            self.stage, self.upload_started = "uploading", True
            await editor.select_video(request.materials.video)
            self.stage = "metadata"
            await editor.fill_metadata(request.materials)
            await self._check_metadata(editor, request.materials)
            self.stage = "uploading"
            await self._check_video(editor, request.materials)
            self.stage = "settings"
            await editor.apply_settings(request.settings)
            if await editor.read_settings() != request.settings:
                self._fail("settings_not_preserved", "Check the explicit platform settings in the retained editor.")
            self.stage = "cover"
            for slot, path in request.covers():
                evidence = await editor.set_cover(slot, path)
                if (not isinstance(evidence, CoverEvidence) or evidence.slot != slot or evidence.input_sha256 != covers[slot]
                        or evidence.preview_changed is not True or evidence.preview_stable is not True or evidence.save_confirmed is not True):
                    self._fail("cover_unverified", "Keep the original editor and inspect the requested cover slot.")
            self.stage = "handoff"
            await editor.assert_controlled_fixture()
            await self._check_metadata(editor, request.materials)
            await self._check_video(editor, request.materials)
            if (await editor.verify_account() != request.binding.account_id or await editor.read_settings() != request.settings
                    or await editor.editor_ready() is not True or request.materials.fingerprint() != original_materials
                    or any(file_sha256(path) != covers[slot] for slot, path in request.covers())):
                self._fail("handoff_unverified", "Inspect the retained editor; no completed preparation was recorded.")
            return AdapterFixtureResult(self.platform, lease.editor_id, lease.profile_key, tuple(covers))
        except PrepareFailure as error:
            if self.upload_started:
                error.upload_started, error.retry_safe = True, False
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            self._fail("adapter_stage_failed", "Inspect the retained editor; raw browser or credential diagnostics were withheld.")

    async def _check_metadata(self, editor, materials):
        if await editor.read_metadata() != (materials.title, materials.description, materials.tags):
            self._fail("metadata_not_preserved", "Restore the original text; no text is silently truncated or rewritten.")

    async def _check_video(self, editor, materials):
        evidence = await editor.wait_video_ready()
        if (not isinstance(evidence, VideoEvidence) or evidence.filename != materials.video.name
                or evidence.size != materials.video.stat().st_size or evidence.transfer_complete is not True
                or evidence.processing_complete is not True):
            self._fail("video_not_ready", "Keep the editor and wait for verified transfer and processing completion; do not reupload.")
