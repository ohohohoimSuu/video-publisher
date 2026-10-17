"""Prepare-only state machine for controlled, local editor fixtures.

This is deliberately NOT a production browser executor. It accepts only an
about:blank document carrying our fixture contract and never navigates, launches
browsers, imports a session, emits the publisher result protocol, or submits.
The selectors describe the checked-in fixture, not a verified live platform UI.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import time
import uuid


FIXTURE_CSP = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:"


class PrepareFailure(RuntimeError):
    """Fixed, non-sensitive diagnostic. The existing editor is never closed."""

    def __init__(self, stage, reason, next_action, *, upload_started=False):
        super().__init__(f"{stage}: {reason}. {next_action}")
        self.stage = stage
        self.reason = reason
        self.next_action = next_action
        self.upload_started = upload_started
        self.retry_safe = not upload_started


@dataclass(frozen=True)
class PrepareMaterials:
    video: Path
    title: str
    description: str
    tags: tuple[str, ...]
    cover: Path | None = None

    def validate(self):
        if not isinstance(self.title, str) or not self.title.strip():
            raise PrepareFailure("validation", "title_required", "Supply the original title.")
        if not isinstance(self.description, str) or not isinstance(self.tags, tuple) or any(
            not isinstance(tag, str) or not tag or "\n" in tag or "\r" in tag for tag in self.tags
        ):
            raise PrepareFailure("validation", "invalid_metadata", "Check the original description and tags.")
        # Limits below bound fixture input only; they are NOT live platform limits.
        if len(self.title) > 10000 or len(self.description) > 100000 or len(self.tags) > 100 or any(len(t) > 1000 for t in self.tags):
            raise PrepareFailure("validation", "fixture_limit_exceeded", "Reduce fixture input; no text was truncated.")
        for path in (self.video, self.cover):
            if path is not None and (not isinstance(path, Path) or not path.is_file() or path.stat().st_size == 0):
                raise PrepareFailure("validation", "asset_missing", "Restore the selected local asset.")

    def fingerprint(self):
        assets = []
        for path in (self.video, self.cover):
            if path is None:
                assets.append(None)
                continue
            stat = path.stat()
            # Compare content as well as metadata: a same-size replacement with
            # a restored timestamp must not silently change a cover continuation.
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            assets.append([str(path.resolve()), stat.st_size, stat.st_mtime_ns, digest.hexdigest()])
        value = [assets, self.title, self.description, self.tags]
        return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class FixtureCheckpoint:
    platform: str
    editor_token: str
    materials_fingerprint: str


@dataclass(frozen=True)
class FixtureResult:
    platform: str
    checkpoint: FixtureCheckpoint
    cover_status: str
    status: str = "fixture_prepared"
    phase: str = "fixture_ready_to_submit"
    production_enabled: bool = False
    submitted: bool = False


class FixturePrepareDriver:
    """Fill a provided fixture page and leave it open for human inspection.

    Failure preserves the page and checkpoint. Cover-only continuation requires
    the same document token, file identity and exact text, and never reuploads.
    A fixture result cannot be mistaken for the production ``prepared`` protocol.
    """

    def __init__(self, platform, *, timeout_seconds=5.0, poll_seconds=0.02):
        if platform not in {"bilibili", "tencent"}:
            raise ValueError("Unsupported experimental platform")
        if timeout_seconds <= 0 or poll_seconds <= 0:
            raise ValueError("Positive fixture timeouts are required")
        self.platform = platform
        self.timeout_seconds = timeout_seconds
        self.poll_seconds = poll_seconds
        self.stage = "validation"
        self.upload_started = False
        self.checkpoint = None

    def fail(self, reason, next_action):
        raise PrepareFailure(self.stage, reason, next_action, upload_started=self.upload_started) from None

    async def _guard(self, page):
        if page.is_closed():
            self.fail("editor_closed", "Inspect the existing draft; do not automatically reupload.")
        if page.url != "about:blank":
            self.fail("production_disabled", "Use only the checked-in controlled fixture. Live access is disabled.")
        markers = page.locator('meta[name="publisher-fixture"]')
        csp = page.locator('meta[http-equiv="Content-Security-Policy"]')
        if (await markers.count() != 1 or await markers.get_attribute("content") != f"{self.platform}-v1"
                or await csp.count() != 1 or await csp.get_attribute("content") != FIXTURE_CSP):
            self.fail("fixture_contract_missing", "Load the controlled fixture; no production page is supported.")
        return await self._one(page, "editor")

    async def _one(self, scope, name, *, visible=True):
        target = scope.locator(f'[data-publisher="{name}"]')
        if visible:
            target = target.filter(visible=True)
        if await target.count() != 1:
            self.fail("control_missing_or_ambiguous", "Inspect the retained editor; selectors were not broadened.")
        return target

    async def _assert_identity(self, page, expected_account):
        editor = await self._guard(page)
        account = await editor.get_attribute("data-account-id")
        if not expected_account or account != expected_account:
            self.fail("account_mismatch", "Verify the intended account before selecting any material.")
        return editor

    async def _wait(self, page, probe, reason):
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            await self._guard(page)
            if await probe():
                return
            await asyncio.sleep(self.poll_seconds)
        self.fail(reason, "Keep the editor and materials. Inspect the unfinished step before retrying.")

    async def _video_ready(self, editor, materials):
        status = await self._one(editor, "transfer")
        return (await status.get_attribute("data-state") == "complete"
                and await status.get_attribute("data-file-name") == materials.video.name
                and await status.get_attribute("data-file-size") == str(materials.video.stat().st_size))

    async def _metadata_matches(self, editor, materials):
        expected = {"title": materials.title, "description": materials.description, "tags": "\n".join(materials.tags)}
        for name, value in expected.items():
            if await (await self._one(editor, name)).input_value() != value:
                return False
        return True

    async def _cover_signature(self, editor):
        preview = await self._one(editor, "cover-preview")
        return await preview.evaluate("""node => {
            if (node.tagName !== 'IMG' || !node.complete || node.naturalWidth < 1 || node.naturalHeight < 1) return null;
            try {
                const canvas = document.createElement('canvas');
                canvas.width = node.naturalWidth; canvas.height = node.naturalHeight;
                canvas.getContext('2d').drawImage(node, 0, 0);
                return canvas.toDataURL();
            } catch (_) { return null; }
        }""")

    async def _cover(self, page, editor, materials):
        if materials.cover is None:
            return "not_requested"
        self.stage = "cover"
        before = await self._cover_signature(editor)
        if not before:
            self.fail("cover_unverifiable", "Inspect the current preview; no cover was saved.")
        await (await self._one(editor, "cover-file", visible=False)).set_input_files(str(materials.cover))
        previous, stable = None, 0

        async def changed_and_stable():
            nonlocal previous, stable
            signature = await self._cover_signature(editor)
            if not signature or signature == before:
                previous, stable = None, 0
                return False
            stable = stable + 1 if signature == previous else 1
            previous = signature
            return stable >= 3

        await self._wait(page, changed_and_stable, "cover_preview_unchanged_or_unready")
        # This is the fixture's scoped cover confirmation, never final publish.
        confirm = await self._one(editor, "save-cover")
        if not await confirm.is_enabled():
            self.fail("cover_save_disabled", "Wait for the selected cover to finish processing.")
        await confirm.click()
        if await editor.get_attribute("data-cover-saved") != "true":
            self.fail("cover_save_unconfirmed", "Inspect the retained cover; no prepared result was recorded.")
        return "verified_fixture"

    async def _finish(self, page, editor, materials, expected_account, cover_status):
        self.stage = "handoff"
        await self._assert_identity(page, expected_account)
        if (self.checkpoint.materials_fingerprint != materials.fingerprint()
                or not await self._video_ready(editor, materials) or not await self._metadata_matches(editor, materials)):
            self.fail("editor_changed", "Inspect the retained editor; the original material no longer matches.")
        final = await self._one(editor, "publish")
        if not await final.is_enabled():
            self.fail("editor_not_ready", "Complete the remaining required fields in the retained editor.")
        # Merely observing an enabled button is not an upload-completion signal.
        # Transfer, exact metadata and requested cover were all checked separately.
        return FixtureResult(self.platform, self.checkpoint, cover_status)

    async def _safe_operation(self, operation):
        try:
            return await operation
        except PrepareFailure as error:
            # Validation during continuation must not reopen a fresh-upload retry.
            if self.upload_started:
                error.upload_started = True
                error.retry_safe = False
            raise
        except asyncio.CancelledError:
            # Leave ownership and checkpoint intact for the caller's cancellation
            # handling; do not close the page or claim a successful handoff.
            raise
        except Exception:
            self.fail("browser_operation_failed", "Inspect the retained editor. Raw browser diagnostics were not exposed.")

    async def prepare_fixture(self, page, materials, *, expected_account):
        return await self._safe_operation(self._prepare_fixture(page, materials, expected_account=expected_account))

    async def _prepare_fixture(self, page, materials, *, expected_account):
        self.stage, self.upload_started, self.checkpoint = "validation", False, None
        materials.validate()
        editor = await self._assert_identity(page, expected_account)
        token = await editor.get_attribute("data-prepare-owner")
        if token:
            self.upload_started = True
            self.fail("editor_already_owned", "Continue the existing editor; do not upload the video again.")
        token = uuid.uuid4().hex
        await editor.evaluate("(node, token) => node.setAttribute('data-prepare-owner', token)", token)
        self.checkpoint = FixtureCheckpoint(self.platform, token, materials.fingerprint())
        self.stage = "uploading"
        self.upload_started = True
        await (await self._one(editor, "video-file", visible=False)).set_input_files(str(materials.video))
        self.stage = "metadata"
        for name, value in {"title": materials.title, "description": materials.description, "tags": "\n".join(materials.tags)}.items():
            await (await self._one(editor, name)).fill(value)
        if not await self._metadata_matches(editor, materials):
            self.fail("metadata_not_preserved", "Restore the original text; no silent truncation is accepted.")
        self.stage = "uploading"
        await self._wait(page, lambda: self._video_ready(editor, materials), "video_not_ready")
        cover_status = await self._cover(page, editor, materials)
        return await self._finish(page, editor, materials, expected_account, cover_status)

    async def resume_cover_fixture(self, page, materials, checkpoint, *, expected_account):
        return await self._safe_operation(self._resume_cover_fixture(page, materials, checkpoint, expected_account=expected_account))

    async def _resume_cover_fixture(self, page, materials, checkpoint, *, expected_account):
        self.stage, self.upload_started = "resume", True
        materials.validate()
        editor = await self._assert_identity(page, expected_account)
        if (not isinstance(checkpoint, FixtureCheckpoint) or checkpoint.platform != self.platform
                or checkpoint.materials_fingerprint != materials.fingerprint()
                or await editor.get_attribute("data-prepare-owner") != checkpoint.editor_token
                or not await self._metadata_matches(editor, materials)
                or not await self._video_ready(editor, materials)):
            self.fail("editor_mismatch", "Find the original editor; no new page or upload was started.")
        self.checkpoint = checkpoint
        cover_status = await self._cover(page, editor, materials)
        return await self._finish(page, editor, materials, expected_account, cover_status)

    async def prepare(self, *args, **kwargs):
        self.stage = "validation"
        self.fail("production_disabled", "Real-account acceptance is required before enabling this executor.")
