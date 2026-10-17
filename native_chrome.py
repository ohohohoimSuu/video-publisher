"""Authenticated normal-Chrome transport; stores no website credentials."""
from __future__ import annotations
import atexit
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time
import urllib.request
from flask import Blueprint, jsonify, request

PLATFORMS = ('douyin', 'xiaohongshu', 'bilibili')
ENDPOINTS = {'/api/chrome/pair', '/api/chrome/connect'}
ORIGIN = re.compile(r'chrome-extension://[a-p]{32}\Z')
KEY = re.compile(r'[a-f0-9]{64}\Z')

class NativeChrome:
    def __init__(self, directory, root):
        self.directory, self.root = Path(directory), Path(root)
        self.lock = threading.RLock()
        self.pending = None
        self.process = None
        self.port = None
        self.control_token = secrets.token_hex(32)
        self.extension_token = secrets.token_hex(32)
        self.pair_file = self.directory / 'chrome-upload-connection.json'
        try:
            self.pairing = json.loads(self.pair_file.read_text())
        except (OSError, ValueError):
            self.pairing = None
        atexit.register(self.close)

    def issue(self):
        self.pending = (secrets.token_hex(32), time.monotonic()+600)
        return {'code':self.pending[0], 'expires_in':600}

    def pair(self, code, origin):
        with self.lock:
            if not self.pending or time.monotonic()>=self.pending[1] or not isinstance(code,str) or not secrets.compare_digest(code,self.pending[0]):
                raise ValueError('连接码无效或已过期，请在发布台重新生成。')
            if self.status()['connected']:
                raise ValueError('已有 Chrome 连接，请先在原扩展中断开。')
            self.close()
            self.pairing={'key':secrets.token_hex(32),'origin':origin}
            self.pending=None
            self.directory.mkdir(parents=True,exist_ok=True)
            fd=os.open(self.pair_file,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
            with os.fdopen(fd,'w') as f:json.dump(self.pairing,f)
            return {'key':self.pairing['key']}

    def authenticate(self,key,origin):
        return bool(self.pairing and isinstance(key,str) and origin==self.pairing.get('origin') and secrets.compare_digest(key,self.pairing.get('key','')))

    def start(self):
        with self.lock:
            if self.process and self.process.poll() is None:return
            import patchright
            driver=Path(patchright.__file__).parent/'driver'
            self.process=subprocess.Popen([str(driver/('node.exe' if os.name=='nt' else 'node')),str(self.root/'chrome_relay.cjs')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)
            self.process.stdin.write(json.dumps({'controlToken':self.control_token,'extensionToken':self.extension_token,'extensionOrigin':self.pairing['origin'],'bundlePath':str(driver/'package/lib/utilsBundle.js')})+'\n')
            self.process.stdin.flush()
            # Node only emits the loopback port; browser data and tokens aren't logged.
            import selectors
            with selectors.DefaultSelector() as selector:
                selector.register(self.process.stdout,selectors.EVENT_READ)
                if not selector.select(10):self.close();raise RuntimeError('Chrome 连接组件未能启动。')
                self.port=json.loads(self.process.stdout.readline())['port']

    def call(self,path,payload=None):
        if not self.port:raise RuntimeError('请先连接普通 Chrome 扩展。')
        req=urllib.request.Request(f'http://127.0.0.1:{self.port}{path}',data=json.dumps(payload).encode() if payload is not None else None,headers={'X-Relay-Token':self.control_token,'Content-Type':'application/json'})
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req,timeout=40) as response:return json.load(response)

    def status(self):
        try:connected=self.call('/status')['connected'] if self.process and self.process.poll() is None else False
        except Exception:connected=False
        return {'connected':connected,'paired':bool(self.pairing),'platforms':list(PLATFORMS)}

    def prepare(self,platforms,plans):
        files=[]
        for plan in plans:
            for field in ('video_path','cover_path','landscape_cover_path'):
                if plan.get(field):
                    path=Path(plan[field]).resolve()
                    if not path.is_relative_to((self.directory/'assets').resolve()) or not path.is_file():raise ValueError('任务素材已失效，未传给 Chrome。')
                    files.append(str(path))
        return self.call('/prepare',{'platforms':platforms,'files':list(dict.fromkeys(files))})['endpoints']

    def release(self,endpoint):
        try:self.call('/release',{'key':endpoint.rsplit('/',1)[-1]})
        except Exception:pass

    def close(self):
        p=self.process;self.process=None;self.port=None
        if p:
            try:p.stdin.close();p.wait(timeout=3)
            except Exception:p.terminate()

def install(app,store,manager,root):
    bridge=NativeChrome(store.directory,root);manager.native_chrome=bridge
    app.extensions['native_chrome']=bridge
    bp=Blueprint('native_chrome',__name__)
    @bp.post('/api/chrome/code')
    def issue():return jsonify(bridge.issue())
    @bp.post('/api/chrome/test-open')
    def test_open():
        # Deliberately contains no files, upload worker, or publish job.
        if manager.draining or manager.maintenance_state()['active_jobs']:
            return jsonify(error='请等待当前任务结束，再测试打开页面。'),409
        if not bridge.status()['connected']:
            return jsonify(error='普通 Chrome 扩展尚未连接，未打开平台。'),409
        try:
            endpoints=bridge.prepare(list(PLATFORMS),[])
            for endpoint in endpoints.values():bridge.release(endpoint)
            return jsonify(opened=list(endpoints),uploaded=False,published=False)
        except Exception:
            return jsonify(error='扩展未能完成打开页面，请检查 Chrome 连接。没有上传或发布。'),503
    @bp.route('/api/chrome/pair',methods=['POST','OPTIONS'])
    def pair():
        if request.method=='OPTIONS':return '',204
        try:return jsonify(bridge.pair((request.get_json(silent=True) or {}).get('code'),request.headers.get('Origin')))
        except ValueError as e:return jsonify(error=str(e)),400
    @bp.route('/api/chrome/connect',methods=['POST','OPTIONS'])
    def connect():
        if request.method=='OPTIONS':return '',204
        if not bridge.authenticate(request.headers.get('X-Chrome-Key'),request.headers.get('Origin')):return jsonify(error='请重新连接发布台。'),403
        try:bridge.start()
        except Exception:return jsonify(error='本机 Chrome 连接服务未能启动。'),503
        return jsonify(websocket=f'ws://127.0.0.1:{bridge.port}/extension/{bridge.extension_token}')
    app.register_blueprint(bp)
    return bridge

def guard():
    if not ORIGIN.fullmatch(request.headers.get('Origin','')):return jsonify(error='仅允许 Chrome 扩展连接。'),403
    if request.method not in ('POST','OPTIONS'):return jsonify(error='不支持的请求方法。'),405
    request.max_content_length=4096

def cors(response):
    origin=request.headers.get('Origin','')
    if request.path in ENDPOINTS and ORIGIN.fullmatch(origin):
        response.headers.update({'Access-Control-Allow-Origin':origin,'Access-Control-Allow-Methods':'POST, OPTIONS','Access-Control-Allow-Headers':'Content-Type, X-Chrome-Key','Cache-Control':'no-store'})
        response.vary.add('Origin')
    return response
