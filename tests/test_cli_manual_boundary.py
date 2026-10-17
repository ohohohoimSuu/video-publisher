"""Public CLI must not expose historical auto-publish or scheduling paths."""
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
import sau_cli


class CliManualBoundaryTests(unittest.TestCase):
    def test_unset_and_explicit_zero_reject_all_video_platforms_before_dispatch(self):
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / 'synthetic.mp4'; video.write_bytes(b'never uploaded')
            for platform in ('douyin', 'xiaohongshu', 'youtube', 'bilibili', 'tencent'):
                args = [platform, 'upload-video', '--account', 'fixture', '--file', str(video), '--title', 'fixture']
                if platform == 'bilibili': args += ['--tid', '21', '--desc', 'fixture']
                if platform == 'youtube': args += ['--made-for-kids', 'false', '--altered-synthetic', 'false', '--visibility', 'private']
                for value in (None, '0', 'false'):
                    with self.subTest(platform=platform, value=value), patch.dict(os.environ, {}, clear=True), patch.object(sau_cli, 'dispatch', new=AsyncMock()) as dispatch, patch('sys.stderr', io.StringIO()) as stderr:
                        if value is not None: os.environ['SAU_PUBLISHER_PREPARE_ONLY'] = value
                        with self.assertRaises(SystemExit) as raised: sau_cli.main(args)
                        self.assertEqual(raised.exception.code, 2)
                        self.assertIn('禁止自动发布', stderr.getvalue())
                        dispatch.assert_not_awaited()

    def test_schedule_is_rejected_in_explicit_prepare_mode(self):
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / 'synthetic.mp4'; video.write_bytes(b'never uploaded')
            with patch.dict(os.environ, {'SAU_PUBLISHER_PREPARE_ONLY': '1'}), patch.object(sau_cli, 'dispatch', new=AsyncMock()) as dispatch, patch('sys.stderr', io.StringIO()):
                with self.assertRaises(SystemExit):
                    sau_cli.main(['douyin', 'upload-video', '--file', str(video), '--account', 'fixture', '--title', 'fixture', '--schedule', '2030-01-01 12:00'])
                dispatch.assert_not_awaited()
