"""Local fake processes only: no Bilibili calls or real account material."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


RUNTIME_PATH = (Path(__file__).resolve().parents[1] / "vendor/social-auto-upload"
                / "uploader/bilibili_uploader/runtime.py")
SPEC = importlib.util.spec_from_file_location("publisher_biliup_progress_runtime", RUNTIME_PATH)
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


class BilibiliProgressTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.script = self.root / "fake_biliup.py"
        self.finished = self.root / "finished"

    def run_fake(self, source, emit):
        self.script.write_text(source, encoding="utf-8")
        # Python's -u takes no value; biliup's -u takes the following path. This
        # exercises the real wrapper's argument shape with a local Python child.
        arguments = ["-u", str(self.script), "upload"]
        with patch.dict(os.environ, {"SAU_PUBLISHER_SAFE_LOG": "1"}), \
             patch.object(runtime, "ensure_biliup_binary", return_value=Path(sys.executable)), \
             patch.object(runtime, "BILIUP_UPLOAD_HEARTBEAT_SECONDS", 0.03), \
             patch.object(runtime, "print", side_effect=emit, create=True):
            return runtime.run_biliup_command(arguments)

    def test_silent_upload_emits_live_heartbeats_before_completion(self):
        events = []
        result = self.run_fake(
            "import time\nfrom pathlib import Path\n"
            "time.sleep(0.15)\n"
            f"Path({str(self.finished)!r}).write_text('done')\n",
            lambda line, **kwargs: events.append((line, self.finished.exists(), kwargs)),
        )
        self.assertIsInstance(result, subprocess.CompletedProcess)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertGreaterEqual(len(events), 2)
        self.assertTrue(any(not finished for _, finished, _ in events))
        self.assertTrue(all(line == "B站上传进程仍在运行。" for line, _, _ in events))
        self.assertTrue(all(kwargs.get("flush") is True for _, _, kwargs in events))

    def test_native_progress_and_secrets_remain_private_on_error(self):
        events = []
        stdout = "Uploading 47%\ncookie=FAKE_COOKIE\nprivate title and caption\n"
        stderr = "ERROR token=FAKE_TOKEN https://example.invalid/private?secret=FAKE\n"
        result = self.run_fake(
            "import sys,time\n"
            f"sys.stdout.write({stdout!r}); sys.stdout.flush()\n"
            f"sys.stderr.write({stderr!r}); sys.stderr.flush()\n"
            "time.sleep(0.09)\nsys.exit(7)\n",
            lambda line, **kwargs: events.append(line),
        )
        # Existing callers still receive the original private streams and code.
        self.assertEqual((result.returncode, result.stdout, result.stderr), (7, stdout, stderr))
        self.assertGreaterEqual(len(events), 2)
        self.assertEqual(set(events), {"B站上传进程仍在运行。"})
        forwarded = "\n".join(events)
        for secret in ("FAKE", "cookie", "token", "https://", "47%", "private"):
            self.assertNotIn(secret, forwarded)

    @unittest.skipIf(os.name == "nt", "POSIX process group assertion")
    def test_upload_child_stays_in_callers_process_group(self):
        result = self.run_fake("import os\nprint(os.getpgrp())\n", lambda *args, **kwargs: None)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(int(result.stdout.strip()), os.getpgrp())

    def test_heartbeat_exception_kills_and_reaps_child(self):
        children = []
        real_popen = subprocess.Popen

        def start(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child

        def broken_output(*args, **kwargs):
            raise BrokenPipeError("fixture output closed")

        with patch.object(runtime.subprocess, "Popen", side_effect=start):
            with self.assertRaises(BrokenPipeError):
                self.run_fake("import time\ntime.sleep(30)\n", broken_output)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())

    def test_nonpublisher_upload_keeps_original_capture_path(self):
        expected = subprocess.CompletedProcess(["fixture"], 0, "ok", "")
        with patch.dict(os.environ, {"SAU_PUBLISHER_SAFE_LOG": "0"}), \
             patch.object(runtime, "ensure_biliup_binary", return_value=Path("fixture")), \
             patch.object(runtime.subprocess, "run", return_value=expected) as run, \
             patch.object(runtime, "_run_upload_with_heartbeat") as streaming:
            self.assertIs(runtime.run_biliup_command(["upload", "fixture.mp4"]), expected)
        streaming.assert_not_called()
        self.assertTrue(run.call_args.kwargs["capture_output"])

    def test_read_commands_and_interactive_login_keep_original_path(self):
        for arguments, interactive in [(["-u", "upload", "list"], False), (["login"], True)]:
            with self.subTest(arguments=arguments), \
                 patch.dict(os.environ, {"SAU_PUBLISHER_SAFE_LOG": "1"}), \
                 patch.object(runtime, "ensure_biliup_binary", return_value=Path("fixture")), \
                 patch.object(runtime.subprocess, "run") as run, \
                 patch.object(runtime, "_run_upload_with_heartbeat") as streaming:
                runtime.run_biliup_command(arguments, interactive=interactive)
                streaming.assert_not_called()
                run.assert_called_once()
                self.assertEqual("capture_output" in run.call_args.kwargs, not interactive)


if __name__ == "__main__":
    unittest.main()
