"""Experimental Bilibili fixture adapter and bounded native-session conversion.

No native upload CLI, navigation, HTTP client or production executor is used.
A cookie's claimed UID is not proof of login. A matching independent nav result
is required by the fixture adapter; live account/profile acceptance remains open.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import time

from utils.experimental_prepare import FixturePrepareDriver, PrepareFailure

MAX_NATIVE_BYTES = 128 * 1024
MAX_COOKIES = 64
COOKIE_NAMES = frozenset({"SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid", "buvid3", "buvid4", "b_nut"})
REQUIRED_COOKIES = frozenset({"SESSDATA", "bili_jct", "DedeUserID"})


def _reject(reason="native_session_invalid"):
    raise PrepareFailure("account", reason, "Use a bounded Bilibili-only native session and independently verify its account.") from None


def _unique_keys(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            _reject("native_session_ambiguous")
        result[name] = value
    return result


def _uid(value):
    if type(value) not in {str, int} or not re.fullmatch(r"[1-9][0-9]{0,19}", str(value)):
        _reject("account_id_invalid")
    return str(value)


@dataclass(frozen=True)
class BilibiliSession:
    account_id: str
    binding_sha256: str
    storage_state: dict = field(repr=False)


def convert_native_session(raw: bytes, *, expected_account_id: str, now: float | None = None) -> BilibiliSession:
    """Convert the narrow native cookie_info shape; never carry token_info out.

    The native format commonly omits domain and expiry. Missing domain is mapped
    solely to .bilibili.com; missing expiry becomes a session cookie, never a
    guessed lifetime. Explicit foreign domains and duplicate names fail closed.
    Unneeded cookies are discarded, and every input is size/count bounded.
    """
    if not isinstance(raw, bytes) or len(raw) > MAX_NATIVE_BYTES:
        _reject()
    expected = _uid(expected_account_id)
    now = time.time() if now is None else now
    try:
        source = json.loads(raw, object_pairs_hook=_unique_keys)
        cookies = source["cookie_info"]["cookies"]
        if not isinstance(cookies, list) or not 1 <= len(cookies) <= MAX_COOKIES:
            _reject()
        selected, seen = [], set()
        for item in cookies:
            if not isinstance(item, dict):
                _reject()
            name, value = item.get("name"), item.get("value")
            if (not isinstance(name, str) or not 1 <= len(name) <= 128 or name in seen
                    or not isinstance(value, str) or not 1 <= len(value) <= 8192
                    or any(ord(char) < 32 or ord(char) == 127 for char in value)):
                _reject()
            seen.add(name)
            domain = item.get("domain", ".bilibili.com")
            # Do not accept suffix tricks, arbitrary subdomains, or other accounts'
            # browser storage. This conversion is narrower than browser import.
            if domain not in {"bilibili.com", ".bilibili.com"} or item.get("path", "/") != "/":
                _reject("native_session_scope_invalid")
            if name not in COOKIE_NAMES:
                continue
            expiry = item.get("expires", item.get("expiry", -1))
            if (type(expiry) not in {int, float} or not (expiry == -1 or now < expiry < 2**53)):
                _reject("native_session_expired")
            selected.append({"name": name, "value": value, "domain": ".bilibili.com", "path": "/",
                             "expires": expiry, "httpOnly": name in {"SESSDATA", "sid"}, "secure": True, "sameSite": "Lax"})
        names = {item["name"] for item in selected}
        if not REQUIRED_COOKIES <= names:
            _reject("native_session_incomplete")
        uid = _uid(next(item["value"] for item in selected if item["name"] == "DedeUserID"))
        token = source.get("token_info", {})
        if not isinstance(token, dict) or ("mid" in token and _uid(token["mid"]) != uid) or uid != expected:
            _reject("account_mismatch")
        selected.sort(key=lambda item: item["name"])
        fingerprint = hashlib.sha256(json.dumps(selected, sort_keys=True).encode()).hexdigest()
        return BilibiliSession(uid, fingerprint, {"cookies": selected, "origins": []})
    except (ValueError, KeyError, TypeError, UnicodeError, RecursionError):
        _reject()


def load_native_session(path: Path, *, expected_account_id: str) -> BilibiliSession:
    try:
        with Path(path).open("rb") as handle:
            raw = handle.read(MAX_NATIVE_BYTES + 1)
    except OSError:
        _reject("native_session_unreadable")
    return convert_native_session(raw, expected_account_id=expected_account_id)


def verify_nav_identity(payload: dict, *, session: BilibiliSession, expected_account_id: str) -> str:
    """Validate an independently obtained read-only identity response, no request.

    Passing synthetic data here provides L1 evidence only, never online login
    verification. Tokens/cookies are never logged or included in diagnostics.
    """
    expected = _uid(expected_account_id)
    if not isinstance(payload, dict) or type(payload.get("code")) is not int or payload["code"] != 0:
        _reject("account_unverified")
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("isLogin") is not True:
        _reject("account_unverified")
    observed = _uid(data.get("mid"))
    if observed != expected or session.account_id != expected:
        _reject("account_mismatch")
    return observed


class BilibiliFixtureAdapter(FixturePrepareDriver):
    def __init__(self, **kwargs):
        super().__init__("bilibili", **kwargs)

    async def prepare_fixture(self, page, materials, *, session, identity_payload, expected_account_id):
        account = verify_nav_identity(identity_payload, session=session, expected_account_id=expected_account_id)
        return await super().prepare_fixture(page, materials, expected_account=account)

    async def resume_cover_fixture(self, page, materials, checkpoint, *, session, identity_payload, expected_account_id):
        account = verify_nav_identity(identity_payload, session=session, expected_account_id=expected_account_id)
        return await super().resume_cover_fixture(page, materials, checkpoint, expected_account=account)


# Production-shaped adapter without an automatic-post fallback. The source
# repository contains no verified Bilibili web selector binder to reuse.
from utils.manual_prepare_contract import ManualPrepareAdapter


class BilibiliPrepareAdapter(ManualPrepareAdapter):
    platform = "bilibili"
    missing_capabilities = (
        "live_web_selector_binder", "independent_online_identity", "owned_profile_browser_binding",
        "live_transfer_and_processing_evidence", "category_copyright_and_source_controls",
        "live_cover_pixel_and_save_evidence", "real_account_handoff_acceptance",
    )
