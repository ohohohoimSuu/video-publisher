"""YouTube auth diagnostics and channel safety with synthetic browser fixtures only."""
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from uploader.youtube_uploader import main as yt


CHANNEL = "UC" + "a" * 22
OTHER_CHANNEL = "UC" + "b" * 22


class YouTubeAuthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.page = MagicMock()
        self.page.url = f"https://studio.youtube.com/channel/{CHANNEL}"
        self.page.goto = AsyncMock(return_value=SimpleNamespace(status=200))
        self.page.wait_for_timeout = AsyncMock()
        self.control = MagicMock()
        self.control.first = self.control
        self.control.is_visible = AsyncMock(return_value=True)
        self.control.click = AsyncMock()
        self.page.get_by_role.return_value = self.control
        self.field = MagicMock()
        self.field.first = self.field
        self.field.wait_for = AsyncMock()
        self.field.set_input_files = AsyncMock()
        self.page.locator.return_value = self.field
        self.context = MagicMock()
        self.context.new_page = AsyncMock(return_value=self.page)
        self.context.storage_state = AsyncMock()
        self.browser = MagicMock()
        self.browser.new_context = AsyncMock(return_value=self.context)
        self.browser.close = AsyncMock()
        self.playwright = MagicMock()
        self.playwright.chromium.launch = AsyncMock(return_value=self.browser)
        self.manager = MagicMock()
        self.manager.__aenter__ = AsyncMock(return_value=self.playwright)
        self.manager.__aexit__ = AsyncMock(return_value=False)
        for target, value in (
            ("async_playwright", MagicMock(return_value=self.manager)),
            ("set_init_script", AsyncMock(return_value=self.context)),
            ("_publisher_binding", MagicMock(return_value={"channel_id": CHANNEL})),
        ):
            patcher = patch.object(yt, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def auth(self):
        result = await yt.cookie_auth("/synthetic-session-not-read.json")
        self.context.storage_state.assert_not_called()
        return result

    async def test_target_channel_and_visible_studio_control_verify(self):
        self.assertIs(await self.auth(), True)
        self.page.goto.assert_awaited_once_with(
            f"{yt.STUDIO_URL}/channel/{CHANNEL}", wait_until="domcontentloaded", timeout=30000)
        self.browser.close.assert_awaited_once()
        result = yt.get_last_auth_diagnostic()
        self.assertEqual(result, {
            "reason": "verified", "stage": "complete", "error_type": None,
            "net_error": None, "http_status": 200, "expected_channel": CHANNEL,
            "actual_channel": CHANNEL, "host": "studio.youtube.com",
        })
        result["reason"] = "caller_mutation"
        self.assertEqual(yt.get_last_auth_diagnostic()["reason"], "verified")

    async def test_unbound_session_still_uses_studio_root(self):
        yt._publisher_binding.return_value = {}
        self.assertIs(await self.auth(), True)
        self.page.goto.assert_awaited_once_with(yt.STUDIO_URL, wait_until="domcontentloaded", timeout=30000)

    async def test_navigation_error_is_sanitized(self):
        self.page.url += "?sensitive-query=do-not-leak"
        self.page.goto.side_effect = RuntimeError(
            "Page.goto net::ERR_CONNECTION_CLOSED https://studio.youtube.com/private?token=secret "
            "Cookie: SAPISID=synthetic-sensitive-cookie")
        self.assertIs(await self.auth(), False)
        result = yt.get_last_auth_diagnostic()
        self.assertEqual(result["reason"], "navigation_failed")
        self.assertEqual(result["stage"], "navigation")
        self.assertEqual(result["error_type"], "RuntimeError")
        self.assertEqual(result["net_error"], "ERR_CONNECTION_CLOSED")
        serialized = json.dumps(result)
        for private in ("Cookie", "SAPISID", "secret", "token", "https://", "sensitive", "private"):
            self.assertNotIn(private, serialized)
        self.browser.close.assert_awaited_once()

    async def test_http_error_is_navigation_failure_even_at_channel_url(self):
        self.page.goto.return_value = SimpleNamespace(status=503)
        self.assertIs(await self.auth(), False)
        self.assertEqual(yt.get_last_auth_diagnostic()["reason"], "navigation_failed")
        self.assertEqual(yt.get_last_auth_diagnostic()["http_status"], 503)
        self.page.get_by_role.assert_not_called()
        self.browser.close.assert_awaited_once()

    async def test_channel_url_without_visible_control_is_not_verified(self):
        self.control.is_visible.return_value = False
        self.assertIs(await self.auth(), False)
        self.assertEqual(yt.get_last_auth_diagnostic()["reason"], "studio_not_ready")
        self.assertEqual(self.page.wait_for_timeout.await_count, 20)
        self.browser.close.assert_awaited_once()

    async def test_studio_root_without_channel_is_not_verified(self):
        self.page.url = yt.STUDIO_URL
        self.assertIs(await self.auth(), False)
        self.assertEqual(yt.get_last_auth_diagnostic()["reason"], "studio_not_ready")
        self.assertIsNone(yt.get_last_auth_diagnostic()["actual_channel"])

    async def test_delayed_login_redirect_is_identified(self):
        self.control.is_visible.return_value = False

        async def redirect(_):
            self.page.url = "https://accounts.google.com/v3/signin?private=do-not-leak"

        self.page.wait_for_timeout.side_effect = redirect
        self.assertIs(await self.auth(), False)
        self.assertEqual(yt.get_last_auth_diagnostic()["reason"], "login_redirect")
        self.assertEqual(yt.get_last_auth_diagnostic()["host"], "accounts.google.com")
        self.page.wait_for_timeout.assert_awaited_once()

    async def test_wrong_channel_is_rejected_without_waiting(self):
        self.page.url = f"{yt.STUDIO_URL}/channel/{OTHER_CHANNEL}"
        self.assertIs(await self.auth(), False)
        self.assertEqual(yt.get_last_auth_diagnostic()["reason"], "channel_mismatch")
        self.assertEqual(yt.get_last_auth_diagnostic()["actual_channel"], OTHER_CHANNEL)
        self.page.wait_for_timeout.assert_not_awaited()
        self.page.get_by_role.assert_not_called()

    async def test_session_lock_failure_resets_previous_result(self):
        self.assertIs(await self.auth(), True)
        yt._publisher_binding.side_effect = RuntimeError("synthetic-session-private")
        self.assertIs(await self.auth(), False)
        result = yt.get_last_auth_diagnostic()
        self.assertEqual(result["reason"], "session_lock_invalid")
        self.assertIsNone(result["http_status"])
        self.assertIsNone(result["actual_channel"])
        self.assertNotIn("private", json.dumps(result))
        self.playwright.chromium.launch.assert_awaited_once()

    async def test_browser_launch_failure_stays_boolean(self):
        self.playwright.chromium.launch.side_effect = RuntimeError("private executable path")
        self.assertIs(await self.auth(), False)
        self.assertEqual(yt.get_last_auth_diagnostic()["reason"], "browser_start_failed")
        self.assertEqual(yt.get_last_auth_diagnostic()["stage"], "browser_start")
        self.browser.close.assert_not_awaited()

    async def test_context_failure_closes_browser(self):
        self.browser.new_context.side_effect = RuntimeError("private storage state")
        self.assertIs(await self.auth(), False)
        self.assertEqual(yt.get_last_auth_diagnostic()["reason"], "browser_start_failed")
        self.assertEqual(yt.get_last_auth_diagnostic()["stage"], "context_setup")
        self.browser.close.assert_awaited_once()

    async def test_readiness_error_and_close_error_do_not_escape(self):
        self.control.is_visible.side_effect = RuntimeError("private DOM information")
        self.browser.close.side_effect = RuntimeError("private close error")
        self.assertIs(await self.auth(), False)
        self.assertEqual(yt.get_last_auth_diagnostic()["reason"], "studio_not_ready")
        self.assertNotIn("private", json.dumps(yt.get_last_auth_diagnostic()))

    def video(self):
        return yt.YouTubeVideo("synthetic", "/synthetic-not-uploaded.mp4", [],
                               "/synthetic-session-not-read.json", visibility="private")

    async def test_upload_uses_channel_then_observed_upload_button(self):
        self.field.set_input_files.side_effect = RuntimeError("synthetic stop at file selection")
        with self.assertRaisesRegex(RuntimeError, "synthetic stop"):
            await self.video().upload(self.playwright)
        self.page.goto.assert_awaited_once_with(
            f"{yt.STUDIO_URL}/channel/{CHANNEL}", wait_until="domcontentloaded")
        self.control.click.assert_awaited_once_with(timeout=20000)
        self.field.set_input_files.assert_awaited_once_with("/synthetic-not-uploaded.mp4")
        self.browser.close.assert_awaited_once()

    async def test_existing_authuser_hint_is_used_by_both_check_and_upload(self):
        for authuser in (0, 2, 99):
            with self.subTest(authuser=authuser):
                yt._publisher_binding.return_value = {"channel_id": CHANNEL, "authuser": authuser}
                self.page.goto.reset_mock()
                self.assertIs(await self.auth(), True)
                target = f"{yt.STUDIO_URL}/channel/{CHANNEL}?authuser={authuser}"
                self.assertEqual(self.page.goto.await_args.args[0], target)
                self.page.goto.reset_mock()
                self.field.set_input_files.side_effect = RuntimeError("synthetic stop at file selection")
                with self.assertRaisesRegex(RuntimeError, "synthetic stop"):
                    await self.video().upload(self.playwright)
                self.assertEqual(self.page.goto.await_args.args[0], target)

    async def test_authuser_hint_never_relaxes_channel_lock(self):
        yt._publisher_binding.return_value = {"channel_id": CHANNEL, "authuser": 1}
        self.page.url = f"{yt.STUDIO_URL}/channel/{OTHER_CHANNEL}?authuser=1"
        self.assertIs(await self.auth(), False)
        self.assertEqual(yt.get_last_auth_diagnostic()["reason"], "channel_mismatch")
        with self.assertRaisesRegex(RuntimeError, "频道"):
            await self.video().upload(self.playwright)
        self.field.set_input_files.assert_not_awaited()

    async def test_upload_wrong_channel_stops_before_controls_or_file(self):
        self.page.url = f"{yt.STUDIO_URL}/channel/{OTHER_CHANNEL}"
        with self.assertRaisesRegex(RuntimeError, "频道"):
            await self.video().upload(self.playwright)
        self.page.get_by_role.assert_not_called()
        self.page.locator.assert_not_called()
        self.field.set_input_files.assert_not_awaited()
        self.browser.close.assert_awaited_once()

    async def test_channel_switch_while_upload_dialog_opens_stops_before_file(self):
        async def switch(**_):
            self.page.url = f"{yt.STUDIO_URL}/channel/{OTHER_CHANNEL}"

        self.control.click.side_effect = switch
        with self.assertRaisesRegex(RuntimeError, "频道"):
            await self.video().upload(self.playwright)
        self.page.locator.assert_not_called()
        self.field.set_input_files.assert_not_awaited()
        self.browser.close.assert_awaited_once()

    async def test_channel_switch_while_file_input_loads_stops_before_file(self):
        async def switch(**_):
            self.page.url = f"{yt.STUDIO_URL}/channel/{OTHER_CHANNEL}"

        self.field.wait_for.side_effect = switch
        with self.assertRaisesRegex(RuntimeError, "频道"):
            await self.video().upload(self.playwright)
        self.field.set_input_files.assert_not_awaited()
        self.browser.close.assert_awaited_once()


class YouTubeBindingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.account = Path(temporary.name) / "synthetic.json"
        self.account.write_text('{"cookies":[],"origins":[]}')
        self.sidecar = Path(str(self.account) + ".publisher.json")
        self.legacy = {"source": "chrome", "channel_id": CHANNEL,
                       "session_sha256": hashlib.sha256(self.account.read_bytes()).hexdigest()}

    def write_binding(self, authuser=1):
        value = {**self.legacy, "authuser": authuser}
        context = {key: value.get(key) for key in ("channel_id", "authuser", "session_sha256")}
        value["context_sha256"] = hashlib.sha256(
            json.dumps(context, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.sidecar.write_text(json.dumps(value))
        return value

    def test_legacy_binding_without_authuser_remains_compatible(self):
        self.sidecar.write_text(json.dumps(self.legacy))
        self.assertEqual(yt._publisher_channel(self.account), CHANNEL)
        self.assertEqual(yt._publisher_binding(self.account), {"channel_id": CHANNEL})

    def test_authuser_channel_and_session_are_checked_as_one_context(self):
        self.write_binding(99)
        self.assertEqual(yt._publisher_binding(self.account), {"channel_id": CHANNEL, "authuser": 99})
        for key, value in (("authuser", 2), ("channel_id", OTHER_CHANNEL),
                           ("session_sha256", "0" * 64), ("context_sha256", "0" * 64)):
            metadata = self.write_binding()
            metadata[key] = value
            self.sidecar.write_text(json.dumps(metadata))
            with self.subTest(key=key), self.assertRaisesRegex(RuntimeError, "频道锁"):
                yt._publisher_binding(self.account)
        metadata = self.write_binding()
        del metadata["authuser"]
        self.sidecar.write_text(json.dumps(metadata))
        with self.assertRaises(RuntimeError):
            yt._publisher_binding(self.account)

    def test_authuser_requires_valid_integer_and_context_checksum(self):
        for value in (-1, 100, True, "1", None, "private-account@example.invalid"):
            self.write_binding(value)
            with self.subTest(value=value), self.assertRaises(RuntimeError) as raised:
                yt._publisher_binding(self.account)
            self.assertNotIn("example.invalid", str(raised.exception))
        metadata = self.write_binding()
        del metadata["context_sha256"]
        self.sidecar.write_text(json.dumps(metadata))
        with self.assertRaises(RuntimeError):
            yt._publisher_binding(self.account)


if __name__ == "__main__":
    unittest.main()
