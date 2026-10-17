"""Opt-in L2: sandboxed Chromium, intercepted fixtures only, no platform traffic.

PUBLISHER_TEST_DOM_BROWSER=1 PUBLISHER_TEST_CHROMIUM=/usr/bin/chromium \\
  PYTHONPATH=vendor/social-auto-upload .venv/bin/python -m unittest tests.test_primary_material_browser -v

If the host cannot start a sandboxed browser the opted-in suite fails explicitly.
No test disables the Chromium sandbox or reuses a real profile/session.
"""
import asyncio
import os
import tempfile
from pathlib import Path
import unittest

from patchright.async_api import async_playwright
from PIL import Image

from utils.publisher_materials import preview_state, wait_changed_preview
from uploader.youtube_uploader import main as yt
from uploader.douyin_uploader import main as dy


FIXTURE = """<!doctype html><meta charset=utf-8>
<style>ytcp-thumbnail-uploader {display:block;width:360px;height:240px} img,canvas {width:240px;height:160px}
[hidden] {display:none!important}</style>
<ytcp-thumbnail-uploader hidden><input type=file id=stale><canvas width=240 height=160></canvas></ytcp-thumbnail-uploader>
<ytcp-thumbnail-uploader id=editor><input type=file id=cover><img id=preview>
<div role=progressbar hidden id=busy></div><div role=alert hidden id=error>Upload failed</div></ytcp-thumbnail-uploader>
<button id=done onclick="window.finalClicks++">Publish</button>
<script>
window.finalClicks=0;window.delay=100;window.fail=false;window.unchanged=false;
const canvas=document.createElement('canvas');canvas.width=240;canvas.height=160;
canvas.getContext('2d').fillRect(0,0,240,160);preview.src=canvas.toDataURL();
cover.onchange=()=>{busy.hidden=false;setTimeout(()=>{
 if(window.fail){error.hidden=false;}else if(!window.unchanged){preview.src=URL.createObjectURL(cover.files[0]);}
 busy.hidden=true;
},window.delay);};
</script>"""


@unittest.skipUnless(os.environ.get("PUBLISHER_TEST_DOM_BROWSER") == "1", "sandboxed fixture browser opt-in")
class PrimaryMaterialBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = await async_playwright().start()
        self.addAsyncCleanup(self.manager.stop)
        self.browser = await self.manager.chromium.launch(
            executable_path=os.environ.get("PUBLISHER_TEST_CHROMIUM", "/usr/bin/chromium"),
            headless=True, chromium_sandbox=True)
        self.addAsyncCleanup(self.browser.close)
        self.context = await self.browser.new_context()
        await self.context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=FIXTURE))
        self.page = await self.context.new_page()
        await self.page.goto("https://publisher-fixture.invalid/")
        self.temporary = tempfile.TemporaryDirectory(prefix="publisher-dom-fixture-")
        self.addCleanup(self.temporary.cleanup)
        self.cover = Path(self.temporary.name) / "synthetic-cover.png"
        Image.new("RGB", (320, 200), (30, 180, 70)).save(self.cover)
        await self.page.locator("#preview").evaluate("img => img.decode()")

    async def test_youtube_delayed_image_ignores_hidden_old_input_and_never_clicks_publish(self):
        await self.page.evaluate("window.delay=700")
        await yt._set_verified_thumbnail(self.page, str(self.cover))
        self.assertEqual(await self.page.locator("#cover").evaluate("el=>el.files.length"), 1)
        self.assertEqual(await self.page.locator("#stale").evaluate("el=>el.files.length"), 0)
        self.assertEqual(await self.page.evaluate("window.finalClicks"), 0)
        self.assertFalse(self.page.is_closed())

    async def test_unchanged_preview_fails_even_when_done_is_enabled(self):
        root = self.page.locator("#editor")
        before = await preview_state(root)
        await self.page.evaluate("window.unchanged=true")
        await self.page.locator("#cover").set_input_files(str(self.cover))
        with self.assertRaisesRegex(RuntimeError, "未完成更新"):
            await wait_changed_preview(root, before, timeout_seconds=.4, sample_interval=.05)
        self.assertTrue(await self.page.locator("#done").is_enabled())
        self.assertEqual(await self.page.evaluate("window.finalClicks"), 0)

    async def test_visible_error_and_unreadable_canvas_are_not_evidence(self):
        root = self.page.locator("#editor")
        before = await preview_state(root)
        await self.page.evaluate("window.fail=true")
        await self.page.locator("#cover").set_input_files(str(self.cover))
        with self.assertRaisesRegex(RuntimeError, "处理失败"):
            await wait_changed_preview(root, before, timeout_seconds=.5, sample_interval=.05)
        await root.evaluate("""root=>{root.innerHTML='<canvas width=240 height=160></canvas>';
            const getContext=HTMLCanvasElement.prototype.getContext;
            HTMLCanvasElement.prototype.getContext=function(...args){const ctx=getContext.apply(this,args);
                ctx.getImageData=()=>{throw new DOMException('synthetic tainted pixels','SecurityError')};return ctx;};} """)
        self.assertFalse((await preview_state(root))["ready"])

    async def test_new_url_with_same_pixels_and_unrelated_thumbnail_cannot_pass(self):
        root = self.page.locator("#editor")
        before = await preview_state(root)
        await root.evaluate("""root=>{const img=root.querySelector('img');const canvas=document.createElement('canvas');
            canvas.width=240;canvas.height=160;canvas.getContext('2d').fillRect(0,0,240,160);
            canvas.toBlob(blob=>{img.src=URL.createObjectURL(blob)});} """)
        await asyncio.sleep(.05)
        await self.page.locator("#preview").evaluate("img=>img.decode()")
        self.assertEqual((await preview_state(root))["signature"], before["signature"])
        await root.evaluate("""root=>{const ref=document.createElement('canvas');ref.width=120;ref.height=80;
            ref.style.width='120px';ref.style.height='80px';ref.getContext('2d').fillStyle='red';
            ref.getContext('2d').fillRect(0,0,120,80);root.append(ref);} """)
        self.assertEqual((await preview_state(root))["signature"], before["signature"])
        with self.assertRaisesRegex(RuntimeError, "未完成更新"):
            await wait_changed_preview(root, before, timeout_seconds=.2, sample_interval=.02)

    async def test_douyin_hidden_reupload_does_not_prove_completion(self):
        await self.page.set_content("""<div class=long-card-test><div hidden>重新上传</div></div>
            <div class=progress-div>上传中 50%</div><button>发布</button>""")
        state = await dy._upload_state(self.page)
        self.assertEqual(state, dict(completed=False, error=False, busy=True))
        await self.page.locator(".long-card-test div").evaluate("el=>el.hidden=false")
        await self.page.locator(".progress-div").evaluate("el=>el.remove()")
        self.assertEqual(await dy._upload_state(self.page), dict(completed=True, error=False, busy=False))

    async def test_youtube_hidden_complete_does_not_override_visible_progress(self):
        await self.page.set_content('<span class=progress-label hidden>Upload complete</span><span class=progress-label>Processing</span>')
        # Poll the real DOM contract once without waiting a five-second interval.
        controls = self.page.locator('.progress-label').filter(visible=True)
        self.assertEqual(await controls.count(), 1)
        self.assertEqual(await controls.inner_text(), 'Processing')


if __name__ == "__main__":
    unittest.main()
