import json
import unittest
import adapter


class ProgressIntegrationTests(unittest.TestCase):
    def test_bilibili_live_heartbeats_survive_outer_log_filter(self):
        code = '''import os,json
from pathlib import Path
print("B站上传进程仍在运行。", flush=True)
print("B站上传进程仍在运行。", flush=True)
Path(os.environ['PUBLISHER_RESULT_FILE']).write_text(json.dumps({
 'nonce':os.environ['PUBLISHER_RESULT_NONCE'],'platform':'bilibili','action':'publish','status':'submitted'}))
'''
        logs = []
        observed = adapter._execute([str(adapter.PYTHON), '-u', '-c', code], 'bilibili', 'publish', logs.append, None, timeout_seconds=5)
        self.assertEqual(observed['returncode'], 0)
        self.assertTrue(observed['submitted'])
        self.assertEqual(len(logs), 2)
        self.assertTrue(all('B站上传进程仍在运行（已运行 ' in line for line in logs))


if __name__ == '__main__':
    unittest.main()
