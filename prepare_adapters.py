"""Read-only factory boundary for the two disabled experimental prepare adapters.

The service's executable list remains platform_workflows.PREPARE_PLATFORMS.
Importing or constructing these adapters does not launch a browser or enable a
legacy uploader. No shared CLI route is modified by this factory.
"""
from uploader.bilibili_uploader.prepare import BilibiliPrepareAdapter
from uploader.tencent_uploader.prepare import WeChatChannelsPrepareAdapter


def create_experimental_prepare_adapter(platform):
    adapters = {"bilibili": BilibiliPrepareAdapter, "tencent": WeChatChannelsPrepareAdapter}
    if platform not in adapters:
        raise ValueError("Only the named experimental prepare adapters use this factory")
    return adapters[platform]()
