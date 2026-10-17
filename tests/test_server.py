import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
import server


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = server.create_app(self.temp.name, start_worker=False)
        self.client = self.app.test_client()
        self.store = self.app.extensions['store']
        self.manager = self.app.extensions['manager']
        self.headers = {'X-Publisher-Token': self.app.extensions['publisher_token']}
        self.runtime = patch('adapter.runtime_status', return_value={'ready': True, 'message': 'mock only'})
        self.runtime.start()
        self.addCleanup(self.runtime.stop)
        self.addCleanup(self.temp.cleanup)
        for aid, kind, ext in [('video', 'video', '.mp4'), ('cover', 'cover', '.png')]:
            path = self.store.assets_dir / (aid + ext)
            path.write_bytes(b'fixture-not-a-real-video')
            self.store.put('asset', aid, {'id': aid, 'kind': kind, 'path': str(path), 'name': aid + ext, 'sha256': aid, 'probe_verified': True})
        self.payload = {'video_id': 'video', 'cover_id': 'cover', 'title': '一次简单的视频分享', 'description': '正文原样保留', 'tags': ['生活'],
                        'ai_generated': False, 'platforms': ['douyin'],
                        'youtube': {'visibility': 'private', 'made_for_kids': False, 'altered_synthetic': False}}

    def post(self, url, payload=None):
        return self.client.post(url, json=payload if payload is not None else self.payload, headers=self.headers)

    def ready_accounts(self):
        for pid in self.payload['platforms']:
            self.store.put('account', pid, {'status': 'verified', 'message': 'synthetic verified fixture'})

    def historical_multi_platform_job(self):
        # Exercise recovery/execution of historical records independently of
        # the current HTTP gate, which only accepts manual Douyin preparation.
        payload = {**self.payload, 'platforms': ['douyin', 'youtube']}
        valid, plans = server.validate_payload(payload, self.store)
        self.assertTrue(valid['valid'])
        return self.manager.create('publish', payload['platforms'], payload, plans)

    def test_state_read_does_not_run_platform_operations(self):
        with patch('adapter.run') as run:
            state = self.client.get('/api/state').get_json()
        self.assertEqual(len(state['platforms']), 11)
        self.assertTrue(all(p['account_status'] in ('unverified', 'not_connected') for p in state['platforms']))
        run.assert_not_called()

    def test_requires_local_origin_and_session_token(self):
        self.assertEqual(self.client.post('/api/jobs', json=self.payload).status_code, 403)
        self.assertEqual(self.client.get('/api/state', headers={'Host': 'evil.example'}).status_code, 403)
        self.assertEqual(self.client.post('/api/validate', json=self.payload, headers={**self.headers, 'Origin': 'https://evil.example'}).status_code, 403)

    def test_validation_preserves_copy_and_requires_youtube_answers(self):
        self.payload['platforms'] = ['douyin', 'youtube']
        result = self.post('/api/validate').get_json()
        self.assertTrue(result['valid'])
        self.assertEqual(result['platforms'][1]['description'], self.payload['description'])
        self.payload['youtube'].pop('made_for_kids')
        self.assertFalse(self.post('/api/validate').get_json()['valid'])

    def test_title_overflow_and_missing_declaration_block_dispatch(self):
        self.payload['title'] = '字' * 101
        self.payload.pop('ai_generated')
        response = self.post('/api/jobs')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.manager.queue.qsize(), 0)

    def test_platform_specific_fields_and_override(self):
        self.payload['platforms'] = ['tencent', 'bilibili', 'weibo', 'hupu']
        self.assertFalse(self.post('/api/validate').get_json()['valid'])
        self.payload.update(bilibili={'category': '科技数码', 'declaration': 'none'}, weibo={'content_type': 'original'}, hupu={'zone': 'walking_street'}, overrides={'tencent': {'title': '视频号单独标题'}})
        result, plans = server.validate_payload(self.payload, self.store)
        self.assertTrue(result['valid'], result['errors'])
        self.assertEqual(plans[0]['title'], '视频号单独标题')
        self.assertEqual(plans[2]['weibo']['content_type'], 'original')

    def test_duplicate_video_blocked_even_if_uploaded_again(self):
        self.ready_accounts()
        response = self.post('/api/jobs')
        self.assertEqual(response.status_code, 201)
        asset = {**self.store.get('asset', 'video'), 'id': 'another-id'}
        self.store.put('asset', 'another-id', asset)
        self.payload['video_id'] = 'another-id'
        self.assertEqual(self.post('/api/jobs').status_code, 409)

    def test_worker_continues_after_one_failure_and_does_not_claim_published(self):
        self.ready_accounts()
        job = self.historical_multi_platform_job()
        def fake_run(pid, kind, metadata, on_log, event):
            on_log('mock progress')
            if pid == 'douyin':
                raise RuntimeError('simulated failure')
            return {'platform': pid, 'status': 'submitted', 'message': 'mock platform accepted'}
        with patch('adapter.run', side_effect=fake_run) as run:
            self.manager.execute(job['id'])
        done = self.store.get('job', job['id'])
        self.assertEqual(run.call_count, 2)
        self.assertEqual([r['status'] for r in done['results']], ['needs_attention', 'submitted'])
        self.assertEqual(done['status'], 'needs_attention')
        self.assertNotIn('published', [r['status'] for r in done['results']])

    def test_restart_marks_unknown_jobs_without_replaying(self):
        self.ready_accounts()
        job = self.historical_multi_platform_job()
        self.manager.update(job['id'], lambda j: j['results'][0].update(status='running', retry_safe=False))
        manager = server.JobManager(self.store, start_worker=False)
        self.assertEqual(manager.queue.qsize(), 0)
        stored = self.store.get('job', job['id'])
        self.assertEqual(stored['status'], 'interrupted')
        self.assertEqual(stored['results'][0]['status'], 'needs_attention')
        self.assertFalse(stored['results'][0]['retry_safe'])
        self.assertEqual(stored['results'][1]['status'], 'skipped')
        self.assertTrue(stored['results'][1]['retry_safe'])

    def test_cancel_before_start_never_calls_platform(self):
        self.ready_accounts()
        job = self.post('/api/jobs').get_json()['job']
        self.post('/api/jobs/' + job['id'] + '/cancel', {})
        with patch('adapter.run') as run:
            self.manager.execute(job['id'])
        run.assert_not_called()
        self.assertEqual(self.store.get('job', job['id'])['results'][0]['status'], 'skipped')

    def test_asset_upload_rejects_fake_image_and_preserves_valid_image(self):
        response = self.client.post('/api/assets', data={'kind': 'cover', 'file': (io.BytesIO(b'<script>bad</script>'), 'fake.png')}, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        img = io.BytesIO()
        Image.new('RGB', (30, 20), 'white').save(img, format='PNG')
        img.seek(0)
        response = self.client.post('/api/assets', data={'kind': 'cover', 'file': (img, '../../封面.png')}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        asset = response.get_json()['asset']
        self.assertTrue(Path(asset['path']).is_relative_to(self.store.assets_dir))
        self.assertEqual((asset['width'], asset['height']), (30, 20))
        with self.client.get(asset['url']) as response:
            self.assertEqual(response.status_code, 200)

    def test_tampered_asset_path_is_rejected(self):
        self.store.put('asset', 'cover', {'id': 'cover', 'kind': 'cover', 'path': '/etc/passwd', 'name': 'not-a-cover'})
        self.assertFalse(self.post('/api/validate').get_json()['valid'])

    def test_runtime_not_ready_blocks_queue(self):
        with patch('adapter.runtime_status', return_value={'ready': False, 'message': 'missing dependency'}):
            response = self.post('/api/jobs')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.manager.queue.qsize(), 0)

    def test_real_mp4_decoding_and_upload(self):
        import cv2
        import numpy as np
        path = Path(self.temp.name) / 'local-test.mp4'
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*'mp4v'), 10, (64, 64))
        self.assertTrue(writer.isOpened())
        for _ in range(3):
            writer.write(np.zeros((64, 64, 3), dtype=np.uint8))
        writer.release()
        with path.open('rb') as stream:
            response = self.client.post('/api/assets', data={'kind': 'video', 'file': (stream, 'local-test.mp4')}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.get_json())
        asset = response.get_json()['asset']
        self.assertTrue(asset['probe_verified'])
        self.assertEqual((asset['width'], asset['height']), (64, 64))
        self.assertEqual(len(asset['sha256']), 64)

    def test_fake_mp4_never_enters_asset_store(self):
        response = self.client.post('/api/assets', data={'kind': 'video', 'file': (io.BytesIO(b'not a video'), 'fake.mp4')}, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(self.store.all('asset')), 2)

    def test_material_preflight_reuses_actual_adapter_validation(self):
        cases = [
            {'platforms': ['baijiahao'], 'title': '八个字以内'},
            {'platforms': ['xiaohongshu'], 'tags': [str(i) for i in range(11)]},
            {'platforms': ['bilibili'], 'bilibili': {'category': '科技数码', 'declaration': 'none'}, 'tags': [str(i) for i in range(11)]},
            {'platforms': ['youtube'], 'tags': ['x' * 78 + str(i) for i in range(8)]},
            {'platforms': ['douyin'], 'title': '含有\n换行的标题'},
            {'platforms': ['alipay'], 'title': '字' * 29, 'tags': ['标签']},
        ]
        for delta in cases:
            with self.subTest(delta=delta):
                response = self.post('/api/jobs', {**self.payload, **delta})
                self.assertEqual(response.status_code, 400, response.get_json())
        self.assertEqual(self.manager.queue.qsize(), 0)

    def test_wrong_architecture_ffprobe_falls_back_to_decoder(self):
        path = Path(self.temp.name) / 'stub.mp4'
        path.write_bytes(b'\x00\x00\x00\x18ftypisom' + b'0' * 20)
        import subprocess
        decoded = subprocess.CompletedProcess([], 0, '{"ok":true,"width":640,"height":360,"duration":1}', '')
        with patch('server.shutil.which', return_value='/broken/ffprobe'), patch('server.subprocess.run', side_effect=[OSError('bad CPU type'), decoded]):
            result = server.inspect_video(path)
        self.assertTrue(result['probe_verified'])
        self.assertEqual(result['probe_method'], 'opencv-first-frame')

    def test_hash_topic_list_is_normalized_before_real_adapter_validation(self):
        self.payload['platforms'] = ['xiaohongshu', 'bilibili']
        self.payload['bilibili'] = {'category': '科技数码', 'declaration': 'none'}
        self.payload['tags'] = ['混沌  #蝴蝶效应  #三体  #科普  #数学之美  #物理']
        result, plans = server.validate_payload(self.payload, self.store)
        self.assertTrue(result['valid'], result['errors'])
        self.assertEqual(plans[0]['tags'], ['混沌','蝴蝶效应','三体','科普','数学之美','物理'])
        self.assertEqual(plans[0]['tags'], plans[1]['tags'])
        self.assertEqual(server.normalize_tags(['C# #编程', 'machine learning', '#编程']), ['C#','编程','machine learning'])


if __name__ == '__main__':
    unittest.main()
