import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import server


class RecoveryMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = server.create_app(self.temp.name, start_worker=False)
        self.client = self.app.test_client()
        self.store = self.app.extensions['store']
        self.manager = self.app.extensions['manager']
        self.headers = {'X-Publisher-Token': self.app.extensions['publisher_token']}
        runtime = patch('adapter.runtime_status', return_value={'ready': True})
        runtime.start()
        self.addCleanup(runtime.stop)
        for aid, kind, ext in [('v', 'video', '.mp4'), ('c', 'cover', '.png')]:
            path = self.store.assets_dir / (aid + ext)
            path.write_bytes(b'test fixture only')
            self.store.put('asset', aid, {'id': aid, 'name': aid + ext, 'kind': kind, 'path': str(path), 'size': path.stat().st_size, 'sha256': aid, 'probe_verified': True})
        self.payload = {'video_id': 'v', 'cover_id': 'c', 'title': '恢复材料测试标题', 'description': '\n原文换行\n',
                        'tags': ['#一 #二'], 'platforms': ['douyin'], 'ai_generated': False}
        self.store.put('account', 'douyin', {'status': 'verified'})

    def post(self, url, data):
        return self.client.post(url, json=data, headers=self.headers)

    def make_job(self):
        response = self.post('/api/jobs', self.payload)
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()['job']['id']

    def uncertain_job(self):
        jid = self.make_job()
        def mark(job):
            job['status'] = 'interrupted'
            job['results'][0].update(status='needs_attention', retry_safe=False, message='unknown')
        self.manager.update(jid, mark)
        return jid

    def test_restore_materials_never_starts_or_reuploads(self):
        jid = self.uncertain_job()
        before = self.manager.queue.qsize()
        with patch('adapter.run') as run:
            response = self.client.get('/api/jobs/' + jid + '/draft')
        data = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(data['payload']['description'], '\n原文换行\n')
        self.assertEqual(data['payload']['tags'], ['一', '二'])
        self.assertEqual(data['assets']['video']['id'], 'v')
        self.assertEqual(data['assets']['video']['url'], '/api/assets/v')
        self.assertNotIn('path', data['assets']['video'])
        self.assertEqual(self.manager.queue.qsize(), before)
        run.assert_not_called()

    def test_missing_asset_clears_only_that_reference(self):
        jid = self.uncertain_job()
        (self.store.assets_dir / 'c.png').unlink()
        data = self.client.get('/api/jobs/' + jid + '/draft').get_json()
        self.assertIsNone(data['payload']['cover_id'])
        self.assertEqual(data['payload']['video_id'], 'v')
        self.assertIsNone(data['assets']['cover'])
        self.assertTrue(data['warnings'])
        self.assertEqual(self.client.get('/api/assets/c/info').status_code, 404)

    def test_asset_info_does_not_expose_local_paths(self):
        data = self.client.get('/api/assets/v/info').get_json()['asset']
        self.assertEqual(data['id'], 'v')
        self.assertNotIn('path', data)

    def test_unknown_resolution_requires_explicit_confirmation_and_no_active_job(self):
        jid = self.make_job()
        data = {'platform': 'douyin', 'resolution': 'not_submitted', 'confirmed': True}
        self.assertEqual(self.post('/api/jobs/' + jid + '/resolve', data).status_code, 409)
        self.manager.update(jid, lambda j: j.update(status='interrupted'))
        self.manager.update(jid, lambda j: j['results'][0].update(status='needs_attention'))
        self.assertEqual(self.post('/api/jobs/' + jid + '/resolve', {**data, 'confirmed': False}).status_code, 400)
        self.assertEqual(self.client.post('/api/jobs/' + jid + '/resolve', json=data).status_code, 403)

    def test_confirm_no_submission_unlocks_but_never_automatically_retries(self):
        jid = self.uncertain_job()
        self.assertEqual(self.post('/api/jobs', self.payload).status_code, 409)
        before = self.manager.queue.qsize()
        response = self.post('/api/jobs/' + jid + '/resolve', {'platform': 'douyin', 'resolution': 'not_submitted', 'confirmed': True})
        job = response.get_json()['job']
        self.assertEqual(job['results'][0]['status'], 'failed')
        self.assertTrue(job['results'][0]['retry_safe'])
        self.assertEqual(job['resolutions'][0]['source'], 'user_confirmation')
        self.assertEqual(self.manager.queue.qsize(), before)
        self.assertEqual(self.post('/api/jobs', self.payload).status_code, 201)

    def test_confirm_existing_post_keeps_duplicate_guard_and_not_publication_claim(self):
        jid = self.uncertain_job()
        response = self.post('/api/jobs/' + jid + '/resolve', {'platform': 'douyin', 'resolution': 'submitted', 'confirmed': True})
        result = response.get_json()['job']['results'][0]
        self.assertEqual(result['status'], 'submitted')
        self.assertEqual(result['resolution_source'], 'user_confirmation')
        self.assertFalse(result['retry_safe'])
        self.assertEqual(self.post('/api/jobs', self.payload).status_code, 409)

    def test_progress_is_monotonic_and_discards_sensitive_fields(self):
        jid = self.make_job()
        self.manager.progress(jid, 0, {'phase': 'submitting', 'cookie': 'must-not-save'})
        self.manager.progress(jid, 0, {'phase': 'metadata'})
        result = self.store.get('job', jid)['results'][0]
        self.assertEqual(result['phase'], 'submitting')
        self.assertNotIn('must-not-save', json.dumps(self.store.get('job', jid)))

    def test_actual_callback_persists_stage_and_safe_failure(self):
        jid = self.make_job()
        def fake_run(pid, kind, metadata, log, event):
            log.event({'phase': 'metadata', 'error_code': 'metadata_failed'})
            return {'platform': pid, 'status': 'failed', 'phase': 'metadata', 'retry_safe': True, 'message': 'test'}
        with patch('adapter.run', side_effect=fake_run):
            self.manager.execute(jid)
        job = self.store.get('job', jid)
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['results'][0]['error_code'], 'metadata_failed')
        self.assertTrue(job['results'][0]['retry_safe'])

    def test_maintenance_blocks_new_work_but_preserves_existing_queue(self):
        self.make_job()
        before = self.manager.queue.qsize()
        response = self.post('/api/maintenance/drain', {})
        self.assertEqual(response.get_json()['maintenance']['active_jobs'], 1)
        self.assertEqual(self.post('/api/jobs', self.payload).status_code, 503)
        self.assertEqual(self.post('/api/accounts/douyin/check', {}).status_code, 503)
        self.assertEqual(self.post('/api/browser/pair', {'platform': 'douyin'}).status_code, 503)
        self.assertEqual(self.manager.queue.qsize(), before)
        self.assertFalse(self.manager.events[next(iter(self.manager.events))].is_set())
        self.assertFalse(self.post('/api/maintenance/resume', {}).get_json()['maintenance']['draining'])

    def test_shutdown_cannot_be_resumed_and_waits_for_task_completion(self):
        self.make_job()
        stopped = threading.Event()
        handler = server.GracefulShutdown(self.manager, stopped.set)
        handler.handle(2, None)
        self.assertFalse(stopped.wait(0.05))
        self.assertEqual(self.post('/api/maintenance/resume', {}).status_code, 503)
        self.assertEqual(self.post('/api/jobs', self.payload).status_code, 503)
        handler.handle(2, None)
        self.assertTrue(next(iter(self.manager.events.values())).is_set())
        self.manager.queue.get_nowait()
        self.manager.queue.task_done()
        self.assertTrue(stopped.wait(1))

    def test_unverified_account_never_queues_publish(self):
        self.store.put('account', 'douyin', {'status': 'needs_login'})
        self.assertEqual(self.post('/api/jobs', self.payload).status_code, 409)
        self.assertEqual(self.manager.queue.qsize(), 0)

    def test_known_unsupported_ai_metadata_is_blocked_before_job(self):
        payload = {**self.payload, 'platforms': ['xiaohongshu'], 'ai_generated': True}
        self.store.put('account', 'xiaohongshu', {'status': 'verified'})
        response = self.post('/api/jobs', payload)
        self.assertEqual(response.status_code, 400)
        self.assertIn('AI', response.get_json()['errors'][0])
        self.assertEqual(self.manager.queue.qsize(), 0)


if __name__ == '__main__':
    unittest.main()
