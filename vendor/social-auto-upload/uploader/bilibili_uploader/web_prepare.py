"""Bilibili's visible web editor. This module never submits or saves a draft."""
from __future__ import annotations
import asyncio
import base64
import json
import time
from pathlib import Path
from patchright.async_api import async_playwright, Error as BrowserError
from utils import publisher_handoff
from utils.publisher_progress import emit_progress
from uploader.base_video import BaseVideoUploader

UPLOAD_URL = 'https://member.bilibili.com/platform/upload/video/frame'
CATEGORIES = ('影视','娱乐','音乐','舞蹈','动画','绘画','鬼畜','游戏','资讯','知识','人工智能','科技数码','汽车','时尚美妆','家装房产','户外潮流','健身','体育运动','手工','美食','小剧场','旅游出行','三农','动物','亲子','健康','情感','vlog','生活兴趣','生活经验')
DECLARATIONS = {'none':'内容无需标注','ai':'含AI生成内容','fiction':'含虚构演绎内容','marketing':'内容含营销信息','opinion':'个人观点，仅供参考','repost':'内容为转载'}

class BilibiliWebPrepare:
    def __init__(self, request, account_file):
        self.request = request
        self.account_file = Path(account_file)

    def validate(self):
        r = self.request
        if not r.prepare_only or r.publish_date:
            raise ValueError('B站网页流程仅支持准备后本人发布，不支持自动或定时投稿')
        if not r.title.strip() or len(r.title) > 80 or len(r.description) > 2000:
            raise ValueError('B站标题或简介超出限制')
        if not 1 <= len(r.tags) <= 10 or len(set(r.tags)) != len(r.tags) or any(not t or len(t)>20 for t in r.tags):
            raise ValueError('B站需要1至10个不同标签，每个不超过20字')
        if r.category not in CATEGORIES or r.declaration not in DECLARATIONS:
            raise ValueError('请选择当前 B站分区和创作声明')
        self.video = BaseVideoUploader.validate_video_file(r.video_file)
        self.cover = BaseVideoUploader.validate_image_file(r.thumbnail_file)

    async def verify_account(self, context):
        cookies, _, _ = publisher_handoff._load_session(self.account_file, 'bilibili')
        expected = next(c['value'] for c in cookies if c['name']=='DedeUserID')
        # Only retry the read-only identity request; never retry file transfer.
        for attempt in range(2):
            try:
                response = await context.request.get('https://api.bilibili.com/x/web-interface/nav', timeout=20000)
                break
            except BrowserError:
                if attempt:
                    raise RuntimeError('B站在线账号核验连接失败，视频尚未上传') from None
                await asyncio.sleep(.5)
        if response.url.split('?')[0] != 'https://api.bilibili.com/x/web-interface/nav':
            raise RuntimeError('B站账号核验发生异常跳转')
        value = await response.json()
        if value.get('code') != 0 or value.get('data',{}).get('isLogin') is not True or str(value.get('data',{}).get('mid')) != expected:
            raise RuntimeError('B站专用窗口账号与已授权账号不一致，未上传')

    async def wait_uploaded(self, page, timeout=1800):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if await page.get_by_text('上传失败',exact=False).filter(visible=True).count():
                raise RuntimeError('B站视频上传失败，未继续填写')
            if await page.get_by_text('上传完成',exact=True).filter(visible=True).count():
                return
            await asyncio.sleep(1)
        raise TimeoutError('B站视频上传超时')

    async def fill_metadata(self, page):
        r=self.request
        await page.get_by_placeholder('请输入稿件标题',exact=True).fill(r.title)
        desc=page.locator('.desc-text-wrp .ql-editor[contenteditable="true"]').filter(visible=True)
        await desc.fill(r.description)
        statement=page.locator('.statement-content .bcc-select-input-wrap').filter(visible=True)
        await statement.click()
        await page.locator('.statement-content .bcc-option').filter(has_text=DECLARATIONS[r.declaration],visible=True).click()
        category=page.locator('.select-controller').filter(visible=True)
        await category.click()
        await page.get_by_text(r.category,exact=True).filter(visible=True).last.click()
        tags=page.locator('.tag-input-wrp').filter(visible=True)
        if await tags.count()!=1:
            raise RuntimeError('B站标签编辑器不唯一')
        # Remove inherited/recommended tags from this new form before filling.
        for _ in range(10):
            close=tags.locator('.label-item-v2-container .close')
            if not await close.count():break
            await close.first.click()
        if await tags.locator('.label-item-v2-content').count():
            raise RuntimeError('B站旧标签未清空')
        field=tags.get_by_placeholder('按回车键Enter创建标签',exact=True)
        for tag in r.tags:
            await field.fill(tag)
            await field.press('Enter')
            await tags.locator('.label-item-v2-content').get_by_text(tag,exact=True).wait_for(state='visible',timeout=10000)
        await field.press('Tab')
        await self.verify_metadata(page)

    async def verify_metadata(self, page):
        r=self.request
        if await page.get_by_placeholder('请输入稿件标题',exact=True).input_value()!=r.title:
            raise RuntimeError('B站标题回读不一致')
        desc=page.locator('.desc-text-wrp .ql-editor').filter(visible=True)
        if (await desc.evaluate("e=>Array.from(e.children).map(p=>p.innerText==='\\n'?'':p.innerText).join('\\n')")).replace('\r\n','\n').rstrip('\n')!=r.description.replace('\r\n','\n').rstrip('\n'):
            raise RuntimeError('B站简介回读不一致')
        actual=await page.locator('.tag-input-wrp:visible .label-item-v2-content').all_text_contents()
        if actual!=r.tags:
            raise RuntimeError('B站标签回读不一致')
        if await page.get_by_placeholder('请选择符合您视频内容的创作声明',exact=True).input_value()!=DECLARATIONS[r.declaration]:
            raise RuntimeError('B站创作声明回读不一致')
        if (await page.locator('.select-controller:visible .select-item-cont').inner_text()).strip()!=r.category:
            raise RuntimeError('B站分区回读不一致')

    async def cover_matches(self, modal):
        # Compare actual rendered pixels in BOTH editors with the selected file.
        # No page pixels or credentials leave this local browser process.
        data='data:image/'+('png' if self.cover.suffix.lower()=='.png' else 'jpeg')+';base64,'+base64.b64encode(self.cover.read_bytes()).decode()
        return await modal.evaluate('''async (root, source)=>{
            const im=new Image();im.src=source;await im.decode();
            const c=document.createElement('canvas');c.width=c.height=32;
            const x=c.getContext('2d',{willReadFrequently:true});x.drawImage(im,0,0,32,32);
            const expected=x.getImageData(0,0,32,32).data;
            const matches=[];
            for(const name of ['editor_4_3','editor_16_9']){
                const all=root.querySelectorAll('.'+name+' canvas.lower-canvas');
                if(all.length!==1||!all[0].width||!all[0].height)return false;
                x.clearRect(0,0,32,32);x.drawImage(all[0],0,0,32,32);
                const actual=x.getImageData(0,0,32,32).data;
                let error=0;for(let i=0;i<actual.length;i++)if(i%4!==3)error+=Math.abs(actual[i]-expected[i]);
                matches.push(error/(32*32*3)<12);
            }
            return matches.every(Boolean);
        }''',data)

    async def set_cover(self, page):
        modal=page.locator('.cover-editor').filter(visible=True)
        if not await modal.count():
            await page.get_by_text('添加封面',exact=True).filter(visible=True).click()
        await modal.wait_for(state='visible',timeout=15000)
        if await modal.count()!=1:raise RuntimeError('B站封面编辑器不唯一')
        sync=modal.locator('.sync.ratio_4_3 input[type=checkbox]')
        if not await sync.is_checked():
            # The native input is visually hidden; click its visible wrapper.
            await modal.locator('.sync.ratio_4_3 .bcc-checkbox').click()
        if not await sync.is_checked():
            raise RuntimeError('B站双比例封面同步未开启，未上传封面')
        upload=modal.locator('input[type=file][accept*="image"]')
        if await upload.count()!=1:raise RuntimeError('B站封面图片输入不唯一')
        await upload.set_input_files(str(self.cover))
        stable=0;deadline=time.monotonic()+45
        while time.monotonic()<deadline:
            try:matched=await self.cover_matches(modal)
            except Exception:matched=False
            stable=stable+1 if matched else 0
            if stable>=3:break
            await asyncio.sleep(.5)
        else:raise RuntimeError('B站两个比例的封面预览与指定图片不一致，未保存')
        await modal.get_by_text('完成',exact=True).click()
        await modal.wait_for(state='hidden',timeout=45000)
        if await page.locator('.cover-main').get_by_text('添加封面',exact=True).count():
            raise RuntimeError('B站封面保存后仍为空')

    async def fill_and_verify(self,page):
        emit_progress('bilibili','metadata')
        await self.fill_metadata(page)
        emit_progress('bilibili','cover')
        await self.set_cover(page)
        await self.verify_metadata(page)
        await self.wait_uploaded(page,timeout=30)
        submit=page.get_by_text('立即投稿',exact=True).filter(visible=True)
        if await submit.count()!=1 or not await submit.is_enabled():
            raise RuntimeError('B站最终投稿入口尚不可用')
        # No click on submit, no draft save, no editor close.
        await submit.scroll_into_view_if_needed()
        emit_progress('bilibili','ready_to_submit')
        return 'prepared'

    async def main(self):
        self.validate()
        async with async_playwright() as p:
            try:
                _,ctx=await publisher_handoff.connect_publisher_handoff(p,self.account_file,'bilibili')
                if not publisher_handoff.native_chrome_endpoint("bilibili"):
                    await self.verify_account(ctx)
            except Exception as exc:
                print('B站准备未开始：浏览器或在线账号核验未完成。',flush=True)
                raise
            page=await ctx.new_page()
            await page.goto(UPLOAD_URL,wait_until='domcontentloaded',timeout=60000)
            if publisher_handoff.native_chrome_endpoint("bilibili"):
                # Use the account visible in the user's chosen Chrome profile.
                # The identity request runs inside that one task tab; no Cookie export.
                logged_in = await page.evaluate("""async () => {
                    const r = await fetch('https://api.bilibili.com/x/web-interface/nav', {credentials:'include'});
                    const v = await r.json(); return v.code === 0 && v.data?.isLogin === true;
                }""")
                if not logged_in:
                    raise RuntimeError('请在普通 Chrome 的 B站页面完成登录；尚未上传视频')
            upload=page.locator('.bcc-upload-wrapper input[type=file][accept*=".mp4"]')
            await upload.wait_for(state='attached',timeout=60000)
            if await upload.count()!=1:raise RuntimeError('B站视频上传输入不唯一')
            emit_progress('bilibili','uploading')
            await upload.set_input_files(str(self.video))
            await page.get_by_placeholder('请输入稿件标题',exact=True).wait_for(state='visible',timeout=180000)
            await self.wait_uploaded(page)
            return await self.fill_and_verify(page)
