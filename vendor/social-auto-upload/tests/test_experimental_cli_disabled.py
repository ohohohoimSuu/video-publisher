"""The service's prepare-only environment cannot reach legacy native posting."""
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import sau_cli


class ExperimentalCliIsolationTests(unittest.TestCase):
    def test_tencent_upload_action_rejected_before_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "synthetic.mp4"; video.write_bytes(b"never uploaded")
            for platform in ("tencent",):
                args = [platform, "upload-video", "--account", "synthetic", "--file", str(video), "--title", "原文", "--desc", "原文"]
                if platform == "bilibili": args += ["--tid", "249"]
                with self.subTest(platform=platform), patch.dict(os.environ, {"SAU_PUBLISHER_PREPARE_ONLY": "1"}), \
                     patch.object(sau_cli, "dispatch", new=AsyncMock()) as dispatch, \
                     patch.object(sau_cli, "run_biliup_command") as native, patch("sys.stderr", io.StringIO()):
                    with self.assertRaises(SystemExit): sau_cli.main(args)
                    dispatch.assert_not_awaited()
                    native.assert_not_called()


if __name__ == "__main__": unittest.main()
