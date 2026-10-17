"""Real loopback HTTP + SIGINT integration; all platform execution is synthetic.

Run manually: .venv/bin/python tests/integration_shutdown.py
Uses a temporary database/asset directory and never opens a browser or platform.
"""
from pathlib import Path
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))


def child(directory, port):
    import adapter
    import server
    folder = Path(directory)
    adapter.COOKIE_DIR = folder / 'empty-cookies'
    adapter.runtime_status = lambda: {'ready': True, 'message': 'synthetic local test'}
    def fake_run(platform, action, metadata, log, cancel_event):
        log.event({'phase': 'uploading'})
        started = time.monotonic()
        while not (folder / 'release').exists():
            if cancel_event.is_set():
                return {'platform': platform, 'status': 'needs_attention', 'phase': 'uploading', 'retry_safe': False, 'message': 'synthetic cancellation'}
            if time.monotonic() - started > 20:
                raise RuntimeError('test release timeout')
            time.sleep(0.05)
        return {'platform': platform, 'status': 'failed', 'phase': 'metadata', 'retry_safe': True, 'message': 'synthetic pre-submit failure'}
    adapter.run = fake_run
    sys.argv = ['server.py', '--port', str(port), '--data-dir', str(folder)]
    server.main()


def main():
    import requests
    import server
    with tempfile.TemporaryDirectory(prefix='publisher-http-test-') as temporary:
        folder = Path(temporary)
        store = server.Store(folder)
        for aid, kind, suffix in [('video', 'video', '.mp4'), ('cover', 'cover', '.png')]:
            path = store.assets_dir / (aid + suffix)
            path.write_bytes(b'synthetic local test only')
            store.put('asset', aid, {'id': aid, 'name': aid + suffix, 'path': str(path), 'kind': kind, 'sha256': aid, 'probe_verified': True})
        store.put('account', 'douyin', {'status': 'verified', 'message': 'synthetic only'})
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        process = subprocess.Popen([sys.executable, '-u', str(Path(__file__).resolve()), '--child', str(folder), str(port)],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=BASE)
        try:
            session = requests.Session()
            session.trust_env = False
            base = f'http://127.0.0.1:{port}'
            for _ in range(100):
                try:
                    state = session.get(base + '/api/state', timeout=0.2).json()
                    break
                except (requests.RequestException, ValueError):
                    time.sleep(0.05)
            else:
                raise AssertionError('test service did not start')
            headers = {'X-Publisher-Token': state['token']}
            payload = {'video_id': 'video', 'cover_id': 'cover', 'title': '本地维护测试', 'description': 'synthetic',
                       'tags': [], 'platforms': ['douyin'], 'ai_generated': False}
            response = session.post(base + '/api/jobs', json=payload, headers=headers, timeout=2)
            assert response.status_code == 201, response.text
            jid = response.json()['job']['id']
            for _ in range(40):
                job = session.get(base + '/api/jobs/' + jid, timeout=2).json()['job']
                if job['results'][0].get('phase') == 'uploading':
                    break
                time.sleep(0.05)
            else:
                raise AssertionError('phase did not reach persisted state')
            process.send_signal(signal.SIGINT)
            time.sleep(0.2)
            assert process.poll() is None, 'SIGINT interrupted an active upload'
            state = session.get(base + '/api/state', timeout=2).json()
            assert state['maintenance']['draining'] and state['maintenance']['shutdown_requested']
            assert session.post(base + '/api/jobs', json=payload, headers=headers, timeout=2).status_code == 503
            assert session.post(base + '/api/maintenance/resume', json={}, headers=headers, timeout=2).status_code == 503
            (folder / 'release').write_text('finish synthetic task')
            process.wait(timeout=5)
            assert process.returncode == 0
            saved = store.get('job', jid)
            assert saved['status'] == 'failed' and saved['results'][0]['retry_safe'] is True
            assert saved['results'][0]['phase'] == 'metadata'
            print(json.dumps({'integration': 'loopback-http-sigint', 'active_task_preserved': True,
                              'new_jobs_blocked': True, 'terminal_shutdown_not_resumable': True,
                              'result_persisted_before_exit': True, 'external_platform_requests': 0}))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
            if process.stdout:
                process.stdout.close()


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--child':
        child(sys.argv[2], int(sys.argv[3]))
    else:
        main()
