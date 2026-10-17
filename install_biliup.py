"""Install the pinned official biliup release locally; verify GitHub's SHA-256 digest."""
from pathlib import Path
import argparse
import hashlib
import io
import json
import os
import platform
import re
import subprocess
import tarfile
import zipfile
import requests

VERSION = 'v1.2.11'
ROOT = Path(__file__).resolve().parent
KNOWN_SHA256 = {'biliupR-v1.2.11-aarch64-macos.tar.xz': '0e32d150841755e5c2fd324e9f32864154c94d842cd8ccfc1dceed0f4069f390'}


def installation_paths():
    system = {'Darwin': 'macos', 'Windows': 'windows', 'Linux': 'linux'}.get(platform.system())
    machine = {'arm64': 'aarch64', 'aarch64': 'aarch64', 'AMD64': 'x86_64', 'x86_64': 'x86_64'}.get(platform.machine())
    if not system or not machine:
        raise SystemExit('该系统架构暂未配置 B站上传组件。')
    folder = ROOT / 'data' / 'tools' / 'biliup' / f'{system}-{machine}'
    executable = folder / ('biliup.exe' if system == 'windows' else 'biliup')
    version_file = folder / 'version.txt'
    return folder, executable, version_file


def binary_version_valid(executable):
    try:
        result = subprocess.run([str(executable), '--version'], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=10, shell=False)
        return result.returncode == 0 and result.stdout.strip() == 'biliup-cli ' + VERSION.removeprefix('v')
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return False


def installation_valid(executable, version_file):
    try:
        if not executable.is_file() or version_file.read_text().strip() != VERSION:
            return False
        provenance = json.loads((executable.parent / 'provenance.json').read_text())
        if provenance.get('version') != VERSION:
            return False
        expected = provenance.get('binary_sha256')
        if expected and hashlib.sha256(executable.read_bytes()).hexdigest() != expected:
            return False
        # Older local installs have only the archive digest. Actually execute the
        # version command as well, so a stale marker cannot hide a broken binary.
        return binary_version_valid(executable)
    except (OSError, ValueError, TypeError, AttributeError, UnicodeError):
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Check the local installation without downloading.')
    args = parser.parse_args()
    os.umask(0o077)
    folder, executable, version_file = installation_paths()
    if installation_valid(executable, version_file):
        print('B站上传组件已就绪：' + VERSION)
        return 0
    if args.check:
        print('B站上传组件缺失或校验失败，请重新运行安装脚本。')
        return 1
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'local-video-publisher'}
    response = requests.get(f'https://api.github.com/repos/biliup/biliup/releases/tags/{VERSION}', headers=headers, timeout=30)
    response.raise_for_status()
    system, machine = folder.name.split('-', 1)
    suffix = f'{machine}-{system}' + ('.zip' if system == 'windows' else '.tar.xz')
    asset = next((item for item in response.json()['assets'] if item['name'].endswith(suffix)), None)
    if not asset:
        raise SystemExit('官方版本中未找到该系统的 B站上传组件。')
    expected = (asset.get('digest') or '').removeprefix('sha256:') or KNOWN_SHA256.get(asset['name'])
    if not expected or not re.fullmatch(r'[0-9a-f]{64}', expected):
        raise SystemExit('官方资源未提供可核验的 SHA-256，停止安装。')
    print('下载 B站上传组件：' + asset['name'], flush=True)
    response = requests.get(asset['browser_download_url'], timeout=(30, 180))
    response.raise_for_status()
    blob = response.content
    if hashlib.sha256(blob).hexdigest() != expected:
        raise SystemExit('B站组件 SHA-256 不符，未安装。')
    if asset['name'].endswith('.zip'):
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            member = next(n for n in archive.namelist() if Path(n).name == executable.name)
            binary = archive.read(member)
    else:
        with tarfile.open(fileobj=io.BytesIO(blob), mode='r:xz') as archive:
            member = next(m for m in archive.getmembers() if m.isfile() and Path(m.name).name == executable.name)
            binary = archive.extractfile(member).read()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    staged = folder / ('.installing-' + executable.name)
    try:
        staged.write_bytes(binary)
        staged.chmod(0o700)
        if not binary_version_valid(staged):
            raise SystemExit('B站组件无法运行或版本不符，未替换现有组件。')
        os.replace(staged, executable)
    finally:
        staged.unlink(missing_ok=True)
    version_file.write_text(VERSION + '\n')
    (folder / 'provenance.json').write_text(json.dumps({'version': VERSION, 'url': asset['browser_download_url'], 'archive_sha256': expected, 'binary_sha256': hashlib.sha256(binary).hexdigest()}, indent=2) + '\n')
    print('B站上传组件已安装并校验：' + VERSION)


if __name__ == '__main__':
    raise SystemExit(main())
