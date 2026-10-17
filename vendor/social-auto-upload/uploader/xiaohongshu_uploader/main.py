# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import inspect
import os
import re
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from patchright.async_api import Page
from patchright.async_api import Playwright
from patchright.async_api import async_playwright

from conf import DEBUG_MODE, LOCAL_CHROME_HEADLESS, LOCAL_CHROME_PATH
from uploader.base_video import BaseVideoUploader
from utils.base_social_media import set_init_script
from utils.login_qrcode import build_login_qrcode_path
from utils.login_qrcode import decode_qrcode_from_path
from utils.login_qrcode import print_terminal_qrcode
from utils.login_qrcode import remove_qrcode_file
from utils.login_qrcode import save_data_url_image
from utils.log import xiaohongshu_logger
from utils.publisher_progress import diagnostic_enabled, emit_progress, record_diagnostic
from utils import publisher_handoff

XHS_DEFAULT_CREATOR_BASE_URL = "https://creator.xiaohongshu.com"
XHS_CREATOR_BASE_URL_ENV = "SAU_XHS_CREATOR_BASE_URL"
XHS_PUBLISH_SUCCESS_URL_PATTERN = "**/publish/success?**"
XHS_LOGIN_BOX_SELECTOR = "div[class*='login-box']"
XHS_LOGIN_SWITCH_SELECTOR = "img.css-wemwzq"
XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE = "immediate"
XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED = "scheduled"


def _build_xhs_creator_url(path: str) -> str:
    base_url = os.getenv(
        XHS_CREATOR_BASE_URL_ENV,
        XHS_DEFAULT_CREATOR_BASE_URL,
    ).strip().rstrip("/")
    if not base_url:
        base_url = XHS_DEFAULT_CREATOR_BASE_URL
    return f"{base_url}/{path.lstrip('/')}"


def _msg(emoji: str, text: str) -> str:
    return f"{emoji} {text}"


def _publisher_failure_hold_seconds(headless: bool) -> int:
    """Opt-in, bounded pause for inspecting a headed publisher failure."""
    if headless or os.environ.get("SAU_PUBLISHER_SAFE_LOG") != "1" or not diagnostic_enabled("xiaohongshu"):
        return 0
    try:
        return max(0, min(180, int(os.environ.get("PUBLISHER_FAILURE_HOLD_SECONDS", "0"))))
    except (TypeError, ValueError):
        return 0


async def _cover_preview_state(modal):
    # Pixel data stays in page memory; only a digest and load readiness return.
    # A different blob URL is not evidence that the rendered cover changed.
    return await modal.evaluate("""async (root) => {
        const visible = (element) => {
            if (!element.getClientRects().length) return false;
            for (let current = element; current; current = current.parentElement) {
                const style = getComputedStyle(current);
                if (style.display === 'none' || style.visibility === 'hidden' ||
                    style.visibility === 'collapse' || Number(style.opacity) === 0) return false;
            }
            return true;
        };
        const unavailable = {signature: null, ready: false};
        if (!visible(root) || root.getAttribute('aria-busy') === 'true' ||
            [...root.querySelectorAll('[aria-busy="true"], [role="progressbar"]')].some(visible)) {
            return unavailable;
        }
        const candidates = [];
        for (const element of root.querySelectorAll('img, canvas')) {
            const rect = element.getBoundingClientRect();
            if (rect.width >= 200 && rect.height >= 100 && visible(element)) {
                candidates.push({element, area: rect.width * rect.height});
            }
        }
        if (!candidates.length) return unavailable;
        // The large editing preview must change, not a smaller thumbnail or AI
        // reference image. Multiple comparably large previews are ambiguous.
        candidates.sort((a, b) => b.area - a.area);
        if (candidates.length > 1 && candidates[1].area >= candidates[0].area * 0.75) return unavailable;
        const element = candidates[0].element;
        const isImage = element.tagName === 'IMG';
        const width = isImage ? element.naturalWidth : element.width;
        const height = isImage ? element.naturalHeight : element.height;
        if (!width || !height || (isImage && !element.complete)) return unavailable;
        try {
            const canvas = document.createElement('canvas');
            // A fixed normalized raster means source URL, dimensions, or layout
            // changes alone cannot count as changed cover pixels. No pixels or
            // source URLs leave the page; unreadable pixels fail closed.
            canvas.width = canvas.height = 256;
            const context = canvas.getContext('2d', {willReadFrequently: true});
            context.drawImage(element, 0, 0, canvas.width, canvas.height);
            const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
            const digest = await crypto.subtle.digest('SHA-256', pixels);
            return {signature: Array.from(new Uint8Array(digest))
                .map(x => x.toString(16).padStart(2, '0')).join(''), ready: true};
        } catch (_) {
            return unavailable;
        }
    }""")


async def _wait_for_cover_preview(modal, confirm, previous, timeout_seconds=30):
    deadline = time.monotonic() + timeout_seconds
    last_signature, stable_samples = None, 0
    while time.monotonic() < deadline:
        current = await _cover_preview_state(modal)
        signature = current.get("signature")
        changed = signature and signature != previous.get("signature") and current.get("ready") is True
        if changed and await confirm.count() == 1 and await confirm.is_enabled():
            stable_samples = stable_samples + 1 if signature == last_signature else 1
            if stable_samples >= 3:
                return
            last_signature = signature
        else:
            stable_samples, last_signature = 0, None
        await asyncio.sleep(0.5)
    raise TimeoutError("封面预览未完成更新，停止保存")


async def _wait_for_initial_cover_preview(modal, timeout_seconds=10):
    """Do not mistake late rendering of the old frame for the chosen file."""
    deadline = time.monotonic() + timeout_seconds
    previous_signature = None
    while time.monotonic() < deadline:
        current = await _cover_preview_state(modal)
        signature = current.get("signature") if current.get("ready") is True else None
        if signature and signature == previous_signature:
            return current
        previous_signature = signature
        await asyncio.sleep(0.5)
    raise TimeoutError("无法读取稳定的原封面预览，停止更换封面")


def _resume_text_matches(text: str, description: str, tags: list[str]) -> bool:
    """Match the entire authored body and ordered, complete topic labels.

    Whitespace/layout normalization is permitted; added prose, topic prefixes,
    missing/extra topics, and reordered topics are not the original editor.
    """
    normalize = lambda value: " ".join(unicodedata.normalize("NFKC", value).split())
    expected = re.escape(normalize(description))
    for tag in tags:
        expected += r"\s*#" + re.escape(normalize(tag))
    return re.fullmatch(expected, normalize(text)) is not None


async def _js_click_by_text(page: Page, text: str) -> bool:
    """用 JS 找到文字完全匹配的最内层元素并点击它及其祖先（绕过 span pointer-events:none / 遮罩拦截）。

    小红书很多可点项文字在 <span class="d-text"> 里，pointer-events 常被禁用，
    Playwright 常规 click 会超时。用原生 click 冒泡触发 Vue 事件更可靠。
    """
    return await page.evaluate(
        """(t) => {
            const nodes = [...document.querySelectorAll('*')].filter(
                e => e.children.length === 0 && (e.textContent || '').trim() === t
            );
            if (!nodes.length) return false;
            let el = nodes[nodes.length - 1];
            for (let i = 0; i < 4 && el; i++) { try { el.click(); } catch (e) {} el = el.parentElement; }
            return true;
        }""",
        text,
    )


async def _emit_qrcode_callback(qrcode_callback, payload: dict):
    if not qrcode_callback:
        return

    callback_result = qrcode_callback(payload)
    if inspect.isawaitable(callback_result):
        await callback_result


def _build_login_result(
    success: bool,
    status: str,
    message: str,
    account_file: str,
    qrcode: dict | None = None,
    current_url: str = "",
) -> dict:
    return {
        "success": success,
        "status": status,
        "message": message,
        "account_file": str(account_file),
        "qrcode": qrcode,
        "current_url": current_url,
    }


async def _open_xhs_qrcode_panel(page: Page) -> None:
    login_box = page.locator(XHS_LOGIN_BOX_SELECTOR).first
    await login_box.wait_for(state="visible", timeout=30000)

    scan_text = login_box.locator("div:has-text('扫一扫')").first
    if await scan_text.count():
        return

    switch_img = login_box.locator(XHS_LOGIN_SWITCH_SELECTOR).first
    await switch_img.wait_for(state="visible", timeout=10000)
    await switch_img.click()
    await login_box.locator("div:has-text('扫一扫')").first.wait_for(state="visible", timeout=10000)


async def _find_xhs_qrcode_locator(page: Page):
    await _open_xhs_qrcode_panel(page)

    qrcode_img = page.locator('.login-box-container').get_by_text("APP扫一扫登录").filter(visible=True).locator("xpath=..//following-sibling::div//img").nth(0)

    if await qrcode_img.count():
        return qrcode_img

    raise RuntimeError("未在扫一扫登录区域找到小红书二维码图片")


async def _extract_xhs_qrcode_src(page: Page) -> str:
    qrcode_img = await _find_xhs_qrcode_locator(page)
    await qrcode_img.wait_for(state="visible", timeout=30000)
    qrcode_src = await qrcode_img.get_attribute("src")
    if not qrcode_src:
        raise RuntimeError("未获取到小红书登录二维码地址")
    return qrcode_src


async def _save_xhs_qrcode(
    page: Page,
    account_file: str,
    previous_qrcode_path: Path | None = None,
    qrcode_callback=None,
) -> dict:
    qrcode_src = await _extract_xhs_qrcode_src(page)
    qrcode_path = build_login_qrcode_path(account_file, suffix="xhs_login_qrcode")
    qrcode_img = await _find_xhs_qrcode_locator(page)

    if qrcode_src.startswith("data:image/"):
        save_data_url_image(qrcode_src, qrcode_path)
    else:
        qrcode_path.parent.mkdir(parents=True, exist_ok=True)
        await qrcode_img.screenshot(path=str(qrcode_path))

    if previous_qrcode_path and previous_qrcode_path != qrcode_path:
        if remove_qrcode_file(previous_qrcode_path):
            xiaohongshu_logger.info(_msg("🧹", f"临时二维码文件已清理: {previous_qrcode_path}"))

    xiaohongshu_logger.info(_msg("🖼️", f"二维码已经准备好啦，已保存到: {qrcode_path}"))
    qrcode_content = decode_qrcode_from_path(qrcode_path)
    if qrcode_content:
        print_terminal_qrcode(qrcode_content, qrcode_path, "小红书APP")
    else:
        xiaohongshu_logger.warning(_msg("😵", f"终端没法完整显示二维码，请打开 {qrcode_path} 扫码"))

    qrcode_info = {
        "image_path": str(qrcode_path),
        "image_data_url": qrcode_src,
    }
    await _emit_qrcode_callback(qrcode_callback, qrcode_info)
    return qrcode_info


async def _is_xhs_login_completed(page: Page) -> bool:
    if page.url.startswith(_build_xhs_creator_url("/login")):
        return False

    login_box = page.locator(XHS_LOGIN_BOX_SELECTOR).first
    if not await login_box.count():
        return True

    try:
        return not await login_box.is_visible()
    except Exception:
        return True


async def _xhs_auth_state(page, timeout_seconds=15):
    """True requires an authenticated upload control; False requires login UI."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if page.url.startswith(_build_xhs_creator_url("/login")):
            return False
        login_box = page.locator(XHS_LOGIN_BOX_SELECTOR).first
        if await login_box.count() and await login_box.is_visible():
            return False
        upload = page.locator("input[type='file'][accept*='video'], input[type='file'][accept*='.mp4']")
        if await upload.count():
            return True
        await asyncio.sleep(.3)
    raise publisher_handoff.AuthVerificationUnavailable("小红书页面未完成核验；已保留登录，请稍后重试检查。")


async def cookie_auth(account_file):
    if not os.path.exists(account_file):
        return False
    # Same owned profile as login/upload, but no window or visible probe tab.
    # An unsupported background check stays unavailable; it must not pop up a
    # normal tab, start QR login, or disturb a prepared editor.
    try:
        async with publisher_handoff.quiet_publisher_playwright() as playwright:
            browser, context = await publisher_handoff.connect_publisher_handoff(
                playwright, account_file, "xiaohongshu", quiet=True)
            async with publisher_handoff.hidden_auth_page(browser, context) as page:
                response = await page.goto(_build_xhs_creator_url("/publish/publish?from=homepage&target=video"),
                                           wait_until="domcontentloaded", timeout=30000)
                if response is not None and response.status >= 400:
                    raise publisher_handoff.AuthVerificationUnavailable("小红书暂时无法检查；已保留登录，请稍后重试。")
                valid = await _xhs_auth_state(page)
                if valid:
                    await publisher_handoff.save_publisher_session(context, account_file, "xiaohongshu", probe_page=page)
                return valid
    except publisher_handoff.AuthVerificationUnavailable:
        raise
    except Exception:
        raise publisher_handoff.AuthVerificationUnavailable("小红书连接或检查暂时失败；已保留登录，请重试检查，无需重新扫码。") from None


async def xiaohongshu_setup(
    account_file,
    handle=False,
    return_detail=False,
    qrcode_callback=None,
    headless: bool = LOCAL_CHROME_HEADLESS,
):
    if not os.path.exists(account_file) or not await cookie_auth(account_file):
        if not handle:
            result = _build_login_result(False, "cookie_invalid", "cookie文件不存在或已失效", account_file)
            return result if return_detail else False
        xiaohongshu_logger.info(_msg("🥹", "cookie 失效了，准备打开浏览器重新登录"))
        result = await xiaohongshu_cookie_gen(
            account_file,
            qrcode_callback=qrcode_callback,
            headless=headless,
        )
        return result if return_detail else result["success"]

    result = _build_login_result(True, "cookie_valid", "cookie有效", account_file)
    return result if return_detail else True


async def xiaohongshu_cookie_gen(
    account_file,
    qrcode_callback=None,
    poll_interval: int = 3,
    max_checks: int = 100,
    headless: bool = LOCAL_CHROME_HEADLESS,
):
    if headless:
        xiaohongshu_logger.info(_msg("🖼️", "小红书登录将以无头模式运行，小人会输出终端二维码并保存本地二维码图片"))

    account_path = Path(account_file)
    account_path.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        browser, context = await publisher_handoff.connect_publisher_handoff(
            playwright, account_file, "xiaohongshu", allow_login=True)
        qrcode_path = None
        qrcode_info = None
        result = _build_login_result(False, "failed", "小红书登录失败", account_file)
        try:
            page = await context.new_page()
            # A previous timed-out scan may have completed in the retained
            # browser. Check that same environment before asking for another QR.
            await page.goto(_build_xhs_creator_url("/publish/publish?from=homepage&target=video"),
                            wait_until="domcontentloaded", timeout=30000)
            if await _xhs_auth_state(page):
                await publisher_handoff.save_publisher_session(context, account_file, "xiaohongshu")
                result = _build_login_result(True, "success", "已复用小红书登录，无需再次扫码", account_file)
                return result
            qrcode_info = await _save_xhs_qrcode(page, account_file, qrcode_callback=qrcode_callback)
            qrcode_path = Path(qrcode_info["image_path"])
            xiaohongshu_logger.info(_msg("🧍", "请扫码，小人正在耐心等待登录完成"))

            for _ in range(max_checks):
                if await _is_xhs_login_completed(page):
                    await asyncio.sleep(2)
                    await page.goto(_build_xhs_creator_url("/publish/publish?from=homepage&target=video"),
                                    wait_until="domcontentloaded", timeout=30000)
                    if await _xhs_auth_state(page):
                        await publisher_handoff.save_publisher_session(context, account_file, "xiaohongshu")
                        xiaohongshu_logger.success(_msg("🥳", "小红书扫码登录成功，小人开心收工"))
                        result = _build_login_result(True, "success", "小红书扫码登录成功", account_file, qrcode_info, page.url)
                    else:
                        result = _build_login_result(
                            False,
                            "cookie_invalid",
                            "小红书扫码流程结束，但 cookie 校验失败",
                            account_file,
                            qrcode_info,
                            page.url,
                        )
                    return result

                await asyncio.sleep(poll_interval)

            result = _build_login_result(
                False,
                "timeout",
                "等待小红书扫码登录超时",
                account_file,
                qrcode_info,
                page.url,
            )
        except Exception as exc:
            result = _build_login_result(False, "failed", str(exc), account_file, current_url=page.url if "page" in locals() else "")
        finally:
            if remove_qrcode_file(qrcode_path):
                xiaohongshu_logger.info(_msg("🧹", f"临时二维码文件已清理: {qrcode_path}"))
            if not result["success"]:
                xiaohongshu_logger.error(_msg("😢", f"登录失败: {result['message']}"))
            if result["success"] and "page" in locals():
                await page.close()
            # On timeout keep the official login page open for the user. Never
            # close the owned browser, which may also contain an edited draft.
        return result


class XiaoHongShuBaseUploader(BaseVideoUploader):
    def __init__(
        self,
        publish_date: datetime | int,
        account_file,
        publish_strategy: str = XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
    ):
        self.publish_date = publish_date
        self.account_file = str(account_file)
        self.publish_strategy = publish_strategy
        self.debug = debug
        self.date_format = "%Y年%m月%d日 %H:%M"
        self.local_executable_path = LOCAL_CHROME_PATH
        self.headless = headless

    async def validate_base_args(self):
        from utils.publisher_handoff import native_chrome_endpoint
        native = bool(getattr(self, "prepare_only", False) and native_chrome_endpoint("xiaohongshu"))
        if not native and not os.path.exists(self.account_file):
            raise RuntimeError(f"cookie文件不存在，请先完成小红书登录: {self.account_file}")
        if not native and not await cookie_auth(self.account_file):
            raise RuntimeError(f"cookie文件已失效，请先完成小红书登录: {self.account_file}")

        if self.publish_strategy not in {
            XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
            XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED,
        }:
            raise ValueError(f"不支持的发布策略: {self.publish_strategy}")

        if self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED:
            self.publish_date = self.validate_publish_date(self.publish_date)
        else:
            self.publish_date = 0

    async def set_schedule_time_xiaohongshu(self, page: Page, publish_date: datetime):
        xiaohongshu_logger.info(_msg("🕒", f"小人准备设置定时发布时间: {publish_date.strftime(self.date_format)}"))
        await page.locator('.custom-switch-card').filter(has_text="定时发布").locator('.d-switch').click()
        await asyncio.sleep(1)
        publish_date_hour = publish_date.strftime("%Y-%m-%d %H:%M")
        time_input = page.locator('.d-datepicker-input-filter input.d-text')
        await time_input.fill(str(publish_date_hour))
        await asyncio.sleep(1)

    async def set_location(self, page: Page, location: str = "青岛市"):
        if not location:
            return True

        xiaohongshu_logger.info(_msg("📍", f"小人准备设置位置: {location}"))
        loc_ele = await page.wait_for_selector('div.d-text.d-select-placeholder.d-text-ellipsis.d-text-nowrap')
        await loc_ele.click()
        await page.wait_for_timeout(1000)
        await page.keyboard.type(location)
        dropdown_selector = 'div.d-popover.d-popover-default.d-dropdown.--size-min-width-large'
        await page.wait_for_timeout(2000)
        try:
            await page.wait_for_selector(dropdown_selector, timeout=3000)
        except Exception:
            xiaohongshu_logger.warning(_msg("😵", "位置下拉列表没按预期出现，小人继续按旧逻辑查找"))
        await page.wait_for_timeout(1000)
        flexible_xpath = (
            f'//div[contains(@class, "d-popover") and contains(@class, "d-dropdown")]'
            f'//div[contains(@class, "d-options-wrapper")]'
            f'//div[contains(@class, "d-grid") and contains(@class, "d-options")]'
            f'//div[contains(@class, "name") and text()="{location}"]'
        )
        await page.wait_for_timeout(3000)
        try:
            location_option = await page.wait_for_selector(
                flexible_xpath,
                timeout=3000
            )

            if not location_option:
                location_option = await page.wait_for_selector(
                    f'//div[contains(@class, "d-popover") and contains(@class, "d-dropdown")]'
                    f'//div[contains(@class, "d-options-wrapper")]'
                    f'//div[contains(@class, "d-grid") and contains(@class, "d-options")]'
                    f'/div[1]//div[contains(@class, "name") and text()="{location}"]',
                    timeout=2000
                )

            await location_option.scroll_into_view_if_needed()
            await location_option.click()
            xiaohongshu_logger.success(_msg("🥳", f"位置已经设置成 {location}"))
            return True
        except Exception as e:
            xiaohongshu_logger.error(_msg("😢", f"设置位置失败: {e}"))
            try:
                all_options = await page.query_selector_all(
                    '//div[contains(@class, "d-popover") and contains(@class, "d-dropdown")]'
                    '//div[contains(@class, "d-options-wrapper")]'
                    '//div[contains(@class, "d-grid") and contains(@class, "d-options")]'
                    '/div'
                )
                xiaohongshu_logger.debug(_msg("🧍", f"位置下拉里一共找到 {len(all_options)} 个选项"))
                for i, option in enumerate(all_options[:3]):
                    option_text = await option.inner_text()
                    xiaohongshu_logger.debug(_msg("🧾", f"候选位置 {i + 1}: {option_text.strip()[:50]}"))
            except Exception as inner_e:
                xiaohongshu_logger.debug(_msg("😵", f"读取位置候选列表失败: {inner_e}"))
            return False

    async def fill_title(self, page: Page) -> None:
        if len(self.title) > 20:
            raise ValueError("小红书当前适配标题最多 20 字，不会截断标题")
        title_container = page.locator('input[placeholder*="填写标题"]')
        await title_container.fill(self.title)

    async def fill_desc(self, page: Page) -> None:
        if not getattr(self, "desc", ""):
            return

        desc = page.locator('p[data-placeholder*="输入正文描述"]')
        await desc.click()
        await page.keyboard.press("Backspace")
        await page.keyboard.press("Control+KeyA")
        await page.keyboard.press("Delete")
        await page.keyboard.type(self.desc)
        await page.keyboard.press("Enter")

    async def fill_tags(self, page: Page) -> None:
        if not getattr(self, "tags", None):
            return

        # 小红书标签上限为 10 个，超过会导致死循环卡住发布
        max_tags = 10
        if len(self.tags) > max_tags:
            error = ValueError("小红书当前适配最多支持 10 个话题，不会截断话题列表")
            record_diagnostic("xiaohongshu", "topic", error)
            raise error

        if not getattr(self, "desc", ""):
            desc = page.locator('p[data-placeholder*="输入正文描述"]')
            try:
                await desc.click()
            except Exception as exc:
                record_diagnostic("xiaohongshu", "topic", exc)
                raise

        for topic_index, tag in enumerate(self.tags, 1):  # 循环处理所有 tags
            # 话题候选下拉框依赖小红书联想接口实时返回，网络抖动/无匹配时会等不到。
            # Never substitute the first similar suggestion for the requested tag.
            candidate = None
            candidate_count = None
            try:
                await page.keyboard.type("#" + tag, delay=30)
                await page.locator('#creator-editor-topic-container').wait_for(
                    state="visible",
                    timeout=6000
                )
                exact_text = page.get_by_text(re.compile(r"^\s*#?" + re.escape(tag) + r"\s*$"))
                candidate = page.locator('#creator-editor-topic-container .item').filter(has=exact_text)
                await candidate.first.wait_for(state="visible", timeout=4000)
                candidate_count = await candidate.count()
                if candidate_count != 1:
                    raise RuntimeError("未找到唯一且完全匹配的话题")
                await candidate.click()
            except Exception as exc:
                if candidate is not None and candidate_count is None and diagnostic_enabled("xiaohongshu"):
                    try:
                        candidate_count = await candidate.count()
                    except Exception:
                        pass
                record_diagnostic("xiaohongshu", "topic", exc, topic_index=topic_index,
                                  candidate_count=candidate_count)
                raise RuntimeError("请求的话题未能填写，停止提交") from exc

    async def fill_meta(self, page: Page) -> None:
        for step, method in (("title", self.fill_title), ("description", self.fill_desc)):
            try:
                await method(page)
            except Exception as exc:
                record_diagnostic("xiaohongshu", step, exc)
                raise
        await self.fill_tags(page)

    async def check_original_declaration(self, page: Page) -> None:
        """设置「来源转载」声明，填写转载来源。

        流程（对应 codegen 录制）：
          点「添加内容类型声明」→ 点包含「来源转载」的 div
          → 填 placeholder「请输入媒体名称」→ 点 button「确认」。
        容错：任一步失败记 warning 跳过、继续发布，不中断。
        """
        source = getattr(self, "repost_source", "") or ""
        if not source:
            return
        try:
            # 1. 点「添加内容类型声明」
            trigger = page.get_by_text("添加内容类型声明", exact=False).first
            try:
                await trigger.scroll_into_view_if_needed(timeout=5000)
            except Exception:
                pass
            await trigger.click(force=True)
            await page.wait_for_timeout(1500)

            # 2. 选「来源转载」选项
            import re as _re
            repost_option = page.locator("#publish-container div").filter(
                has_text=_re.compile(r"^来源转载$")
            ).last
            if await repost_option.count():
                await repost_option.click(force=True)
            else:
                await _js_click_by_text(page, "来源转载")
            await page.wait_for_timeout(1500)

            # 3. 填写媒体名称
            source_input = page.get_by_placeholder("请输入媒体名称").first
            await source_input.wait_for(state="visible", timeout=8000)
            await source_input.click()
            await source_input.fill(source)
            await page.wait_for_timeout(500)

            # 4. 点「确认」按钮
            confirm = page.get_by_role("button", name="确认").first
            try:
                await confirm.wait_for(state="visible", timeout=5000)
                await confirm.click()
            except Exception:
                await _js_click_by_text(page, "确认")

            await page.wait_for_timeout(1000)
            xiaohongshu_logger.success(_msg("🧾", f"来源转载已声明（来源：{source}）"))
        except Exception as exc:
            xiaohongshu_logger.warning(_msg("⚠️", f"设置来源转载失败，跳过继续发布: {exc}"))
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass


class XiaoHongShuVideo(XiaoHongShuBaseUploader):
    def __init__(
        self,
        title,
        file_path,
        tags,
        publish_date: datetime | int,
        account_file,
        thumbnail_path=None,
        desc: str | None = None,
        publish_strategy: str = XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
        prepare_only: bool = False,
        resume_cover: bool = False,
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
        self.tags = tags or []
        self.thumbnail_path = thumbnail_path
        self.desc = desc or ""
        self.prepare_only = prepare_only
        self.resume_cover = resume_cover

    async def validate_upload_args(self):
        if not self.title or not str(self.title).strip():
            raise ValueError("视频模式下，title 是必须的")
        if len(self.title) > 20:
            raise ValueError("小红书当前适配标题最多 20 字，不会截断标题")
        if len(self.tags) > 10:
            raise ValueError("小红书当前适配最多支持 10 个话题，不会截断话题列表")
        await self.validate_base_args()

        self.file_path = str(self.validate_video_file(self.file_path))
        if self.thumbnail_path:
            self.thumbnail_path = str(self.validate_image_file(self.thumbnail_path))

    async def handle_upload_error(self, page: Page):
        xiaohongshu_logger.warning(_msg("😵", "视频上传摔了一跤，小人马上重新上传"))
        await page.locator('div.progress-div [class^="upload-btn-input"]').set_input_files(self.file_path)

    async def set_thumbnail(self, page: Page, thumbnail_path: str):
        if not thumbnail_path:
            return

        xiaohongshu_logger.info(_msg("🖼️", "小人准备设置封面"))

        # Every image input and save action belongs to one visible cover editor.
        try:
            heading = page.get_by_role("heading", name="设置封面", exact=True).filter(visible=True)

            async def find_modal():
                count = await heading.count()
                if count > 1:
                    raise RuntimeError("封面弹窗不唯一，停止操作")
                if count == 1:
                    # Modern editor: choose the nearest non-document ancestor
                    # containing both the file input and the observed save button.
                    modern = heading.locator("xpath=ancestor::*[not(self::body) and not(self::html) and .//input[@type='file'] and (.//button[normalize-space(.)='完成'] or .//*[@role='button' and normalize-space(.)='完成'])][1]")
                    if await modern.count() == 1 and await modern.is_visible():
                        return modern, True
                legacy = page.locator("div.d-modal").filter(has_text="设置封面", visible=True)
                count = await legacy.count()
                if count > 1:
                    raise RuntimeError("封面弹窗不唯一，停止操作")
                return (legacy, False) if count == 1 else (None, False)

            modal, modern = await find_modal()
            if modal is None and await heading.count() == 0:
                preview = page.locator("div.cover-plugin-preview").filter(visible=True)
                if await preview.count() > 1:
                    raise RuntimeError("封面预览不唯一，停止操作")
                if await preview.count() == 1:
                    await preview.hover()
                edit = page.get_by_text("编辑封面", exact=True).filter(visible=True)
                try:
                    await edit.wait_for(state="visible", timeout=5000)
                except Exception:
                    pass
                if await edit.count() > 1:
                    raise RuntimeError("封面编辑入口不唯一，停止操作")
                if await edit.count() == 1:
                    await edit.click()
                else:
                    fallback = page.locator("div.upload-cover").filter(visible=True)
                    if await fallback.count() != 1:
                        raise RuntimeError("未找到唯一可见的封面编辑入口")
                    await fallback.click()
            deadline = time.monotonic() + 10
            while modal is None and time.monotonic() < deadline:
                modal, modern = await find_modal()
                if modal is None:
                    await asyncio.sleep(0.2)
            if modal is None:
                raise TimeoutError("未找到可见封面编辑器")

            if modern:
                file_input = modal.get_by_label("上传封面图片", exact=True)
                if await file_input.count() == 0:
                    file_input = modal.locator("input[type='file'][aria-label='上传封面图片']")
                confirm = modal.get_by_role("button", name="完成", exact=True).filter(visible=True)
            else:
                upload_tab = modal.get_by_text("上传封面", exact=True).filter(visible=True)
                await upload_tab.wait_for(state="visible", timeout=10000)
                if await upload_tab.count() != 1:
                    raise RuntimeError("封面上传选项不唯一")
                await upload_tab.click()
                file_input = modal.locator("input[type='file'][accept*='image']")
                confirm = modal.get_by_role("button", name="确定", exact=True).filter(visible=True)
            if await file_input.count() != 1:
                raise RuntimeError("封面图片输入控件不唯一")
            previous_preview = await _wait_for_initial_cover_preview(modal)
            await file_input.set_input_files(thumbnail_path)
            await confirm.wait_for(state="visible", timeout=10000)
            if await confirm.count() != 1:
                raise RuntimeError("封面保存按钮不唯一")
            await _wait_for_cover_preview(modal, confirm, previous_preview)
            await confirm.click(timeout=15000)
            await modal.wait_for(state="hidden", timeout=15000)
            xiaohongshu_logger.success(_msg("🥳", "封面已经设置完成"))
        except Exception as exc:
            record_diagnostic("xiaohongshu", "cover", exc)
            raise RuntimeError("封面设置失败，停止提交") from exc

    async def _find_resume_editor(self, context):
        """Inspect every retained page before permitting any mutation."""
        filename = re.compile(r"(?<![\w.-])" + re.escape(Path(self.file_path).name) + r"(?![\w.-])")
        matches = []
        try:
            for page in context.pages:
                url = urlsplit(page.url)
                if url.scheme != "https" or url.netloc != "creator.xiaohongshu.com" or url.path != "/publish/publish":
                    continue
                title = page.locator('input[placeholder*="填写标题"]').filter(visible=True)
                if await title.count() != 1 or await title.input_value(timeout=2000) != self.title:
                    continue
                if await page.get_by_text(filename).filter(visible=True).count() == 0:
                    continue
                editors = page.locator('[contenteditable="true"]').filter(visible=True)
                for index in range(await editors.count()):
                    text = await editors.nth(index).inner_text(timeout=2000)
                    if _resume_text_matches(text, self.desc, self.tags):
                        # Multiple matching editors, even within one page, are
                        # ambiguous. Do not silently choose the first field.
                        matches.append(page)
        except Exception as exc:
            error = publisher_handoff.HandoffError('editor_missing', "无法核对保留的原编辑页，未修改页面；请检查原窗口是否仍在")
            record_diagnostic('xiaohongshu', 'resume', error)
            raise error from exc
        if len(matches) != 1:
            error = publisher_handoff.HandoffError('editor_missing' if not matches else 'editor_ambiguous', "未找到唯一匹配原视频和文案的编辑页，未修改页面")
            record_diagnostic('xiaohongshu', 'resume', error)
            raise error
        return matches[0]

    async def resume_cover_from_editor(self, context) -> str:
        """Read-only matching first; resume never navigates or transfers video."""
        if not self.prepare_only or not self.thumbnail_path:
            raise RuntimeError("续接封面仅支持已指定封面的手动接手任务")
        page = await self._find_resume_editor(context)
        await self.wait_for_video_material_ready(page, timeout_seconds=30)
        # Readiness may take time. Recheck all tabs and the original content so
        # a user edit, replacement page, or second candidate cannot be adopted.
        if await self._find_resume_editor(context) is not page:
            error = publisher_handoff.HandoffError('editor_missing', "原编辑页已改变，未修改页面；请先核对原窗口")
            record_diagnostic('xiaohongshu', 'resume', error)
            raise error
        emit_progress("xiaohongshu", "cover")
        await self.set_thumbnail(page, self.thumbnail_path)
        emit_progress("xiaohongshu", "ready_to_submit")
        return "prepared"

    async def wait_for_video_material_ready(self, page: Page, timeout_seconds: float = 1800) -> None:
        """Editable metadata is not upload completion; wait before opening cover."""
        busy_pattern = re.compile(r"上传中(?:\s*\d+(?:\.\d+)?\s*%)?|正在上传|处理中|转码中|正在转码")
        failure_pattern = re.compile(r"上传失败|上传出错|上传异常|转码失败|处理失败")

        async def visible_status(pattern):
            matches = page.get_by_text(pattern)
            for index in range(await matches.count()):
                if await matches.nth(index).is_visible():
                    return True
            return False

        started = time.monotonic()
        deadline = started + timeout_seconds
        last_heartbeat = started - 15
        publish = page.get_by_role("button", name="发布", exact=True)
        while time.monotonic() < deadline:
            if await visible_status(failure_pattern):
                raise RuntimeError("视频素材上传或处理失败，停止提交")
            if (not await visible_status(busy_pattern) and await publish.count() == 1
                    and await publish.is_visible() and await publish.is_enabled()):
                xiaohongshu_logger.success(_msg("🥳", "视频已经传完，素材处理完成"))
                return
            current = time.monotonic()
            if current >= deadline:
                break
            if current - last_heartbeat >= 15:
                print("小红书视频仍在上传或处理中。", flush=True)
                last_heartbeat = current
            await asyncio.sleep(min(2, deadline - current))
        raise TimeoutError("等待视频素材上传或处理完成超时，停止提交")

    async def upload_video_content(self, page: Page) -> str | None:
        xiaohongshu_logger.info(_msg("🏃", f"小人开始搬运视频: {self.title}.mp4"))
        xiaohongshu_logger.info(_msg("🧭", "小人正在赶往视频发布页"))
        publish_url = _build_xhs_creator_url(
            "/publish/publish?from=homepage&target=video"
        )
        # Retry only opening the page before any file transfer. Waiting for
        # DOMContentLoaded avoids unrelated external resources blocking load.
        for attempt in range(2):
            input_selection_error = False
            try:
                await page.goto(publish_url, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_url(publish_url, wait_until="domcontentloaded", timeout=60000)
                upload_input = page.locator("input[type='file'][accept*='video'], input[type='file'][accept*='.mp4']")
                # first is only for waiting; file selection uses the strict
                # locator after checking uniqueness, never an arbitrary match.
                await upload_input.first.wait_for(state="attached", timeout=30000)
                if await upload_input.count() != 1:
                    input_selection_error = True
                    raise RuntimeError("未找到唯一的视频上传控件，未选择素材")
                if not await upload_input.is_enabled():
                    raise RuntimeError("视频上传控件尚未可用")
                break
            except Exception as exc:
                if input_selection_error or attempt == 1:
                    record_diagnostic("xiaohongshu", "upload" if input_selection_error else "navigation", exc)
                    raise
                xiaohongshu_logger.warning("小红书发布页暂未就绪，正在重新打开一次；尚未上传素材。")
        emit_progress("xiaohongshu", "uploading")
        try:
            await upload_input.set_input_files(self.file_path)
        except Exception as exc:
            record_diagnostic("xiaohongshu", "upload", exc)
            raise

        # The title editor can appear while a large video is still uploading.
        # It permits metadata entry only and must not unlock cover or submission.
        await page.locator('input[placeholder*="填写标题"]').wait_for(state="visible", timeout=180000)

        xiaohongshu_logger.info(_msg("✍️", "小人开始填标题、描述和话题"))
        emit_progress("xiaohongshu", "metadata")
        await self.fill_meta(page)
        await self.wait_for_video_material_ready(page)

        emit_progress("xiaohongshu", "cover")
        await self.set_thumbnail(page, self.thumbnail_path)

        if self.prepare_only:
            emit_progress("xiaohongshu", "ready_to_submit")
            xiaohongshu_logger.info("小红书素材、文案和封面已准备，编辑窗口保留；最终发布由使用者点击。")
            return "prepared"

        # await self.set_location(page, "青岛市")

        await self.check_original_declaration(page)

        if self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED and self.publish_date != 0:
            await self.set_schedule_time_xiaohongshu(page, self.publish_date)

        # One submit click only. An uncertain response must not create duplicates.
        emit_progress("xiaohongshu", "ready_to_submit")
        emit_progress("xiaohongshu", "submitting")
        if self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED:
            await page.locator('button:has-text("定时发布")').click()
        else:
            await page.get_by_role("button", name="发布", exact=True).click()
        try:
            await page.wait_for_url(XHS_PUBLISH_SUCCESS_URL_PATTERN, timeout=30000)
        except Exception as exc:
            raise RuntimeError("NEEDS_ATTENTION: 已点击提交但未观察到完成页面，请核验，勿直接重发") from exc
        emit_progress("xiaohongshu", "submitted")
        xiaohongshu_logger.success(_msg("🥳", "视频提交完成"))

    async def upload(self, playwright: Playwright) -> str | None:
        if self.resume_cover and not self.prepare_only:
            raise RuntimeError("续接封面仅支持小红书手动接手，未开始操作")
        if self.prepare_only and self.headless:
            raise RuntimeError("小红书手动接手需要可见窗口，未开始上传")
        xiaohongshu_logger.info(_msg("🧍", "小人先检查 cookie、视频文件、封面和发布时间"))
        if self.resume_cover:
            if not self.title or not self.thumbnail_path:
                raise ValueError("续接封面需要原始标题和指定封面")
            self.file_path = str(self.validate_video_file(self.file_path))
            self.thumbnail_path = str(self.validate_image_file(self.thumbnail_path))
        else:
            await self.validate_upload_args()
        xiaohongshu_logger.info(_msg("🥳", "上传前检查通过"))
        if self.prepare_only:
            options = {"require_existing": True} if self.resume_cover else {}
            try:
                browser, context = await publisher_handoff.connect_publisher_handoff(playwright, self.account_file, "xiaohongshu", **options)
            except Exception as exc:
                if self.resume_cover:
                    record_diagnostic('xiaohongshu', 'resume', exc)
                raise
        else:
            browser = await playwright.chromium.launch(headless=self.headless, channel="chromium")
            context = await browser.new_context(
                permissions=["geolocation"],
                storage_state=self.account_file,
            )
        if not self.resume_cover:
            context = await set_init_script(context)

        try:
            try:
                if self.resume_cover:
                    outcome = await self.resume_cover_from_editor(context)
                else:
                    page = await context.new_page()
                    outcome = await self.upload_video_content(page)
            except Exception:
                hold_seconds = 0 if self.prepare_only else _publisher_failure_hold_seconds(self.headless)
                if hold_seconds:
                    xiaohongshu_logger.warning("小红书失败现场已暂停，窗口将限时保留，自动流程不会继续操作页面。")
                    # No page interaction, retry, upload or submit during this
                    # pause. Cancellation may still stop the process normally.
                    await asyncio.sleep(hold_seconds)
                raise
            if self.prepare_only:
                if outcome != "prepared":
                    raise RuntimeError("小红书编辑准备未被确认完成，未记录手动接手成功")
                return outcome
            await context.storage_state(path=self.account_file)
            xiaohongshu_logger.success(_msg("🥳", "cookie 更新完毕"))
        finally:
            if not self.prepare_only:
                await context.close()
                await browser.close()

    async def xiaohongshu_upload_video(self):
        async with async_playwright() as playwright:
            return await self.upload(playwright)

    async def main(self):
        return await self.xiaohongshu_upload_video()


class XiaoHongShuNote(XiaoHongShuBaseUploader):
    def __init__(
        self,
        image_paths,
        note,
        tags,
        publish_date: datetime | int,
        account_file,
        title: str | None = None,
        desc: str | None = None,
        publish_strategy: str = XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
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
        self.tags = tags or []
        self.desc = desc if desc is not None else self.note
        self.title = title or ((self.desc or self.note)[:20] if (self.desc or self.note) else "")

    async def validate_upload_args(self):
        await self.validate_base_args()
        if not self.image_paths:
            raise ValueError("图文模式下，图片是必须的")
        if not self.title or not str(self.title).strip():
            raise ValueError("图文模式下，title 是必须的")

        if isinstance(self.image_paths, (str, Path)):
            self.image_paths = [self.image_paths]

        normalized_image_paths = []
        for image_path in self.image_paths:
            normalized_image_paths.append(str(self.validate_image_file(image_path)))
        self.image_paths = normalized_image_paths

    async def upload_note_content(self, page: Page) -> None:
        xiaohongshu_logger.info(_msg("🏃", f"小人开始搬运图文，共 {len(self.image_paths)} 张图片"))
        xiaohongshu_logger.info(_msg("🧭", "小人正在赶往图文发布页"))
        publish_url = _build_xhs_creator_url(
            "/publish/publish?from=homepage&target=image"
        )
        await page.goto(publish_url)
        await page.wait_for_url(publish_url)

        upload_input = page.locator('input[type="file"][accept*="image"]').first
        if not await upload_input.count():
            upload_input = page.locator("div[class^='upload-content'] input[class='upload-input']").first

        await upload_input.wait_for(state="attached", timeout=30000)
        xiaohongshu_logger.info(_msg("📤", "小人正在上传图片"))
        await upload_input.set_input_files(self.image_paths)

        while True:
            try:
                title_container = page.locator('input[placeholder*="填写标题"]').first
                await title_container.wait_for(state="visible", timeout=3000)
                xiaohongshu_logger.success(_msg("🥳", "图文素材已经传完，可以开始填写内容了"))
                break
            except Exception:
                xiaohongshu_logger.debug(_msg("🧍", "图文素材还在上传，小人继续等一会"))
                await asyncio.sleep(1)

        xiaohongshu_logger.info(_msg("✍️", "小人开始填标题、描述和话题"))
        await self.fill_meta(page)

        await self.check_original_declaration(page)

        if self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED and self.publish_date != 0:
            await self.set_schedule_time_xiaohongshu(page, self.publish_date)

        while True:
            try:
                if self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED:
                    await page.locator('button:has-text("定时发布")').click()
                else:
                    await page.locator('button:has-text("发布")').click()
                await page.wait_for_url(
                    XHS_PUBLISH_SUCCESS_URL_PATTERN,
                    timeout=3000
                )
                xiaohongshu_logger.success(_msg("🥳", "图文发布成功，小人开心收工"))
                break
            except Exception:
                xiaohongshu_logger.info(_msg("🏃", "小人正在冲刺发布图文"))
                if self.debug:
                    await page.screenshot(full_page=True)
                await asyncio.sleep(0.5)

    async def upload(self, playwright: Playwright) -> None:
        xiaohongshu_logger.info(_msg("🧍", "小人先检查 cookie、图片和发布时间"))
        await self.validate_upload_args()
        xiaohongshu_logger.info(_msg("🥳", "图文上传前检查通过"))
        browser = await playwright.chromium.launch(headless=self.headless, channel="chromium")
        context = await browser.new_context(
            permissions=["geolocation"],
            storage_state=self.account_file,
        )
        context = await set_init_script(context)

        try:
            page = await context.new_page()
            await self.upload_note_content(page)
            await context.storage_state(path=self.account_file)
            xiaohongshu_logger.success(_msg("🥳", "cookie 更新完毕"))
        finally:
            await context.close()
            await browser.close()

    async def xiaohongshu_upload_note(self):
        async with async_playwright() as playwright:
            await self.upload(playwright)

    async def main(self):
        await self.xiaohongshu_upload_note()
