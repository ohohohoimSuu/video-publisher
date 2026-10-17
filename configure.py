"""Initialize non-secret local runtime settings, without overwriting existing settings."""
from pathlib import Path
import argparse
import importlib
import json
import os
import re
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parent
VENDOR = ROOT / 'vendor' / 'social-auto-upload'
SERVICE_URL = 'http://127.0.0.1:8765'


def check_runtime(include_legacy_bilibili=False):
    """Detect interrupted installs without downloading anything or launching a browser."""
    if not (3, 11) <= sys.version_info[:2] < (3, 13):
        print('请使用 Python 3.11 或 3.12。')
        return 1
    try:
        for module in ('flask', 'PIL', 'requests', 'loguru', 'cv2', 'qrcode', 'segno', 'sau_cli'):
            importlib.import_module(module)
        os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(ROOT / '.browsers')
        from patchright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            executable = Path(playwright.chromium.executable_path)
            if not executable.is_file() or not os.access(executable, os.X_OK):
                raise RuntimeError('缺少专用 Chromium 浏览器')
        if include_legacy_bilibili:
            from install_biliup import installation_paths, installation_valid
            _, executable, version_file = installation_paths()
            if not installation_valid(executable, version_file):
                raise RuntimeError('B站旧原生组件缺失或校验失败')
    except Exception as error:
        print(f'本地依赖尚未完整安装：{error}')
        return 1
    return 0


def server_status():
    """0: this application; 1: no listener; 2: another or unresponsive listener."""
    try:
        # Localhost must not be routed through a system HTTP proxy.
        with build_opener(ProxyHandler({})).open(SERVICE_URL + '/api/health', timeout=2) as response:
            data = json.loads(response.read(1024 * 1024))
        return 0 if isinstance(data, dict) and data.get('application') == 'local-video-publisher' else 2
    except URLError as error:
        return 1 if isinstance(error.reason, ConnectionRefusedError) else 2
    except (OSError, ValueError, UnicodeError):
        return 2


def open_when_ready():
    for _ in range(40):
        status = server_status()
        if status == 0:
            if sys.platform == 'darwin':
                subprocess.Popen(['open', SERVICE_URL])
            elif os.name == 'nt':
                os.startfile(SERVICE_URL)
            else:
                subprocess.Popen(['xdg-open', SERVICE_URL])
            return 0
        if status == 2:
            print('8765 端口已被其他程序占用，未打开页面。请关闭占用程序后重试。')
            return 2
        time.sleep(0.5)
    print('服务尚未启动成功，请查看启动终端中的错误。')
    return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--check-runtime', action='store_true')
    parser.add_argument('--legacy-bilibili', action='store_true', help='显式检查旧B站原生组件；不开放自动投稿')
    actions.add_argument('--check-server', action='store_true')
    actions.add_argument('--open-when-ready', action='store_true')
    args = parser.parse_args()
    if args.check_runtime:
        return check_runtime(include_legacy_bilibili=args.legacy_bilibili)
    if args.check_server:
        return server_status()
    if args.open_when_ready:
        return open_when_ready()
    os.umask(0o077)
    if not (3, 11) <= sys.version_info[:2] < (3, 13):
        raise SystemExit('请使用 Python 3.11 或 3.12。')
    candidates = [
        '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
        str(Path.home() / 'Applications/Google Chrome.app/Contents/MacOS/Google Chrome'),
        os.path.join(os.environ.get('PROGRAMFILES', r'C:\Program Files'), 'Google/Chrome/Application/chrome.exe'),
        os.path.join(os.environ.get('PROGRAMFILES(X86)', r'C:\Program Files (x86)'), 'Google/Chrome/Application/chrome.exe'),
        os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Google/Chrome/Application/chrome.exe'),
        '/usr/bin/google-chrome', '/usr/bin/google-chrome-stable', '/usr/bin/chromium',
    ]
    chrome = next((p for p in candidates if Path(p).is_file()), '')
    path = VENDOR / 'conf.py'
    if not path.exists():
        path.write_text('from pathlib import Path\nBASE_DIR = Path(__file__).parent.resolve()\n'
                        f'LOCAL_CHROME_PATH = {chrome!r}\nLOCAL_CHROME_HEADLESS = False\n'
                        'DEBUG_MODE = False\nYT_PROXY = None\nXHS_SERVER = "http://127.0.0.1:11901"\n', encoding='utf-8')
    elif chrome:
        # The first run may precede Chrome installation. Fill only our empty
        # placeholder; preserve user-selected browser paths and all other settings.
        original = path.read_text(encoding='utf-8')
        updated = re.sub(r'^LOCAL_CHROME_PATH\s*=\s*([\'"])\1\s*$',
                         lambda _: f'LOCAL_CHROME_PATH = {chrome!r}', original, flags=re.MULTILINE)
        if updated != original:
            path.write_text(updated, encoding='utf-8')
    for folder in (ROOT / 'data', VENDOR / 'cookies'):
        folder.mkdir(exist_ok=True, mode=0o700)
    print('本地配置已准备；已有登录状态和代理设置保持不变。')
    if not chrome:
        print('未找到 Google Chrome。请先安装 Chrome，再连接平台账号。')


if __name__ == '__main__':
    raise SystemExit(main())
