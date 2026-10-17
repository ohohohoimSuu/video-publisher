"""Synthetic native credentials only. No login, requests, or credentials on disk."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from uploader.bilibili_uploader.prepare import (MAX_NATIVE_BYTES, BilibiliFixtureAdapter, convert_native_session,
                                                load_native_session, verify_nav_identity)
from utils.experimental_prepare import PrepareFailure


def native():
    return {"cookie_info": {"cookies": [
        {"name": "SESSDATA", "value": "synthetic-session"},
        {"name": "bili_jct", "value": "synthetic-csrf"},
        {"name": "DedeUserID", "value": "42"},
        {"name": "unneeded", "value": "discard-this"},
    ]}, "token_info": {"mid": 42, "access_token": "never-import-this", "refresh_token": "never-import-that"}}


def session(source=None):
    return convert_native_session(json.dumps(source or native()).encode(), expected_account_id="42", now=100)


class NativeSessionTests(unittest.TestCase):
    def test_conversion_is_narrow_and_does_not_expose_tokens_in_repr(self):
        result = session()
        state = result.storage_state
        self.assertEqual(result.account_id, "42")
        self.assertEqual(state["origins"], [])
        self.assertEqual({c["name"] for c in state["cookies"]}, {"DedeUserID", "SESSDATA", "bili_jct"})
        for cookie in state["cookies"]:
            self.assertEqual((cookie["domain"], cookie["path"], cookie["expires"], cookie["secure"]), (".bilibili.com", "/", -1, True))
        for private in ("synthetic-session", "synthetic-csrf", "access_token", "refresh_token", "discard-this"):
            self.assertNotIn(private, repr(result))
            if private not in {"synthetic-session", "synthetic-csrf"}:
                self.assertNotIn(private, json.dumps(state))

    def test_explicit_scope_and_expiry_are_checked(self):
        for field, value in (("domain", "bilibili.com.evil.invalid"), ("domain", "evil.bilibili.com"),
                             ("domain", ".qq.com"), ("path", "/other"), ("expires", 99), ("expires", True),
                             ("expires", float("nan")), ("expires", float("inf"))):
            with self.subTest(field=field, value=value):
                source = native(); source["cookie_info"]["cookies"][0][field] = value
                with self.assertRaises(PrepareFailure): session(source)

    def test_valid_expiry_is_not_extended(self):
        source = native(); source["cookie_info"]["cookies"][0]["expires"] = 101
        actual = next(c for c in session(source).storage_state["cookies"] if c["name"] == "SESSDATA")
        self.assertEqual(actual["expires"], 101)

    def test_duplicates_required_cookie_missing_and_unbounded_values_rejected(self):
        cases = []
        source = native(); source["cookie_info"]["cookies"].append(copy.deepcopy(source["cookie_info"]["cookies"][0])); cases.append(source)
        source = native(); source["cookie_info"]["cookies"].pop(0); cases.append(source)
        source = native(); source["cookie_info"]["cookies"][0]["value"] = "x" * 8193; cases.append(source)
        source = native(); source["cookie_info"]["cookies"][0]["value"] = "private\nsecret"; cases.append(source)
        source = native(); source["cookie_info"]["cookies"] = [{"name": str(i), "value": "x"} for i in range(65)]; cases.append(source)
        for source in cases:
            with self.subTest(source=cases.index(source)), self.assertRaises(PrepareFailure) as raised:
                session(source)
            self.assertNotIn("private", str(raised.exception))
            self.assertNotIn("synthetic-session", str(raised.exception))

    def test_native_token_identity_and_expected_account_must_agree(self):
        for value in (43, None, True, "042", "42.0"):
            source = native(); source["token_info"]["mid"] = value
            with self.subTest(value=value), self.assertRaises(PrepareFailure): session(source)
        with self.assertRaises(PrepareFailure):
            convert_native_session(json.dumps(native()).encode(), expected_account_id="43")

    def test_bounds_and_invalid_shapes_fail_sanitized(self):
        for raw in (b"x" * (MAX_NATIVE_BYTES + 1), b"not-json-secret", b"null", b"[]", b'{}',
                    b'{"cookie_info":{"cookies":{}}}', b'\xff', b'{"cookie_info":{},"cookie_info":{}}', b"[" * 2000 + b"]" * 2000, "not bytes"):
            with self.subTest(type=type(raw)), self.assertRaises(PrepareFailure) as raised:
                convert_native_session(raw, expected_account_id="42")
            self.assertNotIn("not-json-secret", str(raised.exception))

    def test_file_read_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.json"
            path.write_bytes(b"x" * (MAX_NATIVE_BYTES + 1))
            with self.assertRaises(PrepareFailure): load_native_session(path, expected_account_id="42")
            path.write_text(json.dumps(native()))
            self.assertEqual(load_native_session(path, expected_account_id="42").account_id, "42")

    def test_independent_nav_response_requires_exact_logged_in_account(self):
        current = session()
        self.assertEqual(verify_nav_identity({"code": 0, "data": {"isLogin": True, "mid": 42}}, session=current, expected_account_id="42"), "42")
        for response in ({"code": False, "data": {"isLogin": True, "mid": 42}},
                         {"code": -101}, {"code": 0, "data": {"isLogin": False, "mid": 42}},
                         {"code": 0, "data": {"isLogin": True, "mid": 43}},
                         {"code": 0, "data": {"isLogin": True}}, None):
            with self.subTest(response=response), self.assertRaises(PrepareFailure):
                verify_nav_identity(response, session=current, expected_account_id="42")

    def test_binding_changes_with_session_not_account(self):
        source = native(); source["cookie_info"]["cookies"][0]["value"] = "rotated-synthetic"
        first, rotated = session(), session(source)
        self.assertEqual(first.account_id, rotated.account_id)
        self.assertNotEqual(first.binding_sha256, rotated.binding_sha256)


if __name__ == "__main__": unittest.main()
