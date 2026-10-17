# -*- coding: utf-8 -*-
from datetime import datetime

import asyncio
import inspect
import os
import sys
import re
import time
import hashlib
import io
from PIL import Image
from urllib.parse import urlsplit
from pathlib import Path

from patchright.async_api import Page
from patchright.async_api import Playwright
from patchright.async_api import async_playwright
from patchright.async_api import TimeoutError as BrowserTimeoutError

from conf import BASE_DIR, DEBUG_MODE, LOCAL_CHROME_HEADLESS, LOCAL_CHROME_PATH
from uploader.base_video import BaseVideoUploader
from utils.base_social_media import set_init_script
from utils.login_qrcode import build_login_qrcode_path
from utils.login_qrcode import decode_qrcode_from_path
from utils.login_qrcode import print_terminal_qrcode
from utils.login_qrcode import remove_qrcode_file
from utils.login_qrcode import save_data_url_image
from utils.log import douyin_logger
from utils.publisher_handoff import connect_douyin_handoff
from utils.publisher_progress import emit_progress, record_diagnostic, get_diagnostic
from utils.publisher_materials import preview_state, wait_initial_preview, wait_changed_preview, unique_visible, enabled_control

DOUYIN_PUBLISH_STRATEGY_IMMEDIATE = "immediate"
DOUYIN_PUBLISH_STRATEGY_SCHEDULED = "scheduled"


def _msg(emoji: str, text: str) -> str:
    return f"{emoji} {text}"


async def _wait_saved_cover(page, area, previous=None, timeout_seconds=15):
    """Check the visible thumbnail, including CDN images that taint canvas.

    Only this image is captured, in memory, with the pointer outside the cover
    so hover chrome cannot count as a changed picture. No screenshot is logged.
    """
    image = await unique_visible(area.locator('img'), '已保存的封面缩略图')
    await page.mouse.move(0, 0)
    deadline = time.monotonic() + timeout_seconds
    last, stable = None, 0
    while time.monotonic() < deadline:
        loaded = await image.evaluate('(img) => img.complete && img.naturalWidth > 0 && img.naturalHeight > 0')
        if loaded:
            screenshot = await image.screenshot(type='png', timeout=10000)
            with Image.open(io.BytesIO(screenshot)) as frame:
                signature = hashlib.sha256(frame.convert('RGB').resize((128,128)).tobytes()).hexdigest()
            stable = stable + 1 if signature == last else 1
            last = signature
            if stable >= 3 and (previous is None or signature != previous):
                return signature
        else:
            stable, last = 0, None
        await asyncio.sleep(.5)
    raise RuntimeError('封面缩略图未稳定更新，未确认保存；请保留编辑页检查')


async def _upload_cover_slot(page, dialog, file_input, label, path):
    try:
        # The footer also has a "设置竖封面" button. Only the header steps switch
        # the active editing orientation; never select the footer by text.
        tab = await unique_visible(dialog.locator('[class^="steps-"] [class^="step-"]').filter(has_text=label), "封面方向选项")
        await tab.click(timeout=3000)
        active = await tab.evaluate(r"""el => {
            for (let p = el, depth = 0; p && depth < 4; p = p.parentElement, depth++) {
                if (p.getAttribute('aria-selected') === 'true' || p.getAttribute('aria-checked') === 'true' ||
                    p.getAttribute('data-state') === 'active' ||
                    /(?:^|[-_\s])(?:active|selected|checked)(?:$|[-_\s])/.test(p.className || '')) return true;
            }
            return false;
        }""")
        if active is not True:
            raise RuntimeError("封面方向未选中")
    except Exception as exc:
        raise RuntimeError(f"无法确认切换到{label}，已停止提交") from exc
    if await file_input.count() != 1:
        raise RuntimeError("NEEDS_ATTENTION: 封面上传入口缺失或不唯一；未使用其他图片输入")
    # Fabric's upper canvas is a transparent interaction overlay, not a second
    # image. Scope evidence to the one visible editor, excluding AI suggestions
    # and the small preview, and sample only its actual drawing layer.
    editor = await unique_visible(dialog.locator('[class^="cloudArea-"]'), '封面主画布')
    previous = await wait_initial_preview(editor, selector='canvas.lower-canvas')
    if previous["busy"] or previous["error"]:
        raise RuntimeError("NEEDS_ATTENTION: 封面编辑器仍在处理或报错，请保留窗口检查")
    await file_input.set_input_files(path)
    await wait_changed_preview(editor, previous, selector='canvas.lower-canvas')


def _require_editor(page):
    if page.is_closed():
        raise RuntimeError("NEEDS_ATTENTION: 抖音编辑窗口已关闭，未完成准备；请核对平台草稿，勿直接重传")
    url = urlsplit(page.url)
    if (url.scheme != "https" or url.netloc != "creator.douyin.com" or
            url.path not in {"/creator-micro/content/publish", "/creator-micro/content/post/video"}):
        raise RuntimeError("NEEDS_ATTENTION: 当前页面不是抖音视频编辑页，未完成准备；请核对登录与原稿件")


async def _upload_state(page):
    """Visible positive evidence plus absence of active upload or error."""
    return await page.evaluate(r"""() => {
        const visible = el => {
            if (!el.getClientRects().length) return false;
            for (let p = el; p; p = p.parentElement) {
                const s = getComputedStyle(p);
                if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
            }
            return true;
        };
        const leaves = [...document.querySelectorAll('body *')].filter(el =>
            !el.children.length && !el.closest('[contenteditable="true"], textarea, [class*="postAssistant-"]') &&
            visible(el) && el.textContent.trim().length < 120);
        const texts = leaves.map(el => el.textContent.trim());
        const status = [...document.querySelectorAll('.progress-div, [class*="upload-progress"], '
            + '[class*="video-progress"], [role="alert"]')].filter(visible);
        const error = texts.some(t => /上传失败|处理失败|转码失败|视频文件异常/.test(t));
        const processing = texts.some(t => /^(?:视频)?(?:正在处理|处理中|转码中)|^视频检测中/.test(t));
        const uploading = texts.some(t => /^(?:视频)?(?:上传中|正在上传)/.test(t));
        const percentages = texts.filter(t => /^(?:上传中\s*)?\d{1,3}(?:\.\d+)?\s*%$/.test(t))
            .map(t => Number(t.match(/(\d+(?:\.\d+)?)\s*%/)[1])).filter(n => n >= 0 && n <= 100);
        const percent = percentages.length === 1 ? percentages[0] : null;
        const busy = uploading || processing || (percent !== null && percent < 100) ||
            [...document.querySelectorAll('[role="progressbar"], [aria-busy="true"]')]
                .some(el => !el.closest('[class*="postAssistant-"]') && visible(el));
        const readyMedia = [...document.querySelectorAll('video')].some(v => visible(v) &&
            v.readyState >= 1 && Number.isFinite(v.duration) && v.duration > 0 && !v.error);
        const replace = texts.some(t => /^(?:重新上传|更换视频)$/.test(t));
        const success = texts.some(t => /^(?:视频)?上传(?:成功|完成)$/.test(t));
        const completed = !error && !busy && (success || (readyMedia && replace));
        const empty = !readyMedia && !replace && !success && !busy && texts.some(t => /^点击上传/.test(t));
        return {completed, error, busy, processing, percent, empty, selected:replace};
    }""")


async def _wait_douyin_upload(page, max_polls=900):
    stable = empty_samples = 0
    phase, last_observation = 'uploading', None
    last_report = 0
    try:
        for _ in range(max_polls):
            _require_editor(page)
            state = await _upload_state(page)
            if state.get("error") is True:
                raise RuntimeError("NEEDS_ATTENTION: 抖音视频上传或处理失败；保留编辑窗口，未自动重新上传")
            if state.get('processing') is True:
                phase = 'processing'
            percent = state.get('percent')
            if type(percent) not in (int, float) or not 0 <= percent <= 100:
                percent = None
            observation = (phase, percent)
            if observation != last_observation or time.monotonic() - last_report >= 10:
                emit_progress('douyin', phase, **({'percent': percent} if percent is not None else {}))
                last_observation, last_report = observation, time.monotonic()
            empty_samples = empty_samples + 1 if state.get('empty') is True else 0
            if empty_samples >= 15:
                raise RuntimeError("NEEDS_ATTENTION: 抖音已回到空的视频上传区域，未继续等待或自动重传，请检查原页面")
            if state.get("completed") is True and state.get("busy") is False:
                stable += 1
                if stable >= 2:
                    return
            else:
                stable = 0
            await page.wait_for_timeout(2000)
        raise RuntimeError("NEEDS_ATTENTION: 抖音视频上传或处理超时，保留编辑窗口；未自动重传")
    except Exception as exc:
        record_diagnostic('douyin', 'processing' if phase == 'processing' else 'upload', exc)
        raise


async def _require_douyin_handoff(page, *, video_confirmed=False):
    _require_editor(page)
    if await page.locator('input[placeholder*="验证码"]:visible, input[type="tel"]:visible').count():
        raise RuntimeError("NEEDS_ATTENTION: 抖音要求人工身份验证，未完成准备；请在保留窗口处理")
    if await page.locator('.semi-modal-content:visible, .dy-creator-content-modal:visible, [role="dialog"]:visible').count():
        raise RuntimeError("NEEDS_ATTENTION: 抖音编辑窗口仍有未完成弹窗，未完成准备")
    state = await _upload_state(page)
    # Switching to cover/title preview unmounts the video player. A transfer
    # already confirmed in this run stays valid if its replace-video control
    # remains and no new transfer, empty slot, or platform error has appeared.
    ready = state.get('completed') is True or (video_confirmed and state.get('selected') is True and state.get('empty') is False)
    if not ready or state.get("busy") is not False or state.get("error") is not False:
        raise RuntimeError("NEEDS_ATTENTION: 抖音素材仍未就绪，未完成准备；请保留窗口检查")
    publish = await unique_visible(page.get_by_role("button", name="发布", exact=True), "抖音最终发布按钮")
    if not await enabled_control(publish):
        raise RuntimeError("NEEDS_ATTENTION: 抖音最终发布按钮不可用，未完成准备；请保留窗口检查")
    _require_editor(page)


async def _read_verify_code(code_file: str) -> str:
    if os.path.exists(code_file):
        with open(code_file, encoding="utf-8") as file_obj:
            return file_obj.read().strip()

    if not sys.stdin or not sys.stdin.isatty():
        return ""

    try:
        return (await asyncio.to_thread(input, "请输入抖音短信验证码（直接回车可稍后重试）: ")).strip()
    except (EOFError, OSError):
        return ""


def _msg(emoji: str, text: str) -> str:
    return f"{emoji} {text}"


async def _native_click(page, locator) -> bool:
    """对元素做"真人级"点击：真实鼠标点中心 + 派发完整 pointer/mouse 事件序列。
    抖音身份验证组件（uc_verification_component）只认这整套事件，不认单纯 click。
    返回是否点击成功。"""
    try:
        await locator.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass
    try:
        box = await locator.bounding_box()
    except Exception:
        box = None
    if not box:
        try:
            await locator.click(timeout=8000)
            return True
        except Exception:
            return False
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    try:
        await page.mouse.move(x, y)
        await asyncio.sleep(0.15)
        await page.mouse.click(x, y)
        await asyncio.sleep(0.2)
        await page.evaluate(
            """({x, y}) => {
                const el = document.elementFromPoint(x, y);
                if (!el) return;
                const opts = {bubbles:true,cancelable:true,composed:true,clientX:x,clientY:y,view:window,pointerId:1,pointerType:'mouse',isPrimary:true,button:0,buttons:1};
                for (const t of ['pointerover','pointerenter','pointerdown','mousedown','pointerup','mouseup','click']) {
                    const C = t.startsWith('pointer') ? PointerEvent : MouseEvent;
                    try { el.dispatchEvent(new C(t, opts)); } catch(e){ try{ el.dispatchEvent(new MouseEvent(t,opts)); }catch(_){} }
                }
            }""",
            {"x": x, "y": y},
        )
        return True
    except Exception:
        return False


async def _emit_qrcode_callback(qrcode_callback, payload: dict):
    if not qrcode_callback:
        return

    callback_result = qrcode_callback(payload)
    if inspect.isawaitable(callback_result):
        await callback_result


def _build_login_result(success: bool, status: str, message: str, account_file: str, qrcode: dict | None = None, current_url: str = "") -> dict:
    return {
        "success": success,
        "status": status,
        "message": message,
        "account_file": str(account_file),
        "qrcode": qrcode,
        "current_url": current_url,
    }


async def cookie_auth(account_file):
    if not os.path.exists(account_file):
        return False

    use_headless = os.environ.get("DOUYIN_COOKIE_AUTH_HEADLESS", "true").lower() in ("1", "true", "yes")
    launch_kwargs = {"headless": use_headless, "channel": "chromium", "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"]}
    for _attempt in range(3):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**launch_kwargs)
            try:
                context = await browser.new_context(storage_state=account_file)
                context = await set_init_script(context)
                page = await context.new_page()
                await page.goto("https://creator.douyin.com/creator-micro/content/upload", wait_until="domcontentloaded", timeout=90000)
                await page.wait_for_timeout(2500)  # 等页面稳定，避免瞬时跳转误判
                has_login = await page.get_by_text("手机号登录").count() or await page.get_by_text("扫码登录").count()
                if "content/upload" in page.url and not has_login:
                    # The upload URL alone also appears on a blank/loading page.
                    await page.locator('input[type="file"][accept*="video"]').wait_for(state="visible", timeout=15000)
                    return True
            except Exception:
                pass
            finally:
                await browser.close()
    return False


async def douyin_setup(account_file, handle=False, return_detail=False, qrcode_callback=None, headless: bool = LOCAL_CHROME_HEADLESS, cdp_url: str | None = None):
    if not os.path.exists(account_file) or not await cookie_auth(account_file):
        if not handle:
            result = _build_login_result(False, "cookie_invalid", "cookie文件不存在或已失效", account_file)
            return result if return_detail else False
        douyin_logger.info(_msg("🥹", "cookie 失效了，准备打开浏览器重新登录"))
        result = await douyin_cookie_gen(account_file, qrcode_callback=qrcode_callback, headless=headless, cdp_url=cdp_url)
        return result if return_detail else result["success"]

    result = _build_login_result(True, "cookie_valid", "cookie有效", account_file)
    return result if return_detail else True


async def _extract_douyin_qrcode_src(page: Page) -> str:
    # 等 SPA 加载完成（不只等"扫码登录"文字，否则抖音慢加载时 30s 就超时）。
    # 给 domcontentloaded 后足够时间让客户端 JS 注入登录卡。
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    scan_login_tab = page.get_by_text("扫码登录", exact=True).first
    # attached 状态：DOM 里出现即可，不要求 visible/渲染完整，避免 race
    await scan_login_tab.wait_for(state="attached", timeout=60000)

    # 新版抖音创作者中心 (single_tab + animate_qrcode_container) 不再用 aria-label="二维码"。
    # 按优先级兜底多个 selector，至少一个能命中即可。
    qrcode_selectors = [
        'div#animate_qrcode_container img[src^="data:image"]',
        'div[class*="animate_qrcode_container"] img[src^="data:image"]',
        'div[class*="scan_qrcode_login_content"] img[src^="data:image"]',
        'img[aria-label="二维码"]',
    ]
    last_err: Exception | None = None
    for sel in qrcode_selectors:
        qrcode_img = page.locator(sel).first
        try:
            await qrcode_img.wait_for(state="attached", timeout=10000)
        except Exception as e:
            last_err = e
            continue
        src = await qrcode_img.get_attribute("src")
        if src:
            return src
        last_err = RuntimeError(f"selector {sel} 命中但 src 为空")

    raise RuntimeError(f"未获取到抖音登录二维码地址 (last_err={last_err})")


async def _save_douyin_qrcode(page: Page, account_file: str, previous_qrcode_path: Path | None = None, qrcode_callback=None) -> dict:
    # 提取二维码 src 仅为了保存/终端显示；定位不到时不致命——有头浏览器里二维码可见，直接扫码即可
    try:
        qrcode_src = await _extract_douyin_qrcode_src(page)
    except Exception as exc:
        douyin_logger.warning(_msg("😵", f"没定位到二维码元素（{str(exc)[:50]}）——请直接在弹出的浏览器里扫码，小人继续等登录跳转"))
        return {"image_path": "", "image_data_url": ""}
    qrcode_path = save_data_url_image(qrcode_src, build_login_qrcode_path(account_file))
    if previous_qrcode_path and previous_qrcode_path != qrcode_path:
        if remove_qrcode_file(previous_qrcode_path):
            douyin_logger.info(_msg("🧹", f"临时二维码文件已清理: {previous_qrcode_path}"))
    douyin_logger.info(_msg("🖼️", f"二维码已经准备好啦，已保存到: {qrcode_path}"))
    qrcode_content = decode_qrcode_from_path(qrcode_path)
    if qrcode_content:
        print_terminal_qrcode(qrcode_content, qrcode_path, "抖音APP")
    else:
        douyin_logger.warning(_msg("😵", f"终端没法完整显示二维码，请打开 {qrcode_path} 扫码"))
    qrcode_info = {
        "image_path": str(qrcode_path),
        "image_data_url": qrcode_src,
    }
    await _emit_qrcode_callback(qrcode_callback, qrcode_info)
    return qrcode_info


async def _is_douyin_login_completed(page: Page) -> bool:
    # 登录后会跳到 creator-micro 下任意页（home/content 等）；登录页是 creator.douyin.com/ 根路径
    if "creator.douyin.com/creator-micro" not in page.url:
        return False

    login_markers = [
        page.get_by_text("扫码登录", exact=True).first,
        page.get_by_text("手机号登录", exact=True).first,
        page.get_by_text("二维码失效", exact=True).first,
        page.get_by_role("img", name="二维码").first,
    ]

    for marker in login_markers:
        if not await marker.count():
            continue
        try:
            if await marker.is_visible():
                return False
        except Exception:
            continue

    return True


async def _wait_for_douyin_login(page: Page, account_file: str, qrcode_info: dict, qrcode_callback=None, poll_interval: int = 3, max_checks: int = 100) -> dict:
    qrcode_path = Path(qrcode_info["image_path"]) if qrcode_info.get("image_path") else None
    original_url = page.url
    saw_2fa = False
    for _ in range(max_checks):
        if await _is_douyin_login_completed(page):
            douyin_logger.info(_msg("🥳", f"扫码成功，已经跳转到登录后页面: {page.url}"))
            return _build_login_result(True, "success", "抖音扫码登录成功", account_file, qrcode_info, page.url)

        # URL 变化 + sessionid 未到位 → 二验流程，继续等
        if page.url != original_url and not await _is_douyin_login_completed(page):
            sms_input = page.locator('input[placeholder*="验证码"], input[type="tel"], input[placeholder*="短信"], input[placeholder*="手机号"]')
            if await sms_input.count() > 0:
                if not saw_2fa:
                    douyin_logger.warning(_msg("⚠️", f"检测到抖音短信/安全二次验证，请在弹出的浏览器中手动输入。等待 sessionid ({_}/{max_checks})"))
                    saw_2fa = True
            await asyncio.sleep(poll_interval)
            continue

        expired_box = page.get_by_text("二维码失效", exact=True).locator("..").first
        if await expired_box.count() and await expired_box.is_visible():
            douyin_logger.warning(_msg("😵", "二维码失效了，小人马上去刷新"))
            await expired_box.click()
            await asyncio.sleep(1)
            qrcode_info = await _save_douyin_qrcode(page, account_file, qrcode_path, qrcode_callback=qrcode_callback)
            qrcode_path = Path(qrcode_info["image_path"]) if qrcode_info.get("image_path") else None

        await asyncio.sleep(poll_interval)

    return _build_login_result(False, "timeout", "等待抖音扫码登录超时", account_file, qrcode_info, page.url)

async def douyin_cookie_gen(
    account_file,
    qrcode_callback=None,
    poll_interval: int = 2,
    max_checks: int = 60,
    headless: bool = LOCAL_CHROME_HEADLESS,
    cdp_url: str | None = None,
):
    async with async_playwright() as playwright:
        if cdp_url:
            browser = await playwright.chromium.connect_over_cdp(cdp_url)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            should_close_context = False
        else:
            browser = await playwright.chromium.launch(headless=headless, channel="chromium")
            context = await browser.new_context()
            should_close_context = True
        context = await set_init_script(context)
        qrcode_path = None
        result = _build_login_result(False, "failed", "抖音登录失败", account_file)
        try:
            page = await context.new_page()
            await page.goto("https://creator.douyin.com/")
            qrcode_info = await _save_douyin_qrcode(page, account_file, qrcode_callback=qrcode_callback)
            qrcode_path = Path(qrcode_info["image_path"]) if qrcode_info.get("image_path") else None
            douyin_logger.info(_msg("🧍", "请扫码，小人正在耐心等待登录完成"))
            result = await _wait_for_douyin_login(
                page,
                account_file,
                qrcode_info,
                qrcode_callback=qrcode_callback,
                poll_interval=poll_interval,
                max_checks=max_checks,
            )
            if result["success"]:
                await asyncio.sleep(2)
                await context.storage_state(path=account_file)
                # 登录已通过"发布视频"确认成功、storage_state 刚从已登录浏览器抓下来，
                # 不再用 flaky 的浏览器重检（那正是导致成功被误判为失败的老 bug）。
                # 只轻量确认文件里有 sessionid。
                try:
                    import json as _json
                    _d = _json.load(open(account_file))
                    _has_sess = any(c.get("name") == "sessionid" and c.get("value") for c in _d.get("cookies", []))
                    if not _has_sess:
                        result = _build_login_result(
                            False,
                            "cookie_invalid",
                            "抖音扫码流程结束，但 cookie 中无 sessionid",
                            account_file,
                            qrcode_info,
                            page.url,
                        )
                except Exception as _e:
                    douyin_logger.warning(_msg("⚠️", f"cookie 文件校验异常（忽略，按成功处理）: {_e}"))
        except Exception as exc:
            result = _build_login_result(False, "failed", str(exc), account_file, current_url=page.url if "page" in locals() else "")
        finally:
            if remove_qrcode_file(qrcode_path):
                douyin_logger.info(_msg("🧹", f"临时二维码文件已清理: {qrcode_path}"))
            if not result["success"]:
                douyin_logger.error(_msg("😢", f"登录失败: {result['message']}"))
            if should_close_context:
                await context.close()
            await browser.close()
        return result


class DouYinBaseUploader(BaseVideoUploader):
    def __init__(
        self,
        publish_date: datetime | int,
        account_file,
        publish_strategy: str = DOUYIN_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
    ):
        self.publish_date = publish_date
        self.account_file = account_file
        self.publish_strategy = publish_strategy
        self.debug = debug
        self.date_format = "%Y年%m月%d日 %H:%M"
        self.local_executable_path = LOCAL_CHROME_PATH
        self.headless = headless

    async def validate_base_args(self):
        from utils.publisher_handoff import native_chrome_endpoint
        native = bool(getattr(self, "prepare_only", False) and native_chrome_endpoint("douyin"))
        if not native and not os.path.exists(self.account_file):
            raise RuntimeError(f"cookie文件不存在，请先完成抖音登录: {self.account_file}")
        if not native and not await cookie_auth(self.account_file):
            raise RuntimeError(f"cookie文件已失效，请先完成抖音登录: {self.account_file}")
        if self.publish_strategy not in {DOUYIN_PUBLISH_STRATEGY_IMMEDIATE, DOUYIN_PUBLISH_STRATEGY_SCHEDULED}:
            raise ValueError(f"不支持的发布策略: {self.publish_strategy}")

        if self.publish_strategy == DOUYIN_PUBLISH_STRATEGY_SCHEDULED:
            self.publish_date = self.validate_publish_date(self.publish_date)
        else:
            self.publish_date = 0

    async def set_schedule_time_douyin(self, page, publish_date):
        label_element = page.locator("[class^='radio']:has-text('定时发布')")
        await label_element.click()
        await asyncio.sleep(1)
        publish_date_hour = publish_date.strftime("%Y-%m-%d %H:%M")

        await asyncio.sleep(1)
        await page.locator('.semi-input[placeholder="日期和时间"]').click()
        await page.keyboard.press("Control+KeyA")
        await page.keyboard.type(str(publish_date_hour))
        await page.keyboard.press("Enter")
        await asyncio.sleep(1)

    async def fill_title_and_description(self, page: Page, title: str, description: str, tags: list[str] | None = None):
        # 2026-06 抖音发布页 DOM：标题=input[placeholder*=填写作品标题]，描述=div.zone-container[contenteditable]
        # version_2(post/video) 发布页要等视频上传完才渲染表单（实测约 40s），故等待超时给到 120s
        title_input = page.locator('input[placeholder*="填写作品标题"]').first
        await title_input.wait_for(state="visible", timeout=120000)
        await title_input.fill(title[:30])

        description_editor = page.locator('div.zone-container[contenteditable="true"]').first
        await description_editor.wait_for(state="visible", timeout=120000)
        # Replace the editor content without platform-specific Select All keys.
        await description_editor.fill(description or "")

        for tag in tags or []:
            await page.keyboard.type(" #" + tag)
            await page.keyboard.press("Space")
        await page.keyboard.press("Escape")  # 收起话题下拉，避免浮层拦截后续点击

    async def set_location(self, page: Page, location: str = ""):
        if not location:
            return
        await page.locator('div.semi-select span:has-text("输入地理位置")').click()
        await page.keyboard.press("Backspace")
        await page.wait_for_timeout(2000)
        await page.keyboard.type(location)
        await page.wait_for_selector('div[role="listbox"] [role="option"]', timeout=5000)
        await page.locator('div[role="listbox"] [role="option"]').first.click()

    async def handle_product_dialog(self, page: Page, product_title: str):
        await page.wait_for_timeout(2000)
        await page.wait_for_selector('input[placeholder="请输入商品短标题"]', timeout=10000)
        short_title_input = page.locator('input[placeholder="请输入商品短标题"]')
        if not await short_title_input.count():
            douyin_logger.error(_msg("😵", "没找到商品短标题输入框"))
            return False

        product_title = product_title[:10]
        await short_title_input.fill(product_title)
        await page.wait_for_timeout(1000)

        finish_button = page.locator('button:has-text("完成编辑")')
        if "disabled" not in await finish_button.get_attribute("class"):
            await finish_button.click()
            douyin_logger.debug(_msg("🥳", "已点击“完成编辑”按钮"))
            await page.wait_for_selector(".semi-modal-content", state="hidden", timeout=5000)
            return True

        douyin_logger.error(_msg("😵", "“完成编辑”按钮是灰的，小人先把弹窗关掉"))
        cancel_button = page.locator('button:has-text("取消")')
        if await cancel_button.count():
            await cancel_button.click()
        else:
            close_button = page.locator(".semi-modal-close")
            await close_button.click()
        await page.wait_for_selector(".semi-modal-content", state="hidden", timeout=5000)
        return False

    async def set_product_link(self, page: Page, product_link: str, product_title: str):
        await page.wait_for_timeout(2000)
        try:
            await page.wait_for_selector("text=添加标签", timeout=10000)
            dropdown = page.get_by_text("添加标签").locator("..").locator("..").locator("..").locator(".semi-select").first
            if not await dropdown.count():
                douyin_logger.error(_msg("😵", "没找到标签下拉框"))
                return False
            douyin_logger.debug(_msg("🧍", "找到标签下拉框，小人准备选择“购物车”"))
            await dropdown.click()
            await page.wait_for_selector('[role="listbox"]', timeout=5000)
            await page.locator('[role="option"]:has-text("购物车")').click()
            douyin_logger.debug(_msg("🥳", "已经选中“购物车”"))

            await page.wait_for_selector('input[placeholder="粘贴商品链接"]', timeout=5000)
            input_field = page.locator('input[placeholder="粘贴商品链接"]')
            await input_field.fill(product_link)
            douyin_logger.debug(_msg("🔗", f"商品链接已经填好了: {product_link}"))

            add_button = page.locator('span:has-text("添加链接")')
            button_class = await add_button.get_attribute("class")
            if "disable" in button_class:
                douyin_logger.error(_msg("😵", "“添加链接”按钮现在点不了"))
                return False
            await add_button.click()
            douyin_logger.debug(_msg("🥳", "已点击“添加链接”按钮"))

            await page.wait_for_timeout(2000)
            error_modal = page.locator("text=未搜索到对应商品")
            if await error_modal.count():
                confirm_button = page.locator('button:has-text("确定")')
                await confirm_button.click()
                douyin_logger.error(_msg("😢", "这个商品链接无效"))
                return False

            if not await self.handle_product_dialog(page, product_title):
                return False

            douyin_logger.debug(_msg("🥳", "商品链接设置好了"))
            return True
        except Exception as e:
            douyin_logger.error(_msg("😢", f"设置商品链接时出错: {str(e)}"))
            return False

    async def set_self_declaration(self, page: Page, declaration: str) -> bool:
        """抖音「自主声明」：打开声明弹窗 → 单选声明类型 → 确定。

        真实弹窗（用户 F12 实测）：header「请选择声明类型（单选）」，选项为
        label.semi-radio 内 span.semi-radio-addon 文本，「内容由AI生成」与
        「内容为转载信息」「内容为个人观点或见解」等并列；底部 footer 的
        semi-button-primary =「确定」。

        入口/弹窗异步渲染；且填完话题后残留的 mention-wrapper/semi-portal 浮层会盖住入口，
        必须先清浮层再点。失败返回 False。

        Args:
            declaration: 声明类型文本（调用方显式传入）
        """
        try:
            # 清掉会遮挡入口的浮层（话题下拉/引导层），并让输入框失焦
            await self._clear_blocking_overlays(page)

            # 入口：点开声明弹窗（多个候选文案，native 仅作兜底）
            entry = None
            for etext in ["请选择自主声明", "请选择声明类型", "添加自主声明", "自主声明", "作品声明"]:
                cand = page.get_by_text(etext).first
                if await cand.count() and await cand.is_visible():
                    entry = cand
                    break
            if entry is not None:
                try:
                    await entry.scroll_into_view_if_needed(timeout=3000)
                except Exception:
                    pass
                try:
                    await entry.click(timeout=6000)
                except Exception:
                    await _native_click(page, entry)
                await page.wait_for_timeout(1200)

            # 弹窗：header「请选择声明类型（单选）」
            dialog = page.locator(".semi-modal-content:visible").filter(has_text="请选择声明类型").first
            if await dialog.count() == 0:
                dialog = page.locator(".semi-modal-body:visible").filter(has_text="请选择声明类型").first
            if await dialog.count() == 0:
                douyin_logger.warning(_msg("🧾", "自主声明弹窗未打开，声明未确认"))
                return False
            await dialog.first.wait_for(state="visible", timeout=6000)

            # 选项：label.semi-radio 内 span.semi-radio-addon 精确匹配
            option = dialog.locator("label.semi-radio").filter(
                has=page.locator(f'.semi-radio-addon:text-is("{declaration}")')
            ).first
            if await option.count() == 0:
                option = dialog.locator("label.semi-radio").filter(has_text=declaration).first
            if await option.count():
                try:
                    await option.click(timeout=6000)
                except Exception:
                    await _native_click(page, option)
            else:
                await dialog.get_by_text(declaration, exact=True).first.click(timeout=6000, force=True)
            await page.wait_for_timeout(400)
            selected = await option.evaluate("""el => {
                const input = el.querySelector('input[type="radio"]');
                return (input && input.checked) || el.getAttribute('aria-checked') === 'true' ||
                    el.classList.contains('semi-radio-checked');
            }""")
            if selected is not True:
                return False

            # 确定：footer 的 primary 按钮
            confirm_btn = dialog.locator("button.semi-button-primary").filter(has_text="确定").first
            if await confirm_btn.count() == 0:
                confirm_btn = dialog.get_by_role("button", name="确定").first
            if not await enabled_control(confirm_btn):
                return False
            try:
                await confirm_btn.click(timeout=6000)
            except Exception:
                await _native_click(page, confirm_btn)
            try:
                await dialog.first.wait_for(state="hidden", timeout=6000)
            except Exception:
                return False
            saved = page.get_by_text(declaration, exact=True).filter(visible=True)
            if await saved.count() != 1:
                return False
            douyin_logger.success(_msg("🧾", f"自主声明已选择「{declaration}」"))
            return True
        except Exception as exc:
            douyin_logger.warning(_msg("🧾", f"自主声明设置失败，声明未确认"))
            return False

    async def select_bgm(self, page: Page, bgm_name: str) -> bool:
        """为图文发布选择 BGM：可选增强功能，搜索无结果或异常均跳过不中断发布。"""
        try:
            # 点击「选择音乐」按钮
            music_entry = page.locator('text="选择音乐"').nth(1)
            if not await music_entry.count():
                music_entry = page.locator('text="选择音乐"').first
            await music_entry.wait_for(state="visible", timeout=10000)
            await music_entry.click()

            # 等待侧边栏出现并搜索
            sidesheet = page.locator(".semi-sidesheet-content").first
            await sidesheet.wait_for(state="visible", timeout=8000)
            search_input = sidesheet.locator('input.semi-input[placeholder="搜索音乐"]').first
            await search_input.wait_for(state="visible", timeout=5000)
            await search_input.fill(bgm_name)
            await search_input.press("Enter")

            # 等待搜索结果
            await asyncio.sleep(2)
            first_card = sidesheet.locator(".card-container-tmocjc").first
            try:
                await first_card.wait_for(state="visible", timeout=8000)
            except Exception:
                douyin_logger.warning(_msg("🎵", f"音乐「{bgm_name}」搜索结果为空，小人跳过"))
                await self._close_music_sidesheet(page)
                return False

            # 打印找到的音乐名称
            try:
                song_name_el = first_card.locator(".song-name-oRge4d").first
                if await song_name_el.count():
                    song_name = await song_name_el.inner_text()
                    douyin_logger.info(_msg("🎵", f"小人找到了: {song_name}"))
            except Exception:
                pass

            # JS 点击「使用」（按钮 visibility:hidden，普通 click 无效）
            apply_btn = first_card.locator(".apply-btn-LUPP0D").first
            await apply_btn.evaluate("el => el.click()")
            douyin_logger.info(_msg("🥳", f"BGM「{bgm_name}」已应用"))

            # 等待侧边栏关闭，超时则手动关闭
            try:
                await sidesheet.wait_for(state="hidden", timeout=5000)
            except Exception:
                await self._close_music_sidesheet(page)

            return True
        except Exception as exc:
            douyin_logger.warning(_msg("🎵", f"添加 BGM 时出错，跳过该步骤继续发布：{exc}"))
            try:
                await self._close_music_sidesheet(page)
            except Exception:
                pass
            return False

    async def _close_music_sidesheet(self, page: Page) -> None:
        try:
            close_btn = page.locator(".semi-sidesheet-close").first
            if await close_btn.count() and await close_btn.is_visible():
                await close_btn.click()
                await asyncio.sleep(1)
        except Exception:
            pass


class DouYinVideo(DouYinBaseUploader):
    def __init__(
        self,
        title,
        file_path,
        tags,
        publish_date: datetime | int,
        account_file,
        thumbnail_landscape_path=None,
        productLink="",
        productTitle="",
        thumbnail_portrait_path=None,
        desc: str | None = None,
        publish_strategy: str = DOUYIN_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
        collection_name: str | None = None,
        declaration: str | None = None,
        prepare_only: bool = False,
    ):
        super().__init__(
            publish_date=publish_date,
            account_file=account_file,
            publish_strategy=publish_strategy,
            debug=debug,
            headless=headless,
        )
        self.title = title
        self.file_path = file_path
        self.tags = tags
        self.thumbnail_landscape_path = thumbnail_landscape_path
        self.thumbnail_portrait_path = thumbnail_portrait_path
        self.productLink = productLink
        self.productTitle = productTitle
        self.desc = desc or ""
        self.collection_name = collection_name
        self.declaration = declaration.strip() if declaration and declaration.strip() else None
        self.prepare_only = prepare_only

    async def apply_self_declaration(self, page: Page) -> None:
        if not self.declaration:
            return
        if not await self.set_self_declaration(page, self.declaration):
            raise RuntimeError(f"自主声明「{self.declaration}」设置失败，拒绝继续发布")

    async def _clear_blocking_overlays(self, page: Page) -> None:
        """清除会拦截点击的浮层：填完话题后残留的话题/@提及下拉(publish-mention-wrapper)
        及其所在 semi-portal、其它非模态 semi-portal(tooltip/popover)、shepherd 引导层，
        并让当前输入框失焦。合集/声明下拉自身的 portal 是"点开后"才创建，故此处清理不误伤。

        根因见 recorder.log 2026-08-10 05:31：apply_collection 点合集下拉时，
        publish-mention-wrapper / semi-portal 拦截 pointer events → click 超时 → 归集被跳过。
        """
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        try:
            await page.evaluate(
                """() => {
                    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
                    document.querySelectorAll('.shepherd-element,.shepherd-modal-overlay-container').forEach(e=>e.remove());
                    document.querySelectorAll('[class*="mention-wrapper"]').forEach(e=>{ const p=e.closest('.semi-portal'); (p||e).remove(); });
                    // 关闭残留的非模态 Semi 浮层 portal（保留模态框，如声明弹窗）
                    document.querySelectorAll('.semi-portal').forEach(e=>{ if(!e.querySelector('.semi-modal, .semi-modal-content')) e.remove(); });
                }"""
            )
        except Exception:
            pass
        await page.wait_for_timeout(400)

    async def apply_collection(self, page: Page) -> None:
        """在发布表单页"添加合集"区选择目标合集（Semi Design select，字节组件库）。

        结构与快手（Ant Design）不同：合集名是纯文本 span.option-title-*，无 label 属性，
        用文本精确匹配。触发器用专属 class .select-collection-* 定位（页面唯一，第一级
        "合集/系列"类型下拉与此无关，不会误选）。找不到匹配合集时按 Escape 收起下拉，
        保持未选状态直接发布（界面允许留空，不阻断主发布流程）。
        """
        if not self.collection_name:
            return
        try:
            # 关键修复：填完话题后残留的话题/@提及下拉(publish-mention-wrapper)及 semi-portal
            # 浮层盖在"添加合集"下拉上，普通 click 全点在遮罩上→超时→归集被跳过。先清浮层再点。
            await self._clear_blocking_overlays(page)

            trigger = page.locator('[class*="select-collection-"]').first
            if await trigger.count() == 0:
                douyin_logger.warning(_msg("😵", "未找到\"添加合集\"下拉框，跳过归集"))
                return
            selection = trigger.locator(".semi-select-selection")
            try:
                await selection.click(timeout=5000)
            except Exception:
                await self._clear_blocking_overlays(page)
                await _native_click(page, selection)
            await page.wait_for_timeout(800)

            option = page.locator(".semi-select-option.collection-option").filter(
                has=page.locator(f'[class*="option-title-"]:text-is("{self.collection_name}")')
            )
            if await option.count() == 0:
                douyin_logger.warning(
                    _msg("😵", f"合集下拉框未找到「{self.collection_name}」，跳过归集，保持未选状态")
                )
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(300)
                return

            try:
                await option.first.click(timeout=5000)
            except Exception:
                await _native_click(page, option.first)
            await page.wait_for_timeout(500)
            douyin_logger.success(_msg("🥳", f"已选择合集：{self.collection_name}"))
        except Exception as exc:
            douyin_logger.warning(_msg("😵", f"选择合集失败，跳过归集继续发布: {exc}"))
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass

    async def _submit_sms_verify_code(self, page: Page, sms_input, code: str, code_file: str) -> bool:
        douyin_logger.info(_msg("✍️", f"已获取验证码，准备填入: {code}"))
        await sms_input.click()
        await sms_input.fill(code)
        douyin_logger.info(_msg("✅", "验证码已填入输入框"))
        await page.wait_for_timeout(500)

        verify_btn = page.locator('div.uc-ui-verify_sms-verify_button:has-text("验证")').first
        if await verify_btn.count() and await verify_btn.is_visible():
            try:
                await verify_btn.click(force=True)
                douyin_logger.success(_msg("✅", "已点击「验证」按钮(force)"))
            except Exception:
                await page.eval_on_selector('div.uc-ui-verify_sms-verify_button', 'el => el.click()')
                douyin_logger.success(_msg("✅", "已点击「验证」按钮(JS)"))
        else:
            verify_by_text = page.get_by_text("验证", exact=True).first
            if await verify_by_text.count():
                await verify_by_text.click(force=True)
                douyin_logger.success(_msg("✅", "已点击「验证」按钮(text)"))
            else:
                douyin_logger.warning(_msg("⚠️", "未找到验证按钮，尝试按Enter"))
                await page.keyboard.press("Enter")

        if os.path.exists(code_file):
            os.remove(code_file)
            douyin_logger.info(_msg("🧹", "验证码文件已清理"))

        await page.wait_for_timeout(3000)
        douyin_logger.info(_msg("🔄", "验证码处理完成，继续发布流程"))
        return True

    async def validate_upload_args(self):
        await self.validate_base_args()
        if not self.title or not str(self.title).strip():
            raise ValueError("视频模式下，title 是必须的")

        self.file_path = str(self.validate_video_file(self.file_path))
        if self.thumbnail_landscape_path:
            self.thumbnail_landscape_path = str(self.validate_image_file(self.thumbnail_landscape_path))
        if self.thumbnail_portrait_path:
            self.thumbnail_portrait_path = str(self.validate_image_file(self.thumbnail_portrait_path))

    async def handle_upload_error(self, page):
        douyin_logger.warning(_msg("😵", "视频上传摔了一跤，小人马上重新上传"))
        await page.locator('div.progress-div [class^="upload-btn-input"]').set_input_files(self.file_path)

    async def handle_auto_video_cover(self, page):
        if await page.get_by_text("请设置封面后再发布").first.is_visible():
            douyin_logger.info(_msg("🧍", "发布前还得先把封面弄好"))
            recommend_cover = page.locator('[class^="recommendCover-"]').first
            if await recommend_cover.count():
                douyin_logger.info(_msg("🏃", "小人去选第一个推荐封面"))
                try:
                    await recommend_cover.click()
                    await asyncio.sleep(1)
                    confirm_text = "是否确认应用此封面？"
                    if await page.get_by_text(confirm_text).first.is_visible():
                        douyin_logger.info(_msg("🪟", f"弹出确认框了: {confirm_text}"))
                        await page.get_by_role("button", name="确定").click()
                        douyin_logger.info(_msg("🥳", "推荐封面已经应用"))
                        await asyncio.sleep(1)
                    douyin_logger.info(_msg("🥳", "封面选择流程完成"))
                    return True
                except Exception as e:
                    douyin_logger.warning(_msg("😵", f"推荐封面没选成功: {e}"))
        return False

    async def set_thumbnail(self, page: Page):
        if not self.thumbnail_landscape_path and not self.thumbnail_portrait_path:
            return

        douyin_logger.info(_msg("🏃", "小人正在设置视频封面"))
        # 先清掉 shepherd 新手引导浮层，否则它会拦截封面点击导致弹窗打不开
        await page.evaluate(
            "() => document.querySelectorAll('.shepherd-element,.shepherd-modal-overlay-container').forEach(e=>e.remove())"
        )

        cover_area = page.locator('[class*="cover-"]').filter(has=page.locator("img")).first
        if not await cover_area.count():
            cover_area = page.locator('[class*="cover"]').first
        previous_cover = await _wait_saved_cover(page, cover_area)

        # 打开封面弹窗：抖音组件对普通/force click 常静默无效（和"完成"按钮同病），
        # 统一用 _native_click 派发完整原生事件序列；点后校验弹窗是否出现，没出现就重试。
        cover_locator_str = 'div.dy-creator-content-modal:visible'
        cover_locator = page.locator(cover_locator_str).first
        opened = False
        # 刚上传完页面还在过渡，先等封面区渲染稳定，去掉"页面没稳就点空"这个诱因
        try:
            await cover_area.wait_for(state="visible", timeout=8000)
        except Exception:
            pass
        await page.wait_for_timeout(1500)
        for attempt in range(5):
            # hover 若干次，等「编辑封面/选择封面」入口真正浮现，避免回退到封面区中心点空
            trigger = None
            trigger_txt = "封面区域"
            for _ in range(3):
                try:
                    await cover_area.hover(force=True)
                    await page.wait_for_timeout(600)
                except Exception:
                    pass
                for txt in ["编辑封面", "选择封面", "设置封面"]:
                    t = page.get_by_text(txt, exact=True).first
                    if await t.count() and await t.is_visible():
                        trigger, trigger_txt = t, txt
                        break
                if trigger is not None:
                    break
            if trigger is None:
                trigger = cover_area
            # 每轮都用 _native_click（force click 对抖音自定义组件常静默失效，白耗时间）
            await _native_click(page, trigger)
            douyin_logger.info(_msg("🖼️", f"已点「{trigger_txt}」尝试打开封面弹窗(第{attempt + 1}次)"))
            try:
                await page.wait_for_selector(cover_locator_str, timeout=5000)
                opened = True
                break
            except Exception:
                continue
        if not opened:
            raise RuntimeError("封面弹窗打不开，停止提交")

        await page.wait_for_timeout(1500)
        cover_locator = await unique_visible(page.locator(cover_locator_str), "抖音封面编辑器")

        # 封面弹窗内有两个 input.semi-upload-hidden-input（各自还带一个 -replace 兄弟）：
        #   ① 左侧「生成参考图」(AI封面参考图)——drag 区是 semi-upload-drag-area-custom，只有个 + 图标；
        #   ② 帧选择区「上传封面」——drag 区含 .semi-upload-drag-area-main-text「点击上传文件或拖拽…」。
        # 旧代码用 .first 取到了①，封面被塞进 AI 参考图槽→真封面没设上、检测/AI生成一直转，
        # 「完成」永远关不掉弹窗→挡住发布→超时（用户 F12 实测的真根因）。
        # 仅接受同一可见编辑器中的唯一具名拖拽上传区；不退回AI参考图输入。
        cover_upload = cover_locator.locator(
            '.semi-upload:visible:has(.semi-upload-drag-area-main-text:visible) input.semi-upload-hidden-input:not([disabled])'
        )
        if await cover_upload.count() != 1:
            raise RuntimeError("NEEDS_ATTENTION: 无法唯一识别指定封面上传入口；未使用AI参考图输入")

        if self.thumbnail_portrait_path:
            await _upload_cover_slot(page, cover_locator, cover_upload, "设置竖封面", self.thumbnail_portrait_path)
            douyin_logger.info(_msg("🖼️", "竖版封面已上传到预览"))
        if self.thumbnail_landscape_path:
            await _upload_cover_slot(page, cover_locator, cover_upload, "设置横封面", self.thumbnail_landscape_path)
            douyin_logger.info(_msg("🖼️", "横版封面已上传到预览"))

        # ── 等"完成"按钮解禁：封面图处理完成前，"完成"是 semi-button-disabled，点了无效 ──
        def _finish_btn():
            return cover_locator.get_by_role("button", name="完成", exact=True).first

        for _ in range(30):
            if await enabled_control(_finish_btn()):
                break
            await page.wait_for_timeout(500)
        else:
            raise RuntimeError("NEEDS_ATTENTION: 封面完成按钮尚不可用；未确认保存")

        # ── 点"完成"并验证弹窗真正 detach ──
        # 抖音自定义组件普通 click 可能不抛异常也不生效，所以每轮点后都校验弹窗是否消失：
        # 不接受额外警告，也不以 Escape 关闭来假装保存。
        closed = False
        for attempt in range(4):
            btn = _finish_btn()
            if not await btn.count():
                btn = cover_locator.locator("button.semi-button").filter(has_text="完成").first
            if await btn.count() and await btn.is_visible():
                try:
                    await btn.click(timeout=4000)
                except Exception:
                    pass
                await page.wait_for_timeout(1500)
                if await cover_locator.count() == 0:
                    closed = True
                    break
                # 普通点没关掉 → 派发完整原生事件序列
                await _native_click(page, btn)
                await page.wait_for_timeout(1500)
                if await cover_locator.count() == 0:
                    closed = True
                    break

            # An additional warning is not authorization to accept it.
            # Keep the window open so the user can review the exact consequence.
            if await page.locator(".semi-modal-content:visible").count():
                raise RuntimeError("NEEDS_ATTENTION: 封面保存要求额外确认，请在保留窗口核对；未代为确认")
            if await cover_locator.count() == 0:
                closed = True
                break

            # Closing with Escape would not prove that the requested cover saved.
            douyin_logger.debug(_msg("🖼️", f"封面「完成」后弹窗未关，重试(第{attempt + 1}次)"))

        if closed:
            await _wait_saved_cover(page, cover_area, previous_cover)
            douyin_logger.info(_msg("🥳", "指定视频封面已保存并回显，弹窗已关闭"))
        else:
            raise RuntimeError("封面未确认保存，停止提交")


    async def upload(self, playwright: Playwright) -> None:
        if self.prepare_only and self.headless:
            raise RuntimeError("抖音手动接手需要可见窗口，未开始上传")
        douyin_logger.info(_msg("🧍", "小人先检查 cookie、视频文件、封面和发布时间"))
        await self.validate_upload_args()
        douyin_logger.info(_msg("🥳", "上传前检查通过"))

        if self.prepare_only:
            browser, context = await connect_douyin_handoff(playwright, self.account_file)
        else:
            browser = await playwright.chromium.launch(headless=self.headless, channel="chromium", args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
            context = await browser.new_context(
                storage_state=f"{self.account_file}",
                permissions=["geolocation"],
            )
        # The legacy iframe hook shadows document.createElement with a read-only
        # property. Ordinary Chrome needs no such patch, and Douyin's SDK must
        # remain able to wrap this native method itself.
        from utils.publisher_handoff import native_chrome_endpoint
        if not native_chrome_endpoint('douyin'):
            context = await set_init_script(context)

        try:
            return await self._upload_video_page(context, browser)
        except BaseException as exc:
            if get_diagnostic('douyin') is None:
                record_diagnostic('douyin', 'navigation', exc)
            if not self.prepare_only:
                try:
                    await context.close()
                except Exception:
                    pass
                try:
                    await browser.close()
                except Exception:
                    pass
            raise

    async def _upload_video_page(self, context, browser):
        page = await context.new_page()
        douyin_logger.info(_msg("🧭", "小人正在赶往上传主页"))
        upload_input = page.locator('input[type="file"][accept*="video"], input[type="file"][accept*=".mp4"]')
        # Retry navigation only before assigning a file. Do not wait for every
        # analytics/font/image request when the uploader itself is ready.
        for attempt in range(2):
            try:
                await page.goto("https://creator.douyin.com/creator-micro/content/upload", wait_until="domcontentloaded", timeout=45000)
                await upload_input.first.wait_for(state="attached", timeout=30000)
                break
            except BrowserTimeoutError:
                if attempt: raise
                douyin_logger.info('抖音上传入口暂未就绪，重新打开一次；尚未选择文件。')
        if await upload_input.count() != 1:
            raise RuntimeError('抖音视频上传输入不唯一，尚未选择文件')
        douyin_logger.info(_msg("🏃", f"小人开始搬运视频: {self.title}.mp4"))
        emit_progress("douyin", "uploading")
        try:
            await upload_input.set_input_files(self.file_path)
        except Exception as exc:
            record_diagnostic('douyin', 'upload', exc)
            raise

        try:
            await page.wait_for_url(
                re.compile(r"^https://creator\.douyin\.com/creator-micro/content/(?:publish|post/video)(?:\?|$)"),
                timeout=120000, wait_until="domcontentloaded",
            )
        except Exception as exc:
            record_diagnostic('douyin', 'upload', exc)
            raise RuntimeError("NEEDS_ATTENTION: 未进入抖音视频编辑页；保留窗口，请检查上传与登录状态，未自动重传") from exc

        # The editor route can appear before the bytes finish uploading. Keep
        # the transfer/processing stage until the platform confirms readiness.
        await _wait_douyin_upload(page)
        self._video_ready = True
        douyin_logger.info(_msg("✍️", "小人开始填标题、描述和话题"))
        emit_progress("douyin", "metadata")
        try:
            await self.fill_title_and_description(page, self.title, self.desc, self.tags)
        except Exception as exc:
            record_diagnostic('douyin', 'metadata', exc)
            raise
        douyin_logger.info(_msg("🏷️", f"小人一共贴了 {len(self.tags)} 个话题"))

        if self.productLink and self.productTitle:
            douyin_logger.info(_msg("🛒", "小人正在设置商品链接"))
            await self.set_product_link(page, self.productLink, self.productTitle)
            douyin_logger.info(_msg("🥳", "商品链接设置完成"))

        # 自主声明：本项目成片含 AI 生成内容（TTS 配音 / AI 字幕 / AI 前贴片），
        # 按平台合规如实选「内容由AI生成」（与转载等并列，单选，无二级选项、无需填来源）。
        await self.apply_self_declaration(page)

        # 先归集：此时尚未打开封面弹窗，避免 dy-creator-content-portal 封面浮层拦截合集下拉
        # （实测：封面弹窗在 headless 下常滞留"检测中"未关闭，会盖住"添加合集"下拉）
        await self.apply_collection(page)

        # 再设封面（放最后，关掉弹窗，避免残留浮层挡住发布按钮）
        emit_progress("douyin", "cover")
        try:
            await self.set_thumbnail(page)
        except Exception as exc:
            record_diagnostic('douyin', 'cover', exc)
            raise
        return await self.finish_upload(page, context, browser)

    async def finish_upload(self, page, context, browser):
        """The handoff boundary precedes every final-submission side effect."""
        if self.prepare_only:
            try:
                await _require_douyin_handoff(page, video_confirmed=getattr(self, '_video_ready', False))
            except Exception as exc:
                record_diagnostic('douyin', 'handoff', exc)
                raise
            emit_progress("douyin", "ready_to_submit")
            douyin_logger.info("抖音素材、文案和封面已准备，编辑窗口保留；最终发布由使用者点击。")
            # The detached browser and its default context belong to the user
            # now. Do not change sync settings, schedule, or click publish.
            return "prepared"

        third_part_element = '[class^="info"] > [class^="first-part"] div div.semi-switch'
        if await page.locator(third_part_element).count():
            if "semi-switch-checked" not in await page.eval_on_selector(third_part_element, "div => div.className"):
                await page.locator(third_part_element).locator("input.semi-switch-native-control").click()

        if self.publish_strategy == DOUYIN_PUBLISH_STRATEGY_SCHEDULED and self.publish_date != 0:
            await self.set_schedule_time_douyin(page, self.publish_date)

        # Local publisher: do not repeatedly click publish or replace a user's cover.
        sms_input = page.locator('input[placeholder*="验证码"], input[type="tel"]').first
        if await sms_input.count() and await sms_input.is_visible():
            raise RuntimeError("NEEDS_ATTENTION: 平台要求人工身份验证，停止自动提交")
        publish_button = page.get_by_role("button", name="发布", exact=True)
        emit_progress("douyin", "ready_to_submit")
        emit_progress("douyin", "submitting")
        await publish_button.click(force=True)
        try:
            await page.wait_for_url("https://creator.douyin.com/creator-micro/content/manage**", timeout=30000)
        except Exception as exc:
            raise RuntimeError("NEEDS_ATTENTION: 已点击提交但未观察到完成页面，请核验，勿直接重发") from exc
        emit_progress("douyin", "submitted")
        douyin_logger.success(_msg("🥳", "视频提交完成"))

        await context.storage_state(path=self.account_file)
        douyin_logger.success(_msg("🥳", "cookie 更新完毕"))
        await asyncio.sleep(2)
        await context.close()
        await browser.close()

    async def douyin_upload_video(self):
        async with async_playwright() as playwright:
            return await self.upload(playwright)

    async def main(self):
        await self.douyin_upload_video()


class DouYinNote(DouYinBaseUploader):
    def __init__(
        self,
        image_paths,
        note,
        tags,
        publish_date: datetime | int,
        account_file,
        title: str | None = None,
        publish_strategy: str = DOUYIN_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
        bgm: str = "",
    ):
        super().__init__(
            publish_date=publish_date,
            account_file=account_file,
            publish_strategy=publish_strategy,
            debug=debug,
            headless=headless,
        )
        self.image_paths = image_paths
        self.note = note or ""
        self.title = title or (self.note[:30] if self.note else "")
        self.tags = tags or []
        self.bgm = bgm or ""

    async def validate_upload_args(self):
        await self.validate_base_args()
        if not self.title or not str(self.title).strip():
            raise ValueError("图文模式下，title 是必须的")

        if len(self.title) > 20:
            raise ValueError(f"标题不能超过20字符，当前: {len(self.title)}字符")

        if not self.image_paths:
            raise ValueError("图文模式下，图片是必须的")

        if isinstance(self.image_paths, (str, Path)):
            self.image_paths = [self.image_paths]

        if len(self.image_paths) > 35:
            raise ValueError("图文模式下最多只支持上传 35 张图片")

        note_len = len(self.note) if self.note else 0
        if note_len > 1000:
            raise ValueError(f"正文不能超过1000字符，当前: {note_len}字符")

        normalized_image_paths = []
        for image_path in self.image_paths:
            normalized_image_paths.append(str(self.validate_image_file(image_path)))
        self.image_paths = normalized_image_paths

    async def upload_note_content(self, page: Page) -> None:
        douyin_logger.info(_msg("🏃", f"小人开始搬运图文，共 {len(self.image_paths)} 张图片"))
        douyin_logger.info(_msg("🔀", "小人正在切换到图文发布"))
        await page.get_by_text("发布图文", exact=True).click()
        await page.wait_for_timeout(1000)

        douyin_logger.info(_msg("📤", "小人正在上传图片"))
        await page.locator("div[class^='container'] input[accept*='image']").set_input_files(self.image_paths)

        while True:
            try:
                await page.wait_for_url(
                    "**/creator-micro/content/post/image?**",
                    timeout=3000,
                )
                douyin_logger.info(_msg("🥳", "已经进入图文发布页面"))
                break
            except Exception:
                douyin_logger.debug(_msg("🧍", "小人还在等图片上传完成"))
                await asyncio.sleep(0.5)

        await asyncio.sleep(1)
        douyin_logger.info(_msg("✍️", "小人开始填标题、描述和话题"))
        await self.fill_title_and_description(page, self.title, self.note, self.tags)
        title_len = len(self.title) if self.title else 0
        tags_text = " ".join(f"#{t}" for t in self.tags) if self.tags else ""
        desc_and_tags_len = len(self.note or "") + (len(tags_text) + 2 if self.tags else 0)
        douyin_logger.info(_msg("📝", f"标题总字数: {title_len}，描述+话题总字数: {desc_and_tags_len}"))
        douyin_logger.info(_msg("🏷️", f"小人一共贴了 {len(self.tags)} 个话题"))

        if self.bgm:
            await self.select_bgm(page, self.bgm)

        if self.publish_strategy == DOUYIN_PUBLISH_STRATEGY_SCHEDULED and self.publish_date != 0:
            await self.set_schedule_time_douyin(page, self.publish_date)

        while True:
            try:
                publish_button = page.get_by_role("button", name="发布", exact=True)
                if await publish_button.count():
                    await publish_button.click()
                await page.wait_for_url(
                    "**/creator-micro/content/manage?enter_from=publish**",
                    timeout=3000,
                )
                douyin_logger.success(_msg("🥳", "图文发布成功，小人开心收工"))
                break
            except Exception:
                douyin_logger.info(_msg("🏃", "小人正在冲刺发布图文"))
                await asyncio.sleep(0.5)

    async def upload(self, playwright: Playwright) -> None:
        douyin_logger.info(_msg("🧍", "小人先检查 cookie、图片和发布时间"))
        await self.validate_upload_args()
        douyin_logger.info(_msg("🥳", "图文上传前检查通过"))

        browser = await playwright.chromium.launch(headless=self.headless, channel="chromium", args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        context = await browser.new_context(
            storage_state=f"{self.account_file}",
            permissions=["geolocation"],
        )
        context = await set_init_script(context)

        upload_success = False
        try:
            page = await context.new_page()
            await page.goto("https://creator.douyin.com/creator-micro/content/upload", wait_until="domcontentloaded", timeout=90000)
            douyin_logger.info(_msg("🧭", "小人正在赶往图文发布页"))
            await page.wait_for_url("https://creator.douyin.com/creator-micro/content/upload", timeout=90000)

            await self.upload_note_content(page)
            upload_success = True
        finally:
            if upload_success:
                await context.storage_state(path=self.account_file)
                douyin_logger.success(_msg("🥳", "cookie 更新完毕"))
                await asyncio.sleep(2)
            await context.close()
            await browser.close()

    async def douyin_upload_note(self):
        async with async_playwright() as playwright:
            await self.upload(playwright)
