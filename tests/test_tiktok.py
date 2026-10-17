"""Offline adapter tests. No test launches Chrome or contacts TikTok."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import subprocess
import threading
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import adapter_tiktok as tk


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.video = Path(self.temp.name) / 'video.mp4'
        self.cover = Path(self.temp.name) / 'cover.png'
        self.video.write_bytes(b'test fixture, never uploaded')
        self.cover.write_bytes(b'test fixture, never uploaded')
        self.meta = dict(video_path=str(self.video), cover_path=str(self.cover), title='Title',
                         description='Body', tags=['#topic'], ai_generated=False)

    def test_caption_preserves_title_description_and_tags(self):
        caption, tags = tk._validate_publish(self.meta)
        self.assertEqual(caption, 'Title\n\nBody')
        self.assertEqual(tags, ['topic'])

    def test_ai_choice_is_required(self):
        del self.meta['ai_generated']
        with self.assertRaisesRegex(ValueError, 'AI'):
            tk._validate_publish(self.meta)

    def test_cover_never_silently_omitted(self):
        self.meta['cover_path'] = None
        with self.assertRaisesRegex(ValueError, '封面'):
            tk._validate_publish(self.meta)

    def test_command_filters_credentials_and_unknown_fields(self):
        command = tk.build_command('publish', dict(self.meta, token='secret', cookies=['secret']))
        self.assertEqual(command[2], 'publish')
        self.assertNotIn('secret', ' '.join(command))
        self.assertEqual(json.loads(command[-1])['description'], 'Body')

    def test_command_rejects_unrecognized_action(self):
        with self.assertRaises(ValueError):
            tk.build_command('anything')

    def test_no_implicit_caption_truncation(self):
        self.meta['description'] = 'x' * 2201
        with self.assertRaisesRegex(ValueError, '未自动截断'):
            tk._validate_publish(self.meta)

    def test_cancelled_job_does_not_start_process(self):
        cancel = threading.Event()
        cancel.set()
        with patch.object(tk.subprocess, 'Popen') as popen:
            result = tk.run('publish', self.meta, lambda _: None, cancel)
        self.assertEqual(result['status'], 'needs_attention')
        popen.assert_not_called()

    def evidence_fixture(self, status='submitted', action='publish', exitcode=0):
        result = tk._result(status, 'offline fixture')
        result['action'] = action
        return ("import json,os,pathlib,sys; result=" + repr(result) + "; "
                "result['nonce']=os.environ['PUBLISHER_RESULT_NONCE']; "
                "pathlib.Path(os.environ['PUBLISHER_RESULT_FILE']).write_text(json.dumps(result)); "
                "print('raw cookie=do-not-forward'); print('开始上传'); "
                f"sys.exit({exitcode})")

    def run_fixture(self, code, action='publish'):
        logs = []
        with patch.object(tk, 'build_command', return_value=[sys.executable, '-u', '-c', code]):
            result = tk.run(action, self.meta, logs.append)
        return result, logs

    def test_wrapper_accepts_only_private_results_and_safe_logs(self):
        result, logs = self.run_fixture(self.evidence_fixture())
        self.assertEqual(result['status'], 'submitted')
        self.assertNotIn('do-not-forward', ' '.join(logs))
        self.assertTrue(logs)
        self.assertNotIn('nonce', result)

    def test_wrapper_missing_result_never_claims_submitted(self):
        result, _ = self.run_fixture("print('success!')")
        self.assertEqual(result['status'], 'needs_attention')

    def test_stdout_structured_result_cannot_forge_success(self):
        code = "print('TIKTOK_RESULT ' + " + repr(json.dumps(tk._result('submitted', 'forged'))) + ")"
        result, _ = self.run_fixture(code)
        self.assertEqual(result['status'], 'needs_attention')

    def test_nonzero_exit_overrides_even_private_submission_evidence(self):
        result, _ = self.run_fixture(self.evidence_fixture(exitcode=7))
        self.assertEqual(result['status'], 'needs_attention')

    def test_login_cannot_report_a_submission_or_another_action_result(self):
        for code in (self.evidence_fixture(action='login'), self.evidence_fixture(status='verified')):
            result, _ = self.run_fixture(code, action='login')
            self.assertEqual(result['status'], 'failed')

    def test_silent_tiktok_subprocess_is_bounded_and_stdin_is_closed(self):
        with patch.dict(tk.TIMEOUTS, publish=.15):
            result, _ = self.run_fixture("import sys,time; assert sys.stdin.read()==''; time.sleep(30)")
        self.assertEqual(result['status'], 'needs_attention')
        self.assertIn('超时', result['message'])

    def test_caption_limit_counts_separator_before_first_hashtag(self):
        self.meta.update(title='x' * 2198, description='', tags=['a'])
        with self.assertRaises(ValueError):
            tk._validate_publish(self.meta)

    def test_build_command_rejects_missing_publish_material_before_spawn(self):
        with self.assertRaises(ValueError):
            tk.build_command('publish', {})

    def test_real_tiktok_main_uses_private_result_channel(self):
        evidence = Path(self.temp.name) / 'result.json'
        import os
        env = {**os.environ, 'PUBLISHER_RESULT_FILE': str(evidence), 'PUBLISHER_RESULT_NONCE': 'offline-nonce'}
        code = ("import adapter_tiktok as t; from unittest.mock import AsyncMock; "
                "t._execute=AsyncMock(return_value=t._result('verified','fixture')); t.main()")
        process = subprocess.run([sys.executable, '-c', code, 'check'], cwd=tk.BASE, env=env,
                                 capture_output=True, text=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(evidence.read_text())
        self.assertEqual(result['action'], 'check')
        self.assertEqual(result['nonce'], 'offline-nonce')
        self.assertEqual(result['status'], 'verified')
        self.assertNotIn(tk.RESULT_PREFIX, process.stdout)


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmed_result_closes_dedicated_browser(self):
        await self._test_browser_lifecycle('verified', True)

    async def test_manual_intervention_keeps_browser_open(self):
        await self._test_browser_lifecycle('needs_attention', False)

    async def _test_browser_lifecycle(self, status, should_close):
        context = MagicMock()
        context.new_page = AsyncMock()
        browser = SimpleNamespace(contexts=[context])
        playwright = MagicMock()
        playwright.chromium.connect_over_cdp = AsyncMock(return_value=browser)
        manager = MagicMock()
        manager.__aenter__ = AsyncMock(return_value=playwright)
        manager.__aexit__ = AsyncMock(return_value=False)
        close = AsyncMock()
        with patch('patchright.async_api.async_playwright', return_value=manager), \
             patch.object(tk, '_ensure_visible_chrome', return_value='http://127.0.0.1:12345'), \
             patch.object(tk, '_login', AsyncMock(return_value=tk._result(status, 'test'))), \
             patch.object(tk, '_close_detached_browser', close):
            result = await tk._execute('login', {})
        self.assertEqual(result['status'], status)
        if should_close:
            close.assert_awaited_once_with(browser)
        else:
            close.assert_not_awaited()

    async def test_empty_or_unrecognized_page_does_not_verify_login(self):
        page = MagicMock(url=tk.UPLOAD_URL)
        button = SimpleNamespace(is_visible=AsyncMock(return_value=False))
        page.get_by_role.return_value = button
        base = MagicMock()
        base.get_by_role.return_value = button
        with patch.object(tk, '_base', AsyncMock(return_value=base)):
            self.assertEqual(await tk._probe(page), 'needs_attention')

    async def test_login_redirect_is_not_verified(self):
        page = SimpleNamespace(url='https://www.tiktok.com/login')
        self.assertEqual(await tk._probe(page), 'login_required')

    async def test_check_without_saved_account_does_not_launch_browser(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(tk, 'DATA', Path(directory)):
            playwright = MagicMock()
            result = await tk._check(playwright)
        self.assertEqual(result['status'], 'login_required')
        playwright.chromium.launch.assert_not_called()

    async def test_publish_without_saved_account_never_launches_browser(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(tk, 'DATA', Path(directory)), \
             patch.object(tk, '_validate_publish', return_value=('caption', [])), \
             patch.object(tk, '_ensure_visible_chrome') as chrome:
            result = await tk._execute('publish', {})
        self.assertEqual(result['status'], 'login_required')
        chrome.assert_not_called()

    async def test_saved_account_must_match_profile_identity(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(tk, 'DATA', Path(directory)):
            saved = {'name': 'uid_tt', 'value': 'saved-user-fixture', 'domain': '.tiktok.com'}
            (Path(directory) / 'account.json').write_text(json.dumps({'cookies': [saved]}))
            context = SimpleNamespace(cookies=AsyncMock(return_value=[saved]))
            self.assertTrue(await tk._same_saved_account(context))
            context.cookies.return_value = [{**saved, 'value': 'another-user-fixture'}]
            self.assertFalse(await tk._same_saved_account(context))
            context.cookies.return_value = [{**saved, 'domain': 'tiktok.com.attacker.invalid'}]
            self.assertFalse(await tk._same_saved_account(context))
            context.cookies.return_value = []
            self.assertFalse(await tk._same_saved_account(context))

    async def test_saved_session_fallback_does_not_accept_an_unknown_account(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(tk, 'DATA', Path(directory)):
            saved = {'name': 'sessionid', 'value': 'fixture-session', 'domain': '.tiktok.com'}
            (Path(directory) / 'account.json').write_text(json.dumps({'cookies': [saved]}))
            context = SimpleNamespace(cookies=AsyncMock(return_value=[saved]))
            self.assertTrue(await tk._same_saved_account(context))
            context.cookies.return_value = [{**saved, 'value': 'rotated-or-other-session'}]
            self.assertFalse(await tk._same_saved_account(context))

    async def test_post_is_clicked_only_once_when_confirmation_times_out(self):
        post = SimpleNamespace(click=AsyncMock())
        page = SimpleNamespace(wait_for_url=AsyncMock(side_effect=TimeoutError))
        result = await tk._click_post_once(page, post)
        self.assertEqual(result['status'], 'needs_attention')
        post.click.assert_awaited_once()

    async def test_content_page_redirect_is_submission_not_publication(self):
        post = SimpleNamespace(click=AsyncMock())
        page = SimpleNamespace(wait_for_url=AsyncMock())
        result = await tk._click_post_once(page, post)
        self.assertEqual(result['status'], 'submitted')
        self.assertNotIn('video_id', result)
        post.click.assert_awaited_once()

    async def test_caption_check_includes_description_and_tags(self):
        base = MagicMock()
        base.locator.return_value.inner_text = AsyncMock(return_value='Title\n\nBody\n#topic ')
        self.assertTrue(await tk._caption_matches(base, 'Title\n\nBody', ['topic']))
        self.assertFalse(await tk._caption_matches(base, 'Title\n\nMissing', ['topic']))

    async def _run_publish(self, ai=False, cover_failure=False, caption_matches=True, account_matches=None):
        with tempfile.TemporaryDirectory() as directory:
            video, cover = Path(directory) / 'video.mp4', Path(directory) / 'cover.png'
            video.write_bytes(b'not real video')
            cover.write_bytes(b'not real cover')
            meta = dict(video_path=str(video), cover_path=str(cover), title='Title',
                        description='Body', tags=['topic'], ai_generated=ai)
            page, base = MagicMock(), MagicMock()
            page.goto = AsyncMock()
            base.get_by_role.return_value.wait_for = AsyncMock()
            locator = MagicMock()
            locator.click, locator.wait_for = AsyncMock(), AsyncMock()
            base.locator.return_value = locator
            chooser = SimpleNamespace(set_files=AsyncMock())
            chosen = asyncio.get_running_loop().create_future()
            chosen.set_result(chooser)
            page.expect_file_chooser.return_value.__aenter__ = AsyncMock(return_value=SimpleNamespace(value=chosen))
            page.expect_file_chooser.return_value.__aexit__ = AsyncMock(return_value=False)
            uploader = SimpleNamespace(locator_base=base, choose_base_locator=AsyncMock(),
                                       add_title_tags=AsyncMock(), upload_thumbnails=AsyncMock())
            if cover_failure:
                uploader.upload_thumbnails.side_effect = RuntimeError('selector changed')
            click = AsyncMock(return_value=tk._result('submitted', 'submitted'))
            with patch.object(tk, '_base', AsyncMock(return_value=base)), \
                 patch.object(tk, '_probe', AsyncMock(return_value='verified')), \
                 patch.object(tk, '_same_saved_account', AsyncMock(side_effect=account_matches or [True, True])), \
                 patch.object(tk, '_save_account', AsyncMock()), \
                 patch.object(tk, '_vendor_class', return_value=MagicMock(return_value=uploader)), \
                 patch.object(tk, '_wait_uploaded', AsyncMock()), \
                 patch.object(tk, '_caption_matches', AsyncMock(return_value=caption_matches)), \
                 patch.object(tk, '_click_post_once', click), patch.object(tk, '_log'):
                result = await tk._publish(MagicMock(), page, meta)
            return result, click, chooser, uploader

    async def test_changed_profile_account_never_uploads_or_posts(self):
        result, click, chooser, _ = await self._run_publish(account_matches=[False])
        self.assertEqual(result['status'], 'login_required')
        click.assert_not_awaited()
        chooser.set_files.assert_not_awaited()

    async def test_account_switch_during_fill_never_clicks_post(self):
        result, click, chooser, _ = await self._run_publish(account_matches=[True, False])
        self.assertEqual(result['status'], 'needs_attention')
        click.assert_not_awaited()
        chooser.set_files.assert_awaited_once()

    async def test_ai_content_stops_after_filling_and_never_clicks_post(self):
        result, click, chooser, uploader = await self._run_publish(ai=True)
        self.assertEqual(result['status'], 'needs_attention')
        self.assertEqual(result['cover_status'], 'set_in_browser')
        click.assert_not_awaited()
        chooser.set_files.assert_awaited_once()
        uploader.upload_thumbnails.assert_awaited_once()

    async def test_cover_failure_never_clicks_post(self):
        result, click, _, _ = await self._run_publish(cover_failure=True)
        self.assertEqual(result['status'], 'needs_attention')
        self.assertEqual(result['cover_status'], 'needs_attention')
        click.assert_not_awaited()

    async def test_caption_mismatch_never_clicks_post(self):
        result, click, _, _ = await self._run_publish(caption_matches=False)
        self.assertEqual(result['status'], 'needs_attention')
        click.assert_not_awaited()

    async def test_non_ai_complete_metadata_posts_once(self):
        result, click, _, _ = await self._run_publish()
        self.assertEqual(result['status'], 'submitted')
        self.assertEqual(result['cover_status'], 'set_in_browser')
        click.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
