import asyncio
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import adapter
import server


class ManualPrepareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = server.create_app(self.temp.name, start_worker=False)
        self.client = self.app.test_client()
        self.store = self.app.extensions['store']
        self.manager = self.app.extensions['manager']
        self.headers = {'X-Publisher-Token': self.app.extensions['publisher_token']}
        for aid, kind in [('video', 'video'), ('cover', 'cover')]:
            p = self.store.assets_dir / aid
            p.write_bytes(b'synthetic test fixture; never uploaded')
            self.store.put('asset', aid, {'id': aid, 'kind': kind, 'path': str(p), 'name': aid, 'sha256': aid, 'probe_verified': True})
        self.payload = {'video_id': 'video', 'cover_id': 'cover', 'title': '手动确认测试',
                        'description': '测试正文', 'tags': ['科普'], 'platforms': ['douyin'], 'ai_generated': False}
        self.store.put('account', 'douyin', {'status': 'verified'})
        self.runtime = patch('adapter.runtime_status', return_value={'ready': True})
        self.runtime.start(); self.addCleanup(self.runtime.stop)

    def post(self, payload):
        return self.client.post('/api/jobs', json=payload, headers=self.headers)

    def test_legacy_request_forces_prepare_and_prepared_prevents_duplicate(self):
        response = self.post(self.payload)
        self.assertEqual(response.status_code, 201)
        job = self.store.get('job', response.json['job']['id'])
        self.assertEqual(job['payload']['mode'], 'prepare')
        self.assertTrue(job['plans'][0]['prepare_only'])
        job['status'] = 'completed'; job['results'][0]['status'] = 'prepared'
        self.store.put('job', job['id'], job)
        self.assertEqual(self.post(self.payload).status_code, 409)

    def test_automatic_and_other_platform_requests_never_queue(self):
        self.assertEqual(self.post({**self.payload, 'mode': 'automatic'}).status_code, 400)
        self.assertEqual(self.post({**self.payload, 'platforms': ['bilibili'], 'bilibili': {'tid': 201}}).status_code, 400)
        self.assertEqual(self.manager.queue.qsize(), 0)
        state = self.client.get('/api/state').json
        self.assertEqual(state['submission_mode'], 'manual_review')
        self.assertEqual(state['prepare_platforms'], ['douyin', 'xiaohongshu', 'youtube', 'bilibili'])

    def test_multiple_supported_platforms_get_prepare_only_plans(self):
        self.store.put('account', 'xiaohongshu', {'status': 'verified'})
        response = self.post({**self.payload, 'platforms': ['douyin', 'xiaohongshu']})
        self.assertEqual(response.status_code, 201)
        job = self.store.get('job', response.json['job']['id'])
        self.assertTrue(all(p['prepare_only'] is True for p in job['plans']))

    def test_user_can_resolve_prepared_without_republishing(self):
        job = self.post(self.payload).json['job']
        with patch.object(adapter, 'run', return_value={'platform': 'douyin', 'status': 'prepared', 'retry_safe': False}):
            self.manager.execute(job['id'])
        url = '/api/jobs/'+job['id']+'/resolve'
        self.assertEqual(self.client.post(url, json={'platform': 'douyin', 'resolution': 'not_submitted'}, headers=self.headers).status_code, 400)
        queued = self.manager.queue.qsize()
        response = self.client.post(url, json={'platform': 'douyin', 'resolution': 'not_submitted', 'confirmed': True}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.manager.queue.qsize(), queued)
        self.assertEqual(response.json['job']['results'][0]['status'], 'failed')
        self.assertEqual(self.post(self.payload).status_code, 201)

    def test_youtube_duplicate_lock_uses_stable_channel_not_session_import(self):
        self.store.put('account', 'youtube', {'status': 'verified'})
        payload = {**self.payload, 'platforms': ['youtube'], 'youtube': {'visibility': 'private', 'made_for_kids': False, 'altered_synthetic': False}}
        channel_a, channel_b = 'UC'+'a'*22, 'UC'+'b'*22
        with patch.object(server.browser_sessions, 'read_metadata', return_value={'channel_id': channel_a}):
            first = self.post(payload).json['job']
            job = self.store.get('job', first['id']); job['status']='completed';job['results'][0]['status']='submitted'
            self.store.put('job', job['id'], job)
            self.assertEqual(self.post(payload).status_code, 409)
        with patch.object(server.browser_sessions, 'read_metadata', return_value={'channel_id': channel_b}):
            response = self.post({**payload, 'account_scopes': {'youtube': channel_a}})
            self.assertEqual(response.status_code, 201)
            second = self.store.get('job', response.json['job']['id'])
            self.assertEqual(second['payload']['account_scopes'], {'youtube': channel_b})

    def cover_failure_job(self):
        self.store.put('account', 'xiaohongshu', {'status': 'verified'})
        cover=self.store.get('asset','cover');cover['sha256']=hashlib.sha256(Path(cover['path']).read_bytes()).hexdigest()
        self.store.put('asset','cover',cover)
        job=self.post({**self.payload,'platforms':['douyin','xiaohongshu']}).json['job']
        with patch.object(adapter,'run',side_effect=[{'platform':'douyin','status':'prepared'},
            {'platform':'xiaohongshu','status':'needs_attention','phase':'cover','retry_safe':False}]):
            self.manager.execute(job['id'])
        return job

    def test_resume_cover_continues_same_job_and_only_failed_platform(self):
        job=self.cover_failure_job();url='/api/jobs/'+job['id']+'/resume-cover'
        response=self.client.post(url,json={'platform':'xiaohongshu'},headers=self.headers)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['job']['id'],job['id'])
        self.assertEqual(response.json['job']['mode'],'prepare')
        queued=self.manager.queue.qsize()
        self.assertEqual(self.client.post(url,json={'platform':'xiaohongshu'},headers=self.headers).status_code,409)
        self.assertEqual(self.manager.queue.qsize(),queued)
        with patch.object(adapter,'run',return_value={'platform':'xiaohongshu','status':'prepared','phase':'ready_to_submit'}) as run:
            self.manager.execute(job['id'])
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0],'xiaohongshu')
        self.assertTrue(run.call_args.args[2]['resume_cover'])
        done=self.store.get('job',job['id'])
        self.assertEqual([r['status'] for r in done['results']],['prepared','prepared'])
        self.assertEqual(len(self.store.all('job')),1)

    def test_resume_cover_rejects_changed_materials_or_wrong_boundary(self):
        job=self.cover_failure_job();url='/api/jobs/'+job['id']+'/resume-cover'
        self.assertEqual(self.client.post(url,json={'platform':'douyin'},headers=self.headers).status_code,400)
        Path(self.store.get('asset','cover')['path']).write_bytes(b'different file')
        self.assertEqual(self.client.post(url,json={'platform':'xiaohongshu'},headers=self.headers).status_code,409)

    def test_cancel_queued_resume_preserves_all_previous_results_and_locks(self):
        job = self.cover_failure_job()
        before = self.store.get('job', job['id'])['results']
        url = '/api/jobs/' + job['id'] + '/resume-cover'
        self.assertEqual(self.client.post(url, json={'platform': 'xiaohongshu'}, headers=self.headers).status_code, 200)
        self.manager.events[job['id']].set()
        with patch.object(adapter, 'run') as run:
            self.manager.execute(job['id'])
        run.assert_not_called()
        self.assertEqual(self.store.get('job', job['id'])['results'], before)
        self.assertEqual(self.post(self.payload).status_code, 409)
        self.assertEqual(self.client.post(url, json={'platform': 'xiaohongshu'}, headers=self.headers).status_code, 200)

    def test_restart_before_resume_preserves_uploaded_material_uncertainty(self):
        job = self.cover_failure_job()
        before = self.store.get('job', job['id'])['results']
        self.client.post('/api/jobs/' + job['id'] + '/resume-cover', json={'platform': 'xiaohongshu'}, headers=self.headers)
        server.JobManager(self.store, start_worker=False)
        self.assertEqual(self.store.get('job', job['id'])['results'], before)

    def test_resume_never_lowers_unknown_or_submission_evidence(self):
        for phase in ('submitting', 'submitted', 'unknown', None):
            with self.subTest(phase=phase):
                job = self.cover_failure_job()
                url = '/api/jobs/' + job['id'] + '/resume-cover'
                self.assertEqual(self.client.post(url, json={'platform': 'xiaohongshu'}, headers=self.headers).status_code, 200)
                with patch.object(adapter, 'run', return_value={'platform': 'xiaohongshu', 'status': 'needs_attention', 'phase': phase, 'retry_safe': False}):
                    self.manager.execute(job['id'])
                result = self.store.get('job', job['id'])['results'][1]
                self.assertEqual(result['phase'], phase)
                self.assertEqual(self.client.post(url, json={'platform': 'xiaohongshu'}, headers=self.headers).status_code, 409)
                # Clean up only synthetic rows so next subcase can create its fixture.
                with self.store.connect() as db:
                    db.execute("DELETE FROM records WHERE kind='job'")

    def test_resume_unexpected_exception_cannot_reuse_stale_cover_phase(self):
        job = self.cover_failure_job()
        url = '/api/jobs/' + job['id'] + '/resume-cover'
        self.client.post(url, json={'platform': 'xiaohongshu'}, headers=self.headers)
        with patch.object(adapter, 'run', side_effect=RuntimeError('synthetic')):
            self.manager.execute(job['id'])
        result = self.store.get('job', job['id'])['results'][1]
        self.assertEqual(result['phase'], 'unknown')
        self.assertFalse(result['retry_safe'])
        self.assertEqual(self.client.post(url, json={'platform': 'xiaohongshu'}, headers=self.headers).status_code, 409)

    def test_resume_outcome_cannot_erase_persisted_submission_progress(self):
        job = self.cover_failure_job()
        url = '/api/jobs/' + job['id'] + '/resume-cover'
        self.client.post(url, json={'platform': 'xiaohongshu'}, headers=self.headers)
        def inconsistent(platform, kind, meta, log, event):
            log.event({'phase': 'submitting'})
            return {'platform': platform, 'status': 'needs_attention', 'phase': 'preparation', 'retry_safe': True}
        with patch.object(adapter, 'run', side_effect=inconsistent):
            self.manager.execute(job['id'])
        result = self.store.get('job', job['id'])['results'][1]
        self.assertEqual(result['phase'], 'submitting')
        self.assertFalse(result['retry_safe'])
        self.assertEqual(self.client.post(url, json={'platform': 'xiaohongshu'}, headers=self.headers).status_code, 409)

    def test_unknown_progress_cannot_be_lowered_by_later_cover_progress(self):
        job = self.cover_failure_job()
        url = '/api/jobs/' + job['id'] + '/resume-cover'
        self.client.post(url, json={'platform': 'xiaohongshu'}, headers=self.headers)
        def inconsistent(platform, kind, meta, log, event):
            log.event({'phase': None})
            log.event({'phase': 'cover'})
            return {'platform': platform, 'status': 'needs_attention', 'phase': 'cover'}
        with patch.object(adapter, 'run', side_effect=inconsistent):
            self.manager.execute(job['id'])
        self.assertEqual(self.store.get('job', job['id'])['results'][1]['phase'], 'unknown')
        self.assertEqual(self.client.post(url, json={'platform': 'xiaohongshu'}, headers=self.headers).status_code, 409)

    def test_new_request_cannot_inject_resume_mode(self):
        job=self.post({**self.payload,'resume_platform':'xiaohongshu','resume_phase':'cover'}).json['job']
        self.assertNotIn('resume_platform',self.store.get('job',job['id'])['payload'])

    def test_worker_finishes_as_waiting_for_user_not_submitted(self):
        job = self.post(self.payload).json['job']
        with patch.object(adapter, 'run', return_value={'platform': 'douyin', 'status': 'prepared', 'retry_safe': False}):
            self.manager.execute(job['id'])
        done = self.store.get('job', job['id'])
        self.assertEqual(done['status'], 'prepared')
        self.assertEqual(self.manager.maintenance_state()['active_jobs'], 0)

    def test_adapter_requires_prepared_evidence_and_never_maps_submitted_to_prepared(self):
        metadata = {'title': '手动确认测试', 'description': '', 'tags': [], 'prepare_only': True, 'ai_generated': False,
                    'video_path': str(self.store.assets_dir/'video'), 'cover_path': str(self.store.assets_dir/'cover')}
        cookie_dir = Path(self.temp.name) / 'cookies'; cookie_dir.mkdir()
        (cookie_dir/'douyin_default.json').write_text('{}')
        observed = {'returncode': 0, 'cancelled': False, 'timeout': False, 'phase': 'ready_to_submit'}
        with patch.object(adapter, 'COOKIE_DIR', cookie_dir), patch.object(adapter, '_execute') as execute:
            execute.return_value = {**observed, 'result': {'status': 'prepared', 'phase': 'ready_to_submit'}}
            result = adapter.run('douyin', 'publish', metadata)
            self.assertEqual(result['status'], 'prepared')
            self.assertFalse(result['retry_safe'])
            execute.return_value = {**observed, 'phase': 'submitting', 'result': {'status': 'prepared', 'phase': 'ready_to_submit'}}
            self.assertEqual(adapter.run('douyin', 'publish', metadata)['status'], 'needs_attention')
            execute.return_value = {**observed, 'result': {'status': 'submitted'}}
            self.assertEqual(adapter.run('douyin', 'publish', metadata)['status'], 'needs_attention')
        self.assertEqual(adapter._environment(metadata)['SAU_PUBLISHER_PREPARE_ONLY'], '1')
        self.assertEqual(adapter._environment()['SAU_PUBLISHER_PREPARE_ONLY'], '0')
        with self.assertRaises(ValueError): adapter.build_command('bilibili', 'publish', metadata)

    def test_vendor_handoff_checks_page_without_submitting_or_closing_browser(self):
        code = '''import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uploader.douyin_uploader.main import DouYinVideo
class Forbidden:
 def __getattr__(self, name): raise AssertionError('handoff touched final submission or closed window: '+name)
async def verify():
 button=SimpleNamespace(count=AsyncMock(return_value=1),is_visible=AsyncMock(return_value=True),is_enabled=AsyncMock(return_value=True),get_attribute=AsyncMock(return_value=None),click=AsyncMock(side_effect=AssertionError('final click')))
 button.filter=lambda **kwargs:button
 empty=SimpleNamespace(count=AsyncMock(return_value=0))
 page=SimpleNamespace(url='https://creator.douyin.com/creator-micro/content/post/video',is_closed=lambda:False,locator=lambda _:empty,get_by_role=lambda *args,**kwargs:button,evaluate=AsyncMock(return_value={'completed':True,'busy':False,'error':False}))
 uploader=object.__new__(DouYinVideo);uploader.prepare_only=True
 assert await uploader.finish_upload(page,Forbidden(),Forbidden())=='prepared'
 button.click.assert_not_awaited()
asyncio.run(verify())
'''
        result = subprocess.run([str(adapter.PYTHON), '-c', code], cwd=adapter.VENDOR,
                                env=adapter._environment(), capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__': unittest.main()
