"""WeChat Channels controlled-fixture adapter. Live access remains restricted.

This module contains no URL, network client, account importer or production
launcher. The known site-access restriction is not worked around by fixtures.
"""
from utils.experimental_prepare import FixturePrepareDriver


class WeChatChannelsFixtureAdapter(FixturePrepareDriver):
    def __init__(self, **kwargs):
        super().__init__("tencent", **kwargs)


from utils.manual_prepare_contract import ManualPrepareAdapter


class WeChatChannelsPrepareAdapter(ManualPrepareAdapter):
    platform = "tencent"
    missing_capabilities = (
        "site_access_restriction_resolution", "live_account_identity", "owned_profile_browser_binding",
        "live_transfer_and_processing_evidence", "live_declaration_clear_state",
        "live_dual_cover_pixel_and_save_evidence", "real_account_handoff_acceptance",
    )
