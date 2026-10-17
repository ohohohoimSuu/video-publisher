"""Capability evidence must not accidentally enable fixture-only uploaders."""
import unittest
from platform_workflows import FIXTURE_PLATFORMS, PREPARE_PLATFORMS, workflow_info


class CapabilityEvidenceTests(unittest.TestCase):
    def test_only_implemented_executors_are_available(self):
        self.assertEqual(PREPARE_PLATFORMS, ("douyin", "xiaohongshu", "youtube", "bilibili"))
        for platform in PREPARE_PLATFORMS:
            info = workflow_info(platform)
            self.assertTrue(info["prepare_supported"])
            self.assertEqual(info["workflow_status"], "implemented" if platform == "bilibili" else "unverified")
            self.assertEqual(info["implementation_scope"], "production_local_verified" if platform == "bilibili" else "production_unverified")
            self.assertEqual(info["acceptance_status"], "local_account_verified_2026_10_12" if platform == "bilibili" else "real_account_pending")

    def test_fixture_code_never_enables_production_preparation(self):
        self.assertEqual(FIXTURE_PLATFORMS, ("tencent",))
        for platform in FIXTURE_PLATFORMS:
            info = workflow_info(platform)
            self.assertFalse(info["prepare_supported"])
            self.assertEqual(info["workflow_status"], "fixture_only")
            self.assertEqual(info["implementation_scope"], "fixture_only")
            self.assertEqual(info["acceptance_status"], "fixture_only")
            self.assertGreater(len(info["known_limitations"]), 0)
        self.assertTrue(workflow_info("tencent")["live_access_restricted"])

    def test_unimplemented_platforms_remain_disabled(self):
        info = workflow_info("unknown")
        self.assertFalse(info["prepare_supported"])
        self.assertEqual(info["workflow_status"], "manual_only")
        self.assertEqual(info["implementation_scope"], "not_implemented")

    def test_caller_cannot_mutate_registry_limitations(self):
        info = workflow_info("bilibili")
        info["known_limitations"].clear()
        self.assertTrue(workflow_info("bilibili")["known_limitations"])


if __name__ == "__main__": unittest.main()
