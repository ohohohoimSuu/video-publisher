import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import AsyncMock, Mock, patch

import adapter
import server
from uploader.douyin_uploader import main as dy
from utils import publisher_progress as progress

ROOT=Path(__file__).resolve().parents[1]

class UploadProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_navigation_can_retry_but_file_selection_is_never_repeated(self):
        uploader=dy.DouYinVideo('fixture','synthetic.mp4',[],0,'unused',headless=False,prepare_only=True)
        for fail_file in (False,True):
            upload=SimpleNamespace(wait_for=AsyncMock(),count=AsyncMock(return_value=1),set_input_files=AsyncMock())
            upload.first=upload
            page=SimpleNamespace(goto=AsyncMock(side_effect=[dy.BrowserTimeoutError('before file'),None] if not fail_file else None),
                locator=lambda _:upload,wait_for_url=AsyncMock())
            if fail_file:upload.set_input_files.side_effect=dy.BrowserTimeoutError('after selection attempt')
            context=SimpleNamespace(new_page=AsyncMock(return_value=page))
            uploader.fill_title_and_description=AsyncMock();uploader.apply_self_declaration=AsyncMock()
            uploader.apply_collection=AsyncMock();uploader.set_thumbnail=AsyncMock();uploader.finish_upload=AsyncMock(return_value='prepared')
            with patch.object(dy,'_wait_douyin_upload',AsyncMock()),patch.object(dy,'emit_progress'),patch.object(dy,'record_diagnostic'):
                if fail_file:
                    with self.assertRaises(dy.BrowserTimeoutError):await uploader._upload_video_page(context,object())
                else:self.assertEqual(await uploader._upload_video_page(context,object()),'prepared')
            self.assertEqual(page.goto.await_count,1 if fail_file else 2)
            upload.set_input_files.assert_awaited_once_with('synthetic.mp4')
            if not fail_file:self.assertEqual(page.wait_for_url.call_args.kwargs['wait_until'],'domcontentloaded')

    async def test_cover_preview_preserves_proven_video_but_never_accepts_busy_or_empty_slot(self):
        from tests.test_primary_material_readiness import Control
        button=Control();empty=Control(count=0)
        state=dict(completed=False,busy=False,error=False,selected=True,empty=False)
        page=self.page();page.locator=lambda _:empty;page.get_by_role=lambda *a,**k:button
        with patch.object(dy,'_upload_state',AsyncMock(return_value=state)):
            with self.assertRaises(RuntimeError):await dy._require_douyin_handoff(page)
            await dy._require_douyin_handoff(page,video_confirmed=True)
            for change in ({'busy':True},{'empty':True},{'error':True},{'selected':False}):
                with patch.object(dy,'_upload_state',AsyncMock(return_value={**state,**change})):
                    with self.assertRaises(RuntimeError):await dy._require_douyin_handoff(page,video_confirmed=True)
        button.click.assert_not_awaited()

    def page(self):
        return SimpleNamespace(url='https://creator.douyin.com/creator-micro/content/post/video',
            is_closed=lambda:False,wait_for_timeout=AsyncMock())

    async def test_transfer_processing_then_ready_without_early_metadata(self):
        states=[dict(completed=False,busy=True,percent=42),dict(completed=False,busy=True,processing=True),
                dict(completed=True,busy=False),dict(completed=True,busy=False)]
        with patch.object(dy,'_upload_state',AsyncMock(side_effect=states)),patch.object(dy,'emit_progress') as emit:
            await dy._wait_douyin_upload(self.page(),max_polls=4)
        self.assertEqual([c.args[1] for c in emit.call_args_list],['uploading','processing'])
        self.assertEqual(emit.call_args_list[0].kwargs,{'percent':42})
        self.assertEqual(emit.call_args_list[1].kwargs,{})

    async def test_empty_upload_slot_fails_instead_of_waiting_thirty_minutes(self):
        with patch.object(dy,'_upload_state',AsyncMock(return_value=dict(completed=False,busy=False,empty=True))),patch.object(dy,'emit_progress'),patch.object(dy,'record_diagnostic') as diagnostic:
            page=self.page()
            with self.assertRaisesRegex(RuntimeError,'空的视频上传区域'):
                await dy._wait_douyin_upload(page,max_polls=30)
        self.assertEqual(page.wait_for_timeout.await_count,14)
        self.assertEqual(diagnostic.call_args.args[1],'upload')

    async def test_unknown_percent_not_invented_and_error_preserves_upload_stage(self):
        with patch.object(dy,'_upload_state',AsyncMock(side_effect=[dict(completed=False,busy=True),dict(error=True)])),patch.object(dy,'emit_progress') as emit,patch.object(dy,'record_diagnostic') as diagnostic:
            with self.assertRaises(RuntimeError):await dy._wait_douyin_upload(self.page())
        self.assertEqual(emit.call_args.kwargs,{})
        self.assertEqual(diagnostic.call_args.args[1],'upload')

    async def test_normal_chrome_upload_skips_legacy_stealth(self):
        uploader=dy.DouYinVideo('fixture','/tmp/fixture.mp4',[],0,'unused',headless=False,prepare_only=True)
        context=object();browser=object()
        uploader.validate_upload_args=AsyncMock()
        uploader._upload_video_page=AsyncMock(return_value='prepared')
        with patch.object(dy,'connect_douyin_handoff',AsyncMock(return_value=(browser,context))),patch('utils.publisher_handoff.native_chrome_endpoint',return_value='synthetic-local-endpoint'),patch.object(dy,'set_init_script',AsyncMock()) as stealth:
            self.assertEqual(await uploader.upload(object()),'prepared')
        stealth.assert_not_awaited();uploader._upload_video_page.assert_awaited_once_with(context,browser)

class EvidenceTests(unittest.TestCase):
    def test_only_bounded_observed_percent_crosses_progress_channel(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'progress.json'
            env={'PUBLISHER_PROGRESS_FILE':str(path),'PUBLISHER_RESULT_NONCE':'fixture','PUBLISHER_PROGRESS_ACTION':'publish','PUBLISHER_PROGRESS_PLATFORM':'douyin'}
            with patch.dict(os.environ,env):
                for invalid in (-1,101,True,float('nan'),'42'):
                    with self.assertRaises(ValueError):progress.emit_progress('douyin','uploading',percent=invalid)
                progress.emit_progress('douyin','uploading',percent=42.5)
                value=adapter._read_progress(path,'fixture','douyin','publish')
                self.assertEqual(value['percent'],42.5)
                progress.emit_progress('douyin','processing')
                self.assertNotIn('percent',adapter._read_progress(path,'fixture','douyin','publish'))
                raw=json.loads(path.read_text());raw['percent']=float('nan');path.write_text(json.dumps(raw))
                self.assertIsNone(adapter._read_progress(path,'fixture','douyin','publish'))

    def test_server_retains_phase_start_and_history_for_percent_updates(self):
        with tempfile.TemporaryDirectory() as folder:
            app=server.create_app(folder,start_worker=False);m=app.extensions['manager'];s=app.extensions['store']
            j=m.create('publish',['douyin'])
            m.progress(j['id'],0,{'phase':'uploading','percent':21})
            old=s.get('job',j['id'])['results'][0]['phase_started_at']
            m.progress(j['id'],0,{'phase':'uploading','percent':45})
            r=s.get('job',j['id'])['results'][0]
            self.assertEqual(r['phase_started_at'],old);self.assertEqual(r['percent'],45);self.assertEqual(len(r['phase_history']),1)
            m.progress(j['id'],0,{'phase':'processing'})
            r=s.get('job',j['id'])['results'][0]
            self.assertNotIn('percent',r);self.assertEqual([x['phase'] for x in r['phase_history']],['uploading','processing'])

@unittest.skipUnless(os.environ.get('PUBLISHER_TEST_BROWSER')=='1','isolated browser fixture only')
class BrowserRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_cdn_thumbnail_verified_without_disabling_canvas_security(self):
        import io
        from PIL import Image
        from patchright.async_api import async_playwright
        def png(color):
            stream=io.BytesIO();Image.new('RGB',(160,120),color).save(stream,format='PNG');return stream.getvalue()
        with patch.dict(os.environ,PLAYWRIGHT_BROWSERS_PATH=str(ROOT/'.browsers')):
            async with async_playwright() as p:
                browser=await p.chromium.launch(headless=True,chromium_sandbox=True)
                try:
                    page=await browser.new_page()
                    await page.route('https://preview.test/',lambda r:r.fulfill(content_type='text/html',body='<div id="cover"><img width="160" height="120" src="https://images.test/first.png"></div>'))
                    await page.route('https://images.test/*',lambda r:r.fulfill(content_type='image/png',body=png('blue' if r.request.url.endswith('first.png') else 'green')))
                    await page.goto('https://preview.test/')
                    before=await dy._wait_saved_cover(page,page.locator('#cover'))
                    blocked=await page.evaluate('''() => {const c=document.createElement('canvas');c.getContext('2d').drawImage(document.querySelector('img'),0,0);try{c.getContext('2d').getImageData(0,0,1,1);return false}catch(_){return true}}''')
                    self.assertTrue(blocked)
                    with self.assertRaises(RuntimeError):await dy._wait_saved_cover(page,page.locator('#cover'),before,timeout_seconds=1)
                    await page.evaluate("document.querySelector('img').src='https://images.test/second.png'")
                    after=await dy._wait_saved_cover(page,page.locator('#cover'),before)
                    self.assertNotEqual(before,after)
                finally:await browser.close()

    async def test_lower_canvas_evidence_ignores_transparent_interaction_overlay(self):
        from patchright.async_api import async_playwright
        from utils.publisher_materials import preview_state
        with patch.dict(os.environ,PLAYWRIGHT_BROWSERS_PATH=str(ROOT/'.browsers')):
            async with async_playwright() as p:
                browser=await p.chromium.launch(headless=True,chromium_sandbox=True)
                try:
                    page=await browser.new_page()
                    await page.route('https://preview.test/',lambda r:r.fulfill(content_type='text/html',body='<div id="editor" style="width:400px;height:240px"><canvas class="lower-canvas" width="400" height="240"></canvas><canvas class="upper-canvas" width="400" height="240"></canvas></div>'))
                    await page.goto('https://preview.test/')
                    root=page.locator('#editor')
                    self.assertFalse((await preview_state(root))['ready'])
                    before=await preview_state(root,selector='canvas.lower-canvas')
                    self.assertTrue(before['ready'])
                    await page.evaluate("const c=document.querySelector('.upper-canvas').getContext('2d');c.fillStyle='red';c.fillRect(0,0,400,240)")
                    self.assertEqual((await preview_state(root,selector='canvas.lower-canvas'))['signature'],before['signature'])
                    await page.evaluate("const c=document.querySelector('.lower-canvas').getContext('2d');c.fillStyle='blue';c.fillRect(0,0,400,240)")
                    self.assertNotEqual((await preview_state(root,selector='canvas.lower-canvas'))['signature'],before['signature'])
                finally:await browser.close()

    async def test_legacy_script_reproduces_readonly_create_element_and_native_context_works(self):
        from patchright.async_api import async_playwright
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers()
                self.wfile.write(b'<body><script>let accepted=false;try{(()=>{"use strict";const previous=document.createElement;document.createElement=function(...args){return previous.apply(this,args)};})();accepted=true}catch(_){};document.body.dataset.accepted=String(accepted);document.body.dataset.writable=String(Object.getOwnPropertyDescriptor(document,"createElement")?.writable);</script>')
            def log_message(self,*args):pass
        fixture=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=fixture.serve_forever,daemon=True).start()
        self.addCleanup(fixture.server_close);self.addCleanup(fixture.shutdown)
        with patch.dict(os.environ,PLAYWRIGHT_BROWSERS_PATH=str(ROOT/'.browsers')):
            async with async_playwright() as p:
                browser=await p.chromium.launch(headless=True,chromium_sandbox=True)
                try:
                    results=[]
                    for legacy in (True,False):
                        context=await browser.new_context()
                        if legacy:await dy.set_init_script(context)
                        page=await context.new_page()
                        # Run like the site's own SDK in the main world; the
                        # automation evaluator has a separate isolated world.
                        await page.goto('http://127.0.0.1:'+str(fixture.server_port))
                        result=await page.evaluate('''() => ({accepted:document.body.dataset.accepted==='true',writable:document.body.dataset.writable==='true'})''')
                        results.append(result);await context.close()
                    self.assertFalse(results[0]['accepted']);self.assertFalse(results[0]['writable'])
                    self.assertTrue(results[1]['accepted'])
                finally:await browser.close()

    async def test_dom_progress_needs_positive_completion_not_just_hundred_percent(self):
        from patchright.async_api import async_playwright
        with patch.dict(os.environ,PLAYWRIGHT_BROWSERS_PATH=str(ROOT/'.browsers')):
            async with async_playwright() as p:
                browser=await p.chromium.launch(headless=True,chromium_sandbox=True)
                try:
                    page=await browser.new_page()
                    for html,expected in [('<div>上传中 42%</div>',{'completed':False,'percent':42,'busy':True}),
                         ('<div>100%</div><div>视频处理中</div>',{'completed':False,'processing':True}),
                         ('<div>上传完成</div>',{'completed':True,'busy':False}),
                         ('<div contenteditable="true">上传失败</div><div>上传完成</div>',{'completed':True,'error':False}),
                         ('<div class="postAssistant-fixture"><div role="progressbar">检测中9%</div></div><div>上传完成</div>',{'completed':True,'busy':False,'processing':False}),
                         ('<div>点击上传</div>',{'completed':False,'empty':True}),
                         ('<div>上传失败</div>',{'completed':False,'error':True})]:
                        await page.set_content(html)
                        actual=await dy._upload_state(page)
                        for key,value in expected.items():self.assertEqual(actual[key],value,(html,actual))
                finally:await browser.close()

    async def test_orientation_uses_header_step_not_duplicate_footer_button(self):
        from patchright.async_api import async_playwright
        with patch.dict(os.environ,PLAYWRIGHT_BROWSERS_PATH=str(ROOT/'.browsers')):
            async with async_playwright() as p:
                browser=await p.chromium.launch(headless=True,chromium_sandbox=True)
                try:
                    page=await browser.new_page()
                    await page.set_content('''<div id="dialog"><div class="steps-fixture"><div class="step-horizontal step-active-fixture" onclick="this.className='step-horizontal step-active-fixture';this.nextElementSibling.className='step-portrait'">设置横封面</div><div class="step-portrait" onclick="this.className='step-portrait step-active-fixture';this.previousElementSibling.className='step-horizontal'">设置竖封面</div></div><div class="cloudArea-fixture" style="width:300px;height:200px">canvas fixture</div><button onclick="document.body.dataset.wrong='true'">设置竖封面</button></div>''')
                    upload=SimpleNamespace(count=AsyncMock(return_value=1),set_input_files=AsyncMock())
                    with patch.object(dy,'wait_initial_preview',AsyncMock(return_value={'busy':False,'error':False,'signature':'original'})),patch.object(dy,'wait_changed_preview',AsyncMock()):
                        await dy._upload_cover_slot(page,page.locator('#dialog'),upload,'设置竖封面','synthetic-portrait.jpg')
                        await dy._upload_cover_slot(page,page.locator('#dialog'),upload,'设置横封面','synthetic-landscape.jpg')
                    self.assertIsNone(await page.locator('body').get_attribute('data-wrong'))
                    self.assertEqual(upload.set_input_files.await_count,2)
                finally:await browser.close()

if __name__=='__main__':unittest.main()
