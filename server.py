#!/usr/bin/env python3
"""Local-only video publisher. No platform calls occur without an explicit job."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import secrets
import signal
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
import io
import zipfile
from datetime import datetime, timezone
from urllib.parse import urlparse

from flask import Flask, abort, jsonify, request, send_file, send_from_directory
from PIL import Image, UnidentifiedImageError

import adapter
import browser_bridge
import web_access
import browser_sessions
import native_chrome
from platform_workflows import PREPARE_PLATFORMS, workflow_info, BILIBILI_CATEGORIES, BILIBILI_DECLARATIONS

BASE = Path(__file__).resolve().parent
PLATFORM_IDS = {p['id'] for p in adapter.PLATFORMS}
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.m4v', '.webm'}
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg'}
TITLE_LIMITS = {'douyin': 30, 'xiaohongshu': 20, 'bilibili': 80, 'youtube': 100,
                'kuaishou': 50, 'tencent': 16, 'baijiahao': 30, 'alipay': 30,
                'weibo': 30, 'hupu': 40, 'tiktok': 100}


def now():
    return datetime.now(timezone.utc).isoformat()


def normalize_tags(raw):
    """Support the common '#one #two' input without splitting names like C#."""
    if not isinstance(raw, list) or len(raw) > 20 or any(not isinstance(tag, str) or len(tag) > 4000 for tag in raw):
        raise ValueError('话题格式或数量不正确。')
    result = []
    for entry in raw:
        for value in re.split(r'[,，、\r\n]+|\s+(?=#+[^\s#])', entry):
            tag = value.strip().lstrip('#').strip()
            if not tag or tag in result:
                continue
            if len(tag) > 80:
                raise ValueError('每个话题不能超过 80 字，请用逗号或“#话题 #话题”分隔。')
            result.append(tag)
    if len(result) > 20:
        raise ValueError('最多支持 20 个话题；各平台另有数量限制。')
    return result


class Store:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.assets_dir = self.directory / 'assets'
        self.assets_dir.mkdir(exist_ok=True, mode=0o700)
        self.db = self.directory / 'publisher.sqlite3'
        self.lock = threading.RLock()
        with self.connect() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, value TEXT, PRIMARY KEY(kind,id))')
        self.db.chmod(0o600)

    def connect(self):
        return sqlite3.connect(self.db, timeout=30)

    def put(self, kind, identifier, value):
        with self.lock, self.connect() as conn:
            conn.execute('INSERT OR REPLACE INTO records VALUES (?,?,?)', (kind, identifier, json.dumps(value, ensure_ascii=False)))

    def get(self, kind, identifier):
        with self.connect() as conn:
            row = conn.execute('SELECT value FROM records WHERE kind=? AND id=?', (kind, identifier)).fetchone()
        return json.loads(row[0]) if row else None

    def all(self, kind):
        with self.connect() as conn:
            rows = conn.execute('SELECT value FROM records WHERE kind=? ORDER BY rowid DESC', (kind,)).fetchall()
        return [json.loads(row[0]) for row in rows]


def inspect_video(path):
    probe = shutil.which('ffprobe')
    if probe:
        try:
            result = subprocess.run([probe, '-v', 'error', '-show_entries', 'stream=codec_type,width,height:format=duration', '-of', 'json', str(path)], capture_output=True, text=True, timeout=45)
        except OSError:
            # A leftover binary for another CPU architecture is not a bad video.
            probe = None
    if not probe:
        # Patchright's bundled FFmpeg is a screen recorder with limited demuxers.
        # Use the installed OpenCV decoder in a bounded subprocess for media input.
        with path.open('rb') as stream:
            header = stream.read(16)
        if not (header[:4] == b'\x1aE\xdf\xa3' or header[4:8] in (b'ftyp', b'moov', b'mdat', b'wide', b'free')):
            raise ValueError('未识别到 MP4/MOV/WebM 视频容器。')
        code = ('import cv2,json,sys; c=cv2.VideoCapture(sys.argv[1]); ok,f=c.read(); '
                'fps=c.get(cv2.CAP_PROP_FPS); n=c.get(cv2.CAP_PROP_FRAME_COUNT); '
                'r={"ok":bool(ok),"width":int(c.get(3)),"height":int(c.get(4)),"duration":n/fps if fps else None}; '
                'c.release(); print(json.dumps(r)); sys.exit(0 if ok else 2)')
        import sys
        result = subprocess.run([sys.executable, '-c', code, str(path)], capture_output=True, text=True, timeout=45)
        if result.returncode:
            raise ValueError('视频无法解码，请检查格式和文件完整性。')
        info = json.loads(result.stdout)
        info.pop('ok', None)
        return {'probe_verified': True, 'probe_method': 'opencv-first-frame', **info}
    if result.returncode:
        raise ValueError('视频无法解码，请检查文件是否完整。')
    data = json.loads(result.stdout)
    video = next((s for s in data.get('streams', []) if s.get('codec_type') == 'video'), None)
    if not video:
        raise ValueError('文件中未找到视频轨道。')
    return {'probe_verified': True, 'width': video.get('width'), 'height': video.get('height'), 'duration': data.get('format', {}).get('duration')}


def readable_asset_path(asset, assets_dir, expected_kind=None):
    """Cheap local integrity check shared by shelf, preview and material validation.

    It detects missing/out-of-root/changed-size files, not same-size edits.
    A catalog refresh must not rehash gigabytes or re-decode video.
    """
    if (not isinstance(asset, dict) or asset.get('kind') not in ('video', 'cover')
            or (expected_kind and asset.get('kind') != expected_kind)
            or not isinstance(asset.get('path'), str)):
        return None
    try:
        path = Path(asset['path']).resolve()
        if not path.is_relative_to(assets_dir) or not path.is_file():
            return None
        size = path.stat().st_size
        if size == 0 or ('size' in asset and asset['size'] != size):
            return None
        return path
    except (OSError, ValueError, RuntimeError):
        return None


def validate_payload(payload, store):
    errors, warnings, plans = [], [], []
    if not isinstance(payload, dict):
        return {'valid': False, 'errors': ['发布材料必须为对象。'], 'warnings': [], 'platforms': []}, []
    selected = payload.get('platforms', [])
    if not isinstance(selected, list) or not selected or any(not isinstance(p, str) or p not in PLATFORM_IDS for p in selected):
        return {'valid': False, 'errors': ['请选择至少一个受支持的平台。'], 'warnings': [], 'platforms': []}, []
    selected = list(dict.fromkeys(selected))
    assets = {}
    for field, kind, required in [('video_id', 'video', True), ('cover_id', 'cover', True), ('landscape_cover_id', 'cover', False)]:
        identifier = payload.get(field)
        asset = store.get('asset', identifier) if isinstance(identifier, str) else None
        if not asset:
            if required or identifier:
                errors.append('请先上传视频文件。' if field == 'video_id' else '请先上传有效的封面图片。')
            continue
        if readable_asset_path(asset, store.assets_dir, kind) is None:
            errors.append('视频或封面已失效、移动或发生大小变化，请重新选择文件。')
        else:
            assets[field] = asset
    title, description = payload.get('title', ''), payload.get('description', '')
    if not isinstance(title, str) or not isinstance(description, str):
        errors.append('标题与正文必须为文字。')
        title, description = '', ''
    try:
        tags = normalize_tags(payload.get('tags', []))
    except ValueError as exc:
        errors.append(str(exc))
        tags = []
    if type(payload.get('ai_generated')) is not bool:
        errors.append('请选择视频是否含 AI 生成或合成内容，以便填写平台声明。')
    overrides = payload.get('overrides') or {}
    if not isinstance(overrides, dict):
        errors.append('平台单独文案格式不正确。')
        overrides = {}
    for pid in selected:
        name = next(p['name'] for p in adapter.PLATFORMS if p['id'] == pid)
        override = overrides.get(pid) or {}
        if not isinstance(override, dict):
            errors.append(f'{name}单独文案格式不正确。')
            override = {}
        pt = override.get('title') or title
        pd = override.get('description') if override.get('description') not in (None, '') else description
        if not isinstance(pt, str) or not isinstance(pd, str):
            errors.append(f'{name}标题与正文必须为文字。')
            continue
        pt = pt.strip()
        if not pt:
            errors.append(f'{name}缺少标题。')
        limit = TITLE_LIMITS.get(pid, 30)
        if len(pt) > limit:
            errors.append(f'{name}标题超过本工具的 {limit} 字限制，请填写该平台单独标题；不会自动截断。')
        if pid == 'hupu' and len(pt) < 4:
            errors.append('虎扑标题至少需要 4 个字。')
        if pid == 'tencent' and len(pt) < 6:
            errors.append('微信视频号短标题需要 6 至 16 个字，请填写平台单独标题。')
        desc_limit = {'douyin': 1000, 'xiaohongshu': 1000, 'bilibili': 2000, 'youtube': 5000, 'tencent': 1000, 'tiktok': 2200}.get(pid, 1000)
        full_desc = pd + ('\n' + ' '.join('#' + tag for tag in tags) if tags else '')
        if len(full_desc) > desc_limit:
            errors.append(f'{name}正文与话题合计超过本工具的 {desc_limit} 字限制。')
        bili = payload.get('bilibili') or {}
        yt = payload.get('youtube') or {}
        if not isinstance(bili, dict):
            bili = {}
        if not isinstance(yt, dict):
            yt = {}
        if pid == 'bilibili':
            if bili.get('category') not in BILIBILI_CATEGORIES:
                errors.append('请选择 B站分区名称。')
            if bili.get('declaration') not in BILIBILI_DECLARATIONS:
                errors.append('请选择 B站创作声明。')
            elif (payload.get('ai_generated') is True) != (bili['declaration'] == 'ai'):
                errors.append('B站创作声明与 AI 内容选择不一致。')
            if any(len(tag) > 20 for tag in tags):
                errors.append('B站每个标签最多20字。')
            if not tags:
                errors.append('B站请至少填写一个标签。')
        if pid == 'youtube':
            if yt.get('visibility') not in ('public', 'unlisted', 'private'):
                errors.append('请选择 YouTube 可见性。')
            if type(yt.get('made_for_kids')) is not bool:
                errors.append('请明确选择 YouTube 是否为儿童制作。')
            if type(yt.get('altered_synthetic')) is not bool:
                errors.append('请明确选择 YouTube 是否含逼真的修改或合成内容。')
        weibo = payload.get('weibo') if isinstance(payload.get('weibo'), dict) else {}
        hupu = payload.get('hupu') if isinstance(payload.get('hupu'), dict) else {}
        if pid == 'weibo' and weibo.get('content_type') not in ('original', 'recreation'):
            errors.append('请选择微博的内容类型：原创或二创。')
        if pid == 'hupu' and hupu.get('zone') != 'walking_street':
            errors.append('虎扑当前适配支持步行街主干道，请明确选择专区。')
        cover = assets.get('cover_id')
        landscape = assets.get('landscape_cover_id')
        if pid in ('youtube', 'bilibili', 'baijiahao') and landscape:
            cover = landscape
        plans.append({'id': pid, 'prepare_only': pid in PREPARE_PLATFORMS, 'title': pt, 'description': pd, 'tags': tags,
                      'video_path': assets.get('video_id', {}).get('path'),
                      'cover_path': cover.get('path') if cover else None,
                      'landscape_cover_path': landscape.get('path') if landscape else None,
                      'cover_used': cover.get('name') if cover else None,
                      'ai_generated': payload.get('ai_generated'), 'bilibili': bili, 'youtube': yt, 'weibo': weibo, 'hupu': hupu})
    if assets.get('video_id') and not assets['video_id'].get('probe_verified'):
        warnings.append('本机未找到 ffprobe，尚未验证视频编码、时长与分辨率。')
    if 'douyin' in selected and not assets.get('landscape_cover_id'):
        warnings.append('抖音未提供横版封面，将仅设置主封面；页面如要求另一比例，任务会提示处理。')
    if payload.get('ai_generated') is True:
        blocked_ai = [next(p['name'] for p in adapter.PLATFORMS if p['id'] == pid) for pid in selected if pid in ('xiaohongshu', 'kuaishou')]
        if blocked_ai:
            errors.append('当前版本尚未接通这些平台的 AI 内容声明，不能自动发布：' + '、'.join(blocked_ai) + '。请取消选择这些平台或手动完成平台声明。')
        if 'tiktok' in selected:
            warnings.append('TikTok 的 AI 视频将填写素材后停在浏览器，请手动完成 AI 声明和最终发布。')
    # Reuse the actual pure command builder, so green preflight cannot accept
    # metadata that the adapter will immediately reject before starting a browser.
    if not errors:
        for plan in plans:
            try:
                adapter.build_command(plan['id'], 'publish', plan)
            except (ValueError, TypeError, OSError) as exc:
                name = next(p['name'] for p in adapter.PLATFORMS if p['id'] == plan['id'])
                errors.append(f'{name}：{exc}')
    warnings.append('提交成功表示上传流程完成；平台审核和公开可见状态需要另行核验。')
    return {'valid': not errors, 'errors': list(dict.fromkeys(errors)), 'warnings': warnings,
            'platforms': [{k: p[k] for k in ('id', 'title', 'description', 'cover_used')} for p in plans]}, plans


class JobManager:
    def __init__(self, store, start_worker=True):
        self.store = store
        self.queue = queue.Queue()
        self.lock = threading.RLock()
        self.events = {}
        self.draining = False
        self.shutdown_requested = False
        for job in store.all('job'):
            if job['status'] in ('queued', 'running'):
                job.update(status='interrupted', finished_at=now())
                job.setdefault('logs', []).append('服务上次中断。为避免重复发布，未自动重跑；请先在平台检查发布结果。')
                for result in job.get('results', []):
                    if result['status'] == 'queued':
                        if self._restore_pending_resume(job, result):
                            continue
                        result.update(status='skipped', retry_safe=True, phase='preparation', message='服务中断前尚未启动此平台，可恢复材料后重新提交。')
                    elif result['status'] == 'running':
                        result.update(status='needs_attention', retry_safe=False, message='上次运行中断，结果未知；请先检查平台。')
                store.put('job', job['id'], job)
        if start_worker:
            threading.Thread(target=self.worker, daemon=True, name='publisher-worker').start()

    def create(self, kind, platforms, payload=None, plans=None):
        with self.lock:
            if self.draining:
                raise MaintenanceError('发布台正在安全维护，暂不接受新任务；已有材料保留。')
            job = {'id': uuid.uuid4().hex, 'kind': kind, 'status': 'queued', 'created_at': now(),
                   'title': (payload or {}).get('title', ''), 'payload': payload, 'plans': plans,
                   'results': [{'platform': p, 'status': 'queued', 'phase': 'preparation', 'retry_safe': True, 'message': '等待执行'} for p in platforms], 'logs': []}
            self.store.put('job', job['id'], job)
            self.events[job['id']] = threading.Event()
            self.queue.put(job['id'])
        return job

    @staticmethod
    def _restore_pending_resume(job, result):
        """A queued cover retry still owns an already-uploaded, uncertain editor."""
        if (job.get('payload') or {}).get('resume_platform') != result['platform']:
            return False
        history = job.get('resumes') or []
        prior = history[-1].get('previous_result') if history else None
        if isinstance(prior, dict) and prior.get('platform') == result['platform']:
            result.clear()
            result.update(prior)
        else:
            result.update(status='needs_attention', phase='cover', retry_safe=False,
                          message='封面接续尚未开始，原视频和编辑页仍需核对。')
        return True

    def maintenance_state(self):
        with self.lock:
            return {'draining': self.draining, 'shutdown_requested': self.shutdown_requested,
                    'active_jobs': sum(j['status'] in ('queued', 'running') for j in self.store.all('job'))}

    def drain(self, *, shutting_down=False):
        with self.lock:
            self.draining = True
            self.shutdown_requested = self.shutdown_requested or shutting_down
            return self.maintenance_state()

    def resume(self):
        with self.lock:
            if self.shutdown_requested:
                raise MaintenanceError('服务正在退出，不能恢复接收任务。退出完成后请重新启动。')
            self.draining = False
            return self.maintenance_state()

    def cancel_all(self):
        with self.lock:
            self.draining = True
            for jid, event in self.events.items():
                job = self.store.get('job', jid)
                if job and job['status'] in ('queued', 'running'):
                    event.set()

    def update(self, jid, callback):
        with self.lock:
            job = self.store.get('job', jid)
            callback(job)
            self.store.put('job', jid, job)

    def log(self, jid, line):
        def change(job):
            job['logs'] = (job.get('logs', []) + [str(line)[:1500]])[-500:]
            job['updated_at'] = now()
        self.update(jid, change)

    def progress(self, jid, index, event):
        phases = list(adapter.PUBLISH_PHASES)
        if not isinstance(event, dict):
            return
        phase = event.get('phase')
        if phase not in (*phases, None, 'unknown'):
            return
        def change(job):
            result = job['results'][index]
            previous = result.get('phase')
            if previous == 'unknown':
                return
            if previous in ('submitting', 'submitted') and phase not in ('submitting', 'submitted'):
                return
            if previous in phases and phase in phases and phases.index(phase) < phases.index(previous):
                return
            stamp = now()
            if previous != phase or not result.get('phase_started_at'):
                result['phase_started_at'] = stamp
                result['phase_history'] = (result.get('phase_history', []) + [{'phase':phase or 'unknown', 'at':stamp}])[-12:]
            result.update(phase=phase or 'unknown', updated_at=stamp, retry_safe=False)
            result.pop('percent', None)
            percent = event.get('percent')
            if phase in ('uploading','processing') and type(percent) in (int,float) and 0 <= percent <= 100:
                result['percent'] = round(percent, 1)
            if event.get('error_code') in adapter.PUBLISH_ERROR_CODES:
                result['error_code'] = event['error_code']
            job['updated_at'] = now()
        self.update(jid, change)

    def worker(self):
        while True:
            jid = self.queue.get()
            try:
                self.execute(jid)
            except Exception as exc:
                self.log(jid, '任务异常：' + type(exc).__name__)
                def failed(job):
                    job.update(status='needs_attention', finished_at=now())
                    for result in job.get('results', []):
                        if result['status'] == 'running':
                            result.update(status='needs_attention', retry_safe=False, message='执行意外中断，需先核对平台结果。')
                        elif result['status'] == 'queued':
                            if self._restore_pending_resume(job, result):
                                continue
                            result.update(status='skipped', retry_safe=True, message='此平台尚未开始，可恢复材料。')
                self.update(jid, failed)
            finally:
                self.queue.task_done()

    def execute(self, jid):
        job = self.store.get('job', jid)
        event = self.events[jid]
        if event.is_set():
            def cancelled(j):
                j.update(status='cancelled', finished_at=now())
                for r in j['results']:
                    if r['status'] == 'queued' and not self._restore_pending_resume(j, r):
                        r.update(status='skipped', message='已取消，未开始此平台')
            self.update(jid, cancelled)
            return
        self.update(jid, lambda j: j.update(status='running', started_at=now()))
        endpoints = {}
        if job['kind'] == 'publish' and (job.get('payload') or {}).get('browser_mode') == 'chrome':
            try:
                endpoints = self.native_chrome.prepare([r['platform'] for r in job['results']], job['plans'])
                self.log(jid, '已在普通 Chrome 中打开本次所选平台的标签页；使用当前 Chrome 登录。')
            except Exception:
                def no_tabs(j):
                    j.update(status='failed', finished_at=now())
                    for r in j['results']: r.update(status='failed', phase='preparation', retry_safe=True, message='普通 Chrome 未能打开任务标签页，请检查扩展连接。尚未上传视频。')
                self.update(jid, no_tabs)
                return
        resume_platform = (job.get('payload') or {}).get('resume_platform')
        for index, result in enumerate(job['results']):
            pid = result['platform']
            if resume_platform and pid != resume_platform:
                continue
            if event.is_set():
                def cancelled_before_platform(j, i=index):
                    r = j['results'][i]
                    if not self._restore_pending_resume(j, r):
                        r.update(status='skipped', message='用户取消，未开始此平台')
                self.update(jid, cancelled_before_platform)
                continue
            self.update(jid, lambda j, i=index: j['results'][i].update(status='running', retry_safe=False, started_at=now(), updated_at=now(), message='正在执行'))
            meta = next((p for p in (job.get('plans') or []) if p['id'] == pid), None)
            if pid in endpoints:
                meta = {**meta, 'native_chrome_endpoint': endpoints[pid]}
            if resume_platform:
                meta = {**meta, 'resume_cover': True, 'prepare_only': True}
            def progress_log(line, platform=pid):
                self.log(jid, f'[{platform}] {line}')
            progress_log.event = lambda evidence, i=index: self.progress(jid, i, evidence)
            try:
                outcome = adapter.run(pid, job['kind'], meta, progress_log, event)
            except Exception as exc:
                outcome = {'platform': pid, 'status': 'needs_attention' if job['kind'] == 'publish' else 'failed', 'message': '执行异常：' + type(exc).__name__ + '；请查看平台实际结果后再操作。'}
            if pid in endpoints:
                self.native_chrome.release(endpoints[pid])
            # Never lower submitting/submitted/unknown evidence back to cover.
            if resume_platform and outcome.get('status') != 'prepared' and outcome.get('phase') in ('preparation', 'cover'):
                outcome.update(phase='cover', status='needs_attention', retry_safe=False)
            elif resume_platform and outcome.get('status') != 'prepared':
                outcome.setdefault('phase', 'unknown')
                outcome['retry_safe'] = False
            def record_outcome(j, i=index, r=outcome):
                previous_phase = j['results'][i].get('phase')
                merged = {**r, 'updated_at': now()}
                phases = list(adapter.PUBLISH_PHASES)
                next_phase = merged.get('phase')
                # Progress is persisted independently; a later return cannot erase
                # evidence of crossing the final-submit boundary or uncertainty.
                if previous_phase in ('submitting', 'submitted', 'unknown'):
                    if previous_phase == 'unknown' or next_phase not in phases or phases.index(next_phase) < phases.index(previous_phase):
                        merged.update(phase=previous_phase, status='needs_attention', retry_safe=False)
                j['results'][i].update(merged)
            self.update(jid, record_outcome)
            if job['kind'] in ('login', 'check'):
                status = {'verified': 'verified', 'login_required': 'needs_login'}.get(outcome['status'], 'verification_unavailable')
                prior_account = self.store.get('account', pid) or {}
                checked = now()
                last_verified = prior_account.get('last_verified_at') or (prior_account.get('checked_at') if prior_account.get('status') == 'verified' else None)
                self.store.put('account', pid, {**prior_account, 'status': status, 'checked_at': checked,
                    'last_verified_at': checked if status == 'verified' else last_verified,
                    'message': outcome.get('message', '')})
        for endpoint in endpoints.values():
            self.native_chrome.release(endpoint)
        def finish(j):
            states = [r['status'] for r in j['results']]
            if event.is_set():
                status = 'cancelled'
            elif j['kind'] == 'publish' and 'needs_attention' in states:
                status = 'needs_attention'
            elif all(s in ('failed', 'skipped') for s in states):
                status = 'failed'
            elif j['kind'] == 'publish' and any(s in ('failed', 'skipped') for s in states):
                status = 'partial'
            elif j['kind'] == 'publish' and any(s == 'prepared' for s in states):
                status = 'prepared'
            else:
                status = 'completed'
            j.update(status=status, finished_at=now())
        self.update(jid, finish)


class MaintenanceError(RuntimeError):
    pass


class GracefulShutdown:
    """First signal drains safely; a second requests cooperative cancellation."""
    def __init__(self, manager, shutdown):
        self.manager = manager
        self.shutdown = shutdown
        self.started = False

    def handle(self, signum, frame):
        if self.started:
            self.manager.cancel_all()
            print('已请求停止当前任务；已提交内容不会撤回，正在等待上传进程退出。', flush=True)
            return
        self.started = True
        state = self.manager.drain(shutting_down=True)
        print(f'正在安全退出：停止接收新任务，等待 {state["active_jobs"]} 个已有任务结束。再次按 Ctrl+C 可请求取消。', flush=True)
        threading.Thread(target=self._wait, daemon=True, name='publisher-shutdown').start()

    def _wait(self):
        self.manager.queue.join()
        self.shutdown()


def public_job(job, detail=True):
    data = {k: v for k, v in job.items() if k not in ('plans', 'payload')}
    if job.get('kind') == 'publish':
        data['mode'] = (job.get('payload') or {}).get('mode')
        data['browser_mode'] = (job.get('payload') or {}).get('browser_mode', 'dedicated')
    if not detail:
        data.pop('logs', None)
    return data


def create_app(data_dir=None, start_worker=True):
    os.umask(0o077)
    app = Flask(__name__, static_folder=str(BASE / 'static'), static_url_path='/static')
    app.config.update(MAX_CONTENT_LENGTH=8 * 1024**3, MAX_FORM_MEMORY_SIZE=1024 * 1024)
    store = Store(data_dir or BASE / 'data')
    manager = JobManager(store, start_worker)
    token = secrets.token_urlsafe(32)
    app.extensions.update(store=store, manager=manager, publisher_token=token)
    browser_bridge.install(app, store, manager, adapter.COOKIE_DIR, token)
    chrome = native_chrome.install(app, store, manager, BASE)

    @app.before_request
    def local_only():
        host = request.host.split(':')[0]
        if host not in ('127.0.0.1', 'localhost'):
            return jsonify(error='只允许从本机访问。'), 403
        if request.path in native_chrome.ENDPOINTS:
            return native_chrome.guard()
        if browser_bridge.is_extension_request():
            return browser_bridge.authorize_extension_request()
        origin = request.headers.get('Origin')
        web_origin = web_access.trusted_web_origin(origin, store.directory)
        if origin and urlparse(origin).netloc != request.host and not web_origin:
            return jsonify(error='拒绝跨站请求。'), 403
        if web_origin and request.method == 'OPTIONS':
            return '', 204
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            supplied = request.headers.get('X-Publisher-Token', '')
            if not secrets.compare_digest(token, supplied):
                return jsonify(error='页面会话失效，请刷新页面。'), 403

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Cache-Control'] = 'no-store'
        if not browser_bridge.is_extension_request():
            response = web_access.add_web_cors(response, request, store.directory)
        return native_chrome.cors(browser_bridge.add_cors(response))

    @app.errorhandler(413)
    def too_large(_):
        if request.path.startswith('/api/browser/'):
            return jsonify(error='浏览器会话数据超过允许的大小。'), 413
        return jsonify(error='文件超过 8 GB 上限。'), 413

    @app.errorhandler(400)
    def bad_request(_):
        return jsonify(error='请求格式不正确。'), 400

    @app.errorhandler(MaintenanceError)
    def maintenance_error(error):
        return jsonify(error=str(error), maintenance=manager.maintenance_state()), 503

    @app.get('/')
    def index():
        return send_from_directory(BASE / 'static', 'index.html')

    @app.get('/browser-setup')
    def browser_setup():
        return send_from_directory(BASE / 'static', 'browser-setup.html')

    @app.get('/api/browser/extension.zip')
    def browser_extension():
        root = BASE / 'chrome-extension'
        if not (root / 'manifest.json').is_file():
            return jsonify(error='Chrome 连接组件尚未准备完成。'), 503
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(root.rglob('*')):
                if path.is_file() and not path.is_symlink() and path.suffix in {'.json', '.js', '.html', '.css', '.md', '.png', '.svg'}:
                    bundle.write(path, 'chrome-session-bridge/' + path.relative_to(root).as_posix())
        archive.seek(0)
        return send_file(archive, mimetype='application/zip', as_attachment=True, download_name='chrome-session-bridge.zip')

    @app.get('/api/health')
    def health():
        # Liveness must never probe dependencies, credentials or platform accounts.
        # The same localhost/Origin checks still protect this endpoint.
        return jsonify(application='local-video-publisher', version='0.5.1', status='ok')

    @app.get('/api/state')
    def state():
        platforms = []
        for platform in adapter.PLATFORMS:
            account = store.get('account', platform['id']) or {'status': 'unverified'}
            metadata = browser_sessions.read_metadata(platform['id'], adapter.COOKIE_DIR) or {}
            account_status = account['status']
            session_saved = (adapter.COOKIE_DIR / (platform['id'] + '_default.json')).is_file()
            if platform['id'] in (*browser_sessions.SUPPORTED, 'bilibili') and not session_saved:
                account_status = 'not_connected'
            platforms.append({**platform, 'account_status': account_status, 'account_message': account.get('message', ''), 'checked_at': account.get('checked_at'),
                              'session_saved': session_saved,
                              'last_verified_at': account.get('last_verified_at') or (account.get('checked_at') if account['status'] == 'verified' else None),
                              'connection_method': {'xiaohongshu': 'persistent_browser', 'bilibili': 'app_qr'}.get(platform['id'], 'browser'),
                              'account_source': metadata.get('source'), 'imported_at': metadata.get('imported_at'),
                              'account_hint': metadata.get('account_hint'), 'channel_id': metadata.get('channel_id'),
                              **workflow_info(platform['id'])})
        return jsonify(application='local-video-publisher', version='0.5.1', token=token, platforms=platforms,
                       submission_mode='manual_review', prepare_platforms=list(PREPARE_PLATFORMS),
                       jobs=[public_job(j, False) for j in store.all('job')[:100]], runtime=adapter.runtime_status(), defaults={},
                       maintenance=manager.maintenance_state(), native_chrome=chrome.status(),
                       browser_bridge={'platforms': list(browser_sessions.SUPPORTED), 'extension_download': '/api/browser/extension.zip',
                                       'instructions_url': '/browser-setup', 'state': 'available'})

    @app.post('/api/assets')
    def upload_asset():
        file = request.files.get('file')
        kind = request.form.get('kind')
        if not file or kind not in ('video', 'cover'):
            return jsonify(error='请选择素材文件。'), 400
        name = Path(file.filename or '').name
        suffix = Path(name).suffix.lower()
        if suffix not in (VIDEO_EXTENSIONS if kind == 'video' else IMAGE_EXTENSIONS):
            return jsonify(error='视频支持 MP4/MOV/M4V/WebM；封面支持 JPG/PNG。'), 400
        aid = uuid.uuid4().hex
        path = store.assets_dir / (aid + suffix)
        try:
            file.save(path)
            if not path.stat().st_size:
                raise ValueError('文件为空。')
            if kind == 'cover':
                if path.stat().st_size > 20 * 1024**2:
                    raise ValueError('封面大于 20 MB，请压缩后上传。')
                with Image.open(path) as img:
                    if img.format not in ('JPEG', 'PNG'):
                        raise ValueError('请使用真实的 JPG 或 PNG 图片。')
                    info = {'width': img.width, 'height': img.height}
                    img.verify()
            else:
                info = inspect_video(path)
        except (ValueError, OSError, UnidentifiedImageError, subprocess.TimeoutExpired, Image.DecompressionBombError) as exc:
            path.unlink(missing_ok=True)
            return jsonify(error=str(exc) if isinstance(exc, ValueError) else '素材读取失败，请检查文件。'), 400
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        asset = {'id': aid, 'name': name, 'path': str(path), 'sha256': digest.hexdigest(), 'size': path.stat().st_size, 'kind': kind, 'url': '/api/assets/' + aid, 'created_at': now(), **info}
        store.put('asset', aid, asset)
        return jsonify(asset=asset)

    @app.get('/api/assets/<aid>')
    def get_asset(aid):
        asset = store.get('asset', aid)
        if not asset or not asset_info(aid):
            abort(404)
        try:
            return send_file(asset['path'], conditional=True)
        except (FileNotFoundError, PermissionError):
            abort(404)

    def asset_info(aid):
        if not isinstance(aid, str) or not re.fullmatch(r'[a-zA-Z0-9_-]+', aid):
            return None
        asset = store.get('asset', aid)
        if not asset:
            return None
        path = readable_asset_path(asset, store.assets_dir)
        if path is None:
            return None
        try:
            size = path.stat().st_size
        except OSError:
            return None
        public_fields = ('id', 'name', 'kind', 'sha256', 'size', 'width', 'height', 'duration', 'probe_verified', 'created_at')
        result = {key: asset[key] for key in public_fields if key in asset}
        result.update(id=aid, url='/api/assets/' + aid, size=size)
        return result

    @app.get('/api/assets')
    def recent_assets():
        """Reuse already-imported local materials; this never starts an upload."""
        kind = request.args.get('kind')
        raw_limit = request.args.get('limit', '12')
        if kind not in (None, 'video', 'cover') or not re.fullmatch(r'[1-9][0-9]?', raw_limit):
            return jsonify(error='素材类型应为 video 或 cover，数量应为 1 至 50。'), 400
        limit = int(raw_limit)
        if limit > 50:
            return jsonify(error='一次最多读取 50 项本地素材。'), 400
        assets = []
        for candidate in store.all('asset'):
            if kind and candidate.get('kind') != kind:
                continue
            asset = asset_info(candidate.get('id'))
            if asset:
                assets.append(asset)
            if len(assets) > limit:
                break
        return jsonify(assets=assets[:limit], has_more=len(assets) > limit)

    @app.get('/api/assets/<aid>/info')
    def get_asset_info(aid):
        asset = asset_info(aid)
        if not asset:
            return jsonify(error='素材不存在或已移动，请重新选择。'), 404
        return jsonify(asset=asset)

    @app.post('/api/validate')
    def validate():
        result, _ = validate_payload(request.get_json(), store)
        return jsonify(result)

    @app.post('/api/jobs')
    def publish():
        if manager.draining:
            raise MaintenanceError('发布台正在安全维护，已有材料保留；请稍后再提交。')
        payload = request.get_json()
        result, plans = validate_payload(payload, store)
        if not result['valid']:
            return jsonify(error='发布材料尚未通过检查。', **result), 400
        # A stale page or API client must not re-enable final submission.
        if payload.get('mode', 'prepare') != 'prepare':
            return jsonify(error='当前仅允许上传并填写，最终发布由你在浏览器完成。'), 400
        unsupported = [next(p['name'] for p in adapter.PLATFORMS if p['id'] == pid)
                       for pid in payload['platforms'] if pid not in PREPARE_PLATFORMS]
        if unsupported:
            return jsonify(error='这些平台的人工确认流程尚未接入，未开始任务：' + '、'.join(unsupported) + '。'), 400
        payload = {**payload, 'mode': 'prepare'}
        payload.pop('resume_platform', None)
        payload.pop('resume_phase', None)
        for plan in plans:
            plan['prepare_only'] = True
        runtime = adapter.runtime_status()
        if not runtime['ready']:
            return jsonify(error=runtime['message']), 503
        native = payload.get('browser_mode') == 'chrome'
        if native and (any(p not in native_chrome.PLATFORMS for p in payload['platforms']) or not chrome.status()['connected']):
            return jsonify(error='请先连接普通 Chrome 扩展；当前支持抖音、小红书、B站，未启动上传。'),409
        with manager.lock:
            disconnected = []
            for pid in payload['platforms']:
                account = store.get('account', pid) or {}
                if not native and account.get('status') != 'verified':
                    disconnected.append(next(p['name'] for p in adapter.PLATFORMS if p['id'] == pid))
            if disconnected:
                return jsonify(error='以下平台尚未通过登录检查：' + '、'.join(disconnected) + '。请先连接账号并检查状态；未启动上传。'), 409
            # Only a channel verified by the YouTube workflow is a stable
            # destination identity. Session hashes and display names are not.
            scopes = {}
            if 'youtube' in payload['platforms']:
                channel = (browser_sessions.read_metadata('youtube', adapter.COOKIE_DIR) or {}).get('channel_id')
                if isinstance(channel, str) and re.fullmatch(r'UC[A-Za-z0-9_-]{22}', channel):
                    scopes['youtube'] = channel
            payload['account_scopes'] = scopes
            # Same local asset and account may not be queued or submitted twice.
            current_asset = store.get('asset', payload['video_id'])
            for prior in store.all('job'):
                prior_asset = store.get('asset', (prior.get('payload') or {}).get('video_id'))
                if prior['kind'] != 'publish' or not prior_asset or current_asset.get('sha256', current_asset['id']) != prior_asset.get('sha256', prior_asset['id']):
                    continue
                prior_scopes = (prior.get('payload') or {}).get('account_scopes') or {}
                blocked = [r['platform'] for r in prior['results'] if r['platform'] in payload['platforms']
                           and r['status'] in ('queued', 'running', 'prepared', 'submitted', 'published', 'needs_attention')
                           and not (scopes.get(r['platform']) and prior_scopes.get(r['platform'])
                                    and scopes[r['platform']] != prior_scopes[r['platform']])]
                if blocked:
                    return jsonify(error='该视频在这些平台已有提交或结果未知的任务：' + ', '.join(blocked) + '。请先核对平台结果，避免重复发布。'), 409
            job = manager.create('publish', [p['id'] for p in plans], payload, plans)
        return jsonify(job=public_job(job)), 201

    @app.get('/api/jobs/<jid>')
    def get_job(jid):
        job = store.get('job', jid)
        if not job:
            abort(404)
        return jsonify(job=public_job(job))

    @app.get('/api/jobs/<jid>/draft')
    def restore_job_draft(jid):
        job = store.get('job', jid)
        if not job or job.get('kind') != 'publish' or not isinstance(job.get('payload'), dict):
            return jsonify(error='这个任务没有可恢复的发布材料。'), 404
        fields = {'video_id', 'cover_id', 'landscape_cover_id', 'title', 'description', 'tags', 'platforms',
                  'overrides', 'ai_generated', 'bilibili', 'youtube', 'weibo', 'hupu'}
        payload = {key: value for key, value in job['payload'].items() if key in fields}
        try:
            payload['tags'] = normalize_tags(payload.get('tags', []))
        except ValueError:
            pass
        assets = {slot: asset_info(payload.get(field)) for slot, field in [('video', 'video_id'), ('cover', 'cover_id'), ('landscape', 'landscape_cover_id')]}
        warnings = []
        for slot, field in [('video', 'video_id'), ('cover', 'cover_id'), ('landscape', 'landscape_cover_id')]:
            if payload.get(field) and not assets[slot]:
                payload[field] = None
                warnings.append('部分素材已不存在，需要重新选择。')
        return jsonify(payload=payload, assets=assets, warnings=list(dict.fromkeys(warnings)))

    @app.post('/api/maintenance/drain')
    def drain():
        return jsonify(maintenance=manager.drain())

    @app.post('/api/maintenance/resume')
    def resume():
        return jsonify(maintenance=manager.resume())

    @app.post('/api/jobs/<jid>/resolve')
    def resolve_job(jid):
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or data.get('confirmed') is not True or data.get('resolution') not in ('not_submitted', 'submitted'):
            return jsonify(error='请先在平台核对稿件，再明确选择本地核验结果。'), 400
        with manager.lock:
            job = store.get('job', jid)
            if not job or job.get('kind') != 'publish':
                return jsonify(error='发布任务不存在。'), 404
            if job['status'] in ('queued', 'running'):
                return jsonify(error='任务仍在运行，不能改写核验结果。'), 409
            result = next((r for r in job['results'] if r['platform'] == data.get('platform')), None)
            if not result or result['status'] not in ('needs_attention', 'prepared'):
                return jsonify(error='该平台当前不处于待核验或待你发布状态。'), 409
            resolution = data['resolution']
            audit = {'platform': result['platform'], 'resolution': resolution, 'source': 'user_confirmation',
                     'previous_status': result['status'], 'previous_message': result.get('message', ''), 'at': now()}
            job.setdefault('resolutions', []).append(audit)
            if resolution == 'not_submitted':
                result.update(status='failed', retry_safe=True, message='使用者确认平台没有本次稿件；可恢复材料后重新提交，未自动重试。',
                              resolution_source='user_confirmation', updated_at=now())
            else:
                result.update(status='submitted', retry_safe=False, message='使用者确认平台已有本次稿件；未由工具自动核验公开或审核状态。',
                              resolution_source='user_confirmation', updated_at=now())
            states = [r['status'] for r in job['results']]
            job['status'] = ('needs_attention' if 'needs_attention' in states else 'failed' if all(s in ('failed', 'skipped') for s in states)
                             else 'partial' if any(s in ('failed', 'skipped') for s in states)
                             else 'prepared' if 'prepared' in states else 'completed')
            job['updated_at'] = now()
            store.put('job', jid, job)
        return jsonify(job=public_job(job))

    @app.post('/api/jobs/<jid>/resume-cover')
    def resume_cover(jid):
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or data.get('platform') != 'xiaohongshu':
            return jsonify(error='当前仅支持继续小红书封面步骤。'), 400
        with manager.lock:
            if manager.draining:
                raise MaintenanceError('发布台正在维护，暂不继续任务。')
            job = store.get('job', jid)
            if not job or job.get('kind') != 'publish' or (job.get('payload') or {}).get('mode') != 'prepare':
                return jsonify(error='仅人工确认模式的原任务可继续设置封面。'), 409
            if (job.get('payload') or {}).get('browser_mode') == 'chrome':
                return jsonify(error='普通 Chrome 的原任务页仍由你保留，请在该页完成封面；本版本尚不支持自动续接，未重传视频。'), 409
            if job['status'] in ('queued', 'running'):
                return jsonify(error='这个任务正在执行，请勿重复继续。'), 409
            result = next((r for r in job['results'] if r['platform'] == 'xiaohongshu'), None)
            if not result or result['status'] != 'needs_attention' or result.get('phase') != 'cover':
                return jsonify(error='这个任务没有可继续的小红书封面步骤。'), 409
            if (store.get('account', 'xiaohongshu') or {}).get('status') != 'verified':
                return jsonify(error='小红书会话尚未通过验证，未改动原编辑页。'), 409
            if not adapter.runtime_status()['ready']:
                return jsonify(error='本地执行环境尚未就绪，原编辑页保持不变。'), 503
            if any(other['id'] != jid and other['status'] in ('queued', 'running')
                   and any(r['platform'] == 'xiaohongshu' for r in other['results']) for other in store.all('job')):
                return jsonify(error='另一个小红书任务正在执行，请等待它结束。'), 409
            validation, _ = validate_payload({**job['payload'], 'platforms': ['xiaohongshu']}, store)
            if not validation['valid']:
                return jsonify(error='原材料已不能验证，未改动编辑页。', **validation), 400
            cover = store.get('asset', job['payload'].get('cover_id'))
            if not cover or hashlib.sha256(Path(cover['path']).read_bytes()).hexdigest() != cover.get('sha256'):
                return jsonify(error='原封面文件已变化，未继续上传。请核对材料。'), 409
            job.setdefault('resumes', []).append({'at': now(), 'platform': 'xiaohongshu', 'phase': 'cover',
                                                 'previous_status': result['status'], 'previous_message': result.get('message', ''),
                                                 'previous_result': dict(result)})
            job['payload']['resume_platform'] = 'xiaohongshu'
            job.update(status='queued', updated_at=now())
            job.pop('finished_at', None)
            result.update(status='queued', retry_safe=False, message='等待继续设置原编辑页的封面，不重新上传视频。')
            result.pop('diagnostic', None)
            result.pop('error_code', None)
            store.put('job', jid, job)
            manager.events[jid] = threading.Event()
            manager.queue.put(jid)
        return jsonify(job=public_job(job))

    @app.post('/api/jobs/<jid>/cancel')
    def cancel_job(jid):
        job = store.get('job', jid)
        if not job or job['status'] not in ('queued', 'running'):
            return jsonify(error='任务不存在或已经结束。'), 400
        manager.events[jid].set()
        manager.log(jid, '已请求停止。已经提交给平台的内容不会被撤回；运行中平台的结果需另行核验。')
        return jsonify(job=public_job(store.get('job', jid)))

    @app.post('/api/accounts/<pid>/<action>')
    def account_action(pid, action):
        if pid not in PLATFORM_IDS or action not in ('login', 'check'):
            abort(404)
        if manager.draining:
            raise MaintenanceError('发布台正在安全维护，暂不启动新的账号操作。')
        if not adapter.runtime_status()['ready']:
            return jsonify(error=adapter.runtime_status()['message']), 503
        with manager.lock:
            existing = next((j for j in store.all('job') if j['kind'] in ('login', 'check') and j['status'] in ('queued', 'running') and j['results'][0]['platform'] == pid), None)
            if existing:
                return jsonify(job=public_job(existing))
            job = manager.create(action, [pid])
        return jsonify(job=public_job(job)), 201

    @app.get('/api/accounts/bilibili/qr')
    def bilibili_qr():
        qr = BASE / 'vendor' / 'social-auto-upload' / 'qrcode.png'
        active = any(j['kind'] == 'login' and j['status'] == 'running' and j['results'][0]['platform'] == 'bilibili' for j in store.all('job')[:20])
        if not active or not qr.is_file() or time.time() - qr.stat().st_mtime > 300:
            abort(404)
        return send_file(qr, mimetype='image/png')

    return app


def main():
    parser = argparse.ArgumentParser(description='本地视频发布台')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--data-dir')
    args = parser.parse_args()
    # Acquire before recovery: a second process must never mark live jobs interrupted.
    data_directory = Path(args.data_dir or BASE / 'data')
    data_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (data_directory / '.server.lock').open('a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            lock.write(b'0')
            lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit('这个工作区的发布台已经运行，请直接打开现有页面。')
    app = create_app(args.data_dir)
    from werkzeug.serving import make_server
    httpd = make_server('127.0.0.1', args.port, app, threaded=True)
    shutdown = GracefulShutdown(app.extensions['manager'], httpd.shutdown)
    signal.signal(signal.SIGINT, shutdown.handle)
    signal.signal(signal.SIGTERM, shutdown.handle)
    print(f'视频发布台已启动：http://127.0.0.1:{args.port}', flush=True)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


if __name__ == '__main__':
    main()
