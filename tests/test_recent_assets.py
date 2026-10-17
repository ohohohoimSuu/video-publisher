"""Read-only reuse shelf: synthetic local files, never a platform request."""
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image
import server


class RecentAssetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.app = server.create_app(self.temp.name, start_worker=False)
        self.client = self.app.test_client(); self.store = self.app.extensions['store']
        self.token = {'X-Publisher-Token': self.app.extensions['publisher_token']}

    def asset(self, identifier, kind='cover', **extra):
        path = self.store.assets_dir / (identifier + ('.png' if kind == 'cover' else '.mp4'))
        path.write_bytes(b'synthetic material only')
        value = {'id': identifier, 'kind': kind, 'path': str(path), 'name': identifier,
                 'size': path.stat().st_size, 'width': 64, 'height': 48, 'duration': 1.2, **extra}
        self.store.put('asset', identifier, value)
        return path

    def test_filtered_newest_first_and_bounded_without_paths(self):
        self.asset('old'); self.asset('video', 'video'); self.asset('new', token='never expose', cookie='never expose')
        with patch('adapter.run') as run:
            response = self.client.get('/api/assets?kind=cover&limit=1')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json['has_more'])
        item = response.json['assets'][0]
        self.assertEqual((item['id'], item['kind'], item['url']), ('new', 'cover', '/api/assets/new'))
        self.assertNotIn('path', item); self.assertNotIn('token', item); self.assertNotIn('cookie', item)
        self.assertEqual(self.client.get('/api/assets?kind=video').json['assets'][0]['id'], 'video')
        run.assert_not_called(); self.assertEqual(self.app.extensions['manager'].queue.qsize(), 0)

    def test_missing_changed_outside_and_broken_records_are_skipped(self):
        self.asset('good')
        self.asset('missing').unlink()
        self.asset('changed').write_bytes(b'changed')
        self.store.put('asset', 'outside', {'id': 'outside', 'kind': 'cover', 'path': __file__})
        self.store.put('asset', 'broken', {'id': 'broken', 'kind': 'cover'})
        self.store.put('asset', 'other', {'id': 'other', 'kind': 'account', 'path': __file__})
        self.assertEqual([a['id'] for a in self.client.get('/api/assets').json['assets']], ['good'])
        for identifier in ('missing', 'changed', 'outside', 'broken', 'other'):
            self.assertEqual(self.client.get(f'/api/assets/{identifier}/info').status_code, 404)
            self.assertEqual(self.client.get(f'/api/assets/{identifier}').status_code, 404)

    def test_symlink_outside_asset_root_cannot_be_previewed_or_listed(self):
        link = self.store.assets_dir / 'link.png'; link.symlink_to(Path(__file__).resolve())
        self.store.put('asset', 'link', {'id': 'link', 'kind': 'cover', 'path': str(link)})
        self.assertEqual(self.client.get('/api/assets').json['assets'], [])
        self.assertEqual(self.client.get('/api/assets/link').status_code, 404)

    def test_query_validation_and_local_only_guards(self):
        for query in ('kind=image', 'kind=account', 'limit=0', 'limit=51', 'limit=-1', 'limit=1.5', 'limit=abc', 'limit='):
            self.assertEqual(self.client.get('/api/assets?' + query).status_code, 400, query)
        self.assertEqual(self.client.get('/api/assets', headers={'Host': 'evil.example'}).status_code, 403)
        self.assertEqual(self.client.get('/api/assets', headers={'Origin': 'https://evil.example'}).status_code, 403)
        self.assertEqual(self.client.get('/api/assets').json, {'assets': [], 'has_more': False})

    def test_uploaded_image_gets_date_and_survives_new_server_instance(self):
        data = io.BytesIO(); Image.new('RGB', (64, 48), 'red').save(data, format='PNG'); data.seek(0)
        result = self.client.post('/api/assets', data={'kind': 'cover', 'file': (data, 'synthetic.png')}, headers=self.token)
        self.assertEqual(result.status_code, 200)
        item = result.json['asset']; self.assertIn('created_at', item)
        client = server.create_app(self.temp.name, start_worker=False).test_client()
        self.assertEqual(client.get('/api/assets').json['assets'][0]['id'], item['id'])
        with client.get(item['url'], headers={'Range': 'bytes=0-9'}) as response:
            self.assertEqual(response.status_code, 206)
            self.assertEqual(len(response.data), 10)

    def test_catalog_does_not_read_material_bytes_or_probe_media(self):
        self.asset('large', 'video')
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('no material read')), patch('server.inspect_video', side_effect=AssertionError('no reprobe')):
            self.assertEqual(self.client.get('/api/assets').json['assets'][0]['id'], 'large')

    def test_size_change_after_selection_blocks_validation_and_queue(self):
        video = self.asset('video', 'video'); self.asset('cover')
        self.assertEqual(self.client.get('/api/assets/video/info').status_code, 200)
        video.write_bytes(b'changed-size')
        payload = {'video_id': 'video', 'cover_id': 'cover', 'platforms': ['douyin'],
                   'title': '合成测试', 'description': '', 'tags': [], 'ai_generated': False}
        checked = self.client.post('/api/validate', json=payload, headers=self.token)
        self.assertFalse(checked.json['valid'])
        self.assertIn('大小变化', ' '.join(checked.json['errors']))
        with patch('adapter.run') as run:
            submitted = self.client.post('/api/jobs', json=payload, headers=self.token)
        self.assertEqual(submitted.status_code, 400)
        self.assertEqual(self.app.extensions['manager'].queue.qsize(), 0)
        run.assert_not_called()
