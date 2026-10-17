import json
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import adapter


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.video = self.root / "my video.mp4"
        self.cover = self.root / "cover.png"
        self.video.write_bytes(b"local test fixture; never uploaded")
        self.cover.write_bytes(b"local test fixture; never uploaded")
        self.meta = dict(video_path=str(self.video), cover_path=str(self.cover),
                         title="一个明确的视频标题", description="正文包含引号 ‘ 和 $ 不会进入 shell", tags=["test"],
                         ai_generated=False, bilibili={"tid": 21},
                         youtube={"visibility": "private", "made_for_kids": True, "altered_synthetic": False},
                         weibo={"content_type": "original"}, hupu={"zone": "walking_street"})
        self.cookies = self.root / "cookies"
        self.cookies.mkdir()
        for platform in adapter.CLI_NAMES:
            (self.cookies / f"{platform}_default.json").write_text("{}")
        self.observed = dict(returncode=0, submitted=False, login=False, valid=False,
                             invalid=False, cancelled=False, timeout=False, url=None)

    def result(self, platform="douyin", action="publish", **observed):
        with patch.object(adapter, "COOKIE_DIR", self.cookies), \
             patch.object(adapter, "runtime_status", return_value={"ready": True}), \
             patch.object(adapter, "_execute", return_value={**self.observed, **observed}):
            return adapter.run(platform, action, self.meta, lambda _: None)

    def test_all_ten_cli_platforms_receive_real_file_cover_description(self):
        for platform in adapter.CLI_NAMES:
            with self.subTest(platform=platform):
                metadata = {**self.meta, "description": "", "tags": []} if platform == "baijiahao" else self.meta
                command = adapter.build_command(platform, "publish", metadata)
                self.assertEqual(command[:3], [str(adapter.PYTHON), "-u", str(adapter.VENDOR / "sau_cli.py")])
                self.assertEqual(command[3:7], [platform, "upload-video", "--account", "default"])
                for flag, expected in [("--file", str(self.video)), ("--thumbnail", str(self.cover)),
                                       ("--desc", metadata["description"]), ("--title", metadata["title"])]:
                    self.assertEqual(command[command.index(flag) + 1], expected)
                self.assertEqual("--headed" in command, platform != "bilibili")

    def test_login_headed_but_checks_noninteractive(self):
        for platform in adapter.CLI_NAMES:
            self.assertNotIn("--headed", adapter.build_command(platform, "check"))
            self.assertEqual("--headed" in adapter.build_command(platform, "login"), platform != "bilibili")

    def test_bilibili_missing_qr_component_is_not_a_login_failure(self):
        with patch.object(adapter, 'COOKIE_DIR', self.root / 'not-connected'), \
             patch('install_biliup.installation_valid', return_value=False), \
             patch.object(adapter, '_execute') as execute:
            result = adapter.run('bilibili', 'login')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error_code'], 'login_component_missing')
        self.assertIn('二维码尚未生成', result['message'])
        execute.assert_not_called()

    def test_bilibili_account_check_does_not_require_qr_component(self):
        with patch('install_biliup.installation_valid') as installed:
            result = self.result(platform='bilibili', action='check', valid=True, returncode=0)
        self.assertEqual(result['status'], 'verified')
        installed.assert_not_called()

    def test_bilibili_installed_component_allows_login_and_online_check(self):
        observations = [dict(self.observed, returncode=1, invalid=True),
                        dict(self.observed, returncode=0, login=True),
                        dict(self.observed, returncode=0, valid=True)]
        with patch('install_biliup.installation_valid', return_value=True), \
             patch.object(adapter, 'COOKIE_DIR', self.cookies), \
             patch.object(adapter, 'runtime_status', return_value={'ready': True}), \
             patch.object(adapter, '_execute', side_effect=observations) as execute:
            result = adapter.run('bilibili', 'login')
        self.assertEqual(result['status'], 'verified')
        self.assertEqual([c.args[2] for c in execute.call_args_list], ['check', 'login', 'check'])

    def test_commands_are_accepted_by_the_real_vendored_cli_parser_without_running(self):
        commands = [adapter.build_command(platform, action,
                    {**self.meta, "description": "", "tags": []} if platform == "baijiahao" else self.meta)[3:]
                    for platform in adapter.CLI_NAMES for action in ["login", "check", "publish"]]
        probe = subprocess.run(
            [str(adapter.PYTHON), "-c", "import json,sys,sau_cli; p=sau_cli.build_parser(); "
             "[p.parse_args(args) for args in json.load(sys.stdin)]; print('parsed')"],
            input=json.dumps(commands), capture_output=True, text=True,
            cwd=adapter.VENDOR, env=adapter._environment(), timeout=15, shell=False,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertIn("parsed", probe.stdout)

    def test_youtube_explicit_audience_and_visibility_are_preserved(self):
        command = adapter.build_command("youtube", "publish", self.meta)
        self.assertEqual(command[command.index("--made-for-kids") + 1], "true")
        self.assertEqual(command[command.index("--altered-synthetic") + 1], "false")
        self.assertEqual(command[command.index("--visibility") + 1], "private")
        self.meta["youtube"].pop("made_for_kids")
        with self.assertRaises(ValueError):
            adapter.build_command("youtube", "publish", self.meta)

    def test_overlong_titles_fail_instead_of_truncating(self):
        for platform in adapter.CLI_NAMES:
            metadata = {**self.meta, "title": "字" * (adapter.TITLE_LIMITS[platform] + 1)}
            with self.subTest(platform=platform), self.assertRaises(ValueError):
                adapter.build_command(platform, "publish", metadata)

    def test_alipay_will_not_silently_drop_requested_tags(self):
        with self.assertRaises(ValueError):
            adapter.build_command("alipay", "publish", {**self.meta, "title": "字" * 29})

    def test_command_argument_does_not_use_shell_interpolation(self):
        metadata = {**self.meta, "title": "$(touch /tmp/x); `id`"}
        command = adapter.build_command("douyin", "publish", metadata)
        self.assertEqual(command[command.index("--title") + 1], metadata["title"])

    def test_douyin_ai_declaration_requires_explicit_true(self):
        command = adapter.build_command("douyin", "publish", {**self.meta, "ai_generated": True})
        self.assertEqual(command[command.index("--declaration") + 1], "内容由AI生成")
        self.assertNotIn("--declaration", adapter.build_command("douyin", "publish", self.meta))

    def test_platform_environment_preserves_user_choices_and_local_paths(self):
        env = adapter._environment({**self.meta, "ai_generated": True})
        self.assertEqual(env["PUBLISHER_AI_GENERATED"], "true")
        self.assertEqual(env["PUBLISHER_WEIBO_TYPE"], "原创")
        self.assertEqual(env["PUBLISHER_HUPU_ZONE"], "步行街主干道")
        self.assertEqual(env["PLAYWRIGHT_BROWSERS_PATH"], str(adapter.BASE / ".browsers"))
        self.assertEqual(env["SAU_BILIUP_HOME"], str(adapter.BASE / "data/tools/biliup"))

    def test_exit_zero_without_submission_evidence_never_succeeds(self):
        self.assertEqual(self.result()["status"], "needs_attention")

    def test_exit_failure_overrides_submission_marker(self):
        self.assertEqual(self.result(returncode=1, submitted=True)["status"], "needs_attention")

    def test_submission_is_not_publication_and_url_not_invented(self):
        result = self.result(submitted=True)
        self.assertEqual(result["status"], "submitted")
        self.assertNotIn("url", result)
        self.assertEqual(result["cover_status"], "submitted_unverified")
        self.assertIn("尚未核验", result["message"])

    def test_started_upload_timeout_and_cancel_need_reconciliation(self):
        for key in ["timeout", "cancelled"]:
            result = self.result(**{key: True})
            self.assertEqual(result["status"], "needs_attention")
            self.assertIn("勿直接重发", result["message"])

    def test_xhs_failure_diagnostic_is_bounded_and_does_not_allow_retry_after_submit(self):
        evidence = {"status": "failed", "diagnostic": {"step": "topic", "error_type": "TimeoutError",
                    "topic_index": 2, "candidate_count": 0, "page_text": "private", "exception": "secret"}}
        result = self.result(platform="xiaohongshu", returncode=1, retry_safe=True, phase="metadata", result=evidence)
        self.assertEqual(result["diagnostic"], {"step": "topic", "error_type": "TimeoutError", "topic_index": 2, "candidate_count": 0})
        self.assertIn("第 2 个话题", result["message"])
        self.assertNotIn("private", json.dumps(result))
        self.assertEqual(self.result(platform="xiaohongshu", returncode=1, retry_safe=False, phase="submitting", result=evidence)["status"], "needs_attention")
        self.assertIsNone(adapter._xhs_failure_diagnostic({"step": "secret", "error_type": "TimeoutError"}))
        self.assertEqual(adapter._xhs_failure_diagnostic({"step": "topic", "error_type": "Error", "topic_index": True, "candidate_count": 500}), {"step": "topic", "error_type": "Error"})

    def test_missing_account_check_does_not_launch_anything(self):
        with patch.object(adapter, "COOKIE_DIR", self.root / "none"), patch.object(adapter, "_execute") as execute:
            result = adapter.run("bilibili", "check")
        self.assertEqual(result["status"], "login_required")
        self.assertIn("未连接发布台，不能据此判断浏览器是否已登录", result["message"])
        execute.assert_not_called()

    def test_independent_login_clears_import_binding_only_after_success(self):
        sidecar = self.cookies / "youtube_default.json.publisher.json"
        sidecar.write_text('{"source":"chrome","channel_id":"synthetic"}')
        self.result(platform="youtube", action="login", login=False, returncode=1)
        self.assertTrue(sidecar.exists())
        def complete_login(*args):
            (self.cookies / "youtube_default.json").write_text('{"new":"synthetic"}')
            return {**self.observed, "login": True}
        with patch.object(adapter, "COOKIE_DIR", self.cookies), \
             patch.object(adapter, "runtime_status", return_value={"ready": True}), \
             patch.object(adapter, "_execute", side_effect=lambda *a: complete_login(*a) if a[2] == "login" else {**self.observed, "valid": True}):
            result = adapter.run("youtube", "login")
        self.assertEqual(result["status"], "verified")
        self.assertFalse(sidecar.exists())
        self.assertTrue((self.cookies / "youtube_default.json").exists())

    def test_login_reusing_imported_session_preserves_channel_lock(self):
        sidecar = self.cookies / "youtube_default.json.publisher.json"
        sidecar.write_text('{"source":"chrome","channel_id":"synthetic"}')
        with patch.object(adapter, "COOKIE_DIR", self.cookies), \
             patch.object(adapter, "runtime_status", return_value={"ready": True}), \
             patch.object(adapter, "_execute", side_effect=[{**self.observed, "login": True}, {**self.observed, "valid": True}]):
            result = adapter.run("youtube", "login")
        self.assertEqual(result["status"], "verified")
        self.assertTrue(sidecar.exists())

    def test_check_verified_requires_explicit_valid_output(self):
        self.assertEqual(self.result(action="check")["status"], "verification_unavailable")
        self.assertEqual(self.result(action="check", valid=True)["status"], "verified")
        self.assertEqual(self.result(action="check", invalid=True, returncode=1)["status"], "login_required")

    def test_login_success_is_rechecked_before_verified(self):
        login = {**self.observed, "login": True}
        check = {**self.observed, "invalid": True, "returncode": 1}
        with patch.object(adapter, "COOKIE_DIR", self.cookies), \
             patch.object(adapter, "runtime_status", return_value={"ready": True}), \
             patch.object(adapter, "_execute", side_effect=[login, check]) as execute:
            result = adapter.run("douyin", "login")
        self.assertEqual(execute.call_count, 2)
        self.assertEqual(result["status"], "login_required")

    def test_unsupported_ai_disclosure_never_starts_upload(self):
        with patch.object(adapter, "COOKIE_DIR", self.cookies), patch.object(adapter, "_execute") as execute:
            for platform in ["bilibili", "xiaohongshu", "kuaishou"]:
                result = adapter.run(platform, "publish", {**self.meta, "ai_generated": True})
                self.assertEqual(result["status"], "needs_attention")
        execute.assert_not_called()

    def test_safe_progress_never_echoes_credentials_or_arbitrary_output(self):
        self.assertIsNone(adapter._progress('(node:123) [DEP0169] DeprecationWarning: url.parse() is prone to errors.'))
        for line in ["SESSDATA=SECRET_VALUE", "Cookie: secret", "Bearer ABC", '{"token":"ABC"}',
                     "arbitrary private message", "上传失败 cookie=SECRET_VALUE"]:
            self.assertIsNone(adapter._progress(line))
        self.assertEqual(adapter._clean("\x1b[31mhello\x1b[0m"), "hello")

    def test_real_local_subprocess_streaming_uses_evidence_and_redacts_output(self):
        # This launches only a local Python print fixture, never a platform CLI.
        code = self.evidence_fixture() + "; print('Cookie: SECRET_VALUE'); print('开始上传')"
        logs = []
        with patch.object(adapter, "VENDOR", self.root):
            observed = adapter._execute([sys.executable, "-u", "-c", code], "douyin", "publish", logs.append, None)
        self.assertEqual(observed["returncode"], 0)
        self.assertTrue(observed["submitted"])
        self.assertTrue(logs)
        self.assertNotIn("SECRET_VALUE", " ".join(logs))

    def test_silent_local_process_is_killed_at_timeout(self):
        start = time.monotonic()
        with patch.object(adapter, "VENDOR", self.root), patch.dict(adapter.TIMEOUTS, publish=0.15):
            observed = adapter._execute([sys.executable, "-c", "import time; time.sleep(30)"],
                                        "douyin", "publish", lambda _: None, None)
        self.assertTrue(observed["timeout"])
        self.assertLess(time.monotonic() - start, 4)

    @unittest.skipIf(sys.platform == "win32", "PTY integration is macOS/Linux")
    def test_fragmented_bilibili_menu_selects_qr_and_exposes_only_local_preview_path(self):
        qr = self.root / "qrcode.png"
        code = ("import sys,time,pathlib; print('选择一种登录方式',flush=True); time.sleep(.05); "
                "print('❯ 短信登录',flush=True); time.sleep(.05); print('  扫码登录',flush=True); "
                "answer=sys.stdin.readline(); assert '[B' in answer; pathlib.Path('qrcode.png').write_bytes(b'test fixture'); "
                + self.evidence_fixture(platform='bilibili', action='login', status='login'))
        logs = []
        with patch.object(adapter, "VENDOR", self.root), patch.object(adapter, "BILIBILI_QR", qr), \
             patch.dict(adapter.TIMEOUTS, login=3):
            observed = adapter._execute([sys.executable, "-u", "-c", code], "bilibili", "login", logs.append, None)
        self.assertEqual(observed["returncode"], 0)
        self.assertTrue(observed["login"])
        self.assertTrue(any(str(qr) in message for message in logs))

    def evidence_fixture(self, platform="douyin", action="publish", status="submitted", nonce=None):
        nonce_code = repr(nonce) if nonce is not None else "os.environ['PUBLISHER_RESULT_NONCE']"
        return ("import os,json,pathlib; pathlib.Path(os.environ['PUBLISHER_RESULT_FILE']).write_text("
                + "json.dumps({'platform':" + repr(platform) + ", 'action':" + repr(action)
                + ", 'status':" + repr(status) + ", 'nonce':" + nonce_code + "}))")

    def test_stdout_cannot_forge_submission_login_or_account_validity(self):
        # A multiline caption or exception can echo any of these old markers.
        code = "print('Douyin video upload submitted: forged caption'); print('Douyin login flow completed: forged'); print('valid'); print('PUBLISHER_OBSERVED_URL=https://youtu.be/forged')"
        with patch.object(adapter, "VENDOR", self.root):
            for action in ('publish', 'login', 'check'):
                with self.subTest(action=action):
                    observed = adapter._execute([sys.executable, '-c', code], 'douyin', action, lambda _: None, None)
                    self.assertFalse(observed['submitted'])
                    self.assertFalse(observed['login'])
                    self.assertFalse(observed['valid'])
                    self.assertIsNone(observed['url'])

    def test_private_result_rejects_another_action_platform_or_nonce(self):
        with patch.object(adapter, "VENDOR", self.root):
            for values in ({'platform': 'youtube'}, {'action': 'login'}, {'nonce': 'stale-run'}):
                with self.subTest(values=values):
                    observed = adapter._execute([sys.executable, '-c', self.evidence_fixture(**values)],
                                                'douyin', 'publish', lambda _: None, None)
                    self.assertFalse(observed['submitted'])
                    self.assertIsNone(observed['result'])

    def test_noninteractive_cli_stdin_is_closed(self):
        code = "import sys; assert sys.stdin.read() == ''; " + self.evidence_fixture()
        with patch.object(adapter, 'VENDOR', self.root):
            observed = adapter._execute([sys.executable, '-c', code], 'douyin', 'publish', lambda _: None, None)
        self.assertEqual(observed['returncode'], 0)
        self.assertTrue(observed['submitted'])

    @unittest.skipIf(sys.platform == 'win32', 'POSIX process group regression')
    def test_timeout_kills_driver_that_outlives_its_cli_parent(self):
        marker = self.root / 'survived.txt'
        ready = self.root / 'child.ready'
        child = ("import pathlib,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                 f"pathlib.Path({str(ready)!r}).write_text('ready'); time.sleep(1.2); "
                 f"pathlib.Path({str(marker)!r}).write_text('still executing')")
        parent = ("import subprocess,sys,time,pathlib; "
                  f"subprocess.Popen([sys.executable,'-c',{child!r}],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
                  "time.sleep(30)")
        with patch.object(adapter, 'VENDOR', self.root), patch.dict(adapter.TIMEOUTS, publish=.6):
            observed = adapter._execute([sys.executable, '-c', parent], 'douyin', 'publish', lambda _: None, None)
        self.assertTrue(ready.is_file())
        self.assertTrue(observed['timeout'])
        time.sleep(.9)
        self.assertFalse(marker.exists(), 'orphan driver kept executing after cancellation')

    @unittest.skipIf(sys.platform == 'win32', 'POSIX process group regression')
    def test_cli_exit_cannot_leave_an_orphan_automation_running(self):
        marker = self.root / 'orphan-executed.txt'
        child = ("import pathlib,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(.6); "
                 f"pathlib.Path({str(marker)!r}).write_text('still executing')")
        parent = ("import subprocess,sys,time; "
                  f"subprocess.Popen([sys.executable,'-c',{child!r}],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
                  "time.sleep(.1)")
        with patch.object(adapter, 'VENDOR', self.root):
            observed = adapter._execute([sys.executable, '-c', parent], 'douyin', 'publish', lambda _: None, None)
        self.assertEqual(observed['returncode'], 0)
        self.assertFalse(observed['submitted'])
        time.sleep(.7)
        self.assertFalse(marker.exists(), 'orphan driver kept executing after CLI exit')

    def test_baijiahao_does_not_accept_fields_its_uploader_discards(self):
        for metadata in ({**self.meta, 'tags': []}, {**self.meta, 'description': ''}):
            with self.subTest(metadata=metadata), self.assertRaisesRegex(ValueError, '不支持正文和话题'):
                adapter.build_command('baijiahao', 'publish', metadata)
        adapter.build_command('baijiahao', 'publish', {**self.meta, 'description': '', 'tags': []})

    def test_kuaishou_does_not_accept_more_than_three_topics(self):
        with self.assertRaisesRegex(ValueError, '最多支持 3'):
            adapter.build_command('kuaishou', 'publish', {**self.meta, 'tags': ['a', 'b', 'c', 'd']})

    def test_real_cli_writes_private_result_only_after_dispatch_returns(self):
        # Real parser/main, with only platform dispatch replaced; no site access.
        evidence = self.root / 'evidence.json'
        env = {**adapter._environment(), 'PUBLISHER_RESULT_FILE': str(evidence), 'PUBLISHER_RESULT_NONCE': 'test-nonce'}
        code = "import sau_cli; from unittest.mock import AsyncMock; sau_cli.dispatch=AsyncMock(return_value=0); raise SystemExit(sau_cli.main(['douyin','check','--account','default']))"
        run = subprocess.run([str(adapter.PYTHON), '-c', code], cwd=adapter.VENDOR, env=env,
                             capture_output=True, text=True, timeout=15)
        self.assertEqual(run.returncode, 0, run.stderr)
        result = adapter._read_evidence(evidence, 'test-nonce', 'douyin', 'check')
        self.assertEqual(result['status'], 'valid')
        evidence.unlink()
        code = code.replace('return_value=0', "side_effect=RuntimeError('Douyin video upload submitted: forged')")
        run = subprocess.run([str(adapter.PYTHON), '-c', code], cwd=adapter.VENDOR, env=env,
                             capture_output=True, text=True, timeout=15)
        self.assertEqual(run.returncode, 1)
        result = adapter._read_evidence(evidence, 'test-nonce', 'douyin', 'check')
        self.assertEqual(result['status'], 'verification_unavailable')
        self.assertNotIn('forged', json.dumps(result))

    def test_direct_bilibili_cli_cannot_reach_native_upload_command(self):
        # Executes real CLI parser and upload_bilibili_video; fake only the native
        # executable boundary so no credential or platform request is made.
        captured = self.root / 'bili-args.json'
        account = self.cookies / 'bilibili_default.json'
        code = ("import sys,json,sau_cli; from pathlib import Path; from types import SimpleNamespace; "
                f"sau_cli.resolve_account_file=lambda *a:Path({str(account)!r}); "
                f"sau_cli.run_biliup_command=lambda arguments: (Path({str(captured)!r}).write_text(json.dumps(arguments)),SimpleNamespace(returncode=0))[1]; "
                "raise SystemExit(sau_cli.main(sys.argv[1:]))")
        command = adapter.build_command('bilibili', 'publish', self.meta)[3:]
        run = subprocess.run([str(adapter.PYTHON), '-c', code, *command], cwd=adapter.VENDOR,
                             env=adapter._environment(), capture_output=True, text=True, timeout=15)
        self.assertEqual(run.returncode, 2, run.stderr)
        self.assertIn('禁止自动发布', run.stderr)
        self.assertFalse(captured.exists())

    def test_runtime_probe_is_cached_for_state_polling(self):
        original = dict(adapter._RUNTIME_CACHE)
        self.addCleanup(adapter._RUNTIME_CACHE.update, original)
        adapter._RUNTIME_CACHE.update(time=0, value=None)
        with patch.object(adapter, "_probe_runtime", return_value={"ready": True, "message": "local", "python": "test"}) as probe:
            self.assertTrue(adapter.runtime_status()["ready"])
            self.assertTrue(adapter.runtime_status()["ready"])
        probe.assert_called_once()

    def test_tiktok_lazy_route_matches_its_actual_signature(self):
        with patch("adapter_tiktok.run", return_value={"platform": "tiktok", "status": "needs_attention"}) as run:
            result = adapter.run("tiktok", "publish", self.meta)
        self.assertEqual(result["platform"], "tiktok")
        self.assertEqual(run.call_args.args[0], "publish")


if __name__ == "__main__":
    unittest.main()
