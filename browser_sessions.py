"""Validate explicitly shared Chrome sessions without reading browser profiles.

Only this project's private account files are written. Import is a local format
check, never proof of a working login. All errors and returned metadata exclude
cookie/localStorage values. The HTTP caller must serialize imports with jobs.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from urllib.parse import parse_qsl, urlsplit

SUPPORTED = {
    "douyin": {"name": "抖音", "domains": ["douyin.com"],
               "origins": ["https://creator.douyin.com", "https://www.douyin.com"]},
    "xiaohongshu": {"name": "小红书", "domains": ["xiaohongshu.com"],
                    "origins": ["https://creator.xiaohongshu.com", "https://www.xiaohongshu.com"]},
    "youtube": {"name": "YouTube", "domains": ["youtube.com"],
                "origins": ["https://studio.youtube.com", "https://www.youtube.com"]},
}
BILIBILI_UNSUPPORTED_REASON = "B站浏览器会话不能直接用于当前上传组件，请使用发布台的独立登录；现有账号文件未改变。"
MAX_PAYLOAD_BYTES = 2 * 1024 * 1024
MAX_COOKIES = 512
MAX_STORAGE_ITEMS = 256
_CHANNEL = re.compile(r"UC[A-Za-z0-9_-]{22}\Z")
_COOKIE_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")
_LOCK = threading.RLock()


def _platform(pid):
    if pid == "bilibili":
        raise ValueError(BILIBILI_UNSUPPORTED_REASON)
    if not isinstance(pid, str) or pid not in SUPPORTED:
        raise ValueError("此平台不支持 Chrome 会话同步。")
    return SUPPORTED[pid]


def _string(value, maximum, message, *, nonempty=False, controls=False):
    if not isinstance(value, str) or len(value.encode("utf-8")) > maximum:
        raise ValueError(message)
    if nonempty and not value:
        raise ValueError(message)
    if not controls and any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(message)
    return value


def _bool(item, name, default=False):
    value = item.get(name, default)
    if type(value) is not bool:
        raise ValueError("会话中的 Cookie 属性无效。")
    return value


def _allowed_domain(domain, spec):
    return any(domain == base or domain.endswith("." + base) for base in spec["domains"])


def _cookies(raw, spec, now):
    if not isinstance(raw, list) or not raw or len(raw) > MAX_COOKIES:
        raise ValueError("Cookie 数量无效，未保存会话。")
    normalized, seen, stores = [], set(), set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Cookie 格式无效。")
        domain = _string(item.get("domain"), 253, "Cookie 域名无效。", nonempty=True)
        host = domain.removeprefix(".").lower()
        if not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*", host) or not _allowed_domain(host, spec):
            raise ValueError("会话包含所选平台以外的 Cookie，未保存。")
        name = _string(item.get("name"), 256, "Cookie 名称无效。", nonempty=True)
        value = _string(item.get("value"), 32768, "Cookie 内容格式无效。")
        if not _COOKIE_NAME.fullmatch(name) or ";" in value:
            raise ValueError("Cookie 内容格式无效。")
        path = _string(item.get("path", "/"), 2048, "Cookie 路径无效。", nonempty=True)
        if not path.startswith("/"):
            raise ValueError("Cookie 路径无效。")
        session = _bool(item, "session", "expirationDate" not in item)
        expiry = item.get("expirationDate", -1)
        if type(expiry) not in (int, float) or not math.isfinite(expiry):
            raise ValueError("Cookie 到期时间无效。")
        if session and "expirationDate" in item:
            raise ValueError("Cookie 会话与到期属性不一致。")
        if not session and expiry <= now:
            continue
        if item.get("partitionKey") is not None or item.get("partitioned") is True:
            raise ValueError("当前不支持分区 Cookie；未静默删除凭据，请使用平台独立登录。")
        if "partitioned" in item and type(item["partitioned"]) is not bool:
            raise ValueError("Cookie 分区属性无效。")
        secure = _bool(item, "secure")
        http_only = _bool(item, "httpOnly")
        host_only = _bool(item, "hostOnly", not domain.startswith("."))
        if (name.startswith("__Secure-") and not secure) or (name.startswith("__Host-") and (not secure or not host_only or path != "/")):
            raise ValueError("Cookie 安全前缀属性无效。")
        raw_same_site = item.get("sameSite", "unspecified")
        same_site = {"no_restriction": "None", "lax": "Lax", "strict": "Strict", "unspecified": "Lax"}.get(raw_same_site) if isinstance(raw_same_site, str) else None
        if same_site is None or (same_site == "None" and not secure):
            raise ValueError("Cookie SameSite 属性无法安全映射。")
        normalized_domain = host if host_only else "." + host
        identity = (name, normalized_domain, path)
        if identity in seen:
            raise ValueError("会话包含重复 Cookie，可能混合了多个账号。")
        seen.add(identity)
        if "storeId" in item:
            stores.add(_string(item["storeId"], 64, "Cookie 存储区无效。", nonempty=True))
        normalized.append({"name": name, "value": value, "domain": normalized_domain,
                           "path": path, "expires": -1 if session else expiry,
                           "httpOnly": http_only, "secure": secure, "sameSite": same_site})
    if len(stores) > 1:
        raise ValueError("会话混合了多个 Chrome 存储区，未保存。")
    return normalized


def _origins(raw, spec):
    if not isinstance(raw, list) or len(raw) > len(spec["origins"]):
        raise ValueError("页面存储格式无效。")
    normalized, seen = [], set()
    for item in raw:
        if not isinstance(item, dict) or item.get("origin") not in spec["origins"]:
            raise ValueError("会话包含未允许的网站页面存储。")
        origin = item["origin"]
        if origin in seen:
            raise ValueError("页面存储来源重复。")
        seen.add(origin)
        items = item.get("localStorage")
        if not isinstance(items, list) or len(items) > MAX_STORAGE_ITEMS:
            raise ValueError("页面存储数量无效。")
        storage, keys = [], set()
        for entry in items:
            if not isinstance(entry, dict):
                raise ValueError("页面存储内容无效。")
            name = _string(entry.get("name"), 2048, "页面存储键无效。", controls=True)
            value = _string(entry.get("value"), 128 * 1024, "页面存储值过大或无效。", controls=True)
            if name in keys:
                raise ValueError("页面存储键重复。")
            keys.add(name)
            storage.append({"name": name, "value": value})
        normalized.append({"origin": origin, "localStorage": storage})
    return normalized


def channel_id_from_url(value):
    """Only a Studio channel URL can assert the selected upload destination."""
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.netloc != "studio.youtube.com":
            return None
        parts = parsed.path.split("/")
        if len(parts) >= 3 and parts[1] == "channel" and _CHANNEL.fullmatch(parts[2]):
            return parts[2]
    except (TypeError, ValueError):
        pass
    return None


def youtube_authuser_from_url(value):
    """Preserve only an existing account-selection hint, never infer an account."""
    message = "当前 YouTube Studio 账号选择参数无效或重复，请重新打开目标 Studio 频道页面后同步。"
    try:
        values = [v for k, v in parse_qsl(urlsplit(value).query, keep_blank_values=True,
                                         max_num_fields=128) if k == "authuser"]
    except (TypeError, ValueError):
        raise ValueError(message) from None
    if not values:
        return None
    if len(values) != 1 or not re.fullmatch(r"(?:0|[1-9][0-9]?)", values[0]):
        raise ValueError(message)
    return int(values[0])


def _metadata_path(pid, directory):
    return Path(directory) / f"{pid}_default.json.publisher.json"


def _atomic_write(path, content):
    if path.is_symlink():
        raise ValueError("会话文件路径不安全，未写入。")
    fd, temporary = tempfile.mkstemp(prefix=".session-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")


def _safe_metadata(value):
    if not isinstance(value, dict) or value.get("source") != "chrome":
        return {}
    result = {"source": "chrome", "browser_verified": False}
    imported_at = value.get("imported_at")
    count = value.get("cookie_count")
    if not isinstance(imported_at, str) or len(imported_at) > 40 or type(count) is not int or not 0 < count <= MAX_COOKIES:
        return {}
    result.update(imported_at=imported_at, cookie_count=count)
    for field in ("account_hint", "profile_label"):
        text = value.get(field)
        if isinstance(text, str) and len(text.encode("utf-8")) <= 256 and not any(ord(c) < 32 for c in text):
            result[field] = text
    channel = value.get("channel_id")
    if isinstance(channel, str) and _CHANNEL.fullmatch(channel):
        result["channel_id"] = channel
        authuser = value.get("authuser")
        if type(authuser) is int and 0 <= authuser <= 99:
            result["authuser"] = authuser
    return result


def read_metadata(pid, session_dir):
    """Read only the small, allowlisted display sidecar, never return credentials."""
    if not isinstance(pid, str) or pid not in SUPPORTED:
        return {}
    path = _metadata_path(pid, session_dir)
    try:
        if path.is_symlink() or path.stat().st_size > 4096:
            return {}
        return _safe_metadata(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return {}


def clear_metadata(pid, session_dir):
    """Called only after a separate login successfully replaced credentials."""
    if isinstance(pid, str) and pid in SUPPORTED:
        with _LOCK:
            _metadata_path(pid, session_dir).unlink(missing_ok=True)


def _validate_and_save(pid, payload, session_dir: Path, metadata_dir: Path):
    spec = _platform(pid)
    if not isinstance(payload, dict):
        raise ValueError("会话请求格式无效。")
    try:
        if len(_json_bytes(payload)) > MAX_PAYLOAD_BYTES:
            raise ValueError("会话数据过大，未保存。")
    except (TypeError, OverflowError, RecursionError, UnicodeError):
        raise ValueError("会话请求格式无效。") from None
    except ValueError:
        raise ValueError("会话数据过大或格式无效，未保存。") from None
    cookies = _cookies(payload.get("cookies"), spec, time.time())
    origins = _origins(payload.get("origins", []), spec)
    names = {cookie["name"] for cookie in cookies if cookie["value"]}
    auth_markers = {"douyin": {"sessionid", "sessionid_ss"}, "xiaohongshu": {"web_session"},
                    "youtube": {"SID", "__Secure-1PSID", "__Secure-3PSID"}}[pid]
    if not names.intersection(auth_markers):
        raise ValueError("未找到未过期的登录凭据，现有发布台会话未改变；请确认所选 Chrome 页面已登录。")
    metadata = {"source": "chrome", "imported_at": datetime.now(timezone.utc).isoformat(),
                "cookie_count": len(cookies), "browser_verified": False}
    page_url = payload.get("page_url")
    if page_url is not None:
        _string(page_url, 4096, "页面地址无效。")
        try:
            parsed = urlsplit(page_url)
            if parsed.scheme + "://" + parsed.netloc not in spec["origins"]:
                raise ValueError("页面地址不属于允许的网站。")
        except ValueError:
            raise ValueError("页面地址不属于允许的网站。") from None
    if pid == "youtube":
        channel_id = channel_id_from_url(page_url)
        if channel_id is None:
            raise ValueError("请先打开目标 YouTube Studio 频道页面（地址含 /channel/UC…），再同步，以锁定发布频道。")
        metadata["channel_id"] = channel_id
        authuser = youtube_authuser_from_url(page_url)
        if authuser is not None:
            metadata["authuser"] = authuser
    for field in ("account_hint", "profile_label"):
        if payload.get(field):
            metadata[field] = _string(payload[field], 256, "页面显示名称过长或格式无效。")
    # These hints are page-supplied labels, never a verified identity assertion.
    state = _json_bytes({"cookies": cookies, "origins": origins})
    private_metadata = {**metadata, "session_sha256": hashlib.sha256(state).hexdigest()}
    if pid == "youtube":
        # Bind navigation context to this exact session and selected channel.
        # This is a consistency checksum, not authentication or a credential.
        context = {key: private_metadata.get(key) for key in ("channel_id", "authuser", "session_sha256")}
        private_metadata["context_sha256"] = hashlib.sha256(
            json.dumps(context, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    sidecar = _json_bytes(private_metadata)
    session_dir, metadata_dir = Path(session_dir), Path(metadata_dir)
    with _LOCK:
        for directory in {session_dir, metadata_dir}:
            if directory.is_symlink():
                raise ValueError("会话目录不安全，未写入。")
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)
        account = session_dir / f"{pid}_default.json"
        runtime_metadata = _metadata_path(pid, session_dir)
        metadata_file = _metadata_path(pid, metadata_dir)
        targets = list(dict.fromkeys((account, runtime_metadata, metadata_file)))
        # Complete all validation before touching a current session. Keep one
        # private recovery copy, including its channel binding, until next import.
        for target in targets:
            if target.is_symlink() or (target.exists() and not target.is_file()):
                raise ValueError("会话文件路径不安全，未写入。")
        previous = {}
        for target in targets:
            if target.exists():
                if target.stat().st_size > MAX_PAYLOAD_BYTES:
                    raise ValueError("现有会话文件大小异常，未替换。")
                previous[target] = target.read_bytes()
                _atomic_write(target.with_name(target.name + ".previous"), previous[target])
        replaced = []
        try:
            # A mismatched pair fails closed in YouTube. No observer may trust
            # a new sidecar until the account file matches its digest.
            for target in targets:
                _atomic_write(target, state if target == account else sidecar)
                replaced.append(target)
        except Exception:
            for target in reversed(replaced):
                if target in previous:
                    _atomic_write(target, previous[target])
                else:
                    target.unlink(missing_ok=True)
            raise ValueError("会话保存失败，未完成连接；请重试。") from None
    return metadata


def validate_and_save(pid, payload, session_dir: Path, metadata_dir: Path):
    try:
        return _validate_and_save(pid, payload, session_dir, metadata_dir)
    except (OSError, TypeError, UnicodeError, OverflowError, RecursionError):
        raise ValueError("会话格式或本地保存失败，未完成连接；请重新同步。") from None
