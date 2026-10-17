"""Short-lived, explicit Chrome-to-local-service session pairing.

Pair codes and cookies are never written to task records or request URLs.
Only importing a user-selected session is supported; this is not a remote-control API.
"""
from __future__ import annotations

from datetime import datetime, timezone
import re
import secrets
import threading
import time
import sqlite3

from flask import Blueprint, jsonify, request

import browser_sessions

EXTENSION_ORIGIN = re.compile(r'^chrome-extension://[a-p]{32}$')
CODE = re.compile(r'^[a-f0-9]{64}$')
EXTENSION_ENDPOINTS = {'/api/browser/handshake', '/api/browser/import'}
MAX_SESSION_BYTES = 2 * 1024 * 1024


class PairingError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class Pairings:
    def __init__(self, clock=time.time, ttl=600):
        self.clock = clock
        self.ttl = ttl
        self.lock = threading.RLock()
        self.items = {}

    def _record(self, code):
        if not isinstance(code, str) or not CODE.fullmatch(code):
            raise PairingError('连接码无效，请从发布台重新生成。', 404)
        record = self.items.get(code)
        if record is None:
            raise PairingError('连接码无效或服务已重启，请重新生成。', 404)
        if self.clock() >= record['deadline'] and record['status'] == 'pending':
            record['status'] = 'expired'
        return record

    def issue(self, platform):
        if not isinstance(platform, str) or platform not in browser_sessions.SUPPORTED:
            raise PairingError('此平台暂未支持从现有 Chrome 同步，请使用独立登录。')
        with self.lock:
            current = self.clock()
            self.items = {code: item for code, item in self.items.items() if current - item['deadline'] < 600}
            for item in self.items.values():
                if item['platform'] == platform and item['status'] == 'pending':
                    item['status'] = 'cancelled'
            code = secrets.token_hex(32)
            item = {'platform': platform, 'status': 'pending', 'deadline': current + self.ttl, 'extension_origin': None}
            self.items[code] = item
            return {'code': code, 'platform': platform, 'name': browser_sessions.SUPPORTED[platform]['name'],
                    'status': 'pending', 'expires_at': datetime.fromtimestamp(item['deadline'], timezone.utc).isoformat(),
                    'setup_url': '/browser-setup'}

    def handshake(self, code, origin):
        with self.lock:
            record = self._record(code)
            if record['status'] != 'pending':
                raise PairingError('连接码已失效或已被使用，请重新生成。', 409)
            if record['extension_origin'] is not None and record['extension_origin'] != origin:
                raise PairingError('连接码已绑定到另一个扩展，请重新生成。', 403)
            record['extension_origin'] = origin
            return {'platform': record['platform'], 'name': browser_sessions.SUPPORTED[record['platform']]['name'],
                    'expires_at': datetime.fromtimestamp(record['deadline'], timezone.utc).isoformat()}

    def status(self, code):
        with self.lock:
            record = self._record(code)
            status = record['status']
            if status == 'cancelled':
                status = 'expired'
            result = {'status': status, 'platform': record['platform']}
            if record.get('job_id'):
                result['job_id'] = record['job_id']
            return result

    def cancel(self, code):
        with self.lock:
            record = self._record(code)
            if record['status'] == 'pending':
                record['status'] = 'cancelled'
            return self.status(code)


def extension_origin():
    origin = request.headers.get('Origin', '')
    if not EXTENSION_ORIGIN.fullmatch(origin):
        raise PairingError('仅允许 Chrome 连接组件调用此接口。', 403)
    return origin


def install(app, store, manager, session_dir, publisher_token):
    pairs = Pairings()
    app.extensions['browser_pairings'] = pairs
    blueprint = Blueprint('browser_bridge', __name__)

    def body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise PairingError('请求格式不正确。')
        return data

    @blueprint.errorhandler(PairingError)
    def pairing_error(error):
        return jsonify(error=str(error)), error.status

    @blueprint.route('/api/browser/handshake', methods=['POST', 'OPTIONS'])
    def handshake():
        origin = extension_origin()
        if request.method == 'OPTIONS':
            return '', 204
        return jsonify(pairs.handshake(body().get('code'), origin))

    @blueprint.post('/api/browser/pair')
    def issue():
        if manager.draining:
            raise PairingError('发布台正在安全维护，请稍后连接。', 503)
        return jsonify(pairs.issue(body().get('platform'))), 201

    @blueprint.post('/api/browser/status')
    def status():
        return jsonify(pairs.status(body().get('code')))

    @blueprint.post('/api/browser/cancel')
    def cancel():
        return jsonify(pairs.cancel(body().get('code')))

    @blueprint.route('/api/browser/import', methods=['POST', 'OPTIONS'])
    def import_session():
        origin = extension_origin()
        if request.method == 'OPTIONS':
            return '', 204
        code = request.headers.get('X-Bridge-Code')
        payload = body()
        with pairs.lock, manager.lock:
            if manager.draining:
                raise PairingError('发布台正在安全维护，未修改已有会话，请稍后同步。', 503)
            record = pairs._record(code)
            if record['status'] != 'pending':
                raise PairingError('连接码已失效或已被使用，请重新生成。', 409)
            if record['extension_origin'] != origin:
                raise PairingError('请先验证连接码，再同步会话。', 403)
            platform = record['platform']
            if payload.get('platform') != platform:
                raise PairingError('当前浏览器平台与连接码不一致，未同步任何会话。')
            if any(job['status'] in ('running', 'queued') and any(r['platform'] == platform for r in job['results']) for job in store.all('job')):
                raise PairingError('该平台仍有任务运行，请等待结束后再同步，避免执行途中切换账号。', 409)
            previous_account = store.get('account', platform) or {'status': 'unverified'}
            # Revoke stale verification BEFORE touching credentials. A database
            # failure must never leave a new session carrying an old verified label.
            try:
                store.put('account', platform, {**previous_account, 'status': 'pending_verification',
                                                'message': '正在同步 Chrome 会话，尚未完成在线核验。'})
            except (sqlite3.Error, OSError):
                record['status'] = 'failed'
                raise PairingError('账号状态无法保存，未替换已有会话。请检查本地存储后重试。', 503) from None
            try:
                metadata = browser_sessions.validate_and_save(platform, payload, session_dir, session_dir)
            except ValueError as error:
                # Module errors are intentionally generic; never return request values.
                try:
                    store.put('account', platform, previous_account)
                except (sqlite3.Error, OSError):
                    record['status'] = 'failed'
                raise PairingError(str(error)) from None
            except OSError:
                record['status'] = 'failed'
                raise PairingError('本机会话保存失败，已有连接未被确认替换。', 500) from None
            try:
                store.put('account', platform, {'status': 'imported', 'source': 'chrome', 'imported_at': metadata.get('imported_at'),
                                               'message': '已从 Chrome 同步，正在核验；同步本身不代表平台已确认登录。'})
                job = manager.create('check', [platform])
            except (sqlite3.Error, OSError):
                record['status'] = 'failed'
                raise PairingError('会话已同步，但核验任务保存失败。请在发布台手动检查状态；当前不能视为已验证。', 503) from None
            record.update(status='imported', job_id=job['id'])
            # Never return cookies, storage values, or paths to the client.
            return jsonify(accepted=True, platform=platform, job={'id': job['id'], 'kind': job['kind'], 'status': job['status']}), 201

    app.register_blueprint(blueprint)


def is_extension_request():
    return request.path in EXTENSION_ENDPOINTS


def authorize_extension_request():
    """Called by server's global guard before its normal same-origin token check."""
    try:
        extension_origin()
    except PairingError:
        return jsonify(error='此接口仅用于已配对的 Chrome 连接组件。'), 403
    if request.method not in ('POST', 'OPTIONS'):
        return jsonify(error='不支持的请求方法。'), 405
    request.max_content_length = MAX_SESSION_BYTES
    return None


def add_cors(response):
    origin = request.headers.get('Origin', '')
    if request.path in EXTENSION_ENDPOINTS and EXTENSION_ORIGIN.fullmatch(origin):
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Vary'] = 'Origin'
        response.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-Bridge-Code'
        response.headers['Access-Control-Max-Age'] = '60'
        if request.headers.get('Access-Control-Request-Private-Network') == 'true':
            response.headers['Access-Control-Allow-Private-Network'] = 'true'
    return response
