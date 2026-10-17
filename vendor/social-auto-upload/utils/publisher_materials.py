"""Read-only DOM evidence for requested material, never a submission action.

Only digests and booleans leave page memory. These checks prove a controlled DOM
contract, not that any particular live platform has retained a server-side draft.
"""
from __future__ import annotations

import asyncio
import time


async def preview_state(root, *, selector=None):
    """Inspect one visible editor; invisible/stale previews supply no evidence."""
    script = """async (root, selector = 'img, canvas') => {
        const visible = el => {
            if (!el || !el.isConnected || !el.getClientRects().length) return false;
            for (let p = el; p; p = p.parentElement) {
                const s = getComputedStyle(p);
                if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
            }
            return true;
        };
        if (!visible(root)) return {signature:null, ready:false, busy:false, error:false};
        const busy = [...root.querySelectorAll('[aria-busy="true"], [role="progressbar"], '
            + '.semi-spin-spinning, .semi-upload-item-uploading')].some(visible);
        const error = [...root.querySelectorAll('[role="alert"], [aria-invalid="true"], '
            + '.semi-upload-item-error, #error-message, .error-message')]
            .some(el => visible(el) && (el.textContent.trim() || el.getAttribute('aria-invalid') === 'true'));
        const unavailable = {signature:null, ready:false, busy, error};
        const candidates = [...root.querySelectorAll(selector)].filter(el => {
            const rect = el.getBoundingClientRect();
            return visible(el) && rect.width >= 100 && rect.height >= 80;
        }).map(el => ({el, area:el.getBoundingClientRect().width * el.getBoundingClientRect().height}))
          .sort((a,b)=>b.area-a.area);
        // A changing thumbnail or AI reference image cannot prove the main
        // preview changed; similarly sized simultaneous previews are ambiguous.
        if (!candidates.length || (candidates.length > 1 && candidates[1].area >= candidates[0].area * .75)) return unavailable;
        const el = candidates[0].el;
        const isImage = el.tagName === 'IMG';
        if ((isImage && (!el.complete || !el.naturalWidth || !el.naturalHeight)) ||
            (!isImage && (!el.width || !el.height))) return unavailable;
        try {
            // Normalize the sample dimensions: a new URL or different source
            // dimensions with identical pixels must not pass as a new cover.
            const sample = document.createElement('canvas');
            sample.width = 256; sample.height = 256;
            const ctx = sample.getContext('2d', {willReadFrequently:true});
            ctx.drawImage(el, 0, 0, 256, 256);
            const pixels = ctx.getImageData(0, 0, 256, 256).data;
            const digest = await crypto.subtle.digest('SHA-256', pixels);
            return {signature:[...new Uint8Array(digest)].map(v=>v.toString(16).padStart(2,'0')).join(''),
                    ready:!busy && !error, busy, error};
        } catch (_) {
            // Tainted/unreadable pixel data supplies no completion evidence.
            return unavailable;
        }
    }"""
    state = await root.evaluate(script, selector) if selector else await root.evaluate(script)
    if not isinstance(state, dict) or set(state) != {"signature", "ready", "busy", "error"}:
        raise RuntimeError("NEEDS_ATTENTION: 无法核对封面预览状态；请保留编辑窗口手动检查")
    return state


async def wait_initial_preview(root, *, timeout_seconds=10, sample_interval=0.5, selector=None):
    """Establish a stable old frame before attaching the requested cover."""
    deadline = time.monotonic() + timeout_seconds
    last, stable = None, 0
    while time.monotonic() < deadline:
        current = await preview_state(root, **({'selector':selector} if selector else {}))
        if current["error"]:
            raise RuntimeError("NEEDS_ATTENTION: 封面编辑器报错，请在保留窗口检查")
        signature = current["signature"]
        if current["ready"] is True and signature:
            stable = stable + 1 if signature == last else 1
            last = signature
            if stable >= 3:
                return current
        else:
            last, stable = None, 0
        await asyncio.sleep(sample_interval)
    raise RuntimeError("NEEDS_ATTENTION: 原封面预览尚未就绪或无法读取，未选择新文件；请保留窗口检查")


async def wait_changed_preview(root, previous, *, timeout_seconds=30, sample_interval=0.5, selector=None):
    """Require three loaded, error-free, stable observations after selection."""
    deadline = time.monotonic() + timeout_seconds
    last, stable = None, 0
    while time.monotonic() < deadline:
        current = await preview_state(root, **({'selector':selector} if selector else {}))
        if current["error"]:
            raise RuntimeError("NEEDS_ATTENTION: 平台提示封面处理失败；请在保留窗口检查指定封面")
        signature = current["signature"]
        if signature and signature != previous.get("signature") and current["ready"] is True:
            stable = stable + 1 if signature == last else 1
            last = signature
            if stable >= 3:
                return current
        else:
            stable, last = 0, None
        await asyncio.sleep(sample_interval)
    raise RuntimeError("NEEDS_ATTENTION: 指定封面预览未完成更新，未确认保存；请在保留窗口检查")


async def unique_visible(locator, label):
    visible = locator.filter(visible=True)
    if await visible.count() != 1:
        raise RuntimeError(f"NEEDS_ATTENTION: {label}缺失或不唯一；请在保留窗口检查")
    return visible


async def enabled_control(locator):
    return (await locator.count() == 1 and await locator.is_visible()
            and await locator.is_enabled()
            and await locator.get_attribute("aria-disabled") != "true"
            and await locator.get_attribute("disabled") is None
            and "semi-button-disabled" not in (await locator.get_attribute("class") or ""))
