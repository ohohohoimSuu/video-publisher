"""TikTok adapter using the vendored social-auto-upload browser workflow.

Chrome remains open for login or manual correction. This module does not post on
import, during login/check, or when a required field cannot be set. A successful
Post navigation means submitted, never that the video is approved or public.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import types
from urllib.request import urlopen

BASE = Path(__file__).resolve().parent
VENDOR = BASE / 'vendor' / 'social-auto-upload'
DATA = Path(os.environ.get('PUBLISHER_DATA_DIR', BASE / 'data')).resolve() / 'tiktok'
UPLOAD_URL = 'https://www.tiktok.com/tiktokstudio/upload?lang=en'
RESULT_PREFIX = 'TIKTOK_RESULT '
LOG_PREFIX = 'TIKTOK_LOG '
TIMEOUTS = {'login': 330, 'check': 75, 'publish': 960}
CAPABILITIES = {
    'video': True, 'title': 'caption', 'description': 'caption', 'tags': True,
    'cover': 'local_image', 'cover_count': 1, 'landscape_cover': False,
    'ai_label': 'manual_required', 'schedule': False,
    'implementation': 'vendored uploader.tk_uploader.main_chrome.TiktokVideo',
    'runtime_verified': False,
}


def _result(status, message, **extra):
    return dict(platform='tiktok', status=status, message=message, **extra)


def _clean_metadata(metadata):
    fields = ('video_path', 'cover_path', 'landscape_cover_path', 'title',
              'description', 'tags', 'ai_generated', 'schedule_at', 'publish_date')
    return {key: metadata[key] for key in fields if key in (metadata or {})}


def build_command(action, metadata=None):
    if action not in TIMEOUTS:
        raise ValueError('TikTok action must be login, check or publish')
    if action == 'publish':
        _validate_publish(metadata)
    python = BASE / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    return [str(python), str(Path(__file__).resolve()), action,
            '--metadata-json', json.dumps(_clean_metadata(metadata), ensure_ascii=False)]


def run(action, metadata, on_log, cancel_event=None):
    """Use the same bounded process and private evidence channel as other sites."""
    from adapter import _execute as execute_process
    command = build_command(action, metadata)
    if cancel_event is not None and cancel_event.is_set():
        return _result('needs_attention', '任务已取消，未启动 TikTok 操作。')
    try:
        observed = execute_process(command, 'tiktok', action, on_log, cancel_event,
                                   metadata, timeout_seconds=TIMEOUTS[action])
    except Exception:
        return _result('needs_attention' if action == 'publish' else 'failed',
                       'TikTok 进程未能完成；请检查浏览器和平台结果，确认后再重试。')
    if observed['cancelled'] or observed['timeout']:
        reason = '已取消' if observed['cancelled'] else '已超时'
        return _result('needs_attention' if action == 'publish' else 'failed',
                       f'TikTok 操作{reason}并停止；Chrome 可能保留。请先核对平台结果，勿直接重发。')
    candidate = observed.get('result')
    allowed = {'verified', 'login_required', 'failed', 'needs_attention'}
    if action == 'publish':
        allowed = {'submitted', 'login_required', 'failed', 'needs_attention'}
    if observed['returncode'] == 0 and candidate and candidate.get('status') in allowed:
        return {key: value for key, value in candidate.items()
                if key in {'platform', 'status', 'message', 'url', 'cover_status'}}
    return _result('needs_attention' if action == 'publish' else 'failed',
                   'TikTok 适配器未返回完整确认结果；请检查浏览器和平台，勿直接重发。')


def _log(message):
    print(LOG_PREFIX + message, flush=True)


def _chrome_path():
    candidates = [os.environ.get('PUBLISHER_CHROME'),
                  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
                  shutil.which('google-chrome'), shutil.which('chromium')]
    if os.name == 'nt':
        candidates.extend(str(Path(os.environ.get(key, '')) / 'Google/Chrome/Application/chrome.exe')
                          for key in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA'))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    raise RuntimeError('chrome_missing')


def _prepare_data():
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    DATA.chmod(0o700)
    return DATA / 'account.json'


def _devtools_ready(port):
    try:
        with urlopen(f'http://127.0.0.1:{int(port)}/json/version', timeout=1) as response:
            info = json.load(response)
        return info.get('webSocketDebuggerUrl', '').startswith(f'ws://127.0.0.1:{int(port)}/')
    except (OSError, ValueError, TypeError):
        return False


def _ensure_visible_chrome():
    """Use a dedicated profile; detached Chrome survives adapter shutdown."""
    _prepare_data()
    browser_file = DATA / 'browser.json'
    try:
        port = json.loads(browser_file.read_text())['port']
        if isinstance(port, int) and 0 < port < 65536 and _devtools_ready(port):
            return f'http://127.0.0.1:{port}'
    except (OSError, ValueError, KeyError, TypeError):
        pass
    with socket.socket() as reserve:
        reserve.bind(('127.0.0.1', 0))
        port = reserve.getsockname()[1]
    profile = DATA / 'chrome-profile'
    profile.mkdir(exist_ok=True, mode=0o700)
    process = subprocess.Popen([
        _chrome_path(), f'--user-data-dir={profile}', f'--remote-debugging-port={port}',
        '--remote-debugging-address=127.0.0.1', '--no-first-run',
        '--no-default-browser-check', '--lang=en-US', 'about:blank',
    ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if _devtools_ready(port):
            browser_file.write_text(json.dumps({'port': port, 'pid': process.pid}))
            browser_file.chmod(0o600)
            return f'http://127.0.0.1:{port}'
        if process.poll() is not None:
            break
        time.sleep(0.2)
    raise RuntimeError('chrome_start_failed')


def _vendor_class():
    # Isolated CLI owns these module entries. Do not import global conf secrets or
    # upstream loguru sinks, whose exception dumps may include browser state.
    conf = types.ModuleType('conf')
    conf.BASE_DIR, conf.LOCAL_CHROME_PATH, conf.LOCAL_CHROME_HEADLESS = VENDOR, _chrome_path(), False
    sys.modules['conf'] = conf
    logging_module = types.ModuleType('utils.log')
    noop = lambda *args, **kwargs: None
    logging_module.tiktok_logger = types.SimpleNamespace(info=noop, success=noop, error=noop, exception=noop)
    sys.modules['utils.log'] = logging_module
    sys.path.insert(0, str(VENDOR))
    return importlib.import_module('uploader.tk_uploader.main_chrome').TiktokVideo


async def _base(page):
    if await page.locator('iframe[data-tt="Upload_index_iframe"]').count():
        return page.frame_locator('[data-tt="Upload_index_iframe"]')
    return page.locator('body')


async def _probe(page):
    # Fail closed: neither an empty page nor a network error proves a login.
    if '/login' in page.url:
        return 'login_required'
    base = await _base(page)
    if await base.get_by_role('button', name='Select video', exact=True).is_visible():
        return 'verified'
    if await page.get_by_role('button', name='Log in', exact=True).is_visible():
        return 'login_required'
    return 'needs_attention'


async def _save_account(context):
    account = _prepare_data()
    await context.storage_state(path=str(account))
    account.chmod(0o600)


async def _same_saved_account(context):
    """Do not publish from a profile switched since the saved account was checked.

    Values stay in memory and are never logged. A rotated/unknown identity fails
    closed; the user can explicitly log in again to save that current account.
    """
    try:
        saved = json.loads((DATA / 'account.json').read_text(encoding='utf-8'))
        current = await context.cookies(['https://www.tiktok.com/'])
        def identity(cookies):
            values = {}
            for cookie in cookies:
                domain = cookie.get('domain', '').lstrip('.').lower()
                if domain == 'tiktok.com' or domain.endswith('.tiktok.com'):
                    if cookie.get('value'):
                        values.setdefault(cookie.get('name'), set()).add(cookie['value'])
            return values
        expected, actual = identity(saved.get('cookies', [])), identity(current)
        for names in ({'uid_tt', 'uid_tt_ss'}, {'sessionid', 'sessionid_ss', 'sid_tt'}):
            common = names & expected.keys() & actual.keys()
            if common:
                return all(expected[name] == actual[name] for name in common)
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return False


async def _check(playwright):
    account = _prepare_data()
    if not account.is_file():
        return _result('login_required', '尚未保存 TikTok 登录状态，请先扫码或登录。')
    browser = await playwright.chromium.launch(executable_path=_chrome_path(), headless=True)
    try:
        context = await browser.new_context(storage_state=str(account), locale='en-US')
        page = await context.new_page()
        await page.goto(UPLOAD_URL, wait_until='domcontentloaded', timeout=40000)
        try:
            await (await _base(page)).get_by_role('button', name='Select video', exact=True).wait_for(timeout=10000)
        except Exception:
            pass
        status = await _probe(page)
        messages = {'verified': '已在 TikTok 上传页确认登录状态。',
                    'login_required': 'TikTok 要求重新登录。',
                    'needs_attention': '未能确认 TikTok 上传权限；可能需要验证码、网络处理或更新页面适配。'}
        return _result(status, messages[status])
    finally:
        await browser.close()


async def _login(context, page):
    await page.goto(UPLOAD_URL, wait_until='domcontentloaded', timeout=40000)
    _log('已打开 TikTok Chrome，请在窗口中完成登录；最多等待 5 分钟。')
    deadline = time.monotonic() + 280
    while time.monotonic() < deadline:
        if await _probe(page) == 'verified':
            await _save_account(context)
            return _result('verified', '已确认 TikTok 上传页可用，登录状态已保存。')
        # TikTok login can return to its homepage. Navigate only after it leaves
        # login, so no credentials or verification steps are interrupted.
        if page.url.split('?')[0].rstrip('/') in ('https://www.tiktok.com', 'https://www.tiktok.com/en'):
            await page.goto(UPLOAD_URL, wait_until='domcontentloaded', timeout=40000)
        await asyncio.sleep(1)
    return _result('login_required', '等待登录超时；Chrome 窗口保留，完成登录后可再运行“登录”。')


def _validate_publish(metadata):
    if not isinstance(metadata, dict):
        raise ValueError('缺少 TikTok 发布材料。')
    for key, label in [('video_path', '视频'), ('cover_path', '本地封面')]:
        value = metadata.get(key)
        if not isinstance(value, str) or not Path(value).is_file():
            raise ValueError(f'TikTok {label}文件不存在；未开始上传。')
    if type(metadata.get('ai_generated')) is not bool:
        raise ValueError('请明确选择 TikTok 视频是否由 AI 生成；未开始上传。')
    for key in ('title', 'description'):
        if not isinstance(metadata.get(key, ''), str):
            raise ValueError('TikTok 标题与正文必须为文字。')
    tags = metadata.get('tags', [])
    if not isinstance(tags, list) or any(not isinstance(t, str) for t in tags):
        raise ValueError('TikTok 话题必须为文字列表。')
    caption = '\n\n'.join(x.strip() for x in [metadata.get('title', ''), metadata.get('description', '')] if x.strip())
    if not caption:
        raise ValueError('请填写 TikTok 标题或正文。')
    tags = [tag.strip().lstrip('#') for tag in tags if tag.strip().lstrip('#')]
    rendered = caption + (' ' + ' '.join('#' + tag for tag in tags) if tags else '')
    if len(rendered) > 2200:
        raise ValueError('文案超出此适配器的 2200 字符保守限制；未自动截断，请缩短后重试。')
    return caption, tags


async def _wait_uploaded(base, timeout=600):
    deadline = time.monotonic() + timeout
    post = base.get_by_role('button', name='Post', exact=True)
    while time.monotonic() < deadline:
        if await post.is_visible() and await post.is_enabled():
            return post
        if await base.locator('button[aria-label="Select file"]').is_visible():
            raise RuntimeError('upload_error')
        await asyncio.sleep(1)
    raise TimeoutError('upload_timeout')


async def _click_post_once(page, post):
    # Never repeat a possibly successful publication click.
    await post.click(timeout=15000)
    try:
        await page.wait_for_url('**/tiktokstudio/content**', timeout=45000)
    except Exception:
        return _result('needs_attention', '已点击一次 Post，但未确认提交结果；Chrome 保留。请先检查平台，勿直接重发。')
    return _result('submitted', 'TikTok 已跳转内容管理页，提交步骤完成；审核与公开状态需在平台核验。',
                   url='https://www.tiktok.com/tiktokstudio/content')


async def _caption_matches(base, caption, tags):
    rendered = await base.locator('div.public-DraftEditor-content').inner_text()
    normalize = lambda text: ' '.join(text.split())
    expected = normalize(caption + ' ' + ' '.join('#' + tag for tag in tags))
    return normalize(rendered) == expected


async def _publish(context, page, metadata):
    caption, tags = _validate_publish(metadata)
    if not await _same_saved_account(context):
        return _result('login_required', '当前 TikTok Chrome 账号与已保存账号不一致或无法匹配；未上传，请重新登录并核对账号。', cover_status='not_set')
    await page.goto(UPLOAD_URL, wait_until='domcontentloaded', timeout=40000)
    try:
        await (await _base(page)).get_by_role('button', name='Select video', exact=True).wait_for(timeout=10000)
    except Exception:
        pass
    status = await _probe(page)
    if status != 'verified':
        return _result(status, '未确认 TikTok 登录或上传权限；Chrome 保留，请先处理页面。', cover_status='not_set')
    await _save_account(context)
    if metadata.get('schedule_at') or metadata.get('publish_date'):
        return _result('needs_attention', '此 TikTok 适配器未实现定时发布；Chrome 保留，未上传或点击 Post。', cover_status='not_set')
    vendor = _vendor_class()
    uploader = vendor(caption, metadata['video_path'], tags, 0,
                      str(DATA / 'account.json'), metadata['cover_path'])
    await uploader.choose_base_locator(page)
    _log('已确认上传权限，开始上传 TikTok 视频。')
    # The same real file-chooser flow used by upstream TiktokVideo.upload().
    async with page.expect_file_chooser(timeout=15000) as chooser:
        await uploader.locator_base.locator('button:has-text("Select video"):visible').click()
    await (await chooser.value).set_files(metadata['video_path'])
    await uploader.add_title_tags(page)
    post = await _wait_uploaded(uploader.locator_base)
    _log('视频上传完成，正在设置本地封面。')
    try:
        await uploader.upload_thumbnails(page)
        await uploader.locator_base.locator('div.cover-edit-panel:not(.hide-panel)').wait_for(state='hidden', timeout=15000)
    except Exception:
        return _result('needs_attention', '本地封面未完成确认，未点击 Post；Chrome 保留，请在 TikTok 页面设置封面并核对。', cover_status='needs_attention')
    if not await _caption_matches(uploader.locator_base, caption, tags):
        return _result('needs_attention', '页面文案与请求的标题、正文或话题不一致，未点击 Post；请在保留的 Chrome 中核对。', cover_status='set_in_browser')
    if metadata['ai_generated']:
        return _result('needs_attention', '视频、文案和本地封面已填入。上游未实现 TikTok AI 内容标签，未点击 Post；请在保留的 Chrome 中启用 AI 标签并检查后手动发布。', cover_status='set_in_browser')
    if not await _same_saved_account(context):
        return _result('needs_attention', 'TikTok 账号在填写期间发生变化或无法匹配；未点击 Post，请核对账号和页面后手动处理。', cover_status='set_in_browser')
    _log('本地封面已在编辑页确认，正在提交一次 Post。')
    outcome = await _click_post_once(page, post)
    outcome['cover_status'] = 'set_in_browser'
    try:
        await _save_account(context)
    except Exception:
        _log('发布步骤后未能刷新本地登录文件；已确认的提交结果不受影响。')
    return outcome


async def _execute(action, metadata):
    if action == 'publish':
        try:
            _validate_publish(metadata)
        except ValueError as exc:
            return _result('failed', str(exc), cover_status='not_set')
        if not (DATA / 'account.json').is_file():
            return _result('login_required', '尚未保存 TikTok 登录状态；未启动上传，请先登录。')
    try:
        from patchright.async_api import async_playwright
    except ImportError:
        return _result('failed', '缺少 Patchright，请完成本地依赖安装。')
    try:
        async with async_playwright() as playwright:
            if action == 'check':
                return await _check(playwright)
            endpoint = await asyncio.to_thread(_ensure_visible_chrome)
            browser = await playwright.chromium.connect_over_cdp(endpoint, timeout=20000)
            # A new CDP browser context is disposed when the driver disconnects.
            # Keep the persistent context so manual-correction pages survive;
            # _publish checks its identity against the saved account twice.
            context = browser.contexts[0]
            context.set_default_timeout(20000)
            page = await context.new_page()
            if action == 'login':
                outcome = await _login(context, page)
            else:
                outcome = await _publish(context, page, metadata)
            if outcome['status'] in ('verified', 'submitted'):
                try:
                    await _close_detached_browser(browser)
                except Exception:
                    outcome['message'] += ' 自动关闭 Chrome 失败，请手动关闭本次窗口。'
            # On needs_attention/login_required, disconnecting leaves detached
            # Chrome available for manual correction. Its port is loopback only.
            return outcome
    except Exception as exc:
        # Only exception type is emitted. Playwright exception text can contain
        # URLs, full page content, credentials or local private paths.
        return _result('needs_attention' if action in ('login', 'publish') else 'failed',
                       f'TikTok 操作未确认完成（{type(exc).__name__}）；请检查网络、Chrome 和平台页面。发布结果未知时勿直接重发。')


async def _close_detached_browser(browser):
    # browser.close() on a CDP attachment only disconnects. Explicitly close our
    # dedicated Chrome so its debugging listener ends after a confirmed result.
    session = await browser.new_browser_cdp_session()
    try:
        await session.send('Browser.close')
    except Exception:
        if browser.is_connected():
            raise
    (DATA / 'browser.json').unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=TIMEOUTS)
    parser.add_argument('--metadata-json', default='{}')
    args = parser.parse_args()
    try:
        metadata = json.loads(args.metadata_json)
        outcome = asyncio.run(asyncio.wait_for(_execute(args.action, metadata), TIMEOUTS[args.action] - 5))
    except Exception as exc:
        outcome = _result('needs_attention' if args.action == 'publish' else 'failed',
                          f'TikTok 操作中断（{type(exc).__name__}）；Chrome 中的发布结果需要人工核对。')
    evidence_file = os.environ.get('PUBLISHER_RESULT_FILE')
    nonce = os.environ.get('PUBLISHER_RESULT_NONCE')
    if evidence_file and nonce:
        Path(evidence_file).write_text(json.dumps({**outcome, 'action': args.action, 'nonce': nonce},
                                                ensure_ascii=False), encoding='utf-8')
    else:
        print(RESULT_PREFIX + json.dumps(outcome, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
