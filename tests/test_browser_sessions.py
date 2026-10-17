"""Synthetic sessions only. No browser profile or live platform is accessed."""
import asyncio
import copy
import hashlib
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import adapter
import browser_sessions as sessions

CHANNEL = "UC" + "a" * 22


def cookie(name="sessionid", domain=".douyin.com", **overrides):
    return {"name": name, "value": "synthetic-session-only", "domain": domain,
            "path": "/", "secure": True, "httpOnly": True, "hostOnly": False,
            "session": True, "sameSite": "no_restriction", "storeId": "0", **overrides}


def fixture(pid="douyin"):
    markers = {"douyin": "sessionid", "xiaohongshu": "web_session", "youtube": "__Secure-3PSID"}
    domain = sessions.SUPPORTED[pid]["domains"][0]
    origin = sessions.SUPPORTED[pid]["origins"][0]
    return {"cookies": [cookie(markers[pid], "." + domain)],
            "origins": [{"origin": origin, "localStorage": [{"name": "demo", "value": "synthetic-storage-only"}]}],
            "account_hint": "页面展示名（未核实）", "profile_label": "测试 Chrome",
            "page_url": origin + ("/channel/" + CHANNEL if pid == "youtube" else "/")}


class BrowserSessionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cookies = self.root / "cookies"

    def save(self, payload=None, pid="douyin", metadata_dir=None):
        return sessions.validate_and_save(pid, payload or fixture(pid), self.cookies,
                                          metadata_dir or self.cookies)

    def test_three_platforms_save_cli_storage_state_and_safe_unverified_metadata(self):
        for pid in sessions.SUPPORTED:
            with self.subTest(pid=pid):
                info = self.save(pid=pid)
                path = self.cookies / f"{pid}_default.json"
                saved = json.loads(path.read_text())
                self.assertEqual(set(saved), {"cookies", "origins"})
                self.assertEqual(saved["cookies"][0]["sameSite"], "None")
                self.assertEqual(saved["cookies"][0]["expires"], -1)
                self.assertNotIn("storeId", saved["cookies"][0])
                self.assertEqual(info["source"], "chrome")
                self.assertIs(info["browser_verified"], False)
                self.assertEqual(info, sessions.read_metadata(pid, self.cookies))
                self.assertNotIn("synthetic-session", json.dumps(info))
                self.assertNotIn("synthetic-storage", json.dumps(info))
                self.assertNotIn("page_url", info)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_rejects_other_domains_google_tencent_and_suffix_confusion(self):
        for domain in (".google.com", "channels.weixin.qq.com", ".youtube.com", ".douyin.com.evil.test", "evildouyin.com", "..douyin.com"):
            payload = fixture()
            payload["cookies"].append(cookie("unexpected", domain))
            with self.subTest(domain=domain), self.assertRaises(ValueError):
                self.save(payload)
        self.assertFalse(self.cookies.exists())

    def test_bilibili_and_tencent_rejected_without_touching_existing_file(self):
        self.cookies.mkdir()
        for pid in ("bilibili", "tencent"):
            path = self.cookies / f"{pid}_default.json"
            path.write_text("synthetic-existing-independent-login")
            with self.assertRaises(ValueError):
                self.save(fixture(), pid=pid)
            self.assertEqual(path.read_text(), "synthetic-existing-independent-login")
            self.assertEqual(sessions.read_metadata(pid, self.cookies), {})

    def test_expired_cookie_dropped_but_expired_login_cannot_overwrite(self):
        self.save()
        path = self.cookies / "douyin_default.json"
        previous = path.read_bytes()
        payload = fixture()
        payload["cookies"][0].update(session=False, expirationDate=1)
        with self.assertRaises(ValueError):
            self.save(payload)
        self.assertEqual(path.read_bytes(), previous)
        payload = fixture()
        payload["cookies"].append(cookie("old_tracking", session=False, expirationDate=1))
        self.assertEqual(self.save(payload)["cookie_count"], 1)

    def test_partitioned_cookies_fail_explicitly_without_leaking_value(self):
        payload = fixture()
        payload["cookies"][0]["partitionKey"] = {"topLevelSite": "https://douyin.com"}
        with self.assertRaisesRegex(ValueError, "分区") as raised:
            self.save(payload)
        self.assertNotIn("synthetic-session", str(raised.exception))
        self.assertFalse(self.cookies.exists())

    def test_origin_whitelist_and_https_are_exact(self):
        for origin in ("http://creator.douyin.com", "https://creator.douyin.com/", "https://www.google.com", "https://creator.douyin.com.evil.test"):
            payload = fixture()
            payload["origins"][0]["origin"] = origin
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                self.save(payload)

    def test_malformed_types_sizes_duplicates_and_mixed_stores_fail(self):
        payloads = []
        for key, value in (("secure", "yes"), ("expirationDate", float("nan")),
                           ("expirationDate", 1),
                           ("sameSite", []), ("value", "x" * 32769), ("path", "bad")):
            p = fixture()
            p["cookies"][0][key] = value
            payloads.append(p)
        p = fixture(); p["cookies"] *= 2; payloads.append(p)
        p = fixture(); p["cookies"].append(cookie("other", storeId="1")); payloads.append(p)
        p = fixture(); p["padding"] = "x" * sessions.MAX_PAYLOAD_BYTES; payloads.append(p)
        p = fixture(); p["origins"][0]["localStorage"] *= 2; payloads.append(p)
        for index, payload in enumerate(payloads):
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.save(payload)

    def test_host_only_and_domain_cookie_mapping_is_preserved(self):
        p = fixture()
        p["cookies"].append(cookie("host_only", "creator.douyin.com", hostOnly=True, sameSite="strict"))
        self.save(p)
        stored = json.loads((self.cookies / "douyin_default.json").read_text())["cookies"]
        self.assertEqual(stored[0]["domain"], ".douyin.com")
        self.assertEqual(stored[1]["domain"], "creator.douyin.com")
        self.assertEqual(stored[1]["sameSite"], "Strict")

    def test_youtube_requires_studio_target_channel_and_binds_exact_session(self):
        payload = fixture("youtube")
        for url in (None, "https://www.youtube.com/channel/" + CHANNEL,
                    "https://studio.youtube.com/", "https://studio.youtube.com/channel/UCbad",
                    "https://studio.youtube.com.evil.test/channel/" + CHANNEL):
            payload["page_url"] = url
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.save(payload, pid="youtube")
        self.save(pid="youtube")
        metadata = json.loads((self.cookies / "youtube_default.json.publisher.json").read_text())
        self.assertEqual(metadata["channel_id"], CHANNEL)
        self.assertEqual(metadata["session_sha256"], hashlib.sha256((self.cookies / "youtube_default.json").read_bytes()).hexdigest())

    def test_one_private_backup_retains_previous_session_and_lock(self):
        self.save(pid="youtube")
        path = self.cookies / "youtube_default.json"
        old = path.read_bytes()
        old_sidecar = (self.cookies / "youtube_default.json.publisher.json").read_bytes()
        payload = fixture("youtube")
        payload["cookies"][0]["value"] = "second-synthetic-session"
        payload["page_url"] = "https://studio.youtube.com/channel/UC" + "b" * 22
        self.save(payload, pid="youtube")
        self.assertEqual(path.with_name(path.name + ".previous").read_bytes(), old)
        self.assertEqual((self.cookies / "youtube_default.json.publisher.json.previous").read_bytes(), old_sidecar)
        self.assertEqual(len(list(self.cookies.glob("*.previous"))), 2)
        self.assertTrue(all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in self.cookies.iterdir()))

    def test_youtube_preserves_only_valid_existing_authuser_and_binds_context(self):
        for authuser in (0, 1, 99):
            payload = fixture("youtube")
            payload["page_url"] += f"?other=private-query&authuser={authuser}#private-fragment"
            info = self.save(payload, pid="youtube")
            self.assertEqual(info["authuser"], authuser)
            self.assertEqual(sessions.read_metadata("youtube", self.cookies)["authuser"], authuser)
            stored = json.loads((self.cookies / "youtube_default.json.publisher.json").read_text())
            context = {key: stored.get(key) for key in ("channel_id", "authuser", "session_sha256")}
            expected = hashlib.sha256(json.dumps(context, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            self.assertEqual(stored["context_sha256"], expected)
            self.assertNotIn("private-query", json.dumps(stored))
            self.assertNotIn("private-fragment", json.dumps(stored))
        self.assertNotIn("authuser", self.save(pid="youtube"))

    def test_youtube_rejects_ambiguous_or_malformed_authuser_before_writing(self):
        self.save(pid="youtube")
        before = {p.name: p.read_bytes() for p in self.cookies.iterdir()}
        for query in ("authuser=", "authuser=-1", "authuser=100", "authuser=01", "authuser=1.0",
                      "authuser=+1", "authuser=%EF%BC%91", "authuser=private%40example.invalid",
                      "authuser=%001", "authuser=1&authuser=1", "authuser=1&%61uthuser=2"):
            payload = fixture("youtube")
            payload["page_url"] += "?" + query
            with self.subTest(query=query), self.assertRaisesRegex(ValueError, "重新打开目标 Studio") as raised:
                self.save(payload, pid="youtube")
            self.assertNotIn("example.invalid", str(raised.exception))
        self.assertEqual({p.name: p.read_bytes() for p in self.cookies.iterdir()}, before)

    def test_other_platforms_never_parse_or_preserve_authuser(self):
        for pid in ("douyin", "xiaohongshu"):
            payload = fixture(pid)
            payload["page_url"] += "?authuser=private%40example.invalid&authuser=1"
            self.assertNotIn("authuser", self.save(payload, pid=pid))
            stored = (self.cookies / f"{pid}_default.json.publisher.json").read_text()
            self.assertNotIn("authuser", stored)

    def test_atomic_write_failure_restores_original_pair(self):
        self.save(pid="youtube")
        before = {p.name: p.read_bytes() for p in self.cookies.iterdir()}
        original = sessions._atomic_write
        failed = False
        def fail_once(path, content):
            nonlocal failed
            if path.name == "youtube_default.json.publisher.json" and not failed:
                failed = True
                raise OSError("synthetic failure")
            return original(path, content)
        payload = fixture("youtube")
        payload["cookies"][0]["value"] = "new-synthetic-session"
        with patch.object(sessions, "_atomic_write", side_effect=fail_once), self.assertRaises(ValueError):
            self.save(payload, pid="youtube")
        for name, content in before.items():
            self.assertEqual((self.cookies / name).read_bytes(), content)

    def test_read_metadata_allowlists_and_clear_does_not_remove_session(self):
        self.save()
        path = self.cookies / "douyin_default.json.publisher.json"
        data = json.loads(path.read_text())
        data.update(cookies="do-not-echo", browser_verified=True, dangerous="anything")
        path.write_text(json.dumps(data))
        self.assertNotIn("do-not-echo", json.dumps(sessions.read_metadata("douyin", self.cookies)))
        self.assertIs(sessions.read_metadata("douyin", self.cookies)["browser_verified"], False)
        sessions.clear_metadata("douyin", self.cookies)
        self.assertEqual(sessions.read_metadata("douyin", self.cookies), {})
        self.assertTrue((self.cookies / "douyin_default.json").exists())

    def test_separate_metadata_directory_also_has_runtime_sidecar(self):
        info = self.save(metadata_dir=self.root / "metadata")
        self.assertEqual(sessions.read_metadata("douyin", self.root / "metadata"), info)
        self.assertEqual(sessions.read_metadata("douyin", self.cookies), info)

    def test_symlink_destination_does_not_write_outside_session_directory(self):
        self.cookies.mkdir()
        other = self.root / "unrelated"
        other.write_text("unmodified")
        (self.cookies / "douyin_default.json").symlink_to(other)
        with self.assertRaises(ValueError):
            self.save()
        self.assertEqual(other.read_text(), "unmodified")

    def test_actual_cli_check_and_publish_resolve_same_imported_files_without_network(self):
        for pid in sessions.SUPPORTED:
            self.save(pid=pid)
        # Run the real parser and dispatch, replacing every platform/network
        # boundary. This proves both actions consume the identical imported file.
        code = '''
import asyncio, json, sys
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock
import sau_cli
root = Path(sys.argv[1])
for pid, setup, klass, method in [('douyin','douyin_setup','DouYinVideo','douyin_upload_video'), ('xiaohongshu','xiaohongshu_setup','XiaoHongShuVideo','main'), ('youtube','youtube_setup','YouTubeVideo','main')]:
    expected = root / 'cookies' / (pid + '_default.json')
    auth = AsyncMock(return_value=True)
    readiness = AsyncMock(return_value=True)
    app = MagicMock()
    setattr(app, method, AsyncMock())
    constructor = MagicMock(return_value=app)
    args = [pid,'upload-video','--account','default','--file',str(root/'fixture.mp4'),'--title','synthetic','--desc','','--tags','']
    if pid == 'youtube': args += ['--visibility','private','--made-for-kids','false','--altered-synthetic','false']
    with patch.object(sau_cli,'resolve_runtime_home',return_value=root), patch.object(sau_cli,pid+'_cookie_auth',auth), patch.object(sau_cli,setup,readiness), patch.object(sau_cli,klass,constructor):
        assert asyncio.run(sau_cli.dispatch(sau_cli.build_parser().parse_args([pid,'check','--account','default']))) == 0
        assert auth.call_args.args[0] == str(expected)
        assert asyncio.run(sau_cli.dispatch(sau_cli.build_parser().parse_args(args))) == 0
        assert readiness.call_args.args[0] == str(expected)
        called = constructor.call_args
        actual = called.args[4] if pid == 'douyin' else called.args[3] if pid == 'youtube' else called.kwargs['account_file']
        assert actual == str(expected)
        assert json.loads(expected.read_text())['cookies'][0]['value'] == 'synthetic-session-only'
print('same imported session consumed by check and publish')
'''
        (self.root / "fixture.mp4").write_bytes(b"synthetic; never uploaded")
        result = subprocess.run([str(adapter.PYTHON), "-c", code, str(self.root)],
                                cwd=adapter.VENDOR, env=adapter._environment(),
                                capture_output=True, text=True, timeout=30, shell=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("same imported session", result.stdout)

    def test_youtube_actual_lock_and_upload_fail_before_attaching_file(self):
        self.save(pid="youtube")
        code = '''
import asyncio, sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uploader.youtube_uploader import main as yt
account = Path(sys.argv[1])
expected = 'UC' + 'a'*22
assert yt._publisher_channel(account) == expected
page = MagicMock()
page.url = 'https://studio.youtube.com/channel/UC' + 'b'*22
page.goto = AsyncMock()
page.wait_for_timeout = AsyncMock()
studio_control = MagicMock()
studio_control.first = studio_control
studio_control.is_visible = AsyncMock(return_value=True)
studio_control.click = AsyncMock()
page.get_by_role.return_value = studio_control
context = MagicMock()
context.new_page = AsyncMock(return_value=page)
browser = MagicMock()
browser.new_context = AsyncMock(return_value=context)
browser.close = AsyncMock()
playwright = MagicMock()
playwright.chromium.launch = AsyncMock(return_value=browser)
video = yt.YouTubeVideo('synthetic', '/synthetic-not-uploaded.mp4', [], str(account), visibility='private', made_for_kids=False, altered_synthetic=False)
with patch.object(yt, 'set_init_script', AsyncMock(return_value=context)):
    try: asyncio.run(video.upload(playwright))
    except RuntimeError as error: assert '频道' in str(error)
    else: raise AssertionError('wrong channel accepted')
page.locator.assert_not_called()
manager = MagicMock()
manager.__aenter__ = AsyncMock(return_value=playwright)
manager.__aexit__ = AsyncMock(return_value=False)
with patch.object(yt, 'async_playwright', return_value=manager), patch.object(yt, 'set_init_script', AsyncMock(return_value=context)):
    assert asyncio.run(yt.cookie_auth(account)) is False
page.url = 'https://studio.youtube.com/channel/' + expected
asyncio.run(yt._require_channel(page, expected))
with patch.object(yt, 'async_playwright', return_value=manager), patch.object(yt, 'set_init_script', AsyncMock(return_value=context)):
    assert asyncio.run(yt.cookie_auth(account)) is True
locator = MagicMock()
locator.first = locator
for name in ('wait_for','click','set_input_files'):
    setattr(locator, name, AsyncMock())
locator.count = AsyncMock(return_value=1)
locator.is_visible = AsyncMock(return_value=True)
locator.get_attribute = AsyncMock(return_value='true')
page.locator.return_value = locator
async def upload_finished_then_wrong_channel(page):
    page.url = 'https://studio.youtube.com/channel/UC' + 'b'*22
    return True
click = AsyncMock(return_value=True)
with patch.object(yt, 'set_init_script', AsyncMock(return_value=context)), patch.object(yt, '_fill_video_copy', AsyncMock()), patch.object(yt, '_wait_upload_complete', side_effect=upload_finished_then_wrong_channel), patch.object(yt, '_click_if_present', click):
    try: asyncio.run(video.upload(playwright))
    except RuntimeError as error: assert '频道' in str(error)
    else: raise AssertionError('channel change before submit accepted')
assert not any(call.args[1] == '#done-button' for call in click.call_args_list)
locator.set_input_files.assert_awaited_once()
account.write_text('{}')
try: yt._publisher_channel(account)
except RuntimeError: pass
else: raise AssertionError('changed session accepted')
print('channel mismatch stopped before any file input')
'''
        result = subprocess.run([str(adapter.PYTHON), "-c", code, str(self.cookies / "youtube_default.json")],
                                cwd=adapter.VENDOR, env=adapter._environment(), capture_output=True,
                                text=True, timeout=30, shell=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("stopped before any file", result.stdout)


if __name__ == "__main__":
    unittest.main()
