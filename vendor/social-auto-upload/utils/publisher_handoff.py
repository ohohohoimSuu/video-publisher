"""Owned, detached platform editors that the user will submit.

Only the configured publisher session is imported. No user Chrome profile is
opened, and a different session binding cannot reuse this profile.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import hashlib
import json
import os
import re
import signal
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from urllib.request import build_opener, ProxyHandler
from urllib.parse import urlsplit


PROJECT = Path(__file__).resolve().parents[3]
BROWSER_ROOT = PROJECT / ".browsers"
CREATOR_ORIGIN = "https://creator.douyin.com"


class HandoffError(RuntimeError):
    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


class AuthVerificationUnavailable(RuntimeError):
    """A transient/unknown check result must never be treated as logout."""


SPECS = {
    "douyin": ("抖音", "douyin.com", ("https://creator.douyin.com", "https://www.douyin.com"), ("sessionid", "sessionid_ss")),
    "xiaohongshu": ("小红书", "xiaohongshu.com", ("https://creator.xiaohongshu.com", "https://www.xiaohongshu.com"), ("web_session",)),
    "youtube": ("YouTube", "youtube.com", ("https://studio.youtube.com", "https://www.youtube.com"), ("__Secure-3PAPISID", "SAPISID", "APISID", "SID")),
    "bilibili": ("哔哩哔哩", "bilibili.com", ("https://member.bilibili.com", "https://www.bilibili.com"), ("DedeUserID",)),
}


def _spec(platform):
    if platform not in SPECS:
        raise ValueError("此平台尚未接入保留编辑窗口的流程")
    return SPECS[platform]


def _douyin_domain(domain, platform="douyin"):
    host = domain.lstrip(".").lower() if isinstance(domain, str) else ""
    base = _spec(platform)[1]
    return host == base or host.endswith("." + base)


def _binding(cookies, platform="douyin"):
    for name in _spec(platform)[3]:
        values = sorted({c.get("value") for c in cookies if isinstance(c, dict) and c.get("name") == name
                         and _douyin_domain(c.get("domain"), platform) and isinstance(c.get("value"), str) and c["value"]})
        if values:
            return hashlib.sha256(json.dumps(values).encode()).hexdigest()
    return None


def _load_session(account_file, platform="douyin"):
    spec = _spec(platform)
    try:
        path = Path(account_file)
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError()
        source = json.loads(path.read_text(encoding="utf-8"))
        if platform == "bilibili":
            from uploader.bilibili_uploader.prepare import convert_native_session
            native = source.get("cookie_info", {}).get("cookies", [])
            uid = next((c.get("value") for c in native if isinstance(c, dict) and c.get("name") == "DedeUserID"), None)
            source = convert_native_session(path.read_bytes(), expected_account_id=uid).storage_state
        raw = source.get("cookies", [])
        if not isinstance(raw, list):
            raise ValueError()
        cookies = [dict(cookie) for cookie in raw
                   if isinstance(cookie, dict) and _douyin_domain(cookie.get("domain"), platform)]
        if len(raw) != len(cookies):
            raise ValueError()
        storage = []
        for origin in source.get("origins", []):
            if not isinstance(origin, dict) or origin.get("origin") not in spec[2]:
                raise ValueError()
            items = origin.get("localStorage", [])
            if not isinstance(items, list) or any(not isinstance(i, dict) or not isinstance(i.get("name"), str)
                                                or not isinstance(i.get("value"), str) for i in items):
                raise ValueError()
            storage.append({"origin": origin["origin"], "items": [{"name": i["name"], "value": i["value"]} for i in items]})
        binding = _binding(cookies, platform)
        if not binding:
            raise ValueError()
        return cookies, storage, binding
    except (OSError, ValueError, TypeError, AttributeError):
        raise RuntimeError(f"{spec[0]}会话无法用于保留编辑窗口，请先重新连接并核验账号") from None


def _write_state(path, value):
    descriptor, temporary = tempfile.mkstemp(prefix=".handoff-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _pid_alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _endpoint_ready(port):
    if type(port) is not int or not 0 < port < 65536:
        return False
    try:
        with build_opener(ProxyHandler({})).open(f"http://127.0.0.1:{port}/json/version", timeout=1) as response:
            value = json.load(response)
        return value.get("webSocketDebuggerUrl", "").startswith(f"ws://127.0.0.1:{port}/devtools/browser/")
    except (OSError, ValueError, AttributeError):
        return False


def _owned_process(pid, profile):
    if os.name == "nt":
        return False
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=3)
        return result.returncode == 0 and f"--user-data-dir={profile} " in result.stdout.rstrip() + " "
    except (OSError, subprocess.TimeoutExpired):
        return False


def _stop_owned_headless_browser(state, profile):
    """Switch a check-only process to a visible editor in the same profile.

    A headless process cannot contain a user-visible editor. Re-check both its
    ownership and actual headless flag before stopping it; ordinary browsers
    are never stopped by this path.
    """
    pid = state["pid"]
    result = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=3)
    if (state.get("headless") is not True or result.returncode != 0
            or f"--user-data-dir={profile} " not in result.stdout.rstrip() + " "
            or "--headless=new" not in result.stdout.split()):
        raise HandoffError('browser_unverified', "无法确认后台账号环境，未关闭或替换浏览器。")
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        # When called in the launching process, reap its child as well.
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        if not _pid_alive(pid) and not _endpoint_ready(state.get("port")):
            return
        time.sleep(.1)
    raise HandoffError('browser_busy', "后台账号环境仍在保存，请稍后重试打开编辑页。")


def _start_detached(executable, binding, platform="douyin", proxy=None, session_sha256=None, require_existing=False, quiet=False):
    _spec(platform)
    if not isinstance(binding, str) or not re.fullmatch(r"[a-f0-9]{64}", binding):
        raise RuntimeError("账号绑定格式无效")
    if session_sha256 is not None and (not isinstance(session_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", session_sha256)):
        raise RuntimeError("会话快照标识无效")
    directory = Path(os.environ.get("PUBLISHER_DATA_DIR", str(PROJECT / "data"))).resolve() / f"{platform}-handoff"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(directory / ".launch.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name == "posix":
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        return _start_owned_browser(executable, binding, platform, proxy, session_sha256, require_existing, quiet)
    finally:
        os.close(descriptor)


def _start_owned_browser(executable, binding, platform, proxy, session_sha256, require_existing, quiet=False):
    name = _spec(platform)[0]
    executable = Path(executable).resolve()
    if (not executable.is_relative_to(BROWSER_ROOT.resolve()) or not executable.is_file()
            or executable.name not in {"Google Chrome for Testing", "chrome", "chrome.exe", "chromium"}):
        raise RuntimeError("保留编辑窗口仅允许发布台专用浏览器")
    if proxy:
        parsed = urlsplit(proxy)
        if parsed.scheme not in {"http", "https", "socks5", "socks4"} or not parsed.hostname or parsed.username or parsed.password:
            raise RuntimeError("专用浏览器代理格式不支持，未改动系统代理或开始上传")
    proxy_hash = hashlib.sha256((proxy or "").encode()).hexdigest()
    directory = Path(os.environ.get("PUBLISHER_DATA_DIR", str(PROJECT / "data"))).resolve() / f"{platform}-handoff"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    snapshot = session_sha256 or binding
    profile_key = hashlib.sha256((binding + snapshot).encode()).hexdigest() if session_sha256 else binding
    profile = directory / "profiles" / profile_key
    state_file = directory / "browser.json"
    try:
        state = json.loads(state_file.read_text()) if state_file.exists() else None
    except (OSError, ValueError):
        raise RuntimeError(f"{name}编辑窗口记录损坏，未打开或替换已有窗口") from None
    if state is not None:
        if not isinstance(state, dict):
            raise RuntimeError("编辑窗口记录格式无效")
        if _pid_alive(state.get("pid")):
            if state.get("binding_sha256") != binding or state.get("proxy_hash") != proxy_hash:
                raise HandoffError('session_changed', f"{name}已有其他会话或代理设置的窗口，请先处理并关闭，未覆盖页面")
            if state.get("session_sha256", binding) != snapshot:
                raise HandoffError('snapshot_changed', f"{name}登录会话已重新同步。请先完成或关闭旧专用浏览器，再使用新会话准备；旧编辑页未被覆盖")
            if state.get("profile") != str(profile) or not _owned_process(state["pid"], profile) or not _endpoint_ready(state.get("port")):
                raise HandoffError('browser_unverified', f"无法核对{name}既有专用浏览器，请关闭该专用浏览器后重试")
            if state.get("headless") is True and not quiet:
                if require_existing:
                    raise HandoffError('browser_closed', f"{name}没有保留的可见编辑窗口，未重新上传。")
                _stop_owned_headless_browser(state, profile)
            else:
                return f"http://127.0.0.1:{state['port']}"
    if require_existing:
        raise HandoffError('browser_closed', f"{name}原编辑窗口已关闭，无法继续封面。请先核对平台草稿，再决定是否重新准备")
    marker = profile / "publisher-profile.json"
    if profile.exists() and any(profile.iterdir()):
        try:
            if json.loads(marker.read_text()) != {"platform": platform, "binding_sha256": binding, "session_sha256": snapshot}:
                raise ValueError()
        except (OSError, ValueError):
            raise RuntimeError("专用浏览器配置缺少有效账号绑定，未使用该配置") from None
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    profile.chmod(0o700)
    _write_state(marker, {"platform": platform, "binding_sha256": binding, "session_sha256": snapshot})
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    process = subprocess.Popen([
        str(executable), f"--user-data-dir={profile}", f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1", "--no-first-run", "--no-default-browser-check",
        "--lang=zh-CN", *([f"--proxy-server={proxy}"] if proxy else []),
        *(["--headless=new"] if quiet else []), "about:blank",
    ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        shell=False, start_new_session=True)
    _write_state(state_file, {"binding_sha256": binding, "pid": process.pid, "port": port,
                             "profile": str(profile), "proxy_hash": proxy_hash, "platform": platform,
                             "session_sha256": snapshot, "headless": bool(quiet)})
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        if _endpoint_ready(port):
            return f"http://127.0.0.1:{port}"
        time.sleep(0.2)
    raise RuntimeError(f"{name}专用浏览器未能就绪，未开始上传")


def _session_snapshot(cookies, storage):
    return hashlib.sha256(json.dumps({"cookies": cookies, "storage": storage}, sort_keys=True).encode()).hexdigest()


def _profile_link_path(account_file):
    return Path(str(account_file) + ".profile.json")


def _read_profile_link(account_file, binding, snapshot):
    """Only locally saved refreshes may retain a profile after cookies rotate.

    An imported/replaced credential has a different digest and must follow the
    existing account-isolation path, including preserving any live editor.
    """
    try:
        link = json.loads(_profile_link_path(account_file).read_text())
        current_matches = link.get("source_binding") == binding and link.get("source_snapshot") == snapshot
        previous_matches = link.get("previous_source_binding") == binding and link.get("previous_source_snapshot") == snapshot
        if not current_matches and not previous_matches:
            return None
        if any(not isinstance(link.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", link[key])
               for key in ("profile_binding", "profile_snapshot")):
            return None
        return link
    except (OSError, ValueError, AttributeError):
        return None


@asynccontextmanager
async def quiet_publisher_playwright():
    """Let this check's driver attach to Chromium's windowless page targets.

    The bundled Patchright marks hidden targets as `other`. Scope its existing
    opt-in to the newly spawned driver; do not alter the browser profile or the
    environment of later login/upload drivers.
    """
    from patchright.async_api import async_playwright
    key = "PW_CHROMIUM_ATTACH_TO_OTHER"
    previous = os.environ.get(key)
    os.environ[key] = "1"
    try:
        playwright = await async_playwright().start()
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous
    try:
        yield playwright
    finally:
        await playwright.stop()


@asynccontextmanager
async def hidden_auth_page(browser, context):
    """Own exactly one hidden target, never an existing editor or next UI tab."""
    session = await browser.new_browser_cdp_session()
    target_id = None
    previous_pages = set(context.pages)
    inspected = set()
    try:
        # Chromium can expose CDP before registering its initial background
        # frame targets. Retry only that specific startup race, with a bound.
        # Other errors must not fall back to creating a visible page.
        create_deadline = time.monotonic() + 5
        while True:
            try:
                result = await session.send("Target.createTarget", {
                    "url": "about:blank", "background": True, "hidden": True,
                })
                break
            except Exception as error:
                if ("Hidden target can be created only when remote debugging is enabled" not in str(error)
                        or time.monotonic() >= create_deadline):
                    raise AuthVerificationUnavailable("后台核验页面暂时不可用，已保留登录，请稍后重试检查。") from None
                await asyncio.sleep(.1)
        target_id = result["targetId"]
        # Fail closed on an incompatible browser; never fall back to new_page.
        try:
            await session.send("Browser.getWindowForTarget", {"targetId": target_id})
        except Exception as error:
            if not any(message in str(error) for message in ("No window found", "Browser window not found")):
                raise AuthVerificationUnavailable("无法确认后台核验环境，请稍后重试检查。") from None
        else:
            raise AuthVerificationUnavailable("浏览器不支持后台核验，请更新发布台浏览器后重试。")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            for page in list(context.pages):
                if page in previous_pages or page in inspected:
                    continue
                page_session = await context.new_cdp_session(page)
                try:
                    info = await page_session.send("Target.getTargetInfo")
                finally:
                    await page_session.detach()
                inspected.add(page)
                if info["targetInfo"]["targetId"] == target_id:
                    yield page
                    return
            await asyncio.sleep(.05)
        raise AuthVerificationUnavailable("后台核验页面未能就绪，请稍后重试检查。")
    finally:
        if target_id:
            with suppress(Exception):
                await session.send("Target.closeTarget", {"targetId": target_id})
        with suppress(Exception):
            await session.detach()


async def _quiet_session_state(context, account_file, platform, probe_page):
    """Collect platform state without storage_state's implicit visible page.

    Read only existing targets. Keep previously saved origins if their page is
    absent; a successful empty read, however, records an intentional clear.
    """
    allowed = _spec(platform)[2]
    _, old_storage, _ = _load_session(account_file, platform)
    origins = {item["origin"]: {"origin": item["origin"], "localStorage": item["items"]}
               for item in old_storage}
    read_origins = set()
    for page in [probe_page, *context.pages]:
        parsed = urlsplit(page.url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in allowed:
            if page is probe_page:
                raise AuthVerificationUnavailable("核验页面来源已变化，已保留原登录状态。")
            continue
        if origin in read_origins:
            continue
        session = None
        try:
            session = await context.new_cdp_session(page)
            result = await session.send("DOMStorage.getDOMStorageItems", {
                "storageId": {"securityOrigin": origin, "isLocalStorage": True},
            })
            entries = result["entries"]
            if not isinstance(entries, list) or any(not isinstance(pair, list) or len(pair) != 2
                    or not all(isinstance(value, str) for value in pair) for pair in entries):
                raise ValueError("Invalid storage response")
            origins[origin] = {"origin": origin, "localStorage": [{"name": name, "value": value} for name, value in entries]}
            read_origins.add(origin)
        except Exception:
            if page is probe_page:
                raise AuthVerificationUnavailable("暂时无法保存核验结果，已保留原登录状态，请重试检查。") from None
        finally:
            if session is not None:
                with suppress(Exception):
                    await session.detach()
    return {"cookies": await context.cookies(), "origins": list(origins.values())}


async def save_publisher_session(context, account_file, platform, *, probe_page=None):
    """Save only this platform's state; keep its owned profile across refreshes."""
    ownership = getattr(context, "_publisher_session_owner", None)
    if not ownership or ownership["platform"] != platform or ownership["account_file"] != str(Path(account_file).resolve()):
        raise RuntimeError("无法核对账号环境，未保存会话")
    source = (await _quiet_session_state(context, account_file, platform, probe_page)
              if probe_page is not None else await context.storage_state())
    cookies = [c for c in source.get("cookies", []) if _douyin_domain(c.get("domain"), platform)]
    origins = [o for o in source.get("origins", []) if o.get("origin") in _spec(platform)[2]]
    binding = _binding(cookies, platform)
    if not binding:
        raise RuntimeError("未检测到有效登录会话，保留原账号数据")
    storage = [{"origin": o["origin"], "items": o.get("localStorage", [])} for o in origins]
    snapshot = _session_snapshot(cookies, storage)
    path = Path(account_file)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # A concurrent explicit import must not be overwritten by a stale probe.
    previous_binding, previous_snapshot = None, None
    if path.exists():
        previous_cookies, previous_storage, previous_binding = _load_session(path, platform)
        previous_snapshot = _session_snapshot(previous_cookies, previous_storage)
    if (previous_binding, previous_snapshot) != (ownership["source_binding"], ownership["source_snapshot"]):
        raise HandoffError('session_changed', "核验期间账号已被重新导入，保留新会话，请重新检查")
    # Commit the link first, retaining one previous source digest. A failure
    # between the two atomic replacements can then resolve either credential
    # version to this same profile, without losing or exposing credentials.
    _write_state(_profile_link_path(path), {"source_binding": binding, "source_snapshot": snapshot,
                 "previous_source_binding": previous_binding, "previous_source_snapshot": previous_snapshot,
                 "profile_binding": ownership["binding"], "profile_snapshot": ownership["snapshot"]})
    _write_state(path, {"cookies": cookies, "origins": origins})
    ownership.update(source_binding=binding, source_snapshot=snapshot)


def native_chrome_endpoint(platform):
    endpoint = os.environ.get("PUBLISHER_NATIVE_CHROME_ENDPOINT", "")
    expected = os.environ.get("PUBLISHER_NATIVE_CHROME_PLATFORM", "")
    import re
    if endpoint and (platform != expected or not re.fullmatch(r"ws://127\.0\.0\.1:[0-9]{1,5}/cdp/[a-f0-9]{64}", endpoint)):
        raise RuntimeError("普通 Chrome 任务连接无效，未操作页面")
    return endpoint


async def connect_publisher_handoff(playwright, account_file, platform, *, proxy=None, require_existing=False, allow_login=False, quiet=False):
    endpoint = native_chrome_endpoint(platform)
    if endpoint:
        if require_existing:
            raise RuntimeError("普通 Chrome 封面接续尚未支持；请在保留的原标签页处理，未重传")
        browser = await playwright.chromium.connect_over_cdp(endpoint, timeout=30000, is_local=True)
        if len(browser.contexts) != 1:
            raise RuntimeError("无法核对普通 Chrome 的任务页面")
        return browser, browser.contexts[0]
    spec = _spec(platform)
    pending_login = allow_login and platform == "xiaohongshu" and not Path(account_file).exists()
    if pending_login:
        cookies, storage = [], []
        binding = hashlib.sha256((platform + ":first-login:" + str(Path(account_file).resolve())).encode()).hexdigest()
    else:
        cookies, storage, binding = _load_session(account_file, platform)
    # Identity alone is insufficient: a fresh import can rotate other auth
    # cookies or storage while retaining the primary identity cookie.
    snapshot = _session_snapshot(cookies, storage)
    link = _read_profile_link(account_file, binding, snapshot) if platform == "xiaohongshu" else None
    profile_binding = link["profile_binding"] if link else binding
    profile_snapshot = link["profile_snapshot"] if link else snapshot
    endpoint = await asyncio.to_thread(_start_detached, playwright.chromium.executable_path, profile_binding, platform, proxy, profile_snapshot, require_existing, quiet)
    # This endpoint is an owned browser launched on this same host. Without
    # is_local, Patchright serializes files and rejects videos above 50 MiB.
    browser = await playwright.chromium.connect_over_cdp(endpoint, timeout=20000, is_local=True)
    if not browser.contexts:
        raise RuntimeError("编辑窗口缺少持久浏览器上下文，未开始上传")
    # CDP-created incognito contexts are destroyed on disconnect. Only the
    # browser's default persistent context survives for the user's final click.
    context = browser.contexts[0]
    existing_binding = _binding(await context.cookies(list(spec[2])), platform)
    if require_existing and existing_binding is None:
        raise HandoffError('session_missing', f"{spec[0]}原编辑窗口的会话已变化，未重新登录或改动页面")
    if existing_binding is not None and existing_binding != binding and not link and not pending_login:
        raise HandoffError('session_changed', f"{spec[0]}编辑窗口账号与已授权会话不一致，未覆盖或上传")
    if existing_binding is None:
        await context.add_cookies(cookies)
        if storage:
            script = "(() => { const entry = " + json.dumps(storage) + ".find(x => x.origin === location.origin); "
            script += "if (entry) for (const item of entry.items) if (localStorage.getItem(item.name) === null) localStorage.setItem(item.name, item.value); })();"
            await context.add_init_script(script=script)
    if platform == "xiaohongshu":
        # A saved link authenticates the credential file/profile relationship.
        # The persistent browser can now renew cookies without forcing a fresh
        # browser fingerprint or destroying the editor on the next check.
        context._publisher_session_owner = {"platform": platform, "account_file": str(Path(account_file).resolve()),
                                            "binding": profile_binding, "snapshot": profile_snapshot,
                                            "source_binding": None if pending_login else binding,
                                            "source_snapshot": None if pending_login else snapshot}
        if cookies and not link:
            _write_state(_profile_link_path(account_file), {"source_binding": binding, "source_snapshot": snapshot,
                         "profile_binding": profile_binding, "profile_snapshot": profile_snapshot})
    return browser, context


async def connect_douyin_handoff(playwright, account_file):
    return await connect_publisher_handoff(playwright, account_file, "douyin")
