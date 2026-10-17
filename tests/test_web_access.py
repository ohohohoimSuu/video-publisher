import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import server

class HostedAccessTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.app = server.create_app(self.root, start_worker=False)
        self.client = self.app.test_client()
        self.origin = 'https://publisher.synthetic.workers.dev'
        self.token = self.app.extensions['publisher_token']
        self.enterContext(patch('adapter.runtime_status', return_value={'ready': True}))

    def grant(self, expires=None, origin=None):
        (self.root/'trusted-web-origins.json').write_text(json.dumps({'origins':[{'origin':origin or self.origin,'expires_at':expires}]}))

    def test_unconfigured_website_cannot_read_local_accounts(self):
        r = self.client.get('/api/state', headers={'Origin':self.origin})
        self.assertEqual(r.status_code,403); self.assertNotIn('Access-Control-Allow-Origin',r.headers)
        self.assertNotIn('token',r.get_json())

    def test_exact_grant_can_read_without_starting_platform_work(self):
        self.grant()
        with patch('adapter.run') as run:
            r = self.client.get('/api/state', headers={'Origin':self.origin})
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.headers['Access-Control-Allow-Origin'],self.origin)
        self.assertIn('Origin',r.headers['Vary']); run.assert_not_called()
        self.assertNotIn('Access-Control-Allow-Credentials',r.headers)

    def test_preflight_supports_only_declared_headers_and_methods(self):
        self.grant()
        r=self.client.options('/api/assets',headers={'Origin':self.origin,'Access-Control-Request-Method':'POST',
            'Access-Control-Request-Headers':'content-type,x-publisher-token','Access-Control-Request-Private-Network':'true'})
        self.assertEqual(r.status_code,204)
        self.assertEqual(r.headers['Access-Control-Allow-Private-Network'],'true')
        self.assertNotIn('*',r.headers['Access-Control-Allow-Headers'])
        self.assertNotIn('DELETE',r.headers['Access-Control-Allow-Methods'])

    def test_trusted_origin_still_requires_mutation_token(self):
        self.grant()
        for token in ('','wrong'):
            r=self.client.post('/api/validate',json={},headers={'Origin':self.origin,'X-Publisher-Token':token})
            self.assertEqual(r.status_code,403)
        r=self.client.post('/api/validate',json={},headers={'Origin':self.origin,'X-Publisher-Token':self.token})
        self.assertEqual(r.status_code,200)
        self.assertFalse(r.get_json()['valid'])

    def test_grant_does_not_open_host_guard_or_extension_import(self):
        self.grant()
        r=self.client.get('/api/state',headers={'Origin':self.origin,'Host':'remote.example'})
        self.assertEqual(r.status_code,403); self.assertNotIn('Access-Control-Allow-Origin',r.headers)
        r=self.client.post('/api/browser/import',json={},headers={'Origin':self.origin,'X-Publisher-Token':self.token})
        self.assertEqual(r.status_code,403); self.assertNotIn('Access-Control-Allow-Origin',r.headers)

    def test_other_sites_and_malformed_origins_stay_blocked(self):
        self.grant()
        for origin in ('null','http://publisher.synthetic.workers.dev',self.origin+'.evil.test',
                       self.origin+'/path','https://other.synthetic.workers.dev',self.origin+':443'):
            with self.subTest(origin=origin):
                r=self.client.get('/api/state',headers={'Origin':origin})
                self.assertEqual(r.status_code,403); self.assertNotIn('Access-Control-Allow-Origin',r.headers)

    def test_expired_or_corrupt_grant_fails_closed_and_can_be_revoked_live(self):
        for content in ('invalid','[]','{"origins":null}'):
            (self.root/'trusted-web-origins.json').write_text(content)
            self.assertEqual(self.client.get('/api/state',headers={'Origin':self.origin}).status_code,403)
        self.grant(time.time()-1)
        self.assertEqual(self.client.get('/api/state',headers={'Origin':self.origin}).status_code,403)
        self.grant(time.time()+60)
        self.assertEqual(self.client.get('/api/state',headers={'Origin':self.origin}).status_code,200)
        (self.root/'trusted-web-origins.json').unlink()
        self.assertEqual(self.client.get('/api/state',headers={'Origin':self.origin}).status_code,403)

    def test_local_page_remains_usable_without_any_grant(self):
        self.assertEqual(self.client.get('/api/state').status_code,200)
        self.assertEqual(self.client.get('/api/state',headers={'Origin':'http://localhost'}).status_code,200)

if __name__=='__main__': unittest.main()
