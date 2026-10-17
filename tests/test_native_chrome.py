import asyncio
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch
import adapter
import server
import native_chrome

ORIGIN='chrome-extension://'+'a'*32

class ChromeConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.app=server.create_app(self.temp.name,start_worker=False)
        self.client=self.app.test_client();self.bridge=self.app.extensions['native_chrome']
        self.headers={'X-Publisher-Token':self.app.extensions['publisher_token']}
    def tearDown(self):self.bridge.close();self.temp.cleanup()
    def test_pairing_requires_local_ui_code_and_exact_extension_origin(self):
        self.assertEqual(self.client.post('/api/chrome/code',json={}).status_code,403)
        code=self.client.post('/api/chrome/code',json={},headers=self.headers).json['code']
        for origin in ('https://evil.example','null',ORIGIN+'.evil'):
            self.assertEqual(self.client.post('/api/chrome/pair',json={'code':code},headers={'Origin':origin}).status_code,403)
        result=self.client.post('/api/chrome/pair',json={'code':code},headers={'Origin':ORIGIN})
        self.assertEqual(result.status_code,200)
        self.assertTrue(self.bridge.authenticate(result.json['key'],ORIGIN))
        self.assertFalse(self.bridge.authenticate(result.json['key'],'chrome-extension://'+'b'*32))
        self.assertEqual(self.client.post('/api/chrome/pair',json={'code':code},headers={'Origin':ORIGIN}).status_code,400)
        self.assertEqual(self.bridge.pair_file.stat().st_mode & 0o777,0o600)
    def test_foreign_connection_cannot_start_relay(self):
        with patch.object(self.bridge,'start') as start:
            self.assertEqual(self.client.post('/api/chrome/connect',json={},headers={'Origin':ORIGIN,'X-Chrome-Key':'bad'}).status_code,403)
            start.assert_not_called()
    def test_open_pages_does_not_upload_or_create_jobs_and_requires_auth(self):
        self.assertEqual(self.client.post('/api/chrome/test-open',json={}).status_code,403)
        before=self.app.extensions['store'].all('job')
        endpoints={p:'ws://127.0.0.1:1/cdp/'+p for p in native_chrome.PLATFORMS}
        with patch.object(self.bridge,'status',return_value={'connected':True}),patch.object(self.bridge,'prepare',return_value=endpoints) as prepare,patch.object(self.bridge,'release') as release,patch.object(adapter,'run') as run:
            response=self.client.post('/api/chrome/test-open',json={},headers=self.headers)
        self.assertEqual(response.status_code,200)
        prepare.assert_called_once_with(list(native_chrome.PLATFORMS),[])
        self.assertEqual(release.call_count,3);run.assert_not_called()
        self.assertFalse(response.json['uploaded']);self.assertFalse(response.json['published'])
        self.assertEqual(self.app.extensions['store'].all('job'),before)
    def test_prepare_accepts_only_current_local_assets(self):
        asset=Path(self.temp.name)/'assets'/'movie.mp4';asset.write_bytes(b'synthetic')
        with patch.object(self.bridge,'call',return_value={'endpoints':{'douyin':'synthetic'}}) as call:
            self.bridge.prepare(['douyin'],[{'video_path':str(asset)}]);self.assertEqual(call.call_args.args[1]['files'],[str(asset.resolve())])
            with self.assertRaises(ValueError):self.bridge.prepare(['douyin'],[{'video_path':__file__}])
    def test_all_tabs_open_before_platform_execution_and_failures_continue(self):
        store=self.app.extensions['store'];manager=self.app.extensions['manager']
        order=[];endpoints={p:'ws://127.0.0.1:1/cdp/'+str(i)*64 for i,p in enumerate(native_chrome.PLATFORMS)}
        def prepare(platforms,plans):order.append(('open',platforms));return endpoints
        def run(platform,action,metadata,*args):
            order.append(('run',platform));self.assertEqual(metadata['native_chrome_endpoint'],endpoints[platform])
            return {'platform':platform,'status':'needs_attention','phase':'preparation','message':'synthetic failure'}
        job=manager.create('publish',list(native_chrome.PLATFORMS),{'browser_mode':'chrome'},[{'id':p} for p in native_chrome.PLATFORMS])
        with patch.object(self.bridge,'prepare',side_effect=prepare),patch.object(self.bridge,'release') as release,patch.object(adapter,'run',side_effect=run):manager.execute(job['id'])
        self.assertEqual(order,[('open',list(native_chrome.PLATFORMS))]+[('run',p) for p in native_chrome.PLATFORMS])
        saved=store.get('job',job['id']);self.assertNotIn('/cdp/',json.dumps(saved));self.assertEqual(len(saved['results']),3)
        self.assertGreaterEqual(release.call_count,3)
    def test_no_tabs_failure_never_calls_old_browser_adapter(self):
        manager=self.app.extensions['manager'];job=manager.create('publish',['douyin'],{'browser_mode':'chrome'},[{'id':'douyin'}])
        with patch.object(self.bridge,'prepare',side_effect=RuntimeError),patch.object(adapter,'run') as run:manager.execute(job['id']);run.assert_not_called()
        self.assertEqual(self.app.extensions['store'].get('job',job['id'])['results'][0]['retry_safe'],True)

class NativeHandoffTests(unittest.TestCase):
    def test_connect_never_launches_browser_or_reads_account(self):
        import sys
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'vendor/social-auto-upload'))
        from utils import publisher_handoff as handoff
        endpoint='ws://127.0.0.1:1234/cdp/'+'a'*64
        context=Mock();playwright=Mock();playwright.chromium.connect_over_cdp=AsyncMock(return_value=Mock(contexts=[context]))
        with patch.dict(os.environ,{'PUBLISHER_NATIVE_CHROME_ENDPOINT':endpoint,'PUBLISHER_NATIVE_CHROME_PLATFORM':'douyin'}),patch.object(handoff,'_load_session') as load,patch.object(handoff,'_start_detached') as launch:
            _,actual=asyncio.run(handoff.connect_publisher_handoff(playwright,'missing-account','douyin'))
            self.assertIs(actual,context);load.assert_not_called();launch.assert_not_called()
            with self.assertRaises(RuntimeError):handoff.native_chrome_endpoint('xiaohongshu')

if __name__=='__main__':unittest.main()
