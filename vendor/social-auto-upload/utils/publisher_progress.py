"""Private, bounded progress evidence for the local publisher.

Never pass page text, exception messages, credentials or URLs to this channel.
A failed write raises before the next side effect, especially before submit.
Normal standalone CLI usage has no configured channel and remains a no-op.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile


PHASES = ("preparation", "uploading", "processing", "metadata", "cover", "ready_to_submit", "submitting", "submitted")
PUBLISHER_PLATFORMS = frozenset({"douyin", "xiaohongshu", "youtube", "bilibili"})
ERROR_CODES = frozenset({
    "preparation_failed", "upload_failed", "processing_failed", "metadata_failed", "cover_failed",
    "pre_submit_failed", "submission_unconfirmed", "submission_boundary_unknown", "process_failed",
})
_states: dict[tuple[str, str, str], dict] = {}
DIAGNOSTIC_STEPS = frozenset({"preparation", "account", "browser", "metadata", "navigation", "upload", "processing", "title", "description", "topic", "cover", "handoff", "resume"})
RESUME_REASONS = frozenset({"browser_closed", "browser_unverified", "session_changed", "snapshot_changed", "session_missing", "editor_missing", "editor_ambiguous"})
DIAGNOSTIC_ERROR_TYPES = frozenset({
    "TimeoutError", "Error", "RuntimeError", "ValueError", "TypeError", "AttributeError",
    "OSError", "FileNotFoundError", "PermissionError", "OtherError",
})
_diagnostics: dict[tuple[str, str, str], dict] = {}


def _channel(platform: str):
    path = os.environ.get("PUBLISHER_PROGRESS_FILE")
    nonce = os.environ.get("PUBLISHER_RESULT_NONCE")
    if not path or not nonce or os.environ.get("PUBLISHER_PROGRESS_ACTION") != "publish":
        return None
    if platform not in PUBLISHER_PLATFORMS or os.environ.get("PUBLISHER_PROGRESS_PLATFORM") != platform:
        return None
    return Path(path), nonce


def diagnostic_enabled(platform: str) -> bool:
    return platform in PUBLISHER_PLATFORMS and _channel(platform) is not None


def record_diagnostic(platform: str, step: str, error: Exception, *,
                      topic_index: int | None = None, candidate_count: int | None = None) -> None:
    """Retain fixed labels and bounded counts only, never exception text."""
    if step not in DIAGNOSTIC_STEPS:
        raise ValueError("Unsupported publisher diagnostic step")
    if not diagnostic_enabled(platform):
        return
    path, nonce = _channel(platform)
    error_type = type(error).__name__
    value = {"step": step, "error_type": error_type if error_type in DIAGNOSTIC_ERROR_TYPES else "OtherError"}
    if step == 'resume' and getattr(error, 'reason', None) in RESUME_REASONS:
        value['reason'] = error.reason
    if step == "topic":
        if type(topic_index) is int and 1 <= topic_index <= 10:
            value["topic_index"] = topic_index
        if type(candidate_count) is int and 0 <= candidate_count <= 100:
            value["candidate_count"] = candidate_count
    _diagnostics[(str(path), nonce, platform)] = value


def get_diagnostic(platform: str) -> dict | None:
    channel = _channel(platform)
    if channel is None:
        return None
    path, nonce = channel
    value = _diagnostics.get((str(path), nonce, platform))
    return dict(value) if value else None


def _atomic_json(path: Path, value: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".publisher-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_failure_result(platform: str) -> None:
    """Publish a paired diagnostic result without claiming submission success."""
    diagnostic = get_diagnostic(platform)
    result_path = os.environ.get("PUBLISHER_RESULT_FILE")
    if diagnostic is None or not result_path:
        return
    _atomic_json(Path(result_path), {
        "nonce": os.environ["PUBLISHER_RESULT_NONCE"], "platform": platform, "action": "publish",
        "status": "failed", "diagnostic": diagnostic,
    })


def emit_progress(platform: str, phase: str | None, error_code: str | None = None, *, percent=None) -> None:
    """Atomically record a monotonic phase; None is Bilibili's opaque boundary."""
    if phase not in PHASES and phase is not None:
        raise ValueError("Unsupported publisher phase")
    if error_code is not None and error_code not in ERROR_CODES:
        raise ValueError("Unsupported publisher error code")
    if percent is not None and (phase not in ('uploading', 'processing') or type(percent) not in (int, float) or not 0 <= percent <= 100):
        raise ValueError("Invalid observed upload percentage")
    if phase is None and (platform != "bilibili" or error_code != "submission_boundary_unknown"):
        raise ValueError("Unknown boundary is only supported for native Bilibili upload")
    channel = _channel(platform)
    if channel is None:
        return
    path, nonce = channel
    key = (str(path), nonce, platform)
    previous = _states.get(key)
    if previous:
        old = previous["phase"]
        if ((old is None and phase not in (None, "submitted"))
                or (old is not None and phase is not None and PHASES.index(phase) < PHASES.index(old))
                or (old in {"submitting", "submitted"} and phase is None)):
            raise ValueError("Publisher phase cannot move backwards")
    value = {"version": 1, "nonce": nonce, "platform": platform, "action": "publish",
             "phase": phase, "updated_at": datetime.now(timezone.utc).isoformat()}
    if error_code is not None:
        value["error_code"] = error_code
    if percent is not None:
        value['percent'] = round(percent, 1)
    _atomic_json(path, value)
    _states[key] = value


def record_failure(platform: str) -> None:
    """Keep the furthest phase and add only a fixed, non-sensitive reason."""
    channel = _channel(platform)
    if channel is None:
        return
    path, nonce = channel
    previous = _states.get((str(path), nonce, platform))
    if previous is None:
        return
    phase = previous["phase"]
    code = {
        "preparation": "preparation_failed", "uploading": "upload_failed", "processing": "processing_failed", "metadata": "metadata_failed",
        "cover": "cover_failed", "ready_to_submit": "pre_submit_failed", "submitting": "submission_unconfirmed",
        "submitted": "submission_unconfirmed", None: "submission_boundary_unknown",
    }[phase]
    emit_progress(platform, phase, code)
