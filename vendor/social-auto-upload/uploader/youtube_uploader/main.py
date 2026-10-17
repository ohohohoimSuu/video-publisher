# -*- coding: utf-8 -*-
"""YouTube uploader (browser automation via YouTube Studio).

Unlike the other platforms here, YouTube also offers an official Data API. We deliberately
use browser automation instead, because videos uploaded through an *unaudited* API project
are force-locked to private and cannot be made public without passing Google's compliance
audit (which is impractical for personal/single-channel use). Browser automation has no such
restriction and can publish public videos right away, and it matches the cookie-based pattern
used by every other uploader in this project.

Login is interactive (Google account, no QR code): the browser opens, the user signs in, and
the storage_state is saved. Reuse it afterwards for fully unattended uploads.
"""
import asyncio
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from patchright.async_api import Page, Playwright, async_playwright

from conf import DEBUG_MODE
from uploader.base_video import BaseVideoUploader
from utils.base_social_media import set_init_script
from utils.log import youtube_logger
from utils import publisher_handoff
from utils.publisher_progress import emit_progress
from utils.publisher_materials import preview_state, wait_initial_preview, wait_changed_preview, unique_visible

try:
    # 国内直连 youtube.com 会超时，且 patchright 启的 chromium 不吃系统代理。
    # 在 conf.py 设 YT_PROXY = "http://127.0.0.1:7890"（本地代理端口）即可；不设则不走代理。
    from conf import YT_PROXY
except Exception:
    YT_PROXY = None

STUDIO_URL = "https://studio.youtube.com"
UPLOAD_URL = "https://www.youtube.com/upload"
VISIBILITY = {"public": "PUBLIC", "unlisted": "UNLISTED", "private": "PRIVATE"}
_STUDIO_ACCOUNT_NAME = re.compile(r"^(?:Account(?:\b.*)?|账号|帐户|账户)$", re.I)
_STUDIO_UPLOAD_NAME = re.compile(r"^(?:Upload videos|上传视频)$", re.I)
_last_auth_diagnostic = {"reason": "studio_not_ready", "stage": "not_started"}


def get_last_auth_diagnostic():
    """Only allowlisted metadata; never return exception text, URLs or session data."""
    return dict(_last_auth_diagnostic)


def _record_auth_diagnostic(reason, stage, *, expected=None, page=None, error=None, http_status=None):
    global _last_auth_diagnostic
    url = getattr(page, "url", "") if page is not None else ""
    host = None
    if isinstance(url, str):
        try:
            candidate = urlsplit(url).hostname
            if candidate and re.fullmatch(r"[A-Za-z0-9.-]{1,253}", candidate):
                host = candidate.lower()
        except ValueError:
            pass
    net_error = None
    error_type = None
    if error is not None:
        error_type = type(error).__name__
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", error_type):
            error_type = "Error"
        match = re.search(r"\bnet::(ERR_[A-Z_]+)\b", str(error))
        if match:
            net_error = match.group(1)
    _last_auth_diagnostic = {
        "reason": reason,
        "stage": stage,
        "error_type": error_type,
        "net_error": net_error,
        "http_status": http_status if type(http_status) is int else None,
        "expected_channel": expected,
        "actual_channel": _channel_from_url(url),
        "host": host,
    }


def _is_login_redirect(url):
    if not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
        return parsed.hostname == "accounts.google.com" or "/signin" in parsed.path.lower()
    except ValueError:
        return False


async def _wait_for_studio(page, expected, max_wait_ms=20000):
    """A channel URL plus a visible authenticated Studio control is required."""
    for elapsed in range(0, max_wait_ms + 1, 1000):
        if _is_login_redirect(page.url):
            return "login_redirect"
        actual = _channel_from_url(page.url)
        if expected and actual and actual != expected:
            return "channel_mismatch"
        if actual:
            account = page.get_by_role("button", name=_STUDIO_ACCOUNT_NAME).first
            upload = page.get_by_role("button", name=_STUDIO_UPLOAD_NAME).first
            if await account.is_visible() or await upload.is_visible():
                # Navigation can change while querying the controls.
                if _is_login_redirect(page.url):
                    return "login_redirect"
                if expected and _channel_from_url(page.url) != expected:
                    return "channel_mismatch" if _channel_from_url(page.url) else "studio_not_ready"
                if _channel_from_url(page.url):
                    return "verified"
        if elapsed < max_wait_ms:
            await page.wait_for_timeout(1000)
    return "studio_not_ready"


def _channel_from_url(url):
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "studio.youtube.com":
            return None
        parts = parsed.path.split("/")
        if len(parts) >= 3 and parts[1] == "channel" and re.fullmatch(r"UC[A-Za-z0-9_-]{22}", parts[2]):
            return parts[2]
    except (TypeError, ValueError):
        pass
    return None


def _publisher_binding(account_file):
    """A Chrome import binds its exact session to one explicitly selected channel."""
    path = Path(str(account_file) + ".publisher.json")
    if not path.exists() and not path.is_symlink():
        return {}  # Existing independent login has no Chrome channel binding.
    try:
        account = Path(account_file)
        if path.is_symlink() or path.stat().st_size > 4096 or account.stat().st_size > 2 * 1024 * 1024:
            raise ValueError()
        metadata = json.loads(path.read_text(encoding="utf-8"))
        channel = metadata.get("channel_id")
        if (metadata.get("source") != "chrome" or not isinstance(channel, str)
                or not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", channel)
                or metadata.get("session_sha256") != hashlib.sha256(account.read_bytes()).hexdigest()):
            raise ValueError()
        authuser = metadata.get("authuser")
        if "authuser" in metadata and (type(authuser) is not int or not 0 <= authuser <= 99):
            raise ValueError()
        if "authuser" in metadata or "context_sha256" in metadata:
            context = {key: metadata.get(key) for key in ("channel_id", "authuser", "session_sha256")}
            digest = hashlib.sha256(json.dumps(context, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            if metadata.get("context_sha256") != digest:
                raise ValueError()
        return {"channel_id": channel, **({"authuser": authuser} if authuser is not None else {})}
    except (OSError, ValueError, TypeError, AttributeError):
        raise RuntimeError("NEEDS_ATTENTION: YouTube 同步会话与频道锁不一致，请重新同步目标频道；未开始上传") from None


def _publisher_channel(account_file):
    """Compatibility helper for callers that need only the destination channel."""
    return _publisher_binding(account_file).get("channel_id")


def _studio_target(binding, fallback):
    channel = binding.get("channel_id")
    if not channel:
        return fallback
    target = f"{STUDIO_URL}/channel/{channel}"
    if "authuser" in binding:
        target += f"?authuser={binding['authuser']}"
    return target


async def _require_channel(page, expected):
    if expected and _channel_from_url(page.url) != expected:
        raise RuntimeError("NEEDS_ATTENTION: 当前 YouTube Studio 频道与同步时选定频道不一致，已停止；请切换到目标频道后重新同步")


def _msg(emoji: str, text: str) -> str:
    return f"{emoji} {text}"


def _build_login_result(success, status, message, account_file, current_url=""):
    return {
        "success": success,
        "status": status,
        "message": message,
        "account_file": str(account_file),
        "current_url": current_url,
    }


async def cookie_auth(account_file) -> bool:
    """Validate the bound Studio channel; preserve bool callers and safe diagnostics."""
    _record_auth_diagnostic("studio_not_ready", "session_lock")
    try:
        binding = _publisher_binding(account_file)
        expected_channel = binding.get("channel_id")
    except RuntimeError as exc:
        _record_auth_diagnostic("session_lock_invalid", "session_lock", error=exc)
        return False
    browser = None
    page = None
    stage = "browser_start"
    http_status = None
    try:
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(headless=True, channel="chrome", proxy={"server": YT_PROXY} if YT_PROXY else None)
                stage = "context_setup"
                context = await browser.new_context(storage_state=account_file)
                context = await set_init_script(context)
                page = await context.new_page()
                stage = "navigation"
                target = _studio_target(binding, STUDIO_URL)
                response = await page.goto(target, wait_until="domcontentloaded", timeout=30000)
                status = getattr(response, "status", None)
                http_status = status if type(status) is int else None
                if http_status is not None and http_status >= 400:
                    _record_auth_diagnostic("navigation_failed", stage, expected=expected_channel,
                                            page=page, http_status=http_status)
                    return False
                stage = "studio_readiness"
                reason = await _wait_for_studio(page, expected_channel)
                _record_auth_diagnostic(reason, "complete" if reason == "verified" else stage,
                                        expected=expected_channel, page=page, http_status=http_status)
                return reason == "verified"
            finally:
                if browser is not None:
                    try:
                        await browser.close()
                    except Exception:
                        pass
    except Exception as exc:
        reason = ("navigation_failed" if stage == "navigation" else "studio_not_ready"
                  if stage == "studio_readiness" else "browser_start_failed")
        _record_auth_diagnostic(reason, stage, expected=expected_channel, page=page,
                                error=exc, http_status=http_status)
        return False


async def youtube_cookie_gen(account_file, headless: bool = False):
    """交互式登录：开浏览器让用户登录 Google/YouTube，进入频道页后保存 storage_state。"""
    async with async_playwright() as playwright:
        # 登录必须显形，让用户输账号密码/二步验证
        browser = await playwright.chromium.launch(headless=False, channel="chrome", proxy={"server": YT_PROXY} if YT_PROXY else None)
        context = await browser.new_context()
        context = await set_init_script(context)
        page = await context.new_page()
        await page.goto(STUDIO_URL, wait_until="domcontentloaded")
        youtube_logger.info(_msg("🔐", "请在弹出的浏览器里登录 Google / YouTube 账号，登录后会自动保存"))
        ok = False
        for _ in range(600):  # 最多等 10 分钟
            if "/channel/" in page.url:
                await page.wait_for_timeout(2000)  # 让 cookie 落定
                ok = True
                break
            await asyncio.sleep(1)
        if ok:
            await context.storage_state(path=account_file)
            youtube_logger.success(_msg("✅", f"YouTube 登录态已保存: {account_file}"))
        else:
            youtube_logger.error(_msg("😵", "等待登录超时，未保存登录态"))
        await browser.close()
        return _build_login_result(ok, "logged_in" if ok else "timeout",
                                   "登录成功" if ok else "登录超时", account_file, page.url)


async def youtube_setup(account_file, handle: bool = False, return_detail: bool = False, headless: bool = False):
    """校验登录态，失效且 handle=True 时拉起交互式登录。"""
    if not Path(account_file).exists() or not await cookie_auth(account_file):
        if not handle:
            result = _build_login_result(False, "cookie_invalid", "登录态不存在或已失效", account_file)
            return result if return_detail else False
        youtube_logger.info(_msg("🥹", "YouTube 登录态不存在或失效，准备打开浏览器登录"))
        result = await youtube_cookie_gen(account_file, headless=headless)
        return result if return_detail else result["success"]
    result = _build_login_result(True, "cookie_valid", "登录态有效", account_file)
    return result if return_detail else True


async def _dismiss_autocomplete(page: Page):
    """关掉 # 话题 / @ 提及 自动补全下拉浮层（会挡住后续“继续/发布”按钮）。

    先 blur 失焦；若浮层仍可见再补一次 Escape——仅在检测到浮层时才按，
    避免在没有浮层时误关掉整个上传对话框。"""
    try:
        await page.evaluate("() => { const a = document.activeElement; if (a && a.blur) a.blur(); }")
    except Exception:
        pass
    try:
        dropdown = page.locator("tp-yt-iron-dropdown:visible")
        if await dropdown.count() > 0:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(200)
    except Exception:
        pass


async def _fill_editable(page: Page, selector: str, text: str):
    """填 YouTube Studio 的 contenteditable 富文本框（标题/简介），先清空再输入。

    用 fill() 一次性灌入而非逐字 type()：标题/简介里的 # 字符（如 #Shorts）会触发
    YouTube 的话题自动补全下拉浮层；逐字输入会让浮层持续跟随光标弹出、盖住输入框与
    后续“继续/发布”按钮，导致上传流程卡死。fill() 一次性写入不会逐字触发补全。"""
    box = page.locator(selector).first
    await box.wait_for(state="visible", timeout=30000)
    await box.click()
    await page.keyboard.press("Control+A")
    await page.keyboard.press("Delete")
    try:
        await box.fill(text)            # 一次性灌入，不逐字触发 # 话题自动补全
    except Exception:
        await box.type(text, delay=6)   # 个别 contenteditable 不支持 fill 时退回逐字输入
    await page.wait_for_timeout(400)
    await _dismiss_autocomplete(page)   # 收尾关掉可能弹出的补全浮层


async def _click_if_present(page: Page, selector: str, timeout: int = 4000) -> bool:
    try:
        el = page.locator(selector).first
        await el.wait_for(state="visible", timeout=timeout)
        await el.click()
        return True
    except Exception:
        return False


async def _wait_upload_complete(page: Page, max_polls: int = 360) -> bool:
    """Require visible positive completion; hidden/stale or error text cannot pass."""
    stable = 0
    completed = re.compile(
        r"(?:upload complete|upload finished|uploaded|checks complete|finished processing|processing complete|"
        r"上传完成|已上传|处理完成|检查完成)(?:\s*[:：·-]?\s*100(?:\.0+)?%)?[.!。！]?", re.I)
    unfinished = re.compile(r"uploading|processing|checks running|正在上传|上传中|处理中|检查中|正在检查", re.I)
    negated = re.compile(r"\bnot\b|\bnever\b|\bunable\b|未上传|未完成|尚未", re.I)
    percentages = re.compile(r"(\d+(?:\.\d+)?)\s*%")
    failed = re.compile(r"failed|error|incomplete|失败|出错|错误", re.I)
    for _ in range(max_polls):
        texts = []
        for selector in (".progress-label", "span.progress-label", "ytcp-video-upload-progress"):
            controls = page.locator(selector).filter(visible=True)
            for index in range(await controls.count()):
                text = (await controls.nth(index).inner_text()).strip()
                if text:
                    texts.append(text)
        if any(failed.search(text) for text in texts):
            raise RuntimeError("NEEDS_ATTENTION: YouTube 视频上传或处理报错；保留窗口，请核对原稿件")
        has_completion = any(completed.fullmatch(text) for text in texts)
        blocked = any(
            negated.search(text)
            or any(float(value) != 100 for value in percentages.findall(text))
            or (not completed.fullmatch(text) and unfinished.search(text))
            for text in texts
        )
        if has_completion and not blocked:
            stable += 1
            if stable >= 2:
                return True
        else:
            stable = 0
        await page.wait_for_timeout(5000)
    return False


async def _set_verified_thumbnail(page, path):
    """File selection alone is never thumbnail-completion evidence."""
    uploader = await unique_visible(page.locator("ytcp-thumbnail-uploader"), "YouTube 封面编辑器")
    file_input = uploader.locator("input[type='file']")
    if await file_input.count() != 1:
        raise RuntimeError("NEEDS_ATTENTION: 指定封面上传入口缺失或不唯一，停止准备")
    previous = await wait_initial_preview(uploader)
    if previous["busy"] or previous["error"]:
        raise RuntimeError("NEEDS_ATTENTION: 封面编辑器仍在处理或报错，请在保留窗口检查")
    await file_input.set_input_files(path)
    await wait_changed_preview(uploader, previous)


async def _require_selected(page, selector, label):
    control = await unique_visible(page.locator(selector), label)
    if await control.get_attribute("aria-checked") != "true":
        raise RuntimeError(f"NEEDS_ATTENTION: {label}未确认，未完成准备")


async def _fill_video_copy(page, title, description):
    await _fill_editable(page, "#title-textarea #textbox", title)
    # Empty is explicit: clear channel upload defaults as well.
    await _fill_editable(page, "#description-textarea #textbox", description)


class YouTubeVideo(BaseVideoUploader):
    def __init__(self, title, file_path, tags, account_file, *,
                 description="", thumbnail_path=None, playlist=None,
                 visibility="public", made_for_kids=False, altered_synthetic=None,
                 debug=DEBUG_MODE, headless=False, prepare_only=False):
        self.title = title
        self.file_path = str(file_path)
        self.tags = tags or []
        self.account_file = str(account_file)
        self.description = description or ""
        self.thumbnail_path = str(thumbnail_path) if thumbnail_path else None
        self.playlist = playlist
        if visibility not in VISIBILITY:
            raise ValueError("YouTube 可见范围无效；必须明确选择 public、unlisted 或 private")
        self.visibility = visibility
        self.made_for_kids = made_for_kids
        self.altered_synthetic = altered_synthetic
        self.debug = debug
        self.headless = headless
        self.prepare_only = prepare_only is True

    async def upload(self, playwright: Playwright):
        if len(",".join(self.tags)) > 500:
            raise ValueError("YouTube 标签总长度超过500字符，未截断，未开始上传")
        if self.prepare_only and (type(self.made_for_kids) is not bool or type(self.altered_synthetic) is not bool):
            raise ValueError("YouTube 手动接手必须明确儿童受众与修改／合成内容声明")
        binding = _publisher_binding(self.account_file)
        if self.prepare_only:
            browser, context = await publisher_handoff.connect_publisher_handoff(
                playwright, self.account_file, "youtube", proxy=YT_PROXY)
            # The owned, detached Chrome's default context must survive CDP
            # disconnect, including failures the user may need to inspect.
            return await self._upload_with_browser(browser, binding, context=context)
        browser = await playwright.chromium.launch(
            headless=self.headless, channel="chrome",
            proxy={"server": YT_PROXY} if YT_PROXY else None,
        )
        try:
            return await self._upload_with_browser(browser, binding)
        finally:
            await browser.close()

    async def _upload_with_browser(self, browser, binding, *, context=None):
        expected_channel = binding.get("channel_id")
        if context is None:
            context = await browser.new_context(storage_state=self.account_file)
        context = await set_init_script(context)
        page = await context.new_page()
        page.set_default_timeout(60000)

        youtube_logger.info(_msg("🎬", f"开始上传: {Path(self.file_path).name}"))
        target = _studio_target(binding, UPLOAD_URL)
        response = await page.goto(target, wait_until="domcontentloaded")
        status = getattr(response, "status", None)
        if type(status) is int and status >= 400:
            raise RuntimeError("NEEDS_ATTENTION: YouTube Studio 导航失败，未开始上传")
        if expected_channel:
            reason = await _wait_for_studio(page, expected_channel)
            if reason == "login_redirect":
                raise RuntimeError("YouTube 登录态失效，请重新执行 login")
            if reason == "channel_mismatch":
                await _require_channel(page, expected_channel)
            if reason != "verified":
                raise RuntimeError("NEEDS_ATTENTION: YouTube Studio 尚未就绪，无法确认目标频道；未开始上传")
            # Use the observed Studio control, retaining the selected brand
            # channel instead of letting /upload choose the default channel.
            await _require_channel(page, expected_channel)
            await page.get_by_role("button", name=_STUDIO_UPLOAD_NAME).first.click(timeout=20000)
        else:
            await page.wait_for_timeout(3000)
            if _is_login_redirect(page.url):
                raise RuntimeError("YouTube 登录态失效，请重新执行 login")
            if self.prepare_only:
                if await _wait_for_studio(page, None) != "verified":
                    raise RuntimeError("NEEDS_ATTENTION: 无法确认当前 YouTube Studio 频道，未开始准备")
                # Legacy independent logins have no sidecar. Pin this run to
                # the observed ready channel before attaching any material.
                expected_channel = _channel_from_url(page.url)
        # Stop before any file transfer if Studio changed channel while opening
        # the upload dialog or while waiting for its file input.
        await _require_channel(page, expected_channel)

        # 1) 选择视频文件
        file_input = page.locator('input[type="file"]').first
        await file_input.wait_for(state="attached", timeout=60000)
        await _require_channel(page, expected_channel)
        emit_progress("youtube", "uploading")
        await file_input.set_input_files(self.file_path)

        # 2) 等详情对话框
        await page.locator("#title-textarea").wait_for(state="visible", timeout=120000)

        # 3) 标题
        emit_progress("youtube", "metadata")
        youtube_logger.info(_msg("✍️", "填写标题"))
        await _fill_video_copy(page, self.title, self.description)

        # Local publisher: requested covers are required, never silently skipped.
        if self.thumbnail_path:
            emit_progress("youtube", "cover")
            if not Path(self.thumbnail_path).is_file():
                raise RuntimeError("指定封面文件已不存在，停止准备或提交")
            try:
                await _set_verified_thumbnail(page, self.thumbnail_path)
                youtube_logger.info(_msg("🖼️", "指定封面预览已更新并稳定"))
            except Exception as exc:
                raise RuntimeError("NEEDS_ATTENTION: 封面上传失败或预览未确认，未完成准备；请在保留窗口检查") from exc

        # 6) 加入播放列表（连载/系列追更）。弹窗务必关闭，否则挡住后续步骤。
        if self.playlist:
            try:
                await _click_if_present(
                    page, "#basics ytcp-text-dropdown-trigger, ytcp-video-metadata-playlists ytcp-dropdown-trigger", 8000)
                await page.wait_for_timeout(1200)
                existing = page.locator(
                    f"tp-yt-paper-checkbox:has-text('{self.playlist}'), "
                    f"ytcp-checkbox-group:has-text('{self.playlist}')").first
                if await existing.count():
                    await existing.click()
                else:
                    if await _click_if_present(page, "ytcp-button:has-text('New playlist'), ytcp-button:has-text('创建播放列表')", 4000):
                        await page.wait_for_timeout(800)
                        await _click_if_present(page, "tp-yt-paper-item:has-text('New playlist'), tp-yt-paper-item:has-text('新建播放列表')", 3000)
                        title_box = page.locator("ytcp-playlist-metadata-editor #textbox, #create-playlist-form #textbox").first
                        if await title_box.count():
                            await title_box.click()
                            await title_box.type(self.playlist, delay=6)
                            await _click_if_present(page, "ytcp-button#create-button, tp-yt-paper-dialog ytcp-button:has-text('Create'), tp-yt-paper-dialog ytcp-button:has-text('创建')", 4000)
            except Exception as exc:
                youtube_logger.warning(_msg("⚠️", f"播放列表处理跳过（不影响发布）: {exc}"))
            finally:
                await _click_if_present(page, "ytcp-playlist-dialog #save-button, ytcp-playlist-dialog ytcp-button:has-text('Done'), ytcp-playlist-dialog ytcp-button:has-text('完成')", 3000)
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(600)

        # Explicit user choice; never assume that a video is not made for kids.
        audience = "VIDEO_MADE_FOR_KIDS_MFK" if self.made_for_kids else "VIDEO_MADE_FOR_KIDS_NOT_MFK"
        if not await _click_if_present(page, f"tp-yt-paper-radio-button[name='{audience}']", 10000):
            raise RuntimeError("无法确认儿童受众选项，停止提交")
        if self.prepare_only and await page.locator(f"tp-yt-paper-radio-button[name='{audience}']").first.get_attribute("aria-checked") != "true":
            raise RuntimeError("NEEDS_ATTENTION: 儿童受众选项未确认，未完成准备")

        # Studio's disclosure is about realistic altered/synthetic content.
        # It is deliberately separate from the generic ai_generated choice.
        if self.altered_synthetic is not None:
            await _click_if_present(page, "#toggle-button", 6000)
            choice = "YES" if self.altered_synthetic else "NO"
            radio = page.locator(
                f"tp-yt-paper-radio-button[name='VIDEO_HAS_SYNTHETIC_CONTENT_{choice}'], "
                f"tp-yt-paper-radio-button[name='VIDEO_HAS_SYNTHETIC_MEDIA_{choice}']"
            ).first
            try:
                await radio.wait_for(state="visible", timeout=8000)
                await radio.click()
                if await radio.get_attribute("aria-checked") != "true":
                    raise RuntimeError("合成内容选项未选中")
            except Exception as exc:
                raise RuntimeError("NEEDS_ATTENTION: 无法确认合成内容声明，停止提交，请在 Studio 核对") from exc

        # 8) 标签（“显示更多”里）
        if self.tags:
            try:
                tag_input = page.locator("#tags-container #text-input, ytcp-form-input-container#tags-container input").first
                if not await tag_input.count() or not await tag_input.is_visible():
                    await _click_if_present(page, "#toggle-button", 6000)
                    await page.wait_for_timeout(800)
                await tag_input.click()
                if len(",".join(self.tags)) > 500:
                    raise ValueError("标签超过500字符，拒绝截断")
                await tag_input.type(",".join(self.tags) + ",", delay=4)
            except Exception as exc:
                raise RuntimeError("标签填写失败，停止提交") from exc

        # 9) 连点 Next 到“可见性”步骤
        for _ in range(5):
            vis = page.locator("tp-yt-paper-radio-button[name='PUBLIC']")
            if await vis.count() and await vis.first.is_visible():
                break
            if not await _click_if_present(page, "#next-button", 6000):
                await page.wait_for_timeout(1200)
            await page.wait_for_timeout(1000)

        # 10) 可见性
        youtube_logger.info(_msg("🌐", f"设置可见性 = {self.visibility}"))
        if not await _click_if_present(page, f"tp-yt-paper-radio-button[name='{VISIBILITY[self.visibility]}']", 10000):
            raise RuntimeError("无法设置用户选择的可见性，停止提交")
        if self.prepare_only and await page.locator(f"tp-yt-paper-radio-button[name='{VISIBILITY[self.visibility]}']").first.get_attribute("aria-checked") != "true":
            raise RuntimeError("NEEDS_ATTENTION: 用户选择的可见性未确认，未完成准备")

        # 10.5) 关键：等上传真正传完再发布。浏览器上传靠窗口开着传，
        #       传到一半就点发布+关浏览器 = 上传被掐断卡在中途（如 76%）。
        youtube_logger.info(_msg("📤", "等待上传完成（传完才发布）…"))
        if not await _wait_upload_complete(page):
            raise RuntimeError("NEEDS_ATTENTION: 等待视频上传完成超时，未继续提交")

        # 11) 发布
        await page.wait_for_timeout(1200)
        await _require_channel(page, expected_channel)
        if self.prepare_only:
            done = page.locator("#done-button").first
            await done.wait_for(state="visible", timeout=15000)
            if (not await done.is_visible() or not await done.is_enabled()
                    or await done.get_attribute("aria-disabled") == "true"
                    or await done.get_attribute("disabled") is not None):
                raise RuntimeError("NEEDS_ATTENTION: 最终提交按钮尚不可用，未完成准备；请检查 Studio 页面")
            await _require_channel(page, expected_channel)
            await _require_selected(page, f"tp-yt-paper-radio-button[name='{VISIBILITY[self.visibility]}']", "用户选择的可见性")
            emit_progress("youtube", "ready_to_submit")
            youtube_logger.info("素材与设置已准备好，保留 YouTube Studio 窗口等待你最终发布")
            return "prepared"
        emit_progress("youtube", "ready_to_submit")
        # Persist the irreversible boundary before attempting the final click.
        emit_progress("youtube", "submitting")
        if not await _click_if_present(page, "#done-button", 15000):
            raise RuntimeError("NEEDS_ATTENTION: 未能点击发布按钮，请到 Studio 检查草稿")
        else:
            await page.wait_for_timeout(4000)
            try:
                await page.locator("ytcp-video-share-dialog").wait_for(state="visible", timeout=20000)
            except Exception as exc:
                raise RuntimeError("NEEDS_ATTENTION: 已点击提交但未观察到完成对话框，请到 Studio 核验，勿直接重发") from exc
            emit_progress("youtube", "submitted")
            video_url = ""
            try:
                link = page.locator("a[href*='youtu.be'], a[href*='watch?v=']").first
                if await link.count():
                    video_url = await link.get_attribute("href") or ""
            except Exception:
                pass
            await _click_if_present(page, "ytcp-button:has-text('Close'), ytcp-button:has-text('关闭'), #close-button", 8000)
            youtube_logger.success(_msg("🥳", f"发布完成（{self.visibility}）{(' ' + video_url) if video_url else ''}"))
            if video_url:
                print("PUBLISHER_OBSERVED_URL=" + video_url, flush=True)

        # 刷新 cookie
        try:
            if not expected_channel:
                await context.storage_state(path=self.account_file)
        except Exception:
            pass
        await page.wait_for_timeout(2000)

    async def main(self):
        async with async_playwright() as playwright:
            return await self.upload(playwright)
