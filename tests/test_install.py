"""Offline regression tests: no network, login, package download, or publishing."""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from urllib.error import URLError

import configure
import install_biliup

ROOT = Path(__file__).resolve().parents[1]


class BiliupInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.executable = self.folder / 'biliup'
        self.executable.write_bytes(b'broken executable')
        self.version = self.folder / 'version.txt'
        self.version.write_text(install_biliup.VERSION)
        (self.folder / 'provenance.json').write_text(json.dumps({'version': install_biliup.VERSION}))
        self.addCleanup(self.temp.cleanup)

    def test_stale_version_marker_cannot_hide_broken_executable(self):
        self.assertFalse(install_biliup.installation_valid(self.executable, self.version))

    def test_stored_binary_hash_detects_damage_before_execution(self):
        (self.folder / 'provenance.json').write_text(json.dumps({'version': install_biliup.VERSION, 'binary_sha256': '0' * 64}))
        with patch.object(install_biliup, 'binary_version_valid') as probe:
            self.assertFalse(install_biliup.installation_valid(self.executable, self.version))
            probe.assert_not_called()

    def test_older_archive_only_provenance_requires_actual_version_probe(self):
        with patch.object(install_biliup, 'binary_version_valid', return_value=True) as probe:
            self.assertTrue(install_biliup.installation_valid(self.executable, self.version))
            probe.assert_called_once_with(self.executable)

    def test_wrong_runtime_version_is_rejected(self):
        with patch.object(install_biliup.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'biliup-cli 0.1.0\n', '')):
            self.assertFalse(install_biliup.binary_version_valid(self.executable))

    def test_check_mode_never_downloads(self):
        with patch.object(sys, 'argv', ['install_biliup.py', '--check']), patch.object(install_biliup, 'installation_paths', return_value=(self.folder, self.executable, self.version)), patch.object(install_biliup.requests, 'get') as get:
            self.assertEqual(install_biliup.main(), 1)
            get.assert_not_called()

    def test_invalid_download_digest_never_replaces_existing_binary(self):
        info = {'assets': [{'name': 'biliupR-v1.2.11-aarch64-macos.tar.xz', 'digest': 'sha256:' + '0' * 64, 'browser_download_url': 'https://example.invalid/mock'}]}
        release = MagicMock(); release.json.return_value = info
        download = MagicMock(); download.content = b'not the expected archive'
        folder = self.folder / 'macos-aarch64'; folder.mkdir()
        executable = folder / 'biliup'; executable.write_bytes(b'previous binary')
        with patch.object(sys, 'argv', ['install_biliup.py']), patch.object(install_biliup, 'installation_paths', return_value=(folder, executable, folder / 'version.txt')), patch.object(install_biliup.requests, 'get', side_effect=[release, download]):
            with self.assertRaisesRegex(SystemExit, 'SHA-256'):
                install_biliup.main()
        self.assertEqual(executable.read_bytes(), b'previous binary')


class ConfigurationTests(unittest.TestCase):
    def test_python_310_is_rejected_before_imports_for_locked_numpy(self):
        with patch.object(configure.sys, 'version_info', (3, 10, 15)), patch.object(configure.importlib, 'import_module') as imports:
            self.assertEqual(configure.check_runtime(), 1)
            imports.assert_not_called()

    def test_missing_python_dependency_fails_readiness(self):
        with patch.object(configure.importlib, 'import_module', side_effect=ImportError('missing flask')):
            self.assertEqual(configure.check_runtime(), 1)

    def test_missing_browser_fails_readiness(self):
        from patchright import sync_api
        browser = MagicMock(); browser.__enter__.return_value.chromium.executable_path = '/missing/chromium'
        with patch.object(sync_api, 'sync_playwright', return_value=browser), patch.object(configure.importlib, 'import_module'):
            self.assertEqual(configure.check_runtime(), 1)

    def test_server_probe_checks_application_identity(self):
        for data, expected in [({'application': 'local-video-publisher'}, 0), ({'application': 'other-app'}, 2), ([], 2), ({'status': 'ok'}, 2)]:
            with self.subTest(data=data), patch.object(configure, 'build_opener') as build:
                build.return_value.open.return_value.__enter__.return_value.read.return_value = json.dumps(data).encode()
                self.assertEqual(configure.server_status(), expected)
                self.assertEqual(build.return_value.open.call_args.args[0], configure.SERVICE_URL + '/api/health')

    def test_manual_workflow_runtime_does_not_require_native_biliup(self):
        from patchright import sync_api
        browser = MagicMock()
        browser.__enter__.return_value.chromium.executable_path = sys.executable
        with patch.object(sync_api, 'sync_playwright', return_value=browser), patch.object(configure.importlib, 'import_module'), patch.object(install_biliup, 'installation_valid', return_value=False) as native:
            self.assertEqual(configure.check_runtime(), 0)
            native.assert_not_called()
            self.assertEqual(configure.check_runtime(include_legacy_bilibili=True), 1)
            native.assert_called_once()

    def test_server_probe_distinguishes_closed_port_and_unresponsive_listener(self):
        for error, expected in [(URLError(ConnectionRefusedError()), 1), (URLError('timeout'), 2), (TimeoutError(), 2), (ValueError(), 2)]:
            with self.subTest(error=error), patch.object(configure, 'build_opener') as build:
                build.return_value.open.side_effect = error
                self.assertEqual(configure.server_status(), expected)

    def test_configure_fills_only_empty_chrome_placeholder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); vendor = root / 'vendor'; vendor.mkdir()
            chrome = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
            for prior, expected in [("''", repr(chrome)), (repr('/user/custom/chrome'), repr('/user/custom/chrome'))]:
                conf = vendor / 'conf.py'; conf.write_text(f'LOCAL_CHROME_PATH = {prior}\nYT_PROXY = "keep-user-proxy"\n')
                with patch.object(configure, 'ROOT', root), patch.object(configure, 'VENDOR', vendor), patch.object(sys, 'argv', ['configure.py']), patch.object(Path, 'is_file', return_value=True):
                    configure.main()
                self.assertIn(f'LOCAL_CHROME_PATH = {expected}', conf.read_text())
                self.assertIn('YT_PROXY = "keep-user-proxy"', conf.read_text())


@unittest.skipIf(os.name == 'nt', 'Mac shell launcher regression requires bash')
class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ('setup.command', '启动发布台.command'):
            shutil.copy2(ROOT / name, self.root / name)
        (self.root / '.venv/bin').mkdir(parents=True); (self.root / 'bin').mkdir()
        fake = '#!' + sys.executable + '\n' + '''import json, os, pathlib, sys
root = pathlib.Path(os.environ['TEST_INSTALL_ROOT'])
args = sys.argv[1:]
with (root / 'calls.jsonl').open('a') as log: log.write(json.dumps(args) + '\\n')
if '--check-server' in args: sys.exit(int(os.environ.get('TEST_PORT_STATUS', '1')))
if '--check-runtime' in args: sys.exit(0 if (root / 'ready').exists() else 1)
if args[:3] == ['-m', 'pip', 'install'] and (root / 'fail-pip').exists(): sys.exit(1)
if args == ['-m', 'pip', 'check']: (root / 'ready').write_text('complete')
'''
        for relative in ('.venv/bin/python', 'bin/python3.11'):
            target = self.root / relative; target.write_text(fake); target.chmod(0o700)
        opener = self.root / 'bin/open'; opener.write_text('#!/bin/sh\nexit 0\n'); opener.chmod(0o700)
        self.env = dict(os.environ, TEST_INSTALL_ROOT=str(self.root), PATH=str(self.root / 'bin') + os.pathsep + os.environ.get('PATH', ''))

    def launch(self):
        return subprocess.run(['bash', str(self.root / '启动发布台.command')], cwd=self.root, env=self.env, capture_output=True, text=True, timeout=20)

    def calls(self):
        return [json.loads(line) for line in (self.root / 'calls.jsonl').read_text().splitlines()]

    def test_existing_but_incomplete_venv_resumes_setup(self):
        self.assertEqual(self.launch().returncode, 0)
        calls = self.calls()
        self.assertIn(['-m', 'pip', 'install', '-r', 'requirements-lock.txt', '-e', 'vendor/social-auto-upload'], calls)
        self.assertIn(['-m', 'patchright', 'install', 'chromium'], calls)
        self.assertIn(['server.py', '--port', '8765'], calls)

    def test_failure_then_relaunch_does_not_skip_dependencies(self):
        (self.root / 'fail-pip').touch()
        self.assertNotEqual(self.launch().returncode, 0)
        self.assertNotIn(['server.py', '--port', '8765'], self.calls())
        (self.root / 'fail-pip').unlink()
        self.assertEqual(self.launch().returncode, 0)
        installs = [call for call in self.calls() if call[:3] == ['-m', 'pip', 'install']]
        self.assertEqual(len(installs), 2)

    def test_other_application_on_port_does_not_launch_or_install(self):
        self.env['TEST_PORT_STATUS'] = '2'
        self.assertNotEqual(self.launch().returncode, 0)
        self.assertEqual(self.calls(), [['configure.py', '--check-server']])

    def test_existing_publisher_is_reused_without_install_or_new_server(self):
        self.env['TEST_PORT_STATUS'] = '0'
        self.assertEqual(self.launch().returncode, 0)
        self.assertEqual(self.calls(), [['configure.py', '--check-server']])

    def test_windows_checks_runtime_outside_the_create_venv_branch(self):
        # A Windows runtime is unavailable here; exercise ordering, not cmd.exe behavior.
        script = (ROOT / 'start.bat').read_text()
        creation_end = script.index('\n)', script.index('if not exist'))
        self.assertLess(creation_end, script.index('configure.py --check-runtime'))
        self.assertIn('if errorlevel 2 goto :port_error', script)
        self.assertIn('configure.py --open-when-ready', script)


if __name__ == '__main__':
    unittest.main()
