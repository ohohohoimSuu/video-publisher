"""Boundary regressions for the real Bilibili manual-handoff adapter."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from patchright.async_api import Error as BrowserError
from uploader.bilibili_uploader.web_prepare import BilibiliWebPrepare

class BilibiliWebPrepareTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.video=self.root/'fixture.mp4';self.video.write_bytes(b'never uploaded')
        self.cover=self.root/'fixture.jpg';self.cover.write_bytes(b'never uploaded')
        self.r=SimpleNamespace(prepare_only=True,publish_date=0,title='Fixture',description='line 1\nline 2',tags=['test'],category='科技数码',declaration='none',video_file=self.video,thumbnail_file=self.cover)
        self.app=BilibiliWebPrepare(self.r,self.root/'account.json')

    def test_validation_blocks_unsupported_or_incomplete_input_before_browser(self):
        self.app.validate()
        for key,value in [('prepare_only',False),('publish_date',123),('category','unknown'),('declaration',None),('tags',[]),('tags',['x'*21]),('tags',['same','same'])]:
            old=getattr(self.r,key);setattr(self.r,key,value)
            with self.subTest(key=key,value=value),self.assertRaises(ValueError):self.app.validate()
            setattr(self.r,key,old)

    def account_context(self,mid=42,logged_in=True):
        response=SimpleNamespace(url='https://api.bilibili.com/x/web-interface/nav',json=AsyncMock(return_value={'code':0,'data':{'isLogin':logged_in,'mid':mid}}))
        return SimpleNamespace(request=SimpleNamespace(get=AsyncMock(return_value=response))),response

    def test_wrong_account_or_logged_out_response_is_not_retried(self):
        for mid,logged_in in [(43,True),(42,False)]:
            context,_=self.account_context(mid,logged_in)
            with patch('uploader.bilibili_uploader.web_prepare.publisher_handoff._load_session',return_value=([{'name':'DedeUserID','value':'42'}],[],'')):
                with self.assertRaises(RuntimeError):asyncio.run(self.app.verify_account(context))
            self.assertEqual(context.request.get.await_count,1)

    def test_only_transient_read_only_identity_request_is_retried(self):
        context,response=self.account_context()
        context.request.get.side_effect=[BrowserError('synthetic timeout'),response]
        with patch('uploader.bilibili_uploader.web_prepare.publisher_handoff._load_session',return_value=([{'name':'DedeUserID','value':'42'}],[],'')),patch('uploader.bilibili_uploader.web_prepare.asyncio.sleep',new=AsyncMock()):
            asyncio.run(self.app.verify_account(context))
        self.assertEqual(context.request.get.await_count,2)
        self.assertTrue(all(c.args==('https://api.bilibili.com/x/web-interface/nav',) for c in context.request.get.await_args_list))

if __name__=='__main__':unittest.main()
